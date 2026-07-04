"""
core/workspace_service.py
==========================

La UNICA fachada que luxion_cli.py y ui/ deberian usar para operar sobre
workspaces (ver seccion 4.17 del plan arquitectonico: "workspace_service.py
... es la fachada que tanto luxion_cli.py como ui/workspaces_tab.py deben
usar como unico punto de entrada a la logica de negocio"). Nada fuera de
este archivo deberia importar core.database, core.reconciler, ni
core.lockfile directamente — todo pasa por aqui.

Funciones que MUTAN estado y usan el lock de concurrencia (ver seccion
4.16 del plan: solo estas tres, porque son las unicas que manipulan
ventanas/procesos reales y por lo tanto pueden entrar en conflicto con
otra operacion similar corriendo al mismo tiempo):

    save(...)     -> foto del desktop actual, guardada en un workspace
    load(...)     -> hace que un desktop refleje un workspace guardado
    delete(...)   -> elimina un workspace (y resincroniza sxhkdrc si
                     tenia un atajo asignado)

Funciones que MUTAN solo metadata (nombre, atajo, ajuste de cierre) y
NO usan el lock, porque no tocan ventanas ni procesos, asi que no hay
ningun conflicto real que prevenir corriendolas en paralelo con un
save()/load() en curso:

    rename(...)
    set_hotkey(...)
    set_close_unmatched_windows(...)

Funciones de solo lectura (passthrough directo a core/database.py, para
que ui/ y luxion_cli.py nunca necesiten importar core.database por su
cuenta):

    get_workspace(...)
    list_workspaces(...)
    get_workspace_apps(...)

-------------------------------------------------------------------------
Nota tecnica IMPORTANTE sobre el import diferido de hotkey_manager
-------------------------------------------------------------------------
A diferencia del import diferido de core/config.py (que es una eleccion
de estilo para evitar un ciclo de imports), aqui el import diferido de
`hotkey_manager` es ACTUALMENTE NECESARIO por una razon mas basica:
core/hotkey_manager.py todavia no existe como archivo en el proyecto (es
el siguiente modulo a construir). Si este archivo hiciera
"from . import hotkey_manager" a nivel de modulo, NINGUNA funcion de
workspace_service.py podria siquiera importarse hasta que
hotkey_manager.py exista — incluyendo save()/load(), que no tienen
absolutamente nada que ver con atajos de teclado. Con el import diferido
DENTRO de delete() y set_hotkey() (las unicas dos funciones que
realmente lo necesitan), el resto de este archivo es usable y testeable
HOY. Una vez que core/hotkey_manager.py exista, esto seguira funcionando
exactamente igual sin necesitar ningun cambio aqui.
"""

from __future__ import annotations

from typing import Optional

from . import bspc_client
from . import database
from . import lockfile
from . import reconciler
from . import x11_utils


# ---------------------------------------------------------------------------
# save() y su logica de captura (seccion 4.6 y 4.7 del plan)
# ---------------------------------------------------------------------------


