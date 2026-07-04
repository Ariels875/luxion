#!/usr/bin/env python3
"""
luxion_cli.py
=============

Punto de entrada de linea de comandos de Luxion. Es el UNICO archivo,
junto con luxion_gui.py (que no existe todavia), que vive en la raiz del
proyecto en vez de dentro de core/ o ui/.

REGLA DE ARQUITECTURA (ver seccion 4.17 del plan y core/__init__.py):
este archivo NUNCA debe importar Gtk, Vte, ni nada de ui/. Es lo que
permite que sxhkd invoque un atajo de teclado y obtenga respuesta rapida
— cada pulsacion de un atajo lanza un proceso de Python nuevo desde
cero (ver core/hotkey_manager.py, que genera exactamente el comando
"<python> <ruta a este archivo> load --id N" dentro de sxhkdrc), y ese
proceso no puede pagar el costo de inicializar un toolkit grafico
completo solo para ejecutar "cargar un workspace".

Mientras ui/ no exista todavia, este CLI es ademas la UNICA forma de
interactuar con Luxion — por eso expone bastante mas que el subcomando
minimo 'load' que estrictamente necesita sxhkd: 'save', 'list', 'show',
'delete', 'rename', 'set-hotkey' y 'set-close-unmatched' cubren
practicamente todo el catalogo de features del plan (secciones 4.6 a
4.15), permitiendo probar el sistema completo en una maquina Kali real
sin esperar a que exista la interfaz grafica.

Ejemplos de uso:

    python3 luxion_cli.py save --name "Desarrollo"
    python3 luxion_cli.py list
    python3 luxion_cli.py show --id 1
    python3 luxion_cli.py load --id 1
    python3 luxion_cli.py rename --id 1 --name "Dev Web"
    python3 luxion_cli.py set-hotkey --id 1 --hotkey "super + 1"
    python3 luxion_cli.py set-hotkey --id 1 --clear
    python3 luxion_cli.py set-close-unmatched --id 1 --value false
    python3 luxion_cli.py delete --id 1 --yes

Cada invocacion queda registrada en
~/.local/share/luxion/luxion_cli.log (con rotacion automatica, ver
_setup_logging()) — importante porque, invocado desde un atajo de
teclado via sxhkd, este proceso no tiene ninguna terminal visible: si
algo falla silenciosamente, el log es el UNICO lugar donde queda
constancia de que paso.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional

from core import bspc_client
from core import config
from core import database
from core import hotkey_manager
from core import launcher
from core import lockfile
from core import reconciler
from core import workspace_service
from core import x11_utils


class CliUsageError(Exception):
    """
    Errores de validacion propios de este CLI que argparse no puede
    detectar por si solo (por ejemplo, --id apuntando a un workspace que
    no existe, o un 'delete' sin el flag --yes de confirmacion). Se
    tratan distinto a los errores de dominio de mas abajo (BspcError,
    LauncherError, etc.): representan un problema con el COMANDO que se
    escribio, no con la operacion en si — por eso usan el codigo de
    salida 2, el mismo que argparse usa por convencion para errores de
    uso, en vez del 1 que usan los demas.
    """


# ---------------------------------------------------------------------------
# Logging (ver el docstring del modulo sobre por que esto es importante
# para un proceso invocado silenciosamente desde un atajo de teclado)
# ---------------------------------------------------------------------------


def _setup_logging() -> logging.Logger:
    """
    Configura el logger de este CLI, con rotacion automatica del
    archivo (maximo ~1MB, hasta 2 archivos de respaldo) para que no
    crezca sin limite a lo largo de meses de uso — este log se escribe
    en CADA invocacion, y un atajo de teclado usado varias veces al dia
    durante meses podria acumular un archivo grande si no se rotara.

    Se calcula la ruta del log a partir de config.DATA_DIR EN EL MOMENTO
    de la llamada (no como una constante de modulo calculada una sola
    vez al importar) para que sea redirigible en pruebas simplemente
    mockeando config.DATA_DIR antes de invocar main() — ver el self-test
    al final de este archivo.

    Se limpian los handlers existentes del logger antes de agregar uno
    nuevo: en el uso real, cada invocacion de este CLI es un proceso de
    Python completamente nuevo (por eso el logger siempre arranca sin
    handlers de todas formas), pero en el self-test main() se llama
    muchas veces DENTRO DEL MISMO PROCESO — sin esta limpieza, cada
    llamada acumularia un handler adicional apuntando posiblemente a una
    ruta de log distinta (si el test cambio el mock de config.DATA_DIR
    entre llamadas), duplicando entradas de log o escribiendo en rutas
    obsoletas.
    """
    config.ensure_data_dir()
    log_path = os.path.join(config.DATA_DIR, "luxion_cli.log")

    logger = logging.getLogger("luxion_cli")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

    return logger


# ---------------------------------------------------------------------------
# Definicion de subcomandos (argparse)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="luxion-cli",
        description="Interfaz de linea de comandos de Luxion — gestor de workspaces para bspwm.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_load = subparsers.add_parser("load", help="Carga un workspace en el desktop actualmente enfocado.")
    p_load.add_argument("--id", type=int, required=True, dest="workspace_id")

    p_save = subparsers.add_parser("save", help="Guarda el estado actual del desktop en un workspace.")
    p_save.add_argument(
        "--id",
        type=int,
        default=None,
        dest="workspace_id",
        help="Sobreescribe este workspace existente en vez de crear uno nuevo.",
    )
    p_save.add_argument("--name", type=str, default=None, help="Nombre para el workspace nuevo (se ignora si se dio --id).")
    p_save.add_argument(
        "--desktop",
        type=str,
        default=None,
        dest="desktop_name",
        help="Desktop de bspwm a capturar (por defecto, el actualmente enfocado).",
    )

    subparsers.add_parser("list", help="Lista todos los workspaces guardados.")

    p_show = subparsers.add_parser("show", help="Muestra el detalle de un workspace (sus apps guardadas).")
    p_show.add_argument("--id", type=int, required=True, dest="workspace_id")

    p_delete = subparsers.add_parser("delete", help="Elimina un workspace (accion irreversible).")
    p_delete.add_argument("--id", type=int, required=True, dest="workspace_id")
    p_delete.add_argument("--yes", action="store_true", help="Confirma la eliminacion (requerido).")

    p_rename = subparsers.add_parser("rename", help="Cambia el nombre de un workspace.")
    p_rename.add_argument("--id", type=int, required=True, dest="workspace_id")
    p_rename.add_argument("--name", type=str, required=True)

    p_hotkey = subparsers.add_parser("set-hotkey", help="Asigna o quita el atajo de teclado de un workspace.")
    p_hotkey.add_argument("--id", type=int, required=True, dest="workspace_id")
    hotkey_group = p_hotkey.add_mutually_exclusive_group(required=True)
    hotkey_group.add_argument("--hotkey", type=str, help="Combinacion de teclas, formato sxhkd (ej. 'super + 1').")
    hotkey_group.add_argument("--clear", action="store_true", help="Quita el atajo asignado actualmente.")

    p_close = subparsers.add_parser(
        "set-close-unmatched",
        help="Configura si se cierran las ventanas no coincidentes al cargar este workspace.",
    )
    p_close.add_argument("--id", type=int, required=True, dest="workspace_id")
    p_close.add_argument(
        "--value",
        choices=["true", "false", "default"],
        required=True,
        help="'true'/'false' fuerzan el comportamiento; 'default' vuelve a usar el ajuste global.",
    )

    return parser


# ---------------------------------------------------------------------------
# Implementacion de cada subcomando
#
# Cada funcion recibe el namespace de argparse ya parseado y devuelve un
# string de resumen (o None si no hay nada particular que imprimir) —
# _run_command() se encarga de imprimirlo y registrarlo en el log de
# forma uniforme, para no repetir ese detalle en cada funcion.
# ---------------------------------------------------------------------------


def _cmd_load(args: argparse.Namespace) -> str:
    result = workspace_service.load(args.workspace_id)
    return (
        f"Workspace {args.workspace_id} cargado en desktop '{result.desktop_name}': "
        f"{len(result.reused)} reutilizada(s), {len(result.launched)} lanzada(s), "
        f"{len(result.failed)} fallida(s), {len(result.killed_xids)} cerrada(s)."
    )


def _cmd_save(args: argparse.Namespace) -> str:
    workspace_id = workspace_service.save(
        workspace_id=args.workspace_id, name=args.name, desktop_name=args.desktop_name
    )
    workspace = workspace_service.get_workspace(workspace_id)
    apps = workspace_service.get_workspace_apps(workspace_id)
    return f"Workspace guardado: id={workspace_id} name={workspace['name']!r} ({len(apps)} app(s))."


def _cmd_list(args: argparse.Namespace) -> str:
    workspaces = workspace_service.list_workspaces()
    if not workspaces:
        return "No hay ningun workspace guardado todavia."

    lines = ["Workspaces guardados:"]
    for ws in workspaces:
        apps_count = len(workspace_service.get_workspace_apps(ws["id"]))
        hotkey_display = ws["hotkey"] if ws["hotkey"] else "(sin atajo)"
        lines.append(f"  [{ws['id']}] {ws['name']}  -  {hotkey_display}  -  {apps_count} app(s)")
    return "\n".join(lines)


def _describe_close_setting(value: Optional[int]) -> str:
    if value is None:
        return "usa el ajuste global"
    return "SI cerrar lo que no coincida" if value else "NO cerrar nada"


def _cmd_show(args: argparse.Namespace) -> str:
    workspace = workspace_service.get_workspace(args.workspace_id)
    if workspace is None:
        raise CliUsageError(f"No existe ningun workspace con id={args.workspace_id}.")

    apps = workspace_service.get_workspace_apps(args.workspace_id)
    lines = [
        f"Workspace [{workspace['id']}] {workspace['name']!r}",
        f"  Atajo: {workspace['hotkey'] or '(sin atajo)'}",
        f"  Cierre de no-coincidentes: {_describe_close_setting(workspace['close_unmatched_windows'])}",
        f"  Apps ({len(apps)}):",
    ]
    for app in apps:
        floating_desc = ""
        if app["is_floating"]:
            floating_desc = f" [floating: {app['geom_x']},{app['geom_y']} {app['geom_w']}x{app['geom_h']}]"
        lines.append(
            f"    #{app['launch_order']} {app['wm_class']} (instancia {app['instance_index']}){floating_desc}"
        )
        lines.append(f"        cmd: {app['launch_command']}")
    return "\n".join(lines)


def _cmd_delete(args: argparse.Namespace) -> str:
    if not args.yes:
        raise CliUsageError(
            f"Para eliminar el workspace {args.workspace_id} hace falta confirmar con --yes "
            "(esta accion no se puede deshacer)."
        )
    workspace = workspace_service.get_workspace(args.workspace_id)
    if workspace is None:
        raise CliUsageError(f"No existe ningun workspace con id={args.workspace_id}.")

    name = workspace["name"]
    workspace_service.delete(args.workspace_id)
    return f"Workspace [{args.workspace_id}] {name!r} eliminado."


def _cmd_rename(args: argparse.Namespace) -> str:
    final_name = workspace_service.rename(args.workspace_id, args.name)
    return f"Workspace {args.workspace_id} renombrado a {final_name!r}."


def _cmd_set_hotkey(args: argparse.Namespace) -> str:
    hotkey = None if args.clear else args.hotkey
    workspace_service.set_hotkey(args.workspace_id, hotkey)
    if hotkey:
        return f"Atajo {hotkey!r} asignado al workspace {args.workspace_id}."
    return f"Atajo removido del workspace {args.workspace_id}."


def _cmd_set_close_unmatched(args: argparse.Namespace) -> str:
    value_map = {"true": True, "false": False, "default": None}
    value = value_map[args.value]
    workspace_service.set_close_unmatched_windows(args.workspace_id, value)
    return f"Ajuste de cierre del workspace {args.workspace_id} actualizado a: {args.value}."


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], Optional[str]]] = {
    "load": _cmd_load,
    "save": _cmd_save,
    "list": _cmd_list,
    "show": _cmd_show,
    "delete": _cmd_delete,
    "rename": _cmd_rename,
    "set-hotkey": _cmd_set_hotkey,
    "set-close-unmatched": _cmd_set_close_unmatched,
}


# ---------------------------------------------------------------------------
# Ejecucion + manejo de errores uniforme
# ---------------------------------------------------------------------------


def _run_command(logger: logging.Logger, description: str, func: Callable[[], Optional[str]]) -> int:
    """
    Ejecuta `func` (sin argumentos — tipicamente un lambda que envuelve
    la llamada real al handler del subcomando), atrapando las
    excepciones esperables del dominio y traduciendolas a un mensaje
    claro por stderr + una entrada en el log, en vez de dejar pasar un
    traceback crudo de Python — que, invocado via sxhkd sin ninguna
    terminal visible, el usuario ni siquiera llegaria a ver en ningun
    lado.

    Codigos de salida:
      0  exito.
      2  error de USO del comando (CliUsageError: argumentos
         incompletos en un sentido que argparse no puede validar por si
         solo, como un --id que no existe) — mismo codigo que usa
         argparse por convencion para sus propios errores de parseo.
      1  cualquier otro error de dominio (LuxionBusyError, BspcError,
         LauncherError, etc.) o un error verdaderamente inesperado.
    """
    logger.info("Iniciando: %s", description)
    try:
        summary = func()
        if summary:
            print(summary)
            logger.info("OK: %s -> %s", description, summary)
        else:
            logger.info("OK: %s", description)
        return 0

    except CliUsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.warning("%s (uso invalido: %s)", description, exc)
        return 2

    except lockfile.LuxionBusyError as exc:
        print(f"Luxion esta ocupado en este momento: {exc}", file=sys.stderr)
        logger.warning("%s (ocupado: %s)", description, exc)
        return 1

    except (
        lockfile.LuxionLockError,
        reconciler.ReconcilerError,
        bspc_client.BspcError,
        launcher.LauncherError,
        x11_utils.X11UtilsError,
        hotkey_manager.HotkeyManagerError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("%s (%s: %s)", description, type(exc).__name__, exc)
        return 1

    except sqlite3.IntegrityError as exc:
        print(f"Error de datos (posible nombre o atajo duplicado): {exc}", file=sys.stderr)
        logger.error("%s (IntegrityError: %s)", description, exc)
        return 1

    except Exception as exc:  # ultimo recurso deliberado: nunca dejar pasar un traceback crudo al usuario
        print(f"Error inesperado: {exc}", file=sys.stderr)
        logger.exception("%s (fallo inesperado)", description)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    """
    Punto de entrada programatico (`argv=None` usa sys.argv[1:], igual
    que argparse por defecto — pasar una lista explicita es lo que
    permite al self-test de este archivo invocar comandos completos sin
    tener que lanzar un subproceso de verdad en cada caso).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Idempotente y barato (unos pocos "CREATE TABLE IF NOT EXISTS"): se
    # ejecuta en CADA invocacion para autorepararse si, por algun
    # motivo, la base de datos no existiera todavia o le faltara alguna
    # tabla — en vez de que el primer uso de un atajo de teclado en una
    # instalacion nueva falle con un error de SQL confuso.
    database.init_db()

    logger = _setup_logging()

    handler = _COMMAND_HANDLERS[args.command]
    description = f"{args.command} {vars(args)}"
    return _run_command(logger, description, lambda: handler(args))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _run_self_test() -> None:
    # Se invoca con: python3 luxion_cli.py   (SIN argumentos — ver la
    # logica al final de este archivo que distingue "cero argumentos" de
    # una invocacion normal con subcomando).
    #
    # A diferencia de los self-tests de core/*.py (que se ejecutan con
    # 'python3 -m core.X'), este archivo NO es parte del paquete core/
    # — vive en la raiz del proyecto, junto a core/ y (mas adelante) ui/
    # — asi que su self-test se organiza como una funcion normal
    # invocada desde el bloque `if __name__ == "__main__":` de mas
    # abajo, no via el mecanismo `-m`.
    #
    # La mayoria de las pruebas llaman a main([...]) directamente en el
    # mismo proceso (rapido, permite inspeccionar el codigo de retorno,
    # la salida impresa via redirect_stdout, y el estado de la base de
    # datos despues). Al final se agrega UNA prueba end-to-end real,
    # lanzando este mismo archivo como un subproceso autentico -tal como
    # lo haria sxhkd-, para confirmar que el cableado completo
    # (shebang, imports absolutos "from core import ...", el bloque
    # __main__) funciona de verdad desde fuera, no solo llamando a
    # main() desde adentro.

    import contextlib
    import io
    import os
    import subprocess
    import tempfile
    import unittest.mock as mock

    passed = 0
    failed_count = 0

    def check(label: str, condition: bool):
        nonlocal passed, failed_count
        if condition:
            print(f"OK: {label}")
            passed += 1
        else:
            print(f"FALLO: {label}")
            failed_count += 1

    def run_main(args_list):
        """Corre main() capturando stdout, devuelve (codigo, texto_impreso)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(args_list)
        return code, buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "test_luxion.db")
        tmp_sxhkdrc_path = os.path.join(tmp_dir, "sxhkdrc")
        tmp_data_dir = tmp_dir  # para redirigir tambien el log del CLI

        with mock.patch("core.config.DB_PATH", new=tmp_db_path), \
             mock.patch("core.config.SXHKDRC_PATH", new=tmp_sxhkdrc_path), \
             mock.patch("core.config.DATA_DIR", new=tmp_data_dir), \
             mock.patch("core.config.LOCK_PATH", new=os.path.join(tmp_dir, "luxion.lock")), \
             mock.patch("core.hotkey_manager._restart_sxhkd"):

            # --- build_parser(): estructura basica ---
            parser = build_parser()
            parsed = parser.parse_args(["load", "--id", "5"])
            check("build_parser interpreta 'load --id 5' correctamente", parsed.command == "load" and parsed.workspace_id == 5)

            try:
                parser.parse_args([])
                check("argparse exige un subcomando", False)
            except SystemExit:
                check("argparse exige un subcomando", True)

            # --- save (crear nuevo) ---
            fake_wm_class = {0x1001: "Firefox"}
            fake_launch_command = {0x1001: "true"}
            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch("core.bspc_client.list_window_ids", return_value=[0x1001]), \
                 mock.patch("core.x11_utils.get_wm_class", side_effect=lambda xid: fake_wm_class.get(xid)), \
                 mock.patch("core.x11_utils.get_launch_command", side_effect=lambda xid: fake_launch_command.get(xid)), \
                 mock.patch("core.bspc_client.is_floating", return_value=False):
                code, output = run_main(["save", "--name", "TestWS"])

            check("save crea un workspace nuevo con exito (exit 0)", code == 0)
            check("save imprime un resumen con el nombre", "TestWS" in output)

            workspaces = database.list_workspaces()
            check("save efectivamente persistio el workspace en la base de datos", any(w["name"] == "TestWS" for w in workspaces))
            ws_id = next(w["id"] for w in workspaces if w["name"] == "TestWS")
            check("save guardo la app capturada", len(database.get_workspace_apps(ws_id)) == 1)

            # --- save (sobreescribir existente) ---
            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch("core.bspc_client.list_window_ids", return_value=[]):
                code, output = run_main(["save", "--id", str(ws_id)])
            check("save sobre un id existente devuelve exit 0", code == 0)
            check("save sobre un id existente reemplaza las apps (ahora 0)", len(database.get_workspace_apps(ws_id)) == 0)

            # --- list ---
            code, output = run_main(["list"])
            check("list devuelve exit 0", code == 0)
            check("list incluye el workspace guardado", "TestWS" in output)

            # --- show ---
            code, output = run_main(["show", "--id", str(ws_id)])
            check("show devuelve exit 0", code == 0)
            check("show incluye el nombre del workspace", "TestWS" in output)

            code, output = run_main(["show", "--id", "999999"])
            check("show de un id inexistente devuelve exit 2 (CliUsageError)", code == 2)

            # --- rename ---
            code, output = run_main(["rename", "--id", str(ws_id), "--name", "Renombrado"])
            check("rename devuelve exit 0", code == 0)
            check("rename persiste el nuevo nombre", database.get_workspace(ws_id)["name"] == "Renombrado")

            # --- set-hotkey (asignar) ---
            code, output = run_main(["set-hotkey", "--id", str(ws_id), "--hotkey", "super + 9"])
            check("set-hotkey (asignar) devuelve exit 0", code == 0)
            check("set-hotkey (asignar) persiste el atajo", database.get_workspace(ws_id)["hotkey"] == "super + 9")

            # --- set-hotkey (quitar) ---
            code, output = run_main(["set-hotkey", "--id", str(ws_id), "--clear"])
            check("set-hotkey --clear devuelve exit 0", code == 0)
            check("set-hotkey --clear deja el atajo en NULL", database.get_workspace(ws_id)["hotkey"] is None)

            # --- set-close-unmatched (los 3 valores posibles) ---
            for value_str, expected in [("true", 1), ("false", 0), ("default", None)]:
                code, output = run_main(["set-close-unmatched", "--id", str(ws_id), "--value", value_str])
                check(
                    f"set-close-unmatched --value {value_str} devuelve exit 0 y persiste {expected!r}",
                    code == 0 and database.get_workspace(ws_id)["close_unmatched_windows"] == expected,
                )

            # --- load ---
            def fake_reconcile(workspace_id, desktop_name):
                return reconciler.ReconcileResult(
                    workspace_id=workspace_id,
                    desktop_name=desktop_name,
                    reused=[],
                    launched=[],
                    failed=[],
                    killed_xids=[],
                )

            with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"), \
                 mock.patch("core.reconciler.reconcile_and_load", side_effect=fake_reconcile):
                code, output = run_main(["load", "--id", str(ws_id)])
            check("load devuelve exit 0", code == 0)
            check("load imprime un resumen con el nombre del desktop", "TESTDESK" in output)

            # --- load: LuxionBusyError -> exit 1 ---
            with lockfile.acquire():
                with mock.patch("core.bspc_client.get_focused_desktop_name", return_value="TESTDESK"):
                    code, output = run_main(["load", "--id", str(ws_id)])
            check("load devuelve exit 1 si Luxion esta ocupado", code == 1)

            # --- delete sin --yes ---
            code, output = run_main(["delete", "--id", str(ws_id)])
            check("delete sin --yes devuelve exit 2 (CliUsageError)", code == 2)
            check("delete sin --yes NO elimino el workspace", database.get_workspace(ws_id) is not None)

            # --- delete de un id inexistente ---
            code, output = run_main(["delete", "--id", "999999", "--yes"])
            check("delete de un id inexistente devuelve exit 2", code == 2)

            # --- delete con --yes ---
            code, output = run_main(["delete", "--id", str(ws_id), "--yes"])
            check("delete con --yes devuelve exit 0", code == 0)
            check("delete con --yes efectivamente elimina el workspace", database.get_workspace(ws_id) is None)

            # --- El log se escribio de verdad, en la ruta redirigida ---
            log_path = os.path.join(tmp_data_dir, "luxion_cli.log")
            check("el archivo de log se creo en la ruta esperada", os.path.exists(log_path))
            with open(log_path) as f:
                log_content = f.read()
            check("el log contiene entradas de las operaciones realizadas", "delete" in log_content and "save" in log_content)

    # --- Prueba end-to-end real: invocar este archivo como subproceso genuino ---
    # (fuera del bloque 'with tempfile...' anterior: usa su propio
    # directorio temporal independiente, para no interferir con las
    # aserciones de mas arriba)
    with tempfile.TemporaryDirectory() as tmp_dir_2:
        env = os.environ.copy()
        env["HOME"] = tmp_dir_2  # config.py resuelve DATA_DIR relativo a ~/.local/share/luxion
        script_path = os.path.abspath(__file__)

        # 'list' en una base de datos que no existe todavia: init_db()
        # dentro de main() debe crearla sola (autoreparacion, ver el
        # docstring de main()), sin que haga falta ningun paso previo.
        result = subprocess.run(
            [sys.executable, script_path, "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        check(
            "invocacion real como subproceso (sin BD previa) funciona de punta a punta",
            result.returncode == 0 and "No hay ningun workspace" in result.stdout,
        )

    print(f"\n{passed} pruebas OK, {failed_count} pruebas fallidas.")
    if failed_count:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Invocado sin ningun argumento: se interpreta como "correr el
        # self-test" en vez de dejar que argparse falle con un mensaje
        # de "subcomando requerido" — ver _run_self_test() para el
        # detalle de por que se organizo asi en vez de usar el patron
        # 'python3 -m core.X' del resto del proyecto (este archivo no es
        # parte del paquete core/).
        _run_self_test()
    else:
        sys.exit(main())
