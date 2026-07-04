"""
core/launcher.py
=================

Lanza procesos (subprocess.Popen) y los coordina con un
core.window_watcher.NodeAddWatcher YA ABIERTO por el llamador (ver
seccion 4.9 del plan: reconciler.py abrira UNA sola NodeAddWatcher para
toda la operacion de carga y la pasara aqui, app por app — este modulo
no crea ni administra su propia suscripcion).

Implementa el timeout de apertura de apps (seccion 4.13 del plan): si una
app lanzada no produce una ventana con el WM_CLASS esperado dentro del
tiempo dado, se mata el proceso que se lanzo (para no dejarlo huerfano
corriendo en segundo plano) y se informa el fallo al llamador, que decide
que hacer despues (en el diseño actual, reconciler.py simplemente
continua con la siguiente app de la lista).
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from typing import Optional, Protocol


class LauncherError(Exception):
    """
    Se lanza ante problemas para siquiera INTENTAR lanzar el comando:
    texto de comando malformado, comando vacio, o binario inexistente.
    No se lanza por un timeout (eso es un resultado normal representado
    por el valor de retorno (False, None) de launch_and_wait, no una
    excepcion — ver el docstring de esa funcion).
    """


class _WatcherProtocol(Protocol):
    """
    Protocolo minimo (duck typing estructural) que describe lo unico que
    launch_and_wait() necesita de un "watcher": un metodo
    wait_for_wm_class(expected_wm_class, timeout_seconds) -> Optional[int].

    Se declara como Protocol (en vez de importar directamente
    core.window_watcher.NodeAddWatcher) para que este modulo NO dependa
    en tiempo de importacion de window_watcher.py — cualquier objeto que
    cumpla esta forma sirve, incluyendo un doble de prueba simple en el
    self-test de este archivo. Esto tambien es lo que permite testear
    launch_and_wait() de forma aislada, sin tener que levantar una
    suscripcion real a bspc en cada prueba (eso ya se probo a fondo en
    core/window_watcher.py).
    """

    def wait_for_wm_class(self, expected_wm_class: str, timeout_seconds: float) -> Optional[int]:
        ...


def _parse_command(command: str) -> list[str]:
    """
    Convierte el string de comando guardado en workspace_apps.launch_command
    (capturado originalmente por core.x11_utils.get_launch_command via
    'ps -o args=', ver seccion 4.6 del plan) en una lista de argumentos
    lista para subprocess.Popen(), SIN pasar por un shell — mas seguro
    (sin riesgo de inyeccion de shell) y sin depender de que /bin/sh este
    disponible.

    Se aplica os.path.expanduser() a cada token como medida defensiva
    barata: en el uso normal, 'ps -o args=' ya deberia mostrar rutas con
    "~" EXPANDIDAS (porque si el comando original se escribio en una
    terminal interactiva, fue el propio shell del usuario quien expandio
    el "~" ANTES de ejecutar el programa — ps solo ve el argv[] final,
    ya expandido). Pero si en el futuro se agrega alguna forma de que el
    usuario escriba un comando manualmente con un "~" literal (por
    ejemplo, una funcion de "agregar app manualmente" en la GUI, no
    contemplada en el plan actual), esta linea evita que ese caso
    especifico rompa el lanzamiento.

    ADVERTENCIA CONOCIDA sobre la fidelidad de 'ps -o args=' (documentar
    aqui porque es el punto exacto donde el problema se manifestaria):
    ps reconstruye el string de argumentos separandolos con un unico
    espacio, SIN volver a escapar argumentos que originalmente contenian
    espacios (por ejemplo, una ruta de archivo como
    "/home/user/Mi Proyecto/main.py"). shlex.split() (usado aqui, el
    mismo criterio de separacion de un shell POSIX) NO tiene forma de
    distinguir eso de "dos argumentos separados" al volver a parsear ese
    string mas tarde. Es una limitacion real y conocida del enfoque
    'ps -o args=' que se decidio deliberadamente para
    core.x11_utils.get_launch_command; la alternativa mas precisa seria
    leer /proc/<pid>/cmdline directamente (preserva los limites exactos
    de cada argumento via separadores NUL), pero quedo fuera del alcance
    decidido para esa funcion. Si alguna vez una app con rutas o
    argumentos que contienen espacios no se relanza correctamente al
    cargar un workspace, este es el motivo mas probable.

    Lanza LauncherError si el comando esta vacio, es solo espacios en
    blanco, o tiene una sintaxis de comillas invalida (comillas sin
    cerrar, etc. — shlex.split() lanza ValueError en ese caso, que aqui
    se traduce a un error especifico del dominio de este modulo).
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise LauncherError(
            f"No se pudo interpretar el comando guardado {command!r}: {exc}"
        ) from exc

    if not argv:
        raise LauncherError("El comando de lanzamiento esta vacio.")

    return [os.path.expanduser(token) for token in argv]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """
    Mata inmediatamente (SIGKILL) TODO el grupo de procesos del comando
    lanzado, no solo el proceso hijo directo que Popen() devolvio.

    Se usa os.killpg() en vez de proc.kill() porque el proceso se lanza
    (ver launch_and_wait mas abajo) con start_new_session=True
    (equivalente a setsid): esto lo coloca en su propio grupo de
    procesos nuevo, lo que permite matar de un solo golpe tanto al
    proceso directo como a cualquier hijo/nieto que haya podido generar
    — comun en apps que usan un script "launcher" intermedio, o en
    algunos wrappers tipo Electron, donde el proceso que Popen() lanzo
    directamente termina rapido dejando vivo al proceso real de la app.

    Se usa SIGKILL DIRECTO, sin un SIGTERM previo con tiempo de gracia
    para un cierre ordenado. Esta es la MISMA decision de diseño que
    core.x11_utils.force_kill_by_xid (ver seccion 4.10 del plan):
    prioridad a la velocidad y la simplicidad. Aqui ademas no aplica
    ninguna de las dudas sobre "perder cambios sin guardar en una
    ventana visible" que si podrian discutirse para 4.10 — precisamente
    estamos en esta funcion porque el proceso NUNCA llego a abrir
    ninguna ventana.

    Es una funcion "silenciosa": no devuelve nada ni lanza excepcion
    ante el caso esperado de que el proceso ya haya terminado por su
    cuenta, o de que el grupo de procesos ya no exista para cuando se
    intenta matarlo (ProcessLookupError) — en ambos casos el resultado
    deseado ("que ese proceso no siga corriendo") ya se cumple.
    """
    if proc.poll() is not None:
        return  # ya termino por su cuenta, nada que hacer

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return  # el grupo de procesos ya no existe

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # No deberia pasar nunca contra SIGKILL (a diferencia de
        # SIGTERM, SIGKILL no puede ser ignorado ni bloqueado por el
        # proceso objetivo) — pero se protege de todas formas contra un
        # bloqueo indefinido de este metodo ante alguna circunstancia
        # extrema del sistema operativo.
        pass