def _capture_desktop_snapshot(desktop_name: str) -> list[dict]:
    """
    Recorre todas las ventanas actualmente abiertas en `desktop_name`, EN
    EL ORDEN DEL ARBOL de bspwm, y arma la lista de filas lista para
    core.database.replace_workspace_apps() — WM_CLASS, comando de
    relanzamiento, diferenciacion de instancias, y estado/geometria
    floating cuando corresponde.

    Ventanas que se descartan de la foto (y por que):

      - Si core.x11_utils.get_wm_class() devuelve None: la ventana se
        cerro entre el listado y esta consulta (condicion de carrera
        esperada). No hay nada identificable que guardar.

      - Si core.x11_utils.get_launch_command() devuelve None: no se pudo
        determinar el PID (misma carrera de arriba) o el comando con el
        que se lanzo el proceso. Guardar esta ventana de todas formas
        seria inutil: al cargar el workspace despues, SIEMPRE terminaria
        en la lista de "failed" de ReconcileResult, porque no habria
        ningun comando con el cual relanzarla.

      - Si core.bspc_client.is_floating() dice True pero
        get_floating_rectangle() devuelve None (la ventana se cerro en
        el intervalo entre ambas consultas): se guarda IGUAL, pero como
        NO floating — es preferible recordar la app sin geometria exacta
        (se reinsertara tileada al cargar) a perderla del workspace por
        completo.

    La asignacion de instance_index (seccion 4.7) usa el MISMO criterio
    de orden con el que core.reconciler.match_current_to_target() empareja
    al cargar: el orden en que las ventanas aparecen en el arbol de
    bspwm. Esto es lo que garantiza que "instancia 0" de un wm_class dado
    signifique lo mismo tanto al guardar como al cargar.
    """
    current_xids = bspc_client.list_window_ids(desktop_name)

    captured: list[dict] = []
    for xid in current_xids:
        wm_class = x11_utils.get_wm_class(xid)
        if wm_class is None:
            continue

        launch_command = x11_utils.get_launch_command(xid)
        if launch_command is None:
            continue

        is_floating = bool(bspc_client.is_floating(xid))
        geom: Optional[dict] = None
        if is_floating:
            geom = bspc_client.get_floating_rectangle(xid)
            if geom is None:
                is_floating = False

        captured.append(
            {
                "wm_class": wm_class,
                "launch_command": launch_command,
                "is_floating": is_floating,
                "geom": geom,
            }
        )

    instance_counters: dict[str, int] = {}
    rows: list[dict] = []
    for launch_order, item in enumerate(captured):
        wm_class = item["wm_class"]
        instance_index = instance_counters.get(wm_class, 0)
        instance_counters[wm_class] = instance_index + 1

        row: dict = {
            "launch_order": launch_order,
            "wm_class": wm_class,
            "instance_index": instance_index,
            "launch_command": item["launch_command"],
            "is_floating": item["is_floating"],
        }
        if item["geom"] is not None:
            row["geom_x"] = item["geom"]["x"]
            row["geom_y"] = item["geom"]["y"]
            row["geom_w"] = item["geom"]["w"]
            row["geom_h"] = item["geom"]["h"]
        rows.append(row)

    return rows


def save(
    workspace_id: Optional[int] = None,
    name: Optional[str] = None,
    desktop_name: Optional[str] = None,
) -> int:
    """
    Guarda una foto completa del desktop indicado (o del actualmente
    enfocado, si `desktop_name` es None) en un workspace.

    Dos modos de uso, segun `workspace_id`:

      - `workspace_id=None` (por defecto): crea un workspace NUEVO. Se
        le puede dar un `name`; si se omite (o queda vacio), se
        autogenera "Workspace {id}" (ver core.database.create_workspace).
        Devuelve el id del workspace recien creado.

      - `workspace_id=<int existente>`: SOBREESCRIBE por completo las
        apps guardadas de ESE workspace con el estado actual del
        desktop (ver seccion 4.6 del plan: guardar un workspace que ya
        existia nunca acumula historico, siempre reemplaza). El
        parametro `name` se IGNORA en este modo — para renombrar un
        workspace existente, usar rename() por separado. Devuelve el
        mismo workspace_id que se paso.

    Orden de operaciones (importante para la atomicidad percibida): la
    foto del desktop se captura POR COMPLETO antes de tocar la base de
    datos. Si _capture_desktop_snapshot() fallara a mitad de camino (por
    ejemplo, bspc_client.list_window_ids() lanza BspcError porque bspwm
    no esta corriendo), NO queda ningun workspace nuevo vacio creado a
    medias ni ninguna fila vieja borrada — el error se propaga antes de
    que exista cualquier oportunidad de modificar la base de datos.

    Usa el lock de concurrencia (core.lockfile): lanza
    lockfile.LuxionBusyError si hay otra operacion de save/load/delete
    en curso en este momento.
    """
    with lockfile.acquire(operation="guardar workspace"):
        if desktop_name is None:
            desktop_name = bspc_client.get_focused_desktop_name()

        rows = _capture_desktop_snapshot(desktop_name)

        if workspace_id is None:
            workspace_id = database.create_workspace(name=name)

        database.replace_workspace_apps(workspace_id, rows)
        return workspace_id


# ---------------------------------------------------------------------------
# load() (seccion 4.9 del plan)
# ---------------------------------------------------------------------------


