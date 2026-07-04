"""
core/reconciler.py
===================

El "cerebro" de la operacion de carga de un workspace (ver seccion 4.9
del plan arquitectonico). Es donde convergen todas las piezas
construidas hasta ahora:

    database        -> que apps deberia tener el workspace, en que orden
    bspc_client     -> que ventanas hay REALMENTE abiertas ahora mismo
    x11_utils       -> WM_CLASS de cada una, y matar las que sobran
    window_watcher  -> esperar de forma sincronizada a que aparezcan las
                       que faltan
    launcher        -> lanzar cada app faltante con su timeout
    config          -> los ajustes (timeout, cierre por defecto)

Este modulo es DELIBERADAMENTE una funcion de orquestacion "sin estado
propio": no adquiere el lock de concurrencia (core/lockfile.py, eso es
responsabilidad de core/workspace_service.py, una capa por encima) y no
actualiza desktop_state en la base de datos (tambien responsabilidad de
workspace_service.py). reconcile_and_load() solo hace UNA cosa: dado un
workspace_id y un desktop, hacer que el desktop refleje ese workspace, y
devolver un reporte detallado de que paso. Mantener esta funcion sin
esas dos responsabilidades adicionales la hace mucho mas facil de
testear de forma aislada (como se hace mas abajo) y mas facil de
razonar sobre ella.

-------------------------------------------------------------------------
Sobre las ventanas REUTILIZADAS (ya abiertas y que coinciden): NO se les
vuelve a aplicar geometria/estado floating
-------------------------------------------------------------------------
Decision deliberada, no un descuido: la geometria/estado floating
guardados solo se aplican a las apps que se LANZAN de nuevo durante esta
carga (ver _apply_floating_geometry mas abajo, llamada unicamente dentro
del bucle sobre `missing_apps`). Una ventana que el usuario ya tenia
abierta y que resulto coincidir con el workspace objetivo se deja
exactamente donde esta — moverla sin que el usuario lo pidiera (por
ejemplo, si la habia arrastrado a otra posicion a proposito) seria un
comportamiento sorprendente e indeseado.

-------------------------------------------------------------------------
Sobre workspaces sin ninguna app guardada
-------------------------------------------------------------------------
Si un workspace no tiene ninguna fila en workspace_apps (por ejemplo, se
creo pero nunca se guardo nada en el), reconcile_and_load() simplemente
no tiene nada que "coincida" — bajo la regla de "cerrar todo lo que no
coincide" (activa por defecto, ver seccion 4.11), cargar un workspace
vacio con el cierre activado CIERRA TODO lo que este abierto en ese
desktop. Esto es una consecuencia logica y esperable de la regla tal
como esta especificada, no un caso especial que este modulo intente
evitar — se documenta aqui para que sea evidente y no sorprenda a quien
lea el codigo despues.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import bspc_client
from . import config
from . import database
from . import launcher
from . import window_watcher
from . import x11_utils


class ReconcilerError(Exception):
    """
    Problemas genuinos de datos/entorno que impiden siquiera INTENTAR la
    reconciliacion (ej. el workspace_id no existe). NO se lanza por el
    timeout de una app individual al lanzarla — eso se refleja en
    ReconcileResult.failed, como un resultado normal a reportar, no como
    una excepcion que interrumpa toda la operacion.
    """


@dataclass
class ReconcileResult:
    """
    Reporte detallado de que paso durante una llamada a
    reconcile_and_load(). Pensado para que tanto luxion_cli.py (donde
    puede simplemente imprimirse un resumen por stdout/stderr) como
    ui/workspaces_tab.py (donde puede mostrarse en un dialogo/notificacion)
    puedan informar al usuario sin tener que adivinar que paso a partir
    de efectos secundarios.
    """

    workspace_id: int
    desktop_name: str
    # Apps que ya estaban abiertas y coincidian con el workspace objetivo
    # (no se relanzaron, se dejaron tal cual estaban).
    reused: list[tuple[sqlite3.Row, int]] = field(default_factory=list)
    # Apps que faltaban y se lanzaron con exito (aparecieron a tiempo).
    launched: list[tuple[sqlite3.Row, int]] = field(default_factory=list)
    # Apps que faltaban, se intentaron lanzar, pero no aparecieron dentro
    # del timeout configurado (ver core/launcher.py, seccion 4.13).
    failed: list[sqlite3.Row] = field(default_factory=list)
    # XIDs de ventanas que NO coincidian con el workspace objetivo y se
    # terminaron (ver core.x11_utils.force_kill_by_xid, seccion 4.10).
    killed_xids: list[int] = field(default_factory=list)
    # Si se aplico o no la logica de cierre (ver resolve_close_setting) —
    # informativo, para que quien reciba el resultado sepa si
    # killed_xids esta vacio porque "no habia nada que cerrar" o porque
    # "el cierre estaba desactivado para este workspace".
    close_unmatched_applied: bool = True


# ---------------------------------------------------------------------------
# Matching (seccion 4.7 del plan: diferenciacion de instancias multiples)
# ---------------------------------------------------------------------------


def match_current_to_target(
    current_windows: list[tuple[int, str]],
    target_apps: list[sqlite3.Row],
) -> tuple[dict[int, int], list[sqlite3.Row]]:
    """
    Empareja las ventanas actualmente abiertas con las apps que el
    workspace objetivo necesita, diferenciando correctamente instancias
    multiples del mismo WM_CLASS (ver seccion 4.7 del plan).

    Parametros:
      current_windows: lista de (xid, wm_class), EN EL ORDEN DEL ARBOL de
                        bspwm (el mismo orden que devuelve
                        bspc_client.list_window_ids(), ver seccion 4.5).
      target_apps:      filas de workspace_apps, YA ORDENADAS por
                        launch_order (el orden que devuelve
                        database.get_workspace_apps()).

    Algoritmo: se agrupan las ventanas actuales en "cubetas" por
    wm_class, preservando su orden relativo dentro de cada cubeta. Se
    recorren target_apps EN SU ORDEN (launch_order) y, para cada una, si
    su wm_class tiene alguna ventana disponible en la cubeta
    correspondiente, se "consume" la mas antigua de esa cubeta (FIFO) —
    esto es exactamente el mismo criterio de orden que se uso al
    GUARDAR el workspace para asignar instance_index (ver
    core/database.py -> replace_workspace_apps() y el flujo de guardado
    que construira core/workspace_service.py), asi que la instancia 0
    guardada corresponde de forma consistente a "la primera ventana de
    ese wm_class en el arbol", tanto al guardar como al cargar.

    Devuelve:
      matched:  dict {workspace_apps.id: xid_reutilizado} — las apps que
                YA estaban abiertas y no hace falta relanzar.
      missing:  lista de filas de target_apps (en el MISMO orden que se
                recibieron, es decir, en launch_order) que no tenian
                ninguna ventana actual disponible — estas son las que
                hay que lanzar.
    """
    current_by_class: dict[str, list[int]] = {}
    for xid, wm_class in current_windows:
        current_by_class.setdefault(wm_class, []).append(xid)

    matched: dict[int, int] = {}
    missing: list[sqlite3.Row] = []

    for app in target_apps:
        bucket = current_by_class.get(app["wm_class"])
        if bucket:
            matched[app["id"]] = bucket.pop(0)
        else:
            missing.append(app)

    return matched, missing


# ---------------------------------------------------------------------------
# Ajuste de cierre (seccion 4.11 del plan)
# ---------------------------------------------------------------------------


def resolve_close_setting(workspace_row) -> bool:
    """
    Decide si se deben cerrar las ventanas que no coinciden con el
    workspace objetivo (ver seccion 4.11 del plan):

      - Si workspace_row["close_unmatched_windows"] tiene un valor
        explicito (0 o 1, guardado como override por-workspace en la
        base de datos), ese valor manda, sin importar el ajuste global.
      - Si es None (el caso por defecto: el workspace nunca sobreescribio
        este comportamiento), se usa el ajuste GLOBAL
        "default_close_unmatched_windows" de la tabla settings (ver
        core/config.py).

    `workspace_row` puede ser una sqlite3.Row o cualquier objeto que
    soporte acceso tipo diccionario por la clave
    "close_unmatched_windows" (esto ultimo es lo que permite testear esta
    funcion con un simple dict en el self-test, sin necesitar una fila
    real de base de datos).
    """
    raw = workspace_row["close_unmatched_windows"]
    if raw is not None:
        return bool(raw)
    return bool(config.get_bool("default_close_unmatched_windows", fallback=True))


# ---------------------------------------------------------------------------
# Aplicacion de geometria a apps recien lanzadas
# ---------------------------------------------------------------------------


def _apply_floating_geometry(app: sqlite3.Row, xid: int) -> None:
    """
    Aplica el estado floating y la geometria guardada a una ventana
    RECIEN LANZADA (nunca a una reutilizada, ver la nota al inicio del
    modulo).

    Se envuelve en manejo de errores tolerante a propósito: si algo
    falla aqui (la ventana se cerro en el brevisimo instante entre que
    window_watcher confirmo su aparicion y este punto, una carrera
    posible pero rara), NO se aborta toda la carga del workspace por
    esto — la app ya cuenta como "lanzada" en el reporte final (aparecio
    a tiempo), simplemente no se le pudo aplicar la geometria. Abortar
    la carga completa de un workspace de 10 apps porque la geometria de
    UNA de ellas no se pudo aplicar seria desproporcionado.
    """
    try:
        bspc_client.set_state(xid, "floating")
    except bspc_client.BspcError:
        return

    geom_fields = (app["geom_x"], app["geom_y"], app["geom_w"], app["geom_h"])
    if any(value is None for value in geom_fields):
        # Fila con is_floating=1 pero sin geometria completa guardada.
        # No deberia ocurrir si el guardado se hizo correctamente (ver
        # core/workspace_service.py), pero se protege de todas formas:
        # se deja la ventana en estado floating, simplemente sin
        # reposicionarla a una geometria inexistente.
        return

    x11_utils.set_geometry(xid, geom_fields[0], geom_fields[1], geom_fields[2], geom_fields[3])
    # No se verifica el valor de retorno de set_geometry(): si fallo
    # (por ejemplo la ventana se cerro justo despues del set_state), no
    # hay ninguna accion de recuperacion razonable que tomar al respecto
    # desde aqui.


# ---------------------------------------------------------------------------
# Orquestacion principal
# ---------------------------------------------------------------------------


def reconcile_and_load(workspace_id: int, desktop_name: str) -> ReconcileResult:
    """
    Hace que `desktop_name` refleje el workspace `workspace_id`:

      1. Lee que apps deberia tener el workspace (database).
      2. Lista que ventanas hay REALMENTE abiertas ahora en el desktop
         (bspc_client + x11_utils), ignorando las que se cierran justo
         en este instante (condicion de carrera esperada, ver el manejo
         de wm_class is None mas abajo).
      3. Empareja ambas cosas (match_current_to_target), diferenciando
         instancias multiples del mismo WM_CLASS.
      4. Si corresponde (resolve_close_setting), mata (SIGKILL directo,
         ver seccion 4.10) las ventanas que no coincidieron con nada.
      5. Lanza, EN ORDEN, las apps que faltaban, usando una unica
         NodeAddWatcher para toda la operacion (ver core/window_watcher.py)
         y el timeout configurado en settings.app_open_timeout_seconds.
      6. A las que se lanzaron con exito Y son floating, les aplica la
         geometria guardada.

    Devuelve un ReconcileResult con el detalle completo de que paso.

    Lanza ReconcilerError si `workspace_id` no corresponde a ningun
    workspace existente. Cualquier bspc_client.BspcError que ocurra por
    un problema de ENTORNO real (bspwm no esta corriendo, el proceso de
    suscripcion murio) se propaga tal cual — no se atrapa aqui, porque
    esos casos deben interrumpir toda la operacion de carga, no
    reportarse como un simple "fallo" de una app individual.
    """
    workspace = database.get_workspace(workspace_id)
    if workspace is None:
        raise ReconcilerError(f"El workspace con id={workspace_id} no existe.")

    target_apps = database.get_workspace_apps(workspace_id)

    current_xids = bspc_client.list_window_ids(desktop_name)
    current_windows: list[tuple[int, str]] = []
    for xid in current_xids:
        wm_class = x11_utils.get_wm_class(xid)
        if wm_class is not None:
            current_windows.append((xid, wm_class))
        # Si wm_class es None, la ventana se cerro justo entre el
        # listado (list_window_ids) y esta consulta puntual — una
        # condicion de carrera esperada. Se ignora: ni cuenta como
        # candidata a reutilizar, ni como candidata a cerrar (ya no
        # existe, no hay nada que cerrar).

    matched, missing_apps = match_current_to_target(current_windows, target_apps)

    should_close = resolve_close_setting(workspace)

    killed_xids: list[int] = []
    if should_close:
        matched_xids = set(matched.values())
        for xid, _wm_class in current_windows:
            if xid not in matched_xids:
                if x11_utils.force_kill_by_xid(xid):
                    killed_xids.append(xid)
                # Si force_kill_by_xid devuelve False (no se pudo
                # obtener el PID, o permiso denegado), no se agrega a
                # killed_xids. No se lanza excepcion por esto: abortar
                # toda la carga porque UN proceso no se pudo matar seria
                # desproporcionado.

    reused: list[tuple[sqlite3.Row, int]] = [
        (app, matched[app["id"]]) for app in target_apps if app["id"] in matched
    ]

    launched: list[tuple[sqlite3.Row, int]] = []
    failed: list[sqlite3.Row] = []

    if missing_apps:
        timeout_seconds = config.get_int("app_open_timeout_seconds", fallback=7)
        with window_watcher.NodeAddWatcher() as watcher:
            for app in missing_apps:
                ok, xid = launcher.launch_and_wait(
                    watcher, app["launch_command"], app["wm_class"], timeout_seconds
                )
                if ok and xid is not None:
                    launched.append((app, xid))
                    if app["is_floating"]:
                        _apply_floating_geometry(app, xid)
                else:
                    failed.append(app)
                # Se continua con la siguiente app de la lista sin
                # importar el resultado de esta — ver seccion 4.13 del
                # plan: un timeout individual no detiene el resto de la
                # carga.

    return ReconcileResult(
        workspace_id=workspace_id,
        desktop_name=desktop_name,
        reused=reused,
        launched=launched,
        failed=failed,
        killed_xids=killed_xids,
        close_unmatched_applied=should_close,
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.reconciler
    #
    # Estrategia de testing para este modulo especificamente (distinta a
    # la de los modulos anteriores): reconciler.py compone MUCHAS otras
    # piezas (database, bspc_client, x11_utils, window_watcher,
    # launcher). Cada una de esas piezas YA tiene su propia bateria de
    # pruebas dedicada (algunas con subprocesos reales, como
    # window_watcher.py y launcher.py). Volver a probar la mecanica
    # interna de CADA una desde aqui seria redundante y haria estas
    # pruebas fragiles/lentas sin aportar mas confianza real.
    #
    # Por eso aqui se hace lo contrario: se usa una base de datos SQLite
    # REAL (en un archivo temporal, no la de produccion) para ejercitar
    # database.py de verdad, pero se mockean los PUNTOS DE FRONTERA que
    # dependen de bspwm/xdotool reales (list_window_ids, get_wm_class,
    # force_kill_by_xid, set_state, set_geometry) y de la suscripcion a
    # eventos (NodeAddWatcher, launch_and_wait) — confiando en que ESOS
    # componentes ya fueron verificados por su cuenta. Lo que se quiere
    # probar aqui es la LOGICA DE ORQUESTACION que es unica de este
    # archivo: el matching con diferenciacion de instancias, la decision
    # de que cerrar, el orden de lanzamiento, y que la geometria se
    # aplique solo a lo recien lanzado.

    import os
    import tempfile
    import unittest.mock as mock

    passed = 0
    failed_count = 0

    def check(label: str, condition: bool):
        global passed, failed_count
        if condition:
            print(f"OK: {label}")
            passed += 1
        else:
            print(f"FALLO: {label}")
            failed_count += 1

    # --- Pruebas unitarias de match_current_to_target, sin base de datos ---
    # Se usan dicts simples en vez de sqlite3.Row: match_current_to_target
    # solo hace acceso tipo app["wm_class"] / app["id"], que un dict
    # cumple identico.
    fake_apps = [
        {"id": 1, "wm_class": "Firefox", "launch_order": 0},
        {"id": 2, "wm_class": "Firefox", "launch_order": 1},
        {"id": 3, "wm_class": "Code", "launch_order": 2},
    ]
    fake_current = [(0x1001, "Firefox"), (0x2001, "UnrelatedApp")]
    matched, missing = match_current_to_target(fake_current, fake_apps)
    check(
        "match_current_to_target reutiliza la primera instancia de Firefox disponible",
        matched == {1: 0x1001},
    )
    check(
        "match_current_to_target deja como faltantes la 2da instancia de Firefox y Code",
        [app["id"] for app in missing] == [2, 3],
    )

    # Caso con DOS instancias de Firefox ya abiertas: deben asignarse en
    # orden (FIFO) a las dos filas objetivo de Firefox, respetando el
    # mismo criterio de orden usado al guardar.
    fake_current_2 = [(0x1001, "Firefox"), (0x1002, "Firefox")]
    matched_2, missing_2 = match_current_to_target(fake_current_2, fake_apps)
    check(
        "con 2 instancias de Firefox abiertas, ambas se reutilizan en orden",
        matched_2 == {1: 0x1001, 2: 0x1002},
    )
    check(
        "solo Code queda como faltante cuando ambas instancias de Firefox ya estan abiertas",
        [app["id"] for app in missing_2] == [3],
    )

    # --- resolve_close_setting ---
    check(
        "resolve_close_setting respeta el override explicito 0 (no cerrar)",
        resolve_close_setting({"close_unmatched_windows": 0}) is False,
    )
    check(
        "resolve_close_setting respeta el override explicito 1 (si cerrar)",
        resolve_close_setting({"close_unmatched_windows": 1}) is True,
    )
    with mock.patch("core.config.get_bool", return_value=False):
        check(
            "resolve_close_setting usa el default global cuando el workspace no tiene override",
            resolve_close_setting({"close_unmatched_windows": None}) is False,
        )

    # --- Integracion completa de reconcile_and_load, con DB real en un
    #     archivo temporal y los puntos de frontera con bspwm/xdotool
    #     mockeados ---
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "test_luxion.db")

        with mock.patch("core.config.DB_PATH", new=tmp_db_path):
            database.init_db()

            ws_id = database.create_workspace(name="TestWorkspace")
            database.replace_workspace_apps(
                ws_id,
                [
                    {  # FirefoxA: ya deberia estar abierta -> se reutiliza
                        "launch_order": 0,
                        "wm_class": "Firefox",
                        "instance_index": 0,
                        "launch_command": "true",
                        "is_floating": False,
                    },
                    {  # FirefoxB: falta -> se debe lanzar
                        "launch_order": 1,
                        "wm_class": "Firefox",
                        "instance_index": 1,
                        "launch_command": "true",
                        "is_floating": False,
                    },
                    {  # CodeApp: falta y es floating -> se debe lanzar y reposicionar
                        "launch_order": 2,
                        "wm_class": "Code",
                        "instance_index": 0,
                        "launch_command": "true",
                        "is_floating": True,
                        "geom_x": 10,
                        "geom_y": 20,
                        "geom_w": 300,
                        "geom_h": 400,
                    },
                ],
            )

            # Estado "actual" simulado del desktop: una Firefox abierta
            # (xid 0x1001, coincide con FirefoxA) y una app no relacionada
            # (xid 0x2001, no coincide con nada -> candidata a cerrar).
            fake_wm_classes = {0x1001: "Firefox", 0x2001: "UnrelatedApp"}

            # launch_and_wait simulado: asigna XIDs incrementales segun
            # el wm_class solicitado, registrando cada llamada para
            # verificar despues el orden y los argumentos exactos.
            launch_calls: list[tuple[str, float]] = []
            next_fake_xid = [0x3000]

            def fake_launch_and_wait(watcher, command, wm_class, timeout_seconds):
                launch_calls.append((wm_class, timeout_seconds))
                next_fake_xid[0] += 1
                return True, next_fake_xid[0]

            set_state_calls: list[tuple[int, str]] = []
            set_geometry_calls: list[tuple[int, int, int, int, int]] = []

            with mock.patch("core.bspc_client.list_window_ids", return_value=[0x1001, 0x2001]), \
                 mock.patch("core.x11_utils.get_wm_class", side_effect=lambda xid: fake_wm_classes.get(xid)), \
                 mock.patch("core.x11_utils.force_kill_by_xid", side_effect=lambda xid: True) as mock_kill, \
                 mock.patch("core.window_watcher.NodeAddWatcher") as MockWatcherClass, \
                 mock.patch("core.launcher.launch_and_wait", side_effect=fake_launch_and_wait), \
                 mock.patch("core.bspc_client.set_state", side_effect=lambda xid, state: set_state_calls.append((xid, state))), \
                 mock.patch("core.x11_utils.set_geometry", side_effect=lambda xid, x, y, w, h: set_geometry_calls.append((xid, x, y, w, h)) or True):

                # NodeAddWatcher() se usa como "with NodeAddWatcher() as watcher:";
                # se configura el mock para comportarse como un context
                # manager normal que NO suprime excepciones.
                mock_watcher_instance = mock.MagicMock()
                mock_watcher_instance.__enter__.return_value = mock_watcher_instance
                mock_watcher_instance.__exit__.return_value = False
                MockWatcherClass.return_value = mock_watcher_instance

                result = reconcile_and_load(ws_id, "TESTDESK")

            check("reconcile_and_load reutiliza FirefoxA (ya abierta)", len(result.reused) == 1)
            check(
                "la app reutilizada es la correcta, con el xid correcto",
                result.reused[0][1] == 0x1001 and result.reused[0][0]["wm_class"] == "Firefox",
            )
            check(
                "se mato la ventana no coincidente (UnrelatedApp)",
                result.killed_xids == [0x2001] and mock_kill.call_args == ((0x2001,),),
            )
            check("close_unmatched_applied es True por defecto", result.close_unmatched_applied is True)
            check(
                "se lanzaron las 2 apps faltantes, EN ORDEN (FirefoxB, luego Code)",
                [c[0] for c in launch_calls] == ["Firefox", "Code"],
            )
            check("no hubo fallos de lanzamiento", result.failed == [])
            check("2 apps quedaron registradas como lanzadas", len(result.launched) == 2)

            code_app_row, code_xid = result.launched[1]
            check(
                "a la app floating (Code) se le aplico el estado floating",
                set_state_calls == [(code_xid, "floating")],
            )
            check(
                "a la app floating (Code) se le aplico la geometria guardada",
                set_geometry_calls == [(code_xid, 10, 20, 300, 400)],
            )

            firefox_b_row, firefox_b_xid = result.launched[0]
            check(
                "a la app NO floating (FirefoxB) no se le aplico ninguna geometria",
                all(call[0] != firefox_b_xid for call in set_geometry_calls),
            )

            # --- Segunda corrida: close_unmatched_windows=False (override) ---
            database.update_workspace_close_setting(ws_id, close_unmatched_windows=False)
            set_state_calls.clear()
            set_geometry_calls.clear()
            launch_calls.clear()
            next_fake_xid[0] = 0x4000

            with mock.patch("core.bspc_client.list_window_ids", return_value=[0x1001, 0x2001]), \
                 mock.patch("core.x11_utils.get_wm_class", side_effect=lambda xid: fake_wm_classes.get(xid)), \
                 mock.patch("core.x11_utils.force_kill_by_xid", side_effect=lambda xid: True) as mock_kill_2, \
                 mock.patch("core.window_watcher.NodeAddWatcher") as MockWatcherClass2, \
                 mock.patch("core.launcher.launch_and_wait", side_effect=fake_launch_and_wait), \
                 mock.patch("core.bspc_client.set_state"), \
                 mock.patch("core.x11_utils.set_geometry", return_value=True):

                mock_watcher_instance_2 = mock.MagicMock()
                mock_watcher_instance_2.__enter__.return_value = mock_watcher_instance_2
                mock_watcher_instance_2.__exit__.return_value = False
                MockWatcherClass2.return_value = mock_watcher_instance_2

                result_2 = reconcile_and_load(ws_id, "TESTDESK")

            check(
                "con close_unmatched_windows=False, NO se mata nada",
                result_2.killed_xids == [] and mock_kill_2.call_count == 0,
            )
            check("close_unmatched_applied refleja False en el reporte", result_2.close_unmatched_applied is False)

            # --- Tercera corrida: una app faltante nunca aparece (timeout) ---
            database.update_workspace_close_setting(ws_id, close_unmatched_windows=None)

            def fake_launch_and_wait_with_failure(watcher, command, wm_class, timeout_seconds):
                if wm_class == "Code":
                    return False, None  # simula timeout
                next_fake_xid[0] += 1
                return True, next_fake_xid[0]

            with mock.patch("core.bspc_client.list_window_ids", return_value=[0x1001]), \
                 mock.patch("core.x11_utils.get_wm_class", side_effect=lambda xid: fake_wm_classes.get(xid)), \
                 mock.patch("core.x11_utils.force_kill_by_xid", return_value=True), \
                 mock.patch("core.window_watcher.NodeAddWatcher") as MockWatcherClass3, \
                 mock.patch("core.launcher.launch_and_wait", side_effect=fake_launch_and_wait_with_failure), \
                 mock.patch("core.bspc_client.set_state") as mock_set_state_3, \
                 mock.patch("core.x11_utils.set_geometry") as mock_set_geometry_3:

                mock_watcher_instance_3 = mock.MagicMock()
                mock_watcher_instance_3.__enter__.return_value = mock_watcher_instance_3
                mock_watcher_instance_3.__exit__.return_value = False
                MockWatcherClass3.return_value = mock_watcher_instance_3

                result_3 = reconcile_and_load(ws_id, "TESTDESK")

            check(
                "la app que nunca aparecio queda en failed, no en launched",
                len(result_3.failed) == 1 and result_3.failed[0]["wm_class"] == "Code",
            )
            check(
                "la carga continua con las demas apps pese al fallo de una",
                len(result_3.launched) == 1 and result_3.launched[0][0]["wm_class"] == "Firefox",
            )
            check(
                "no se intenta aplicar geometria a una app que nunca aparecio",
                mock_set_state_3.call_count == 0 and mock_set_geometry_3.call_count == 0,
            )

            # --- Workspace inexistente -> ReconcilerError ---
            try:
                reconcile_and_load(999999, "TESTDESK")
                check("ReconcilerError ante workspace inexistente", False)
            except ReconcilerError:
                check("ReconcilerError ante workspace inexistente", True)

    print(f"\n{passed} pruebas OK, {failed_count} pruebas fallidas.")
    if failed_count:
        raise SystemExit(1)