def launch_and_wait(
    watcher: _WatcherProtocol,
    command: str,
    expected_wm_class: str,
    timeout_seconds: float,
) -> tuple[bool, Optional[int]]:
    """
    Lanza `command` como un proceso nuevo y espera, usando `watcher`
    (una core.window_watcher.NodeAddWatcher YA ABIERTA por el llamador,
    o cualquier objeto que cumpla el mismo protocolo — ver
    _WatcherProtocol arriba), a que aparezca su ventana con
    `expected_wm_class` dentro de `timeout_seconds`.

    Devuelve:
      (True, xid)    si la ventana aparecio a tiempo. `xid` es el ID de
                     la ventana nueva (int), util para que el llamador
                     (core/reconciler.py) le aplique despues el estado
                     floating y la geometria guardada si corresponde
                     (ver seccion 4.6/4.9 del plan).

      (False, None)  si se agoto el timeout. En este caso, ANTES de
                     devolver, esta funcion mata el proceso lanzado (via
                     _kill_process_tree) para no dejarlo huerfano — ver
                     seccion 4.13 del plan: "cuando una app no se abre
                     en 7 segundos, se mata el proceso lanzado y se
                     continua con la siguiente app de la lista (o
                     termina la carga si era la ultima)". Esta funcion
                     se encarga de la parte de "matar el proceso"; el
                     "continuar con la siguiente / terminar si era la
                     ultima" es responsabilidad del bucle en
                     core/reconciler.py, que simplemente sigue
                     iterando su lista de apps sin importar si esta
                     llamada devolvio exito o fallo.

    Lanza:
      LauncherError           si el comando no se pudo interpretar
                              (ver _parse_command) o si el binario no
                              existe en el sistema (FileNotFoundError al
                              intentar lanzarlo) — ambos son problemas
                              de datos/entorno genuinos, no un timeout
                              normal.
      bspc_client.BspcError   se propaga tal cual si watcher.wait_for_wm_class()
                              la lanza (proceso de suscripcion a bspc
                              muerto) — ver core/window_watcher.py. Esta
                              funcion NO la intercepta porque un problema
                              asi afecta a TODA la operacion de carga,
                              no solo a esta app individual; debe
                              propagarse hasta reconciler.py para que
                              decida abortar la carga completa.
    """
    argv = _parse_command(command)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Nuevo grupo de procesos/sesion: necesario para que
            # _kill_process_tree() pueda matar de un solo golpe tanto a
            # este proceso como a cualquier hijo que llegue a generar.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise LauncherError(
            f"El comando '{argv[0]}' no existe o no esta en el PATH."
        ) from exc
    except OSError as exc:
        raise LauncherError(
            f"No se pudo lanzar el comando {argv!r}: {exc}"
        ) from exc

    xid = watcher.wait_for_wm_class(expected_wm_class, timeout_seconds)

    if xid is not None:
        return True, xid

    _kill_process_tree(proc)
    return False, None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.launcher
    #
    # A diferencia de window_watcher.py, aqui NO se necesita un 'watcher'
    # real con una suscripcion genuina a bspc: launch_and_wait() recibe
    # el watcher como parametro (inyeccion de dependencia), asi que basta
    # con un doble de prueba simple que cumpla _WatcherProtocol. Esto
    # mantiene este self-test enfocado en lo que este archivo realmente
    # hace (parsear comandos, lanzar procesos, matar arboles de procesos
    # huerfanos), sin repetir las pruebas de I/O con select()/pipes que
    # ya se hicieron a fondo en core/window_watcher.py.

    import time

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

    class FakeWatcherReturns:
        """Doble de prueba: siempre devuelve el mismo resultado fijo."""

        def __init__(self, xid_to_return: Optional[int]):
            self._xid = xid_to_return
            self.calls: list[tuple[str, float]] = []

        def wait_for_wm_class(self, expected_wm_class: str, timeout_seconds: float) -> Optional[int]:
            self.calls.append((expected_wm_class, timeout_seconds))
            return self._xid

    # --- _parse_command: casos basicos ---
    check(
        "_parse_command separa argumentos simples",
        _parse_command("firefox --new-window") == ["firefox", "--new-window"],
    )
    check(
        "_parse_command respeta comillas (un solo argumento con espacio)",
        _parse_command('echo "hello world"') == ["echo", "hello world"],
    )
    check(
        "_parse_command expande ~ en cada token",
        _parse_command("code ~/proyecto") == ["code", os.path.expanduser("~/proyecto")],
    )

    # --- _parse_command: casos de error ---
    for bad_command, label in [
        ("", "comando vacio"),
        ("   ", "comando solo espacios"),
        ("comando 'sin cerrar comillas", "comillas sin cerrar"),
    ]:
        try:
            _parse_command(bad_command)
            check(f"LauncherError ante {label}", False)
        except LauncherError:
            check(f"LauncherError ante {label}", True)

    # --- launch_and_wait: caso exitoso (comando real, corto, inofensivo) ---
    fake_watcher_success = FakeWatcherReturns(xid_to_return=0x00600123)
    ok, xid = launch_and_wait(fake_watcher_success, "true", "AppBuscada", timeout_seconds=5)
    check("launch_and_wait devuelve (True, xid) en el caso exitoso", ok is True and xid == 0x00600123)
    check(
        "launch_and_wait llamo a wait_for_wm_class con los parametros correctos",
        fake_watcher_success.calls == [("AppBuscada", 5)],
    )

    # --- launch_and_wait: timeout, se debe matar el proceso lanzado ---
    # Se usa 'sleep 30' (un proceso de larga duracion real) para poder
    # verificar de forma genuina que _kill_process_tree lo termino, en
    # vez de simplemente confiar en que "deberia" haberlo hecho.
    fake_watcher_timeout = FakeWatcherReturns(xid_to_return=None)
    ok, xid = launch_and_wait(fake_watcher_timeout, "sleep 30", "NuncaAparece", timeout_seconds=1)
    check("launch_and_wait devuelve (False, None) ante timeout", ok is False and xid is None)

    # Verificacion INDEPENDIENTE (sin depender de la implementacion
    # interna) de que el proceso 'sleep 30' que se lanzo ya no sigue
    # vivo: se recorre /proc buscando algun proceso cuya linea de
    # comando contenga exactamente "sleep" y "30".
    def _sleep_30_still_running() -> bool:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    raw = f.read()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            parts = raw.split(b"\x00")
            if b"sleep" in parts and b"30" in parts:
                return True
        return False

    check(
        "el proceso 'sleep 30' lanzado fue realmente terminado (verificado via /proc)",
        not _sleep_30_still_running(),
    )

    # --- _kill_process_tree: prueba directa y aislada del mecanismo de kill ---
    real_proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    time.sleep(0.1)  # darle un instante a que arranque de verdad
    check("el proceso de prueba para _kill_process_tree esta vivo antes de matarlo", real_proc.poll() is None)
    _kill_process_tree(real_proc)
    check("_kill_process_tree deja el proceso terminado", real_proc.poll() is not None)

    # --- _kill_process_tree: no falla si el proceso ya habia terminado por su cuenta ---
    finished_proc = subprocess.Popen(["true"])
    finished_proc.wait()
    try:
        _kill_process_tree(finished_proc)
        check("_kill_process_tree no lanza excepcion sobre un proceso ya terminado", True)
    except Exception as exc:
        check(f"_kill_process_tree no lanza excepcion sobre un proceso ya terminado (lanzo {exc!r})", False)

    # --- launch_and_wait: binario inexistente ---
    fake_watcher_unused = FakeWatcherReturns(xid_to_return=None)
    try:
        launch_and_wait(
            fake_watcher_unused,
            "este_binario_no_existe_xyz_123",
            "LoQueSea",
            timeout_seconds=1,
        )
        check("LauncherError si el binario no existe", False)
    except LauncherError:
        check("LauncherError si el binario no existe", True)

    print(f"\n{passed} pruebas OK, {failed} pruebas fallidas.")
    if failed:
        raise SystemExit(1)