def load(workspace_id: int) -> reconciler.ReconcileResult:
    """
    Carga `workspace_id` en el desktop de bspwm ACTUALMENTE ENFOCADO —
    determinado en el momento exacto de esta llamada (ver seccion 4.9:
    "revisar las ventanas abiertas en el escritorio EN EL QUE SE INVOCO
    la funcion cargar"), sin importar si esta funcion se invoco desde la
    GUI o desde luxion_cli.py via un atajo de teclado.

    Delega toda la logica de reconciliacion (matching, cierre de lo que
    no coincide, lanzamiento de lo que falta) a
    core.reconciler.reconcile_and_load() — ver ese modulo para el
    detalle completo. Esta funcion se limita a:
      1. Adquirir el lock de concurrencia.
      2. Determinar el desktop enfocado.
      3. Llamar a reconciler.reconcile_and_load().
      4. Registrar en desktop_state que este workspace quedo activo en
         ese desktop (ver core.database.set_desktop_state).

    Devuelve el ReconcileResult completo (ver core/reconciler.py) para
    que el llamador (CLI o GUI) pueda informar al usuario que paso
    exactamente: cuantas apps se reutilizaron, cuantas se lanzaron,
    cuantas fallaron, cuantas ventanas se cerraron.

    Lanza:
      lockfile.LuxionBusyError    si hay otra operacion en curso.
      reconciler.ReconcilerError  si `workspace_id` no existe.
      bspc_client.BspcError       ante un problema de entorno real
                                  (bspwm no esta corriendo, la
                                  suscripcion a eventos murio a mitad de
                                  la carga).
    """
    with lockfile.acquire(operation="cargar workspace"):
        desktop_name = bspc_client.get_focused_desktop_name()
        result = reconciler.reconcile_and_load(workspace_id, desktop_name)
        database.set_desktop_state(desktop_name, workspace_id)
        return result


# ---------------------------------------------------------------------------
# delete() (seccion 4.14 del plan)
# ---------------------------------------------------------------------------


def delete(workspace_id: int) -> None:
    """
    Elimina un workspace. Gracias a "ON DELETE CASCADE"/"ON DELETE SET
    NULL" en el esquema (ver core/database.py), esto tambien limpia
    automaticamente sus filas de workspace_apps y cualquier referencia
    en desktop_state — no hace falta limpieza manual adicional para eso.

    Si el workspace eliminado tenia un atajo de teclado asignado, se
    resincroniza ~/.config/sxhkd/sxhkdrc despues del borrado (ver
    core/hotkey_manager.py, seccion 4.8), para que ese atajo deje de
    estar activo. Como optimizacion sobre el diseño original del plan
    (que resincronizaba SIEMPRE, sin condicion), aqui solo se dispara
    esa resincronizacion — que implica matar y reiniciar el proceso
    sxhkd, una interrupcion momentanea de TODOS los atajos, no solo el
    del workspace eliminado — cuando realmente hacia falta.

    Eliminar un workspace_id que no existe es un no-op silencioso (0
    filas afectadas), no un error — mismo criterio que un DELETE
    idempotente tipico.

    Usa el lock de concurrencia: lanza lockfile.LuxionBusyError si hay
    otra operacion de save/load/delete en curso.
    """
    with lockfile.acquire(operation="eliminar workspace"):
        workspace = database.get_workspace(workspace_id)
        had_hotkey = workspace is not None and workspace["hotkey"] is not None

        database.delete_workspace(workspace_id)

        if had_hotkey:
            from . import hotkey_manager  # ver nota tecnica al inicio del archivo

            hotkey_manager.sync_sxhkdrc_and_restart()


# ---------------------------------------------------------------------------
# Operaciones de metadata (NO usan el lock, ver docstring del modulo)
# ---------------------------------------------------------------------------


def rename(workspace_id: int, new_name: str) -> str:
    """
    Cambia el nombre de un workspace (seccion 4.15 del plan). Si
    `new_name` queda vacio despues de quitarle espacios en blanco
    (incluye el caso de "borrar" el nombre por completo desde la GUI),
    se autogenera "Workspace {id}" en su lugar — la columna
    workspaces.name es NOT NULL UNIQUE, un nombre vacio no es una opcion
    valida.

    Si el nombre (o el nombre por defecto autogenerado) ya esta en uso
    por OTRO workspace, se le agrega un sufijo numerico incremental
    (ver core.database.ensure_unique_name): "Tesis", "Tesis (2)", etc.

    Devuelve el nombre FINAL que efectivamente quedo guardado (puede
    diferir de `new_name` por cualquiera de las dos razones de arriba),
    para que la GUI pueda refrescar el campo de texto con el resultado
    real en vez de asumir que se uso tal cual lo que el usuario escribio.
    """
    clean_name = new_name.strip() if new_name else ""
    if not clean_name:
        clean_name = f"Workspace {workspace_id}"

    final_name = database.ensure_unique_name(clean_name, exclude_id=workspace_id)
    database.update_workspace_name(workspace_id, final_name)
    return final_name


