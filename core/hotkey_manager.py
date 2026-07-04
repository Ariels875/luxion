"""
core/hotkey_manager.py
=======================

Sincroniza los atajos de teclado asignados a los workspaces (columna
workspaces.hotkey, ver seccion 3 del plan) con el archivo real que usa
sxhkd (~/.config/sxhkd/sxhkdrc), y reinicia el proceso sxhkd para que el
cambio tenga efecto de inmediato — ver seccion 4.8 del plan
arquitectonico.

Llamado exclusivamente desde core.workspace_service.set_hotkey() y
core.workspace_service.delete() (esta ultima solo si el workspace
eliminado tenia un atajo asignado). No deberia hacer falta llamar a
sync_sxhkdrc_and_restart() manualmente desde ningun otro lugar del
sistema, pero es una funcion publica e idempotente por si en algun
momento hiciera falta forzar una resincronizacion completa desde cero.

-------------------------------------------------------------------------
Bloque gestionado, delimitado por marcadores
-------------------------------------------------------------------------
Luxion NUNCA reescribe el archivo sxhkdrc completo ni toca nada fuera de
un bloque delimitado explicitamente por dos lineas marcador
(START_MARK/END_MARK). Todo lo que el usuario haya configurado a mano en
cualquier otra parte del archivo (los atajos de bspwm que genero
installer/setup_bspwm_kali.sh, o cualquier atajo personalizado que el
usuario haya agregado por su cuenta) permanece exactamente igual,
sin importar cuantas veces se llame a esta funcion.

-------------------------------------------------------------------------
Como se referencia luxion_cli.py dentro de sxhkdrc (decision de diseño,
no un detalle trivial)
-------------------------------------------------------------------------
El proyecto TODAVIA no tiene definido ningun mecanismo de
instalacion/empaquetado (no existe un "pip install" con un
entry_point que registre un comando "luxion-cli" en el PATH del
sistema — eso podria construirse mas adelante, pero no existe hoy). Por
eso, en vez de asumir que "luxion-cli" es un comando disponible, cada
linea de atajo generada invoca explicitamente:

    <sys.executable> <ruta_absoluta_a_luxion_cli.py> load --id <N>

calculando la ruta a luxion_cli.py de forma dinamica, relativa a la
ubicacion de ESTE archivo (core/hotkey_manager.py vive en
<raiz_del_proyecto>/core/, asi que <raiz_del_proyecto>/luxion_cli.py esta
un nivel arriba). Esto funciona sin importar como el usuario haya
obtenido el proyecto (git clone, copiar la carpeta a mano, etc.),
siempre que la posicion relativa entre este archivo y luxion_cli.py se
mantenga. Si en el futuro se construye un instalador que registre un
comando "luxion-cli" real en el PATH, esta funcion es el UNICO lugar que
habria que simplificar.

sxhkd invoca cada linea de atajo a traves de una shell (tipicamente
"/bin/sh -c '<comando>'"), asi que tanto el interprete de Python como la
ruta al script se escapan con shlex.quote() antes de insertarlos en el
archivo — proteccion barata contra el caso (posible aunque poco comun)
de que el proyecto este clonado dentro de una carpeta cuyo nombre
contenga espacios.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from typing import Optional

from . import config
from . import database

START_MARK = "# === LUXION_WORKSPACES_START (no editar a mano, gestionado por Luxion) ==="
END_MARK = "# === LUXION_WORKSPACES_END ==="

# Ver la nota tecnica del docstring del modulo sobre por que se calcula
# asi en vez de asumir un comando "luxion-cli" en el PATH.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUXION_CLI_PATH = os.path.join(_PROJECT_ROOT, "luxion_cli.py")


class HotkeyManagerError(Exception):
    """
    Se lanza ante problemas genuinos que impiden sincronizar sxhkdrc de
    forma segura, o reiniciar sxhkd:

      - sxhkdrc esta en un estado inconsistente (solo uno de los dos
        marcadores presente, o en el orden invertido) que Luxion no
        puede reparar automaticamente sin arriesgarse a borrar
        contenido del usuario que no le pertenece.
      - Falta alguno de los binarios necesarios ('sxhkd' o 'pkill') en
        el sistema.
    """


# ---------------------------------------------------------------------------
# Construccion del bloque gestionado
# ---------------------------------------------------------------------------


def _build_hotkey_command(workspace_id: int) -> str:
    """
    Arma la linea de comando que sxhkd ejecutara al presionar el atajo
    de un workspace especifico. Ver la nota tecnica del docstring del
    modulo sobre la ruta de invocacion y el escapado con shlex.quote().
    """
    python_bin = shlex.quote(sys.executable)
    cli_path = shlex.quote(LUXION_CLI_PATH)
    return f"{python_bin} {cli_path} load --id {workspace_id}"


def _build_managed_block() -> str:
    """
    Construye el bloque completo (con sus marcadores de inicio/fin
    incluidos) a partir del estado ACTUAL de la base de datos: un par de
    lineas [combinacion de teclas, comando] por cada workspace que tenga
    un hotkey asignado en este momento (ver
    core.database.get_workspaces_with_hotkey(), ya ordenado por nombre
    de workspace para que el bloque generado sea estable y facil de leer
    si el usuario lo llegara a inspeccionar manualmente).

    Si NINGUN workspace tiene un hotkey asignado, se genera un bloque
    "vacio" (los dos marcadores, sin ninguna linea de atajo entre
    medio) — se mantiene siempre el bloque presente, en vez de
    eliminarlo del archivo cuando queda vacio, para no tener que volver
    a decidir DONDE reinsertarlo la proxima vez que se agregue un
    hotkey.
    """
    lines = [START_MARK]
    for workspace in database.get_workspaces_with_hotkey():
        lines.append(workspace["hotkey"])
        lines.append(f"    {_build_hotkey_command(workspace['id'])}")
    lines.append(END_MARK)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lectura/escritura de sxhkdrc, con reemplazo seguro del bloque
# ---------------------------------------------------------------------------


def _read_sxhkdrc() -> str:
    """
    Lee el contenido actual de sxhkdrc. Si el archivo todavia no existe
    (por ejemplo, primera vez que Luxion corre en una instalacion
    recien hecha, antes de que setup_bspwm_kali.sh haya generado el
    archivo base), se trata como si estuviera vacio en vez de fallar —
    _replace_managed_block() maneja correctamente el caso de contenido
    original vacio.
    """
    try:
        with open(config.SXHKDRC_PATH, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _write_sxhkdrc(content: str) -> None:
    """
    Escribe el contenido nuevo de sxhkdrc, creando el directorio
    contenedor (~/.config/sxhkd/) si todavia no existe.
    """
    os.makedirs(os.path.dirname(config.SXHKDRC_PATH), exist_ok=True)
    with open(config.SXHKDRC_PATH, "w") as f:
        f.write(content)


def _replace_managed_block(original_content: str, new_block: str) -> str:
    """
    Devuelve una copia de `original_content` con el bloque delimitado
    por START_MARK/END_MARK reemplazado por `new_block` — o con
    `new_block` agregado al final, si los marcadores todavia no existen
    en el archivo.

    Casos manejados:

      1. NINGUN marcador presente (primera vez que Luxion toca este
         archivo, o el usuario nunca tuvo hotkeys asignados): se agrega
         `new_block` al FINAL del archivo, con una linea en blanco de
         separacion si el archivo ya tenia contenido previo (para no
         pegarlo inmediatamente despues de la ultima linea existente sin
         ningun espacio).

      2. AMBOS marcadores presentes, START antes que END (el caso
         normal en una resincronizacion): se reemplaza todo el texto
         entre ellos (inclusive) por `new_block`, preservando intacto
         todo lo que hay ANTES de START_MARK y DESPUES de END_MARK.

      3. Solo UNO de los dos marcadores presente, o START_MARK aparece
         DESPUES de END_MARK: el archivo esta en un estado inconsistente
         (probablemente editado a mano de forma accidental). Se lanza
         HotkeyManagerError en vez de intentar adivinar una reparacion
         automatica — "adivinar" aqui podria borrar contenido del
         usuario que no tiene nada que ver con Luxion.
    """
    start_idx = original_content.find(START_MARK)
    end_idx = original_content.find(END_MARK)

    if start_idx == -1 and end_idx == -1:
        if original_content and not original_content.endswith("\n"):
            original_content += "\n"
        separator = "\n" if original_content.strip() else ""
        return original_content + separator + new_block + "\n"

    if start_idx == -1 or end_idx == -1:
        raise HotkeyManagerError(
            f"{config.SXHKDRC_PATH} tiene solo uno de los dos marcadores "
            f"de Luxion presente (se esperaban ambos: {START_MARK!r} y "
            f"{END_MARK!r}). El archivo parece haber sido editado a mano "
            "de forma inconsistente; no se modificara automaticamente "
            "para evitar borrar contenido por error. Revisa el archivo "
            "manualmente."
        )

    if start_idx > end_idx:
        raise HotkeyManagerError(
            f"{config.SXHKDRC_PATH} tiene el marcador de fin de Luxion "
            "ANTES que el de inicio — el archivo parece corrupto. No se "
            "modificara automaticamente."
        )

    before = original_content[:start_idx]
    after = original_content[end_idx + len(END_MARK) :]
    return before + new_block + after


# ---------------------------------------------------------------------------
# Reinicio de sxhkd
# ---------------------------------------------------------------------------


def _restart_sxhkd() -> None:
    """
    Mata el proceso 'sxhkd' actual y lo vuelve a lanzar desde cero.

    DECISION DE DISEÑO EXPLICITA DEL USUARIO (no una eleccion de estilo
    de este modulo): se pidio especificamente "terminar el proceso e
    iniciarlo nuevamente", NO la señal de recarga en caliente
    (pkill -USR1 -x sxhkd) que el propio sxhkdrc generado por
    installer/setup_bspwm_kali.sh ya usa para su atajo de "recargar
    configuracion". Un reinicio completo interrumpe TODOS los atajos por
    una fraccion de segundo mientras el proceso viejo termina y el nuevo
    arranca, a cambio de una garantia mas fuerte de que el proceso nuevo
    arranca leyendo el archivo desde cero, sin ningun estado interno
    residual del proceso anterior.

    Se usa 'pkill -x sxhkd' (coincidencia EXACTA del nombre de proceso —
    la misma bandera -x que se uso consistentemente en todo el proyecto,
    ver installer/setup_bspwm_kali.sh) para no matar por error algun
    otro proceso cuyo nombre solo CONTENGA "sxhkd" como subcadena.

    El proceso nuevo se lanza con start_new_session=True (equivalente a
    setsid). Esto es IMPORTANTE, no un detalle cosmetico: sin esto,
    sxhkd quedaria como proceso HIJO DIRECTO de quien llamo a esta
    funcion (la GUI de Luxion, o luxion_cli.py disparado por el propio
    atajo que se esta reconfigurando). Si ese proceso padre termina (el
    usuario cierra la ventana de la GUI, o luxion_cli.py simplemente
    termina su ejecucion normal despues de procesar el atajo), sxhkd NO
    debe morir junto con el — tiene que seguir escuchando atajos de
    forma independiente y permanente, como cualquier daemon de sesion
    normal.

    Lanza HotkeyManagerError si falta 'pkill' o 'sxhkd' en el sistema
    (FileNotFoundError al intentar ejecutarlos) — un problema de entorno
    genuino, no algo que deba fallar en silencio dejando al usuario sin
    saber por que sus atajos dejaron de funcionar.
    """
    try:
        subprocess.run(
            ["pkill", "-x", "sxhkd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError as exc:
        raise HotkeyManagerError(
            "No se pudo ejecutar 'pkill' (parte del paquete procps). "
            "¿Esta instalado?"
        ) from exc
    # No se verifica el returncode de pkill: 0 significa que encontro y
    # mato un proceso, 1 significa que no habia ninguno corriendo (por
    # ejemplo, la primera vez que se asigna un hotkey en una sesion
    # donde sxhkd todavia no se habia lanzado) — ambos son resultados
    # normales para nuestro proposito, ninguno es un error.

    # Pequeña pausa para darle tiempo al proceso viejo a liberar
    # cualquier recurso (el grab de teclado a nivel X11, principalmente)
    # antes de que el nuevo intente tomarlo. Mismo patron defensivo que
    # ya se uso en installer/setup_bspwm_kali.sh al reiniciar xfce4-panel.
    time.sleep(0.3)

    try:
        subprocess.Popen(
            ["sxhkd"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HotkeyManagerError(
            "No se pudo reiniciar sxhkd: el comando 'sxhkd' no se "
            "encontro en el PATH. ¿Esta instalado? (ver "
            "installer/setup_bspwm_kali.sh)"
        ) from exc


# ---------------------------------------------------------------------------
# Punto de entrada publico
# ---------------------------------------------------------------------------


def sync_sxhkdrc_and_restart() -> None:
    """
    Sincroniza el bloque gestionado por Luxion dentro de sxhkdrc con el
    estado ACTUAL de la base de datos, y reinicia sxhkd para que el
    cambio tenga efecto de inmediato (ver seccion 4.8 del plan).

    Orden de operaciones (importante): se construye el bloque nuevo y se
    calcula el contenido final ANTES de escribir nada a disco. Si
    _replace_managed_block() lanza HotkeyManagerError (archivo
    inconsistente), el archivo real en disco queda SIN TOCAR — no se
    llega ni siquiera a intentar la escritura, y por lo tanto tampoco se
    reinicia sxhkd (no tendria sentido reiniciarlo si el archivo que
    tiene que leer no se pudo actualizar de forma segura).
    """
    new_block = _build_managed_block()
    original_content = _read_sxhkdrc()
    updated_content = _replace_managed_block(original_content, new_block)
    _write_sxhkdrc(updated_content)
    _restart_sxhkd()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.hotkey_manager
    #
    # Este modulo toca dos recursos externos reales que no se pueden (ni
    # se deben) ejercitar de verdad en un self-test: el archivo real
    # ~/.config/sxhkd/sxhkdrc del usuario, y el proceso real 'sxhkd'
    # (que ademas no esta instalado en este entorno de desarrollo sin
    # X11). Por eso:
    #   - config.SXHKDRC_PATH se redirige a un archivo temporal (mismo
    #     patron que config.DB_PATH en los self-tests anteriores).
    #   - _restart_sxhkd() se reemplaza por un doble de prueba que solo
    #     registra que fue llamada, usando el patron f"{__name__}.X"
    #     (necesario porque _restart_sxhkd esta definida en ESTE mismo
    #     archivo — parchear "core.hotkey_manager._restart_sxhkd" NO
    #     tendria efecto al correr como -m, por la misma razon
    #     documentada y corregida en el self-test de core/lockfile.py).
    #
    # La construccion del BLOQUE en si (_build_managed_block) SI se
    # prueba contra una base de datos SQLite real en un archivo
    # temporal, para verificar de verdad el formato generado a partir
    # de datos reales de core.database.get_workspaces_with_hotkey().

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

    # --- _build_hotkey_command: contenido basico ---
    cmd = _build_hotkey_command(7)
    check("_build_hotkey_command incluye 'load --id 7'", cmd.endswith("load --id 7"))
    check("_build_hotkey_command referencia luxion_cli.py", "luxion_cli.py" in cmd)

    # --- _build_hotkey_command: escapado real ante rutas con espacios ---
    # (sys.executable normalmente NO tiene espacios en una instalacion
    # tipica, asi que se fuerza un valor con espacios para ejercitar de
    # verdad la logica de shlex.quote(), en vez de confiar en que el
    # valor real del sistema "probablemente" nunca los tenga).
    with mock.patch("sys.executable", "/fake path/con espacios/python3"), \
         mock.patch(f"{__name__}.LUXION_CLI_PATH", "/otra ruta/con espacios/luxion_cli.py"):
        quoted_cmd = _build_hotkey_command(3)
        check(
            "_build_hotkey_command escapa correctamente rutas con espacios",
            shlex.split(quoted_cmd) == [
                "/fake path/con espacios/python3",
                "/otra ruta/con espacios/luxion_cli.py",
                "load",
                "--id",
                "3",
            ],
        )

    # --- _replace_managed_block: archivo vacio (primera vez) ---
    result_empty = _replace_managed_block("", "BLOQUE_NUEVO")
    check(
        "_replace_managed_block en archivo vacio agrega el bloque sin separador extra",
        result_empty == "BLOQUE_NUEVO\n",
    )

    # --- _replace_managed_block: archivo con contenido previo SIN marcadores ---
    contenido_previo = "alt + Return\n    xterm\n"
    result_append = _replace_managed_block(contenido_previo, "BLOQUE_NUEVO")
    check(
        "_replace_managed_block preserva contenido previo y agrega el bloque al final",
        result_append == "alt + Return\n    xterm\n\nBLOQUE_NUEVO\n",
    )

    # --- _replace_managed_block: ya existen ambos marcadores (resincronizacion) ---
    contenido_con_bloque = (
        "alt + Return\n    xterm\n\n"
        f"{START_MARK}\n"
        "super + 1\n    comando_viejo\n"
        f"{END_MARK}\n\n"
        "alt + shift + q\n    bspc node -c\n"
    )
    result_replace = _replace_managed_block(contenido_con_bloque, f"{START_MARK}\nsuper + 2\n    comando_nuevo\n{END_MARK}")
    check(
        "_replace_managed_block preserva el contenido ANTES del bloque viejo",
        result_replace.startswith("alt + Return\n    xterm\n\n"),
    )
    check(
        "_replace_managed_block preserva el contenido DESPUES del bloque viejo",
        result_replace.endswith("alt + shift + q\n    bspc node -c\n"),
    )
    check(
        "_replace_managed_block reemplaza el contenido viejo del bloque por el nuevo",
        "comando_viejo" not in result_replace and "comando_nuevo" in result_replace,
    )

    # --- _replace_managed_block: solo un marcador presente -> HotkeyManagerError ---
    contenido_roto = f"{START_MARK}\nsuper + 1\n    algo\n"  # falta END_MARK
    try:
        _replace_managed_block(contenido_roto, "nuevo")
        check("HotkeyManagerError si falta uno de los dos marcadores", False)
    except HotkeyManagerError:
        check("HotkeyManagerError si falta uno de los dos marcadores", True)

    # --- _replace_managed_block: marcadores en orden invertido -> HotkeyManagerError ---
    contenido_invertido = f"{END_MARK}\nalgo\n{START_MARK}\n"
    try:
        _replace_managed_block(contenido_invertido, "nuevo")
        check("HotkeyManagerError si los marcadores estan en orden invertido", False)
    except HotkeyManagerError:
        check("HotkeyManagerError si los marcadores estan en orden invertido", True)

    # --- Pruebas de integracion con base de datos y archivo reales (temporales) ---
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "test_luxion.db")
        tmp_sxhkdrc_path = os.path.join(tmp_dir, "sxhkdrc")

        with mock.patch("core.config.DB_PATH", new=tmp_db_path), \
             mock.patch("core.config.SXHKDRC_PATH", new=tmp_sxhkdrc_path):
            database.init_db()

            ws_sin_hotkey = database.create_workspace(name="SinAtajo")
            ws_con_hotkey_1 = database.create_workspace(name="Desarrollo", hotkey="super + 1")
            ws_con_hotkey_2 = database.create_workspace(name="Comunicacion", hotkey="super + 2")

            block = _build_managed_block()
            check("_build_managed_block incluye ambos hotkeys reales de la base de datos", "super + 1" in block and "super + 2" in block)
            check(
                "_build_managed_block NO incluye el workspace sin hotkey",
                "SinAtajo" not in block,  # el nombre ni siquiera se usa en el bloque, pero confirma que no aparece nada extra
            )
            check(
                "_build_managed_block genera el comando correcto para cada workspace",
                f"load --id {ws_con_hotkey_1}" in block and f"load --id {ws_con_hotkey_2}" in block,
            )

            # --- sync_sxhkdrc_and_restart(): primera sincronizacion, archivo no existe todavia ---
            restart_calls = []
            with mock.patch(f"{__name__}._restart_sxhkd", side_effect=lambda: restart_calls.append(True)):
                sync_sxhkdrc_and_restart()

            check("sync_sxhkdrc_and_restart() crea el archivo sxhkdrc si no existia", os.path.exists(tmp_sxhkdrc_path))
            with open(tmp_sxhkdrc_path) as f:
                content_after_first_sync = f.read()
            check(
                "el archivo generado contiene ambos hotkeys",
                "super + 1" in content_after_first_sync and "super + 2" in content_after_first_sync,
            )
            check("sync_sxhkdrc_and_restart() llamo a _restart_sxhkd() exactamente una vez", len(restart_calls) == 1)

            # --- Se agrega contenido NO relacionado con Luxion a mano, simulando atajos preexistentes ---
            contenido_manual = "alt + Return\n    xterm\n\n" + content_after_first_sync
            with open(tmp_sxhkdrc_path, "w") as f:
                f.write(contenido_manual)

            # --- Se quita un hotkey y se agrega otro workspace, y se vuelve a sincronizar ---
            database.update_workspace_hotkey(ws_con_hotkey_1, None)
            ws_con_hotkey_3 = database.create_workspace(name="Ocio", hotkey="super + 3")

            restart_calls.clear()
            with mock.patch(f"{__name__}._restart_sxhkd", side_effect=lambda: restart_calls.append(True)):
                sync_sxhkdrc_and_restart()

            with open(tmp_sxhkdrc_path) as f:
                content_after_second_sync = f.read()

            check(
                "una segunda sincronizacion preserva el contenido manual NO relacionado con Luxion",
                "alt + Return" in content_after_second_sync and "xterm" in content_after_second_sync,
            )
            check(
                "el hotkey quitado (super + 1) ya no aparece tras la segunda sincronizacion",
                "super + 1" not in content_after_second_sync,
            )
            check(
                "el hotkey que seguia activo (super + 2) sigue presente",
                "super + 2" in content_after_second_sync,
            )
            check(
                "el hotkey nuevo (super + 3) aparece tras la segunda sincronizacion",
                "super + 3" in content_after_second_sync,
            )
            check("la segunda sincronizacion tambien reinicio sxhkd", len(restart_calls) == 1)

            # --- Si el archivo queda corrupto (un marcador borrado a mano), sync_sxhkdrc_and_restart() NO debe escribir nada ni reiniciar sxhkd ---
            contenido_corrupto = content_after_second_sync.replace(END_MARK, "")
            with open(tmp_sxhkdrc_path, "w") as f:
                f.write(contenido_corrupto)

            restart_calls.clear()
            with mock.patch(f"{__name__}._restart_sxhkd", side_effect=lambda: restart_calls.append(True)):
                try:
                    sync_sxhkdrc_and_restart()
                    check("HotkeyManagerError se propaga si sxhkdrc esta corrupto", False)
                except HotkeyManagerError:
                    check("HotkeyManagerError se propaga si sxhkdrc esta corrupto", True)

            check("_restart_sxhkd() NUNCA se llamo tras el error de archivo corrupto", len(restart_calls) == 0)
            with open(tmp_sxhkdrc_path) as f:
                content_after_failed_sync = f.read()
            check(
                "el archivo en disco quedo SIN TOCAR tras el intento fallido",
                content_after_failed_sync == contenido_corrupto,
            )

    # --- _restart_sxhkd: manejo de binarios faltantes (fuera del bloque de DB temporal, no lo necesita) ---
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        try:
            _restart_sxhkd()
            check("HotkeyManagerError si falta 'pkill'", False)
        except HotkeyManagerError:
            check("HotkeyManagerError si falta 'pkill'", True)

    with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1)), \
         mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
        try:
            _restart_sxhkd()
            check("HotkeyManagerError si falta 'sxhkd'", False)
        except HotkeyManagerError:
            check("HotkeyManagerError si falta 'sxhkd'", True)

    print(f"\n{passed} pruebas OK, {failed_count} pruebas fallidas.")
    if failed_count:
        raise SystemExit(1)
