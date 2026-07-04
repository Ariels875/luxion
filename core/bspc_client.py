"""
core/bspc_client.py
====================

Unico modulo que sabe ejecutar 'bspc' en todo el proyecto. El resto del
codigo (reconciler.py, launcher.py, window_watcher.py) SIEMPRE debe pasar
por aqui en vez de invocar subprocess contra 'bspc' por su cuenta
igual que database.py es el unico lugar que habla SQL directamente.

Este modulo NO sabe nada sobre X11 en si mismo mas alla de lo que bspc
expone (IDs de nodo/desktop/monitor, estado del arbol). Todo lo que
requiere hablar con xdotool/ps (WM_CLASS, PID, geometria absoluta,
matar procesos) vive en core/x11_utils.py.

-------------------------------------------------------------------------
Division de responsabilidades para geometria de ventanas flotantes
-------------------------------------------------------------------------
'bspc node -v' (mover) y 'bspc node -z' (redimensionar) son movimientos
RELATIVOS (deltas desde la posicion actual) — se confirmo revisando la
documentacion oficial de bspc antes de escribir este archivo. bspc NO
ofrece ningun comando para fijar una geometria absoluta de un nodo. Por
eso este modulo solo expone set_state() (fijar el ESTADO del nodo:
floating/tiled/etc.), y la geometria absoluta (posicion X,Y y tamaño
W,H exactos) se resuelve en core.x11_utils.set_geometry(), via
'xdotool windowmove'/'windowsize'. El orden correcto de uso, cuando se
construya core/reconciler.py, es:

    bspc_client.set_state(xid, "floating")
    x11_utils.set_geometry(xid, x, y, w, h)

-------------------------------------------------------------------------
Filosofia de manejo de errores (igual de deliberada que en x11_utils.py)
-------------------------------------------------------------------------
  - get_node_info() devuelve None si el nodo ya no existe (condicion de
    carrera esperada: la ventana se cerro entre el listado y esta
    consulta). NO es un error real del sistema.

  - get_focused_desktop_name() y list_window_ids() SI lanzan BspcError
    ante una respuesta invalida/vacia, porque en su caso de uso normal
    (preguntar "cual es el desktop enfocado ahora mismo") no hay ninguna
    razon legitima para que fallen salvo que bspwm no este corriendo 
    y eso es un problema de entorno real que no se debe enmascarar.

  - set_state() lanza BspcError si el comando falla. Un llamador que
    opere sobre una ventana recien creada (donde SI podria haber una
    carrera legitima) es responsable de envolver la llamada en un
    try/except BspcError si quiere tolerarlo se prefiere que el caso
    por defecto sea ruidoso antes que silencioso, ya que un 'bspc node
    -t floating' que falla silenciosamente dejaria una ventana flotante
    guardada como floating pero corriendo tileada, un bug dificil de
    notar despues.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

BSPC = "bspc"
DEFAULT_TIMEOUT = 5.0


class BspcError(Exception):
    """
    Se lanza ante problemas genuinos: bspc no esta instalado/corriendo,
    un comando devolvio un error que no corresponde a una condicion de
    carrera esperada, o una respuesta JSON invalida.
    """


@dataclass
class _Result:
    returncode: int
    stdout: str
    stderr: str


def _run(*args: str, timeout: float = DEFAULT_TIMEOUT) -> _Result:
    """
    Ejecuta 'bspc <args...>' y devuelve el resultado crudo, sin
    interpretar el returncode — cada funcion publica decide que
    significa un fallo en su propio contexto (ver la filosofia de
    manejo de errores en el docstring del modulo).
    """
    try:
        proc = subprocess.run(
            [BSPC, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BspcError(
            "El comando 'bspc' no se encontro en el PATH. "
            "¿Esta bspwm instalado y corriendo?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BspcError(
            f"'bspc {' '.join(args)}' no respondio en {timeout}s."
        ) from exc

    return _Result(proc.returncode, proc.stdout, proc.stderr)


def _format_node_selector(xid: int) -> str:
    """
    Los selectores de nodo de bspc aceptan el ID en formato hexadecimal
    con prefijo '0x' (es la notacion que el propio bspc usa en TODAS sus
    salidas, tanto en 'query -N' como en los eventos de 'subscribe'), asi
    que se normaliza a ese formato aqui para consistencia interna, sin
    importar en que formato haya llegado `xid` como int.
    """
    return hex(xid)


# ---------------------------------------------------------------------------
# Desktops y listado de nodos
# ---------------------------------------------------------------------------


def get_focused_desktop_name() -> str:
    """
    Devuelve el NOMBRE (no el ID) del desktop de bspwm actualmente
    enfocado, usando:

        bspc query -D -d focused --names

    Esta es la pieza clave que hace que "cargar un workspace" opere
    sobre "el escritorio en el que se invoco la funcion cargar" (ver
    seccion 4.9 del plan) en vez de sobre un desktop fijo: se consulta
    en el momento exacto de la invocacion, tanto si viene de la GUI como
    de un atajo de teclado via luxion_cli.py.

    Lanza BspcError si bspc no responde o devuelve una salida vacia —
    en el uso normal de Luxion esto solo pasaria si bspwm no esta
    corriendo, que es un problema de entorno real, no una carrera.
    """
    result = _run("query", "-D", "-d", "focused", "--names")
    if result.returncode != 0:
        raise BspcError(
            f"No se pudo obtener el desktop enfocado: {result.stderr.strip()}"
        )
    name = result.stdout.strip()
    if not name:
        raise BspcError("bspc devolvio un nombre de desktop vacio.")
    return name


def list_window_ids(desktop_name: str) -> list[int]:
    """
    Devuelve los IDs de todos los nodos (ventanas) de un desktop, EN EL
    ORDEN DEL ARBOL de bspwm, usando:

        bspc query -N -d <desktop_name>

    Este orden es la base de workspace_apps.launch_order (ver seccion
    4.5 del plan: es el mismo orden que se guarda y en el que se
    relanzan las apps al cargar).

    Un desktop sin ninguna ventana abierta devuelve una lista vacia
    (esto NO es un error: 'bspc query -N -d <desktop>' termina con
    returncode 0 y salida vacia cuando el desktop simplemente no tiene
    nodos).

    Lanza BspcError si el desktop_name no existe en absoluto (selector
    invalido) — a diferencia de "desktop vacio", que es un resultado
    valido.
    """
    result = _run("query", "-N", "-d", desktop_name)
    if result.returncode != 0:
        raise BspcError(
            f"No se pudo listar ventanas del desktop {desktop_name!r}: "
            f"{result.stderr.strip()}"
        )

    ids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        ids.append(int(line, 16))
    return ids


# ---------------------------------------------------------------------------
# Informacion de un nodo especifico (JSON tree)
# ---------------------------------------------------------------------------


def get_node_info(xid: int) -> Optional[dict]:
    """
    Devuelve la representacion JSON completa de un nodo, usando:

        bspc query -T -n <xid>

    El JSON incluye (entre otros campos) un sub-objeto "client" con:
      - className / instanceName
      - state: "tiled" | "pseudo_tiled" | "floating" | "fullscreen"
      - hidden: bool
      - floatingRectangle: {"x":.., "y":.., "width":.., "height":..}
      - tiledRectangle: {"x":.., "y":.., "width":.., "height":..}

    Devuelve None si el nodo ya no existe (condicion de carrera
    esperada — la ventana se cerro entre el listado con
    list_window_ids() y esta consulta puntual).

    Lanza BspcError si bspc devolvio algo que no es JSON valido (esto SI
    seria un problema real: o bien una version de bspwm con un formato
    de salida distinto al esperado, o una corrupcion de la respuesta).

    ADVERTENCIA HONESTA: el esquema de campos de arriba esta basado en
    el formato de salida documentado/observado de bspc en versiones
    recientes, pero este modulo se escribio sin acceso a una sesion de
    bspwm real en el entorno de desarrollo (sin X11 disponible). Antes
    de depender de esto en produccion, se recomienda correr una vez a
    mano, en la maquina Kali real:

        bspc query -T -n <algun_xid_valido> | python3 -m json.tool

    y confirmar que los nombres de campo (especialmente
    "floatingRectangle" y "state") coinciden exactamente. Si difieren,
    el unico lugar que hay que ajustar es esta funcion y las que la usan
    mas abajo (get_client_state, is_floating, get_floating_rectangle) —
    el resto del sistema nunca parsea este JSON directamente.
    """
    result = _run("query", "-T", "-n", _format_node_selector(xid))
    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BspcError(
            f"Respuesta JSON invalida de bspc para el nodo {xid:#x}: {exc}"
        ) from exc


def get_client_state(xid: int) -> Optional[str]:
    """
    Devuelve el estado del nodo: "tiled", "pseudo_tiled", "floating" o
    "fullscreen". Devuelve None si el nodo ya no existe, o si por algun
    motivo el JSON no trae la seccion "client" esperada (nodos que son
    receptaculos vacios en el arbol, sin ventana asociada, no tienen
    "client" — no deberian aparecer nunca en list_window_ids(), que solo
    devuelve nodos con ventana real, pero esta funcion se protege de
    todas formas).
    """
    node = get_node_info(xid)
    if node is None:
        return None
    client = node.get("client")
    if not client:
        return None
    return client.get("state")


def is_floating(xid: int) -> Optional[bool]:
    """
    True si el nodo esta actualmente en estado floating, False si esta
    en cualquier otro estado (tiled/pseudo_tiled/fullscreen), None si el
    nodo ya no existe. Usado por core/workspace_service.py -> save() al
    decidir si guardar geometria para esa ventana (ver seccion 4.6).
    """
    state = get_client_state(xid)
    if state is None:
        return None
    return state == "floating"


def get_floating_rectangle(xid: int) -> Optional[dict]:
    """
    Devuelve la geometria floating que bspwm tiene registrada
    internamente para el nodo, como {"x":int,"y":int,"w":int,"h":int}.

    Se prefiere esta fuente (en vez de core.x11_utils.get_geometry, que
    consulta la geometria real actual en pantalla via xdotool) para
    GUARDAR la geometria de una ventana floating, porque es un solo
    query JSON que ya se esta haciendo de todas formas para saber
    is_floating() — evita una llamada extra a xdotool por cada ventana
    floating al guardar un workspace.

    Devuelve None si el nodo no existe, si no tiene seccion "client", o
    si esa seccion no trae "floatingRectangle" (no deberia pasar para
    una ventana con cliente real, pero se protege de todas formas).
    """
    node = get_node_info(xid)
    if node is None:
        return None
    client = node.get("client")
    if not client:
        return None
    rect = client.get("floatingRectangle")
    if not rect:
        return None
    try:
        return {
            "x": int(rect["x"]),
            "y": int(rect["y"]),
            "w": int(rect["width"]),
            "h": int(rect["height"]),
        }
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Modificacion de estado de un nodo
# ---------------------------------------------------------------------------


def set_state(xid: int, state: str) -> None:
    """
    Fija el ESTADO de un nodo (no su geometria — ver la nota de division
    de responsabilidades al inicio del archivo), usando:

        bspc node <xid> -t <state>

    `state` debe ser uno de: "tiled", "pseudo_tiled", "floating",
    "fullscreen" (no se valida aqui contra esa lista para no acoplar
    este wrapper generico a los valores exactos que bspc soporte en
    cada version; si se pasa un valor invalido, bspc mismo lo rechazara
    y esta funcion lo traducira a un BspcError con el mensaje real de
    bspc).

    Lanza BspcError si el comando falla. Ver la nota en el docstring del
    modulo sobre por que esta funcion es "ruidosa" por defecto (a
    diferencia de get_node_info, que es "silenciosa" ante nodos
    inexistentes): un fallo silencioso aqui podria dejar datos
    inconsistentes entre lo guardado en la base de datos y el estado
    real de la ventana.
    """
    result = _run("node", _format_node_selector(xid), "-t", state)
    if result.returncode != 0:
        raise BspcError(
            f"No se pudo fijar el estado {state!r} del nodo {xid:#x}: "
            f"{result.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Suscripcion a eventos (usado por core/window_watcher.py, a construir)
# ---------------------------------------------------------------------------


def start_subscribe(*event_types: str) -> subprocess.Popen:
    """
    Lanza 'bspc subscribe <event_types...>' como un proceso de larga
    duracion y devuelve el objeto Popen, con stdout en modo texto y
    bufferizado LINEA POR LINEA (bufsize=1), listo para que el llamador
    lea eventos con proc.stdout.readline() a medida que van ocurriendo.

    Este comando NO termina por si solo — imprime una linea por cada
    evento que ocurre, indefinidamente, hasta que el proceso se mata. Es
    responsabilidad exclusiva del llamador (core/window_watcher.py)
    terminar el proceso (proc.terminate()) cuando ya no necesite seguir
    escuchando. Esta funcion deliberadamente NO usa un timeout ni
    espera a que el proceso termine, a diferencia de _run(): seria
    contradictorio con la naturaleza de un comando de suscripcion
    continua.

    Ejemplo de uso (tal como lo hara window_watcher.py):

        proc = bspc_client.start_subscribe("node_add")
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break  # el proceso murio inesperadamente
                new_xid = bspc_client.parse_node_add_event(line)
                if new_xid is not None:
                    ...
        finally:
            proc.terminate()

    Lanza BspcError inmediatamente si el binario 'bspc' no existe (no
    tiene sentido devolver un Popen "roto" para que el llamador
    descubra el problema recien al intentar leer de el).
    """
    try:
        return subprocess.Popen(
            [BSPC, "subscribe", *event_types],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise BspcError(
            "El comando 'bspc' no se encontro en el PATH. "
            "¿Esta bspwm instalado y corriendo?"
        ) from exc


def parse_node_add_event(line: str) -> Optional[int]:
    """
    Parsea una linea tal como la imprime 'bspc subscribe node_add'.

    Formato confirmado (campos separados por espacios):

        node_add <monitor_id> <desktop_id> <parent_node_id> <new_node_id> ...

    Ejemplo real (tomado de un script de referencia de la ArchWiki de
    bspwm que usa exactamente este evento):

        node_add 0x40000002 0x40000003 0x00600001 0x00600002 0

    El ID del nodo NUEVO es el QUINTO campo (indice 4, 0-indexado). Se
    devuelve como int.

    Devuelve None si la linea esta vacia, no corresponde a un evento
    "node_add" (por ejemplo si en el futuro se ampliara la suscripcion a
    mas tipos de evento con start_subscribe("node_add", "node_remove")),
    o no tiene suficientes campos para ser valida.
    """
    parts = line.strip().split()
    if len(parts) < 5 or parts[0] != "node_add":
        return None
    try:
        return int(parts[4], 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Self-test (con mocks, sin depender de un bspwm real)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.bspc_client
    #
    # Igual que en x11_utils.py: este entorno no tiene bspwm corriendo,
    # asi que se usa unittest.mock para simular las respuestas de
    # subprocess.run()/Popen() y verificar la LOGICA de parseo y manejo
    # de errores. El formato JSON asumido para 'query -T -n' y el
    # formato de linea de 'subscribe node_add' estan basados en
    # documentacion oficial y un ejemplo real de script de la ArchWiki
    # (citado en el docstring de parse_node_add_event) — pero de todas
    # formas se recomienda la verificacion en vivo descrita en el
    # docstring de get_node_info() antes de confiar en esto en
    # produccion.

    from unittest.mock import patch, MagicMock

    passed = 0
    failed = 0

    def check(label: str, condition: bool):
        global passed, failed
        if condition:
            print(f"OK: {label}")
            passed += 1
        else:
            print(f"FALLO: {label}")
            failed += 1

    def fake_completed(returncode=0, stdout="", stderr=""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    # --- get_focused_desktop_name: caso exitoso ---
    with patch("subprocess.run", return_value=fake_completed(0, "II\n")):
        check("get_focused_desktop_name devuelve el nombre", get_focused_desktop_name() == "II")

    # --- get_focused_desktop_name: salida vacia -> BspcError ---
    with patch("subprocess.run", return_value=fake_completed(0, "\n")):
        try:
            get_focused_desktop_name()
            check("BspcError ante desktop enfocado vacio", False)
        except BspcError:
            check("BspcError ante desktop enfocado vacio", True)

    # --- list_window_ids: varias ventanas ---
    with patch("subprocess.run", return_value=fake_completed(0, "0x00600001\n0x00600002\n")):
        check(
            "list_window_ids parsea varios IDs hex",
            list_window_ids("II") == [0x00600001, 0x00600002],
        )

    # --- list_window_ids: desktop vacio (returncode 0, stdout vacio) ---
    with patch("subprocess.run", return_value=fake_completed(0, "")):
        check("list_window_ids devuelve [] para un desktop vacio", list_window_ids("II") == [])

    # --- list_window_ids: desktop invalido -> BspcError ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "no such desktop")):
        try:
            list_window_ids("NO_EXISTE")
            check("BspcError ante desktop invalido", False)
        except BspcError:
            check("BspcError ante desktop invalido", True)

    # --- get_node_info / get_client_state / is_floating / get_floating_rectangle ---
    fake_node_json = json.dumps(
        {
            "id": "0x00600002",
            "client": {
                "className": "firefox",
                "instanceName": "Navigator",
                "state": "floating",
                "hidden": False,
                "floatingRectangle": {"x": 100, "y": 50, "width": 1200, "height": 800},
                "tiledRectangle": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            },
        }
    )
    with patch("subprocess.run", return_value=fake_completed(0, fake_node_json)):
        node = get_node_info(0x00600002)
        check("get_node_info parsea el JSON", node is not None and node["id"] == "0x00600002")
        check("get_client_state extrae 'floating'", get_client_state(0x00600002) == "floating")
        check("is_floating devuelve True", is_floating(0x00600002) is True)
        rect = get_floating_rectangle(0x00600002)
        check(
            "get_floating_rectangle extrae x/y/w/h",
            rect == {"x": 100, "y": 50, "w": 1200, "h": 800},
        )

    # --- get_node_info: nodo tiled (is_floating debe dar False, no None) ---
    fake_tiled_json = json.dumps({"id": "0x00600003", "client": {"state": "tiled"}})
    with patch("subprocess.run", return_value=fake_completed(0, fake_tiled_json)):
        check("is_floating devuelve False para un nodo tiled", is_floating(0x00600003) is False)

    # --- get_node_info: nodo ya no existe -> None, no excepcion ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "no such node")):
        check("get_node_info devuelve None si el nodo no existe", get_node_info(0x00600002) is None)
        check("is_floating devuelve None si el nodo no existe", is_floating(0x00600002) is None)

    # --- get_node_info: JSON invalido -> BspcError ---
    with patch("subprocess.run", return_value=fake_completed(0, "esto no es json valido {{{")):
        try:
            get_node_info(0x00600002)
            check("BspcError ante JSON invalido", False)
        except BspcError:
            check("BspcError ante JSON invalido", True)

    # --- set_state: caso exitoso ---
    with patch("subprocess.run", return_value=fake_completed(0)):
        try:
            set_state(0x00600002, "floating")
            check("set_state no lanza excepcion en el caso exitoso", True)
        except BspcError:
            check("set_state no lanza excepcion en el caso exitoso", False)

    # --- set_state: falla -> BspcError ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "invalid state")):
        try:
            set_state(0x00600002, "estado_invalido")
            check("set_state lanza BspcError si bspc rechaza el comando", False)
        except BspcError:
            check("set_state lanza BspcError si bspc rechaza el comando", True)

    # --- parse_node_add_event: linea real de ejemplo (ArchWiki) ---
    sample_line = "node_add 0x40000002 0x40000003 0x00600001 0x00600002 0"
    check(
        "parse_node_add_event extrae el nuevo XID (5to campo)",
        parse_node_add_event(sample_line) == 0x00600002,
    )

    # --- parse_node_add_event: linea de otro tipo de evento -> None ---
    check(
        "parse_node_add_event ignora otros tipos de evento",
        parse_node_add_event("node_remove 0x40000002 0x40000003 0x00600002") is None,
    )

    # --- parse_node_add_event: linea vacia/malformada -> None ---
    check("parse_node_add_event ignora lineas vacias", parse_node_add_event("") is None)
    check("parse_node_add_event ignora lineas incompletas", parse_node_add_event("node_add 0x1 0x2") is None)

    # --- start_subscribe: binario faltante -> BspcError ---
    with patch("subprocess.Popen", side_effect=FileNotFoundError):
        try:
            start_subscribe("node_add")
            check("BspcError si 'bspc' no existe al suscribirse", False)
        except BspcError:
            check("BspcError si 'bspc' no existe al suscribirse", True)

    print(f"\n{passed} pruebas OK, {failed} pruebas fallidas.")
    if failed:
        raise SystemExit(1)