def set_hotkey(workspace_id: int, hotkey: Optional[str]) -> None:
    """
    Asigna (`hotkey` como string, ej. "super + 1") o quita (`hotkey=None`)
    el atajo de teclado de un workspace (seccion 4.8 del plan), y
    sincroniza inmediatamente ~/.config/sxhkd/sxhkdrc + reinicia sxhkd
    para que el cambio tenga efecto sin necesidad de cerrar sesion.

    Si `hotkey` ya esta asignado a OTRO workspace, la restriccion UNIQUE
    de workspaces.hotkey hace que database.update_workspace_hotkey()
    falle con sqlite3.IntegrityError — se deja propagar tal cual, sin
    intentar "arreglarlo" automaticamente (a diferencia de los nombres,
    un atajo de teclado duplicado no tiene una resolucion razonable como
    agregarle un sufijo: dos atajos identicos simplemente no pueden
    coexistir en sxhkdrc). Es responsabilidad de ui/hotkey_dialog.py
    validar esto ANTES de llegar a llamar a esta funcion, por ejemplo
    negandose a capturar una combinacion que ya este en uso.
    """
    database.update_workspace_hotkey(workspace_id, hotkey)

    from . import hotkey_manager  # ver nota tecnica al inicio del archivo

    hotkey_manager.sync_sxhkdrc_and_restart()


def set_close_unmatched_windows(workspace_id: int, value: Optional[bool]) -> None:
    """
    Actualiza el override por-workspace de si se deben cerrar (matar)
    las ventanas que no coincidan con el workspace al cargarlo (seccion
    4.11 del plan). `value=None` hace que ese workspace vuelva a usar el
    ajuste global (settings.default_close_unmatched_windows, ver
    core/config.py). `value=True`/`value=False` fuerza el comportamiento
    para ESE workspace especifico, sin importar el ajuste global.
    """
    database.update_workspace_close_setting(workspace_id, value)


# ---------------------------------------------------------------------------
# Lecturas (passthrough directo a core/database.py)
# ---------------------------------------------------------------------------


def get_workspace(workspace_id: int):
    """Passthrough de core.database.get_workspace() — ver ese modulo."""
    return database.get_workspace(workspace_id)


def list_workspaces(order_by: str = "name"):
    """Passthrough de core.database.list_workspaces() — ver ese modulo."""
    return database.list_workspaces(order_by=order_by)


