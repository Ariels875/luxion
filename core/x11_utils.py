"""
core/x11_utils.py
==================

Wrappers sobre herramientas de linea de comandos que operan a nivel de
X11/proceso del sistema operativo: xdotool y ps. Todo lo que este modulo
sabe hacer es indiferente a bspwm en si mismo — no ejecuta ningun 'bspc'.
Lo que sabe hacer sobre bspwm especificamente (listar nodos de un
desktop, consultar el estado floating/tiled de un nodo, suscribirse a
eventos) vive en core/bspc_client.py.

Division de responsabilidades para geometria de ventanas (importante,
ver la nota tecnica dentro de set_geometry() mas abajo): 'bspc node -v'
(mover) y 'bspc node -z' (redimensionar) son movimientos RELATIVOS
(deltas desde la posicion actual) — bspc no expone ningun comando para
fijar una geometria absoluta de un nodo. Por eso la geometria absoluta
(necesaria para recrear una ventana flotante exactamente donde estaba
guardada, ver seccion 4.6 y 4.9 del plan) se resuelve aqui, con
'xdotool windowmove'/'windowsize', y no en bspc_client.py.

Filosofia de manejo de errores en este archivo (deliberada, no un
descuido):

  - Las funciones de LECTURA (get_wm_class, get_pid, get_launch_command,
    get_geometry) devuelven None cuando el comando subyacente falla por
    causas esperables en este dominio: la ventana se cerro justo entre
    el momento en que se listo y el momento en que se le pregunto algo
    (una condicion de carrera normal, no un error de programacion).
    NUNCA lanzan una excepcion por esto.

  - Si el binario en si (xdotool o ps) no esta instalado en el sistema
    (FileNotFoundError), eso SI se considera un problema de entorno
    genuino — no una carrera — y se relanza como X11UtilsError con un
    mensaje claro, para que no quede enmascarado como "la ventana no
    existe" cuando en realidad falta una dependencia del sistema (ver
    tambien installer/dependency_checker.py, seccion 4.1 del plan).
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Optional

XDOTOOL = "xdotool"
PS = "ps"

# Timeout generoso para comandos que en condiciones normales responden
# en milisegundos. Si tardan mas que esto, algo esta genuinamente mal
# (sistema colgado, X server no responde) y preferimos fallar de forma
# visible en vez de bloquear indefinidamente al llamador.
DEFAULT_TIMEOUT = 5.0


class X11UtilsError(Exception):
    """
    Se lanza SOLO ante problemas de entorno genuinos (binario faltante,
    timeout anormal) — nunca por el caso esperado de "la ventana ya no
    existe", que se representa devolviendo None.
    """


@dataclass
class _Result:
    returncode: int
    stdout: str
    stderr: str


def _run(*args: str, timeout: float = DEFAULT_TIMEOUT, env: Optional[dict] = None) -> _Result:
    """
    Ejecuta un comando externo y devuelve su resultado crudo, SIN
    interpretar el returncode (cada funcion publica de este archivo
    decide que significa un fallo en su contexto especifico).

    Lanza X11UtilsError unicamente si:
      - El binario no existe en el PATH (FileNotFoundError) — problema
        de entorno, no de "ventana ausente".
      - El comando no respondio dentro de `timeout` segundos — indica
        que algo esta genuinamente colgado, no una carrera normal.
    """
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise X11UtilsError(
            f"El comando '{args[0]}' no se encontro en el PATH. "
            "Verifica que este instalado (ver installer/dependency_checker.py)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise X11UtilsError(
            f"El comando '{' '.join(args)}' no respondio en {timeout}s."
        ) from exc

    return _Result(proc.returncode, proc.stdout, proc.stderr)


def _format_xid(xid: int) -> str:
    """
    xdotool acepta tanto IDs decimales como hexadecimales para
    seleccionar ventanas; se usa decimal aqui porque es lo que
    'xdotool getwindow*' devuelve de forma nativa en sus propias
    salidas, evitando conversiones de ida y vuelta innecesarias.
    """
    return str(xid)


# ---------------------------------------------------------------------------
# Lectura de informacion de una ventana
# ---------------------------------------------------------------------------


def get_wm_class(xid: int) -> Optional[str]:
    """
    Devuelve el WM_CLASS de una ventana (ej. "Navigator", que xdotool
    normalmente reporta como el "instance name"; ver nota mas abajo
    sobre className vs instanceName) usando:

        xdotool getwindowclassname <xid>

    Devuelve None si la ventana ya no existe (comportamiento esperado
    en una condicion de carrera, no un error).

    NOTA sobre WM_CLASS con dos partes: la propiedad X11 WM_CLASS en
    realidad tiene dos componentes (instance_name, class_name), por
    ejemplo ("Navigator", "firefox") o ("code", "Code"). El comando
    'xdotool getwindowclassname' devuelve unicamente el segundo
    componente (class_name). Si mas adelante 'reconciler.py' necesita
    diferenciar por ambos componentes (por ejemplo dos apps distintas
    que comparten class_name pero no instance_name), se puede obtener
    el WM_CLASS completo con 'xdotool getwindowclassname --shell', o
    cayendo a 'xprop -id <xid> WM_CLASS'. Se deja esta funcion enfocada
    en el caso simple (un solo string) porque es lo que necesita
    workspace_apps.wm_class tal como esta definido en el esquema
    (columna TEXT simple, ver seccion 3 del plan).
    """
    result = _run(XDOTOOL, "getwindowclassname", _format_xid(xid))
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def get_pid(xid: int) -> Optional[int]:
    """
    Devuelve el PID del proceso dueño de la ventana usando:

        xdotool getwindowpid <xid>

    Devuelve None si la ventana ya no existe, o si xdotool no pudo
    determinar el PID (algunas ventanas, raramente, no anuncian
    _NET_WM_PID).
    """
    result = _run(XDOTOOL, "getwindowpid", _format_xid(xid))
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    return int(raw) if raw.isdigit() else None


def get_launch_command(xid: int) -> Optional[str]:
    """
    Devuelve el comando real con el que se lanzo el proceso dueño de la
    ventana, combinando:

        PID=$(xdotool getwindowpid <xid>)
        ps -p $PID -o args=

    Esta es la fuente de workspace_apps.launch_command (ver seccion 4.6
    y la columna correspondiente en la seccion 3 del plan): es lo que
    permite relanzar la app exactamente como estaba (con sus argumentos
    originales), no solo el nombre del binario.

    Devuelve None si no se pudo obtener el PID, o si el proceso ya no
    existe para cuando se consulta a `ps` (misma logica de "condicion
    de carrera esperada, no error" que el resto de este archivo).

    Nota sobre truncamiento de `ps -o args=`: quisimos evitar cualquier
    sorpresa donde `ps` decida truncar la salida a un ancho de terminal
    "adivinado" en algunas configuraciones. Se fuerza explicitamente
    COLUMNS a un valor grande en el entorno del subproceso como medida
    de seguridad barata, sin cambiar el comando en si (que es
    exactamente el que se decidio usar).
    """
    pid = get_pid(xid)
    if pid is None:
        return None

    env = os.environ.copy()
    env["COLUMNS"] = "10000"  # evita truncamiento de `ps -o args=`

    result = _run(PS, "-p", str(pid), "-o", "args=", env=env)
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def get_geometry(xid: int) -> Optional[dict]:
    """
    Devuelve la geometria REAL actual de una ventana en pantalla (no la
    geometria "recordada" por bspwm para cuando esta tileada — para eso
    ver core.bspc_client.get_floating_rectangle, que lee el estado
    interno que bspwm mantiene) usando:

        xdotool getwindowgeometry --shell <xid>

    Que imprime lineas tipo:
        WINDOW=12345
        X=100
        Y=50
        WIDTH=800
        HEIGHT=600
        SCREEN=0

    Devuelve un dict {"x": int, "y": int, "w": int, "h": int}, o None si
    la ventana ya no existe o si la salida no trae las 4 claves
    necesarias (se prefiere None a un dict incompleto: un dato a medias
    es tan inutil como no tener el dato).
    """
    result = _run(XDOTOOL, "getwindowgeometry", "--shell", _format_xid(xid))
    if result.returncode != 0:
        return None

    raw_fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        raw_fields[key.strip().upper()] = value.strip()

    try:
        return {
            "x": int(raw_fields["X"]),
            "y": int(raw_fields["Y"]),
            "w": int(raw_fields["WIDTH"]),
            "h": int(raw_fields["HEIGHT"]),
        }
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Escritura: geometria absoluta y terminacion de procesos
# ---------------------------------------------------------------------------


def set_geometry(xid: int, x: int, y: int, w: int, h: int) -> bool:
    """
    Fija la posicion y el tamano ABSOLUTOS de una ventana:

        xdotool windowmove <xid> <x> <y>
        xdotool windowsize <xid> <w> <h>

    Se hace a nivel X11 (no via 'bspc node') porque, como se explica en
    el docstring del modulo, bspc no ofrece ningun comando de geometria
    absoluta — solo movimientos/resize RELATIVOS ('-v'/'-z', deltas).

    IMPORTANTE: esta funcion asume que el nodo YA esta en estado
    'floating' (ver core.bspc_client.set_state). Llamarla sobre una
    ventana tileada no tiene el efecto esperado: bspwm seguira
    imponiendo la geometria calculada por el arbol de tiling en el
    siguiente repintado, ignorando este cambio. El orden correcto,
    responsabilidad de quien orqueste esto (core/reconciler.py, a
    construir despues) es:
        1. bspc_client.set_state(xid, "floating")
        2. x11_utils.set_geometry(xid, x, y, w, h)

    Devuelve True solo si AMBOS comandos (move y resize) terminaron con
    exito. Si la ventana desaparecio a mitad de camino (se cerro justo
    despues del move pero antes del resize), devuelve False — es la
    misma condicion de carrera esperada que en el resto del archivo, no
    se lanza excepcion por esto.
    """
    move_result = _run(XDOTOOL, "windowmove", _format_xid(xid), str(x), str(y))
    if move_result.returncode != 0:
        return False

    resize_result = _run(XDOTOOL, "windowsize", _format_xid(xid), str(w), str(h))
    return resize_result.returncode == 0


def force_kill_by_xid(xid: int) -> bool:
    """
    Termina INMEDIATAMENTE (SIGKILL) el proceso dueño de una ventana.

    Decision de diseno explicita (ver seccion 4.10 del plan): NO se usa
    cierre respetuoso via protocolo ICCCM (WM_DELETE_WINDOW / 'bspc
    node -c'), que podria disparar un dialogo de confirmacion
    ("hay cambios sin guardar...") y dejar a Luxion esperando
    indefinidamente una interaccion del usuario. Se prioriza velocidad y
    determinismo sobre la posibilidad de perder cambios no guardados en
    esa ventana especifica.

    Devuelve:
      True  si se envio SIGKILL exitosamente, O si el proceso ya no
            existia (ProcessLookupError) — en ambos casos, el resultado
            neto deseado ("que ese proceso no siga corriendo") ya se
            cumple, asi que no tiene sentido tratar "ya estaba muerto"
            como un fallo.
      False si no se pudo determinar el PID de la ventana, o si el
            proceso pertenece a otro usuario y no tenemos permiso para
            matarlo (PermissionError) — este es el unico caso que
            realmente representa "no se pudo hacer lo que se pidio".
    """
    pid = get_pid(xid)
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        # El proceso ya no existia para cuando llegamos a matarlo.
        # El resultado deseado (que no siga corriendo) ya es cierto.
        return True
    except PermissionError:
        return False


def process_is_alive(pid: int) -> bool:
    """
    Comprueba si un PID corresponde a un proceso actualmente vivo,
    usando la señal 0 (que no mata nada, solo verifica permisos/
    existencia — es el truco estandar de Unix para esto).

    Se incluye en este modulo (en vez de en core/lockfile.py, que se
    construira mas adelante) porque es una utilidad generica de
    "verificar processo por PID" que no tiene nada de especifico al
    lock file — lockfile.py la importara para su logica de deteccion de
    locks obsoletos (ver seccion 4.16 del plan: si Luxion crasheo sin
    liberar el lock, no debe quedar bloqueado para siempre).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # El proceso existe pero pertenece a otro usuario: no podemos
        # enviarle señales, pero eso significa que SI existe.
        return True


# ---------------------------------------------------------------------------
# Self-test (con mocks, sin depender de xdotool/ps reales)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.x11_utils
    #
    # Este entorno de desarrollo no tiene xdotool ni una sesion X11 real
    # disponible, asi que este self-test usa unittest.mock para
    # simular las respuestas de subprocess.run() y verificar que la
    # LOGICA de cada funcion (parseo, manejo de errores, composicion de
    # get_launch_command a partir de dos comandos) es correcta. No
    # verifica que xdotool/ps en si mismos se comporten como se asume
    # aqui — eso solo se puede confirmar en una maquina Kali real con
    # bspwm corriendo, ejecutando manualmente los comandos documentados
    # en cada docstring.

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

    # --- get_wm_class: caso exitoso ---
    with patch("subprocess.run", return_value=fake_completed(0, "Navigator.firefox\n")):
        check("get_wm_class devuelve el class name", get_wm_class(12345) == "Navigator.firefox")

    # --- get_wm_class: ventana ya no existe ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "no such window")):
        check("get_wm_class devuelve None si la ventana no existe", get_wm_class(12345) is None)

    # --- get_pid: caso exitoso ---
    with patch("subprocess.run", return_value=fake_completed(0, "6789\n")):
        check("get_pid devuelve el PID como int", get_pid(12345) == 6789)

    # --- get_pid: salida no numerica ---
    with patch("subprocess.run", return_value=fake_completed(0, "\n")):
        check("get_pid devuelve None si la salida esta vacia", get_pid(12345) is None)

    # --- get_launch_command: compone getwindowpid + ps correctamente ---
    def launch_command_side_effect(args, **kwargs):
        if args[0] == XDOTOOL and args[1] == "getwindowpid":
            return fake_completed(0, "6789\n")
        if args[0] == PS:
            return fake_completed(0, "firefox --new-window https://example.com\n")
        raise AssertionError(f"Comando inesperado en el mock: {args}")

    with patch("subprocess.run", side_effect=launch_command_side_effect):
        cmd = get_launch_command(12345)
        check(
            "get_launch_command combina PID + ps -o args=",
            cmd == "firefox --new-window https://example.com",
        )

    # --- get_launch_command: la ventana ya no existe (falla el primer paso) ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "no such window")):
        check(
            "get_launch_command devuelve None si no hay PID",
            get_launch_command(12345) is None,
        )

    # --- get_geometry: parseo del formato --shell ---
    shell_output = "WINDOW=12345\nX=100\nY=50\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n"
    with patch("subprocess.run", return_value=fake_completed(0, shell_output)):
        geom = get_geometry(12345)
        check(
            "get_geometry parsea X/Y/WIDTH/HEIGHT correctamente",
            geom == {"x": 100, "y": 50, "w": 800, "h": 600},
        )

    # --- get_geometry: salida incompleta (sin HEIGHT) -> None, no un dict a medias ---
    incomplete_output = "WINDOW=12345\nX=100\nY=50\nWIDTH=800\n"
    with patch("subprocess.run", return_value=fake_completed(0, incomplete_output)):
        check(
            "get_geometry devuelve None ante salida incompleta",
            get_geometry(12345) is None,
        )

    # --- set_geometry: ambos comandos exitosos ---
    with patch("subprocess.run", return_value=fake_completed(0)):
        check("set_geometry devuelve True si move y resize funcionan", set_geometry(12345, 0, 0, 800, 600) is True)

    # --- set_geometry: falla el resize ---
    def set_geometry_side_effect(args, **kwargs):
        if "windowmove" in args:
            return fake_completed(0)
        if "windowsize" in args:
            return fake_completed(1, "", "window gone")
        raise AssertionError(f"Comando inesperado: {args}")

    with patch("subprocess.run", side_effect=set_geometry_side_effect):
        check(
            "set_geometry devuelve False si falla el resize a mitad de camino",
            set_geometry(12345, 0, 0, 800, 600) is False,
        )

    # --- force_kill_by_xid: caso exitoso ---
    with patch("subprocess.run", return_value=fake_completed(0, "6789\n")):
        with patch("os.kill") as mock_kill:
            result = force_kill_by_xid(12345)
            check("force_kill_by_xid llama os.kill con SIGKILL", mock_kill.call_args == ((6789, signal.SIGKILL),))
            check("force_kill_by_xid devuelve True en el caso exitoso", result is True)

    # --- force_kill_by_xid: el proceso ya no existia ---
    with patch("subprocess.run", return_value=fake_completed(0, "6789\n")):
        with patch("os.kill", side_effect=ProcessLookupError):
            check(
                "force_kill_by_xid devuelve True si el proceso ya estaba muerto",
                force_kill_by_xid(12345) is True,
            )

    # --- force_kill_by_xid: no se pudo obtener el PID ---
    with patch("subprocess.run", return_value=fake_completed(1, "", "no such window")):
        check(
            "force_kill_by_xid devuelve False si no hay PID que matar",
            force_kill_by_xid(12345) is False,
        )

    # --- process_is_alive: contra el propio proceso actual (garantizado vivo) ---
    check("process_is_alive detecta el proceso propio como vivo", process_is_alive(os.getpid()) is True)

    # --- process_is_alive: PID que casi seguro no existe ---
    with patch("os.kill", side_effect=ProcessLookupError):
        check("process_is_alive detecta un PID inexistente", process_is_alive(999999999) is False)

    # --- X11UtilsError: binario faltante ---
    with patch("subprocess.run", side_effect=FileNotFoundError):
        try:
            get_wm_class(12345)
            check("X11UtilsError se lanza si el binario no existe", False)
        except X11UtilsError:
            check("X11UtilsError se lanza si el binario no existe", True)

    print(f"\n{passed} pruebas OK, {failed} pruebas fallidas.")
    if failed:
        raise SystemExit(1)