def get_workspace_apps(workspace_id: int):
    """Passthrough de core.database.get_workspace_apps() — ver ese modulo."""
    return database.get_workspace_apps(workspace_id)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.workspace_service
    #
    # Misma filosofia de testing que core/reconciler.py: base de datos
    # SQLite real en un archivo temporal, mockeando los puntos de
    # frontera con bspwm/xdotool. Para load(), se mockea directamente
    # core.reconciler.reconcile_and_load() en vez de sus dependencias
    # internas (bspc_client, x11_utils, window_watcher, launcher) —
    # reconciler.py ya tiene su propia bateria de pruebas dedicada que
    # verifica esa logica a fondo; aqui solo interesa confirmar que
    # workspace_service.load() lo invoca correctamente y persiste
    # desktop_state despues.
    #
    # Para delete()/set_hotkey() (que dependen de core/hotkey_manager.py,
    # un modulo que TODAVIA NO EXISTE como archivo — es el siguiente a
    # construir), se inyecta un modulo falso directamente en
    # sys.modules["core.hotkey_manager"] ANTES de que el import diferido
    # de esas funciones se ejecute. Esto es una tecnica valida y comun
    # para probar imports diferidos de dependencias que aun no existen
    # fisicamente: el sistema de imports de Python encuentra el modulo
    # ya presente en sys.modules y nunca intenta tocar el sistema de
    # archivos para buscarlo.

    import os
    import sys
    import tempfile
    import types
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

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "test_luxion.db")

        with mock.patch("core.config.DB_PATH", new=tmp_db_path):
            database.init_db()

            # ------------------ save(): creacion de workspace nuevo ------------------
            fake_wm_class = {
                0x1001: "Firefox",
                0x1002: "Firefox",
                0x1003: "Code",
                0x1004: None,  # se cerro justo antes de consultar su wm_class
                0x1005: "NoCmd",  # wm_class SI se obtiene, pero...
                0x1006: "FloatingButRaceOnGeom",
            }
            fake_launch_command = {
                0x1001: "true",
                0x1002: "firefox --other",
                0x1003: "code ~/proyecto",
                0x1005: None,  # ...no se pudo obtener su comando de relanzamiento
                0x1006: "true",
            }
            fake_is_floating = {0x1001: False, 0x1002: False, 0x1003: True, 0x1006: True}
            fake_floating_rect = {
                0x1003: {"x": 10, "y": 20, "w": 300, "h": 400},
                0x1006: None,  # se cerro entre is_floating() y get_floating_rectangle()
            }

            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch(
                     "core.bspc_client.list_window_ids",
                     return_value=[0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1006],
                 ), \
                 mock.patch("core.x11_utils.get_wm_class", side_effect=lambda xid: fake_wm_class.get(xid)), \
                 mock.patch(
                     "core.x11_utils.get_launch_command", side_effect=lambda xid: fake_launch_command.get(xid)
                 ), \
                 mock.patch(
                     "core.bspc_client.is_floating", side_effect=lambda xid: fake_is_floating.get(xid, False)
                 ), \
                 mock.patch(
                     "core.bspc_client.get_floating_rectangle",
                     side_effect=lambda xid: fake_floating_rect.get(xid),
                 ):
                ws_id = save(name="MiWorkspace")

            saved_apps = database.get_workspace_apps(ws_id)
            check("save() crea el workspace y guarda 4 apps (2 se descartan por carreras)", len(saved_apps) == 4)
            check(
                "save() asigna instance_index correctamente para las 2 instancias de Firefox",
                saved_apps[0]["wm_class"] == "Firefox"
                and saved_apps[0]["instance_index"] == 0
                and saved_apps[1]["wm_class"] == "Firefox"
                and saved_apps[1]["instance_index"] == 1,
            )
            check(
                "save() guarda correctamente la geometria de la app floating (Code)",
                saved_apps[2]["wm_class"] == "Code"
                and bool(saved_apps[2]["is_floating"]) is True
                and (saved_apps[2]["geom_x"], saved_apps[2]["geom_y"], saved_apps[2]["geom_w"], saved_apps[2]["geom_h"])
                == (10, 20, 300, 400),
            )
            check(
                "save() degrada a NO floating la ventana con carrera en la geometria, pero la conserva",
                saved_apps[3]["wm_class"] == "FloatingButRaceOnGeom"
                and bool(saved_apps[3]["is_floating"]) is False,
            )

            # ------------------ save(): sobreescritura de workspace existente ------------------
            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch("core.bspc_client.list_window_ids", return_value=[0x1001]), \
                 mock.patch("core.x11_utils.get_wm_class", return_value="Firefox"), \
                 mock.patch("core.x11_utils.get_launch_command", return_value="true"), \
                 mock.patch("core.bspc_client.is_floating", return_value=False):
                returned_id = save(workspace_id=ws_id)

            check("save() sobre un workspace existente devuelve el mismo id", returned_id == ws_id)
            check(
                "save() sobre un workspace existente REEMPLAZA (no acumula) las apps",
                len(database.get_workspace_apps(ws_id)) == 1,
            )

            # ------------------ load() ------------------
            def fake_reconcile_and_load(workspace_id, desktop_name):
                return reconciler.ReconcileResult(workspace_id=workspace_id, desktop_name=desktop_name)

            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch(
                     "core.reconciler.reconcile_and_load", side_effect=fake_reconcile_and_load
                 ) as mock_reconcile:
                result = load(ws_id)

            check("load() delega en reconciler.reconcile_and_load con los argumentos correctos",
                  mock_reconcile.call_args == ((ws_id, "TESTDESK"),))
            check("load() devuelve el ReconcileResult tal cual", result.workspace_id == ws_id)
            desktop_state_row = database.get_desktop_state("TESTDESK")
            check(
                "load() registra el workspace activo en desktop_state",
                desktop_state_row is not None and desktop_state_row["active_workspace_id"] == ws_id,
            )

            # ------------------ rename() ------------------
            final_name = rename(ws_id, "  Nuevo Nombre  ")
            check("rename() recorta espacios en blanco", final_name == "Nuevo Nombre")
            check(
                "rename() persiste el nuevo nombre en la base de datos",
                database.get_workspace(ws_id)["name"] == "Nuevo Nombre",
            )

            empty_name_result = rename(ws_id, "   ")
            check(
                "rename() con nombre vacio autogenera 'Workspace {id}'",
                empty_name_result == f"Workspace {ws_id}",
            )

            other_ws_id = database.create_workspace(name="Ocupado")
            colliding_result = rename(ws_id, "Ocupado")
            check(
                "rename() resuelve colisiones de nombre con un sufijo numerico",
                colliding_result == "Ocupado (2)",
            )

            # ------------------ set_close_unmatched_windows() ------------------
            set_close_unmatched_windows(ws_id, True)
            check(
                "set_close_unmatched_windows(True) persiste 1",
                database.get_workspace(ws_id)["close_unmatched_windows"] == 1,
            )
            set_close_unmatched_windows(ws_id, False)
            check(
                "set_close_unmatched_windows(False) persiste 0",
                database.get_workspace(ws_id)["close_unmatched_windows"] == 0,
            )
            set_close_unmatched_windows(ws_id, None)
            check(
                "set_close_unmatched_windows(None) persiste NULL",
                database.get_workspace(ws_id)["close_unmatched_windows"] is None,
            )

            # ------------------ delete()/set_hotkey() con hotkey_manager falso ------------------
            fake_hotkey_manager = types.ModuleType("core.hotkey_manager")
            fake_hotkey_manager.sync_calls = []

            def _fake_sync():
                fake_hotkey_manager.sync_calls.append(True)

            fake_hotkey_manager.sync_sxhkdrc_and_restart = _fake_sync

            with mock.patch.dict(sys.modules, {"core.hotkey_manager": fake_hotkey_manager}):
                set_hotkey(ws_id, "super + 5")
                check(
                    "set_hotkey() persiste el atajo en la base de datos",
                    database.get_workspace(ws_id)["hotkey"] == "super + 5",
                )
                check(
                    "set_hotkey() dispara la resincronizacion de sxhkdrc",
                    len(fake_hotkey_manager.sync_calls) == 1,
                )

                delete(ws_id)
                check(
                    "delete() de un workspace CON hotkey SI dispara la resincronizacion",
                    len(fake_hotkey_manager.sync_calls) == 2,
                )
                check("delete() efectivamente elimina el workspace", database.get_workspace(ws_id) is None)

                # other_ws_id NUNCA tuvo hotkey asignado -> delete() NO
                # deberia disparar una resincronizacion adicional.
                delete(other_ws_id)
                check(
                    "delete() de un workspace SIN hotkey NO dispara resincronizacion (optimizacion)",
                    len(fake_hotkey_manager.sync_calls) == 2,  # sigue en 2, no subio a 3
                )

            # ------------------ lecturas (passthrough) ------------------
            ws_id_2 = database.create_workspace(name="ParaLecturas")
            database.replace_workspace_apps(
                ws_id_2, [{"launch_order": 0, "wm_class": "X", "launch_command": "true"}]
            )
            check("get_workspace() hace passthrough correcto", get_workspace(ws_id_2)["name"] == "ParaLecturas")
            check(
                "list_workspaces() incluye el workspace recien creado",
                any(w["id"] == ws_id_2 for w in list_workspaces()),
            )
            check(
                "get_workspace_apps() hace passthrough correcto",
                len(get_workspace_apps(ws_id_2)) == 1,
            )

            # ------------------ LuxionBusyError: save()/load()/delete() respetan el lock ------------------
            with lockfile.acquire():
                for label, fn in [
                    ("save()", lambda: save(desktop_name="TESTDESK")),
                    ("load()", lambda: load(ws_id_2)),
                    ("delete()", lambda: delete(ws_id_2)),
                ]:
                    try:
                        fn()
                        check(f"{label} propaga LuxionBusyError si el lock ya esta tomado", False)
                    except lockfile.LuxionBusyError:
                        check(f"{label} propaga LuxionBusyError si el lock ya esta tomado", True)

    print(f"\n{passed} pruebas OK, {failed_count} pruebas fallidas.")
    if failed_count:
        raise SystemExit(1)
