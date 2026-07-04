"""
core/lockfile.py
=================

Mecanismo de exclusion mutua entre procesos (ver seccion 4.16 del plan
arquitectonico) para evitar que dos operaciones de Luxion se ejecuten al
mismo tiempo sobre el mismo desktop — el escenario concreto que hay que
evitar es: la GUI cargando el Workspace A mientras, en el mismo instante,
un atajo de teclado dispara la carga del Workspace B via luxion_cli.py.
Ambos son procesos de sistema operativo independientes, asi que un lock
en memoria (por ejemplo un threading.Lock) no sirve de nada — tiene que
ser un lock visible entre procesos, y la forma mas simple y confiable de
lograrlo en Linux sin dependencias externas es un archivo en disco cuya
existencia se verifica de forma atomica.

-------------------------------------------------------------------------
Por que "cargar el mismo lock dos veces desde el MISMO proceso" tambien
debe fallar (el lock NO es reentrante, a proposito)
-------------------------------------------------------------------------
Si el codigo de Luxion en algun momento llamara a workspace_service.load()
desde dentro de otra funcion que ya adquirio el lock (un bug de
programacion, no un uso legitimo), lo correcto es que la segunda
adquisicion falle con LuxionBusyError en vez de "colarse" silenciosamente
solo porque el PID coincide consigo mismo. Esto es deliberado: un lock
reentrante enmascararia justamente el tipo de bug de doble-invocacion que
este modulo existe para prevenir.

REGLA DE USO: unicamente las funciones de mas alto nivel en
core/workspace_service.py (save(), load(), delete()) deben envolver su
cuerpo en "with lockfile.acquire():". Nada mas por debajo de esas
funciones (reconciler.py, launcher.py, etc.) ni nada por arriba (CLI,
GUI) debe volver a adquirir el lock — si lo hicieran, se dispararia
LuxionBusyError contra si mismos.

-------------------------------------------------------------------------
Deteccion de locks obsoletos (stale locks)
-------------------------------------------------------------------------
Si Luxion (o el proceso que sea) se cierra de forma abrupta -crash,
kill -9, corte de energia en la VM- sin pasar por el "finally" que borra
el lock file, este quedaria huerfano en disco para siempre, bloqueando
cualquier operacion futura sin ninguna razon real. Por eso, antes de
tratar un lock existente como "ocupado", se verifica si el PID guardado
adentro corresponde a un proceso que SIGUE VIVO (via
core.x11_utils.process_is_alive). Si el proceso ya no existe, el lock se
considera obsoleto, se descarta automaticamente, y la adquisicion
continua con normalidad.

-------------------------------------------------------------------------
Seguridad ante condiciones de carrera en la creacion del lock file
-------------------------------------------------------------------------
Crear el lock con os.open(..., os.O_CREAT | os.O_EXCL, ...) es una
operacion ATOMICA a nivel del sistema operativo: si dos procesos
intentan crear el mismo archivo con O_EXCL exactamente al mismo tiempo,
el sistema operativo garantiza que solo UNO de los dos tenga exito (el
otro recibe FileExistsError). Esto cierra la ventana de tiempo que
existiria si en cambio se hiciera "verificar si existe" seguido de
"crear" como dos pasos separados (un patron TOCTOU — time-of-check to
time-of-use — clasicamente propenso a condiciones de carrera, el mismo
tipo de bug que causo varios de los problemas reales encontrados durante
el desarrollo de setup_bspwm_kali.sh).
"""

from __future__ import annotations

import contextlib
import os
from typing import Optional

from . import config
from . import x11_utils

# Cuantas veces se reintenta el ciclo "encontre un lock obsoleto -> lo
# borro -> intento crear el mio" antes de rendirse. Un numero pequeño es
# suficiente: en el caso normal (un solo lock obsoleto que limpiar) basta
# con 1 reintento; el margen extra es solo por si dos procesos estan
# limpiando/recreando el lock casi al mismo tiempo.
MAX_STALE_LOCK_RETRIES = 5


class LuxionBusyError(Exception):
    """
    Se lanza cuando OTRO proceso (todavia vivo) ya tiene el lock. Este es
    el caso "normal" de negocio -alguien mas esta usando Luxion ahora
    mismo- y quien llame a acquire() deberia esperarlo y manejarlo con
    un mensaje claro al usuario, no como un error de programacion.
    """


class LuxionLockError(Exception):
    """
    Se lanza ante problemas de INFRAESTRUCTURA al manejar el lock file
    (permisos insuficientes en ~/.local/share/luxion/, disco lleno,
    sistema de archivos de solo lectura, etc.) — a diferencia de
    LuxionBusyError, esto NO significa "alguien mas lo esta usando", sino
    "el propio mecanismo de lock no pudo operar". Se distingue de
    LuxionBusyError porque el llamador probablemente quiera reaccionar
    distinto ante cada caso (reintentar mas tarde vs. avisar de un
    problema de instalacion/permisos).
    """


def _read_lock_pid(path: str) -> Optional[int]:
    """
    Lee el PID guardado dentro del lock file. Devuelve None tanto si el
    archivo no existe como si su contenido no es un numero valido
    (contenido corrupto se trata exactamente igual que un lock obsoleto:
    algo que no representa a un proceso vivo real, asi que es seguro
    descartarlo).
    """
    try:
        with open(path, "r") as f:
            content = f.read().strip()
    except (FileNotFoundError, OSError):
        return None
    return int(content) if content.isdigit() else None


def _remove_lock_file(path: str) -> None:
    """
    Borra el lock file. Ignora silenciosamente si ya no existe (alguien
    mas pudo haberlo limpiado en una carrera benigna entre dos procesos
    descartando el mismo lock obsoleto casi al mismo tiempo). Cualquier
    otro error de sistema de archivos (permisos, etc.) se traduce a
    LuxionLockError con contexto claro, en vez de dejar pasar una
    excepcion generica de bajo nivel.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LuxionLockError(
            f"No se pudo eliminar el lock file en {path}: {exc}"
        ) from exc


@contextlib.contextmanager
def acquire(operation: Optional[str] = None):
    """
    Context manager que adquiere el lock exclusivo de Luxion para la
    duracion del bloque `with`, y lo libera automaticamente al salir
    (tanto en el caso exitoso como si el bloque lanza una excepcion).

    `operation` es un texto opcional y puramente descriptivo (ej.
    "guardar workspace", "cargar workspace 'Tesis'") que se incluye en el
    mensaje de LuxionBusyError si el lock esta ocupado, para que el CLI o
    la GUI puedan mostrarle al usuario un mensaje mas util que un simple
    "ocupado". No afecta la logica de adquisicion en absoluto.

    Uso tipico (dentro de core/workspace_service.py):

        def load(workspace_id):
            with lockfile.acquire(operation="cargar workspace"):
                desktop = bspc_client.get_focused_desktop_name()
                reconciler.reconcile_and_load(workspace_id, desktop)
                database.set_desktop_state(desktop, workspace_id)

    Lanza:
      LuxionBusyError  si otro proceso (todavia vivo) tiene el lock.
      LuxionLockError  si hay un problema de infraestructura para crear
                       o limpiar el lock file (permisos, disco, etc.).
    """
    config.ensure_data_dir()
    lock_path = config.LOCK_PATH
    my_pid = os.getpid()

    attempts = 0
    while True:
        attempts += 1
        try:
            # O_CREAT | O_EXCL: creacion atomica, falla con
            # FileExistsError si el archivo ya existe. Ver la nota sobre
            # TOCTOU en el docstring del modulo.
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing_pid = _read_lock_pid(lock_path)
            if existing_pid is not None and x11_utils.process_is_alive(existing_pid):
                suffix = f" ({operation})" if operation else ""
                raise LuxionBusyError(
                    "Ya hay una operacion de Luxion en curso "
                    f"(PID {existing_pid}){suffix}. Intenta de nuevo en "
                    "un momento."
                )

            # El lock existente es obsoleto (el proceso que lo creo ya
            # no esta vivo) o su contenido esta corrupto. Se descarta y
            # se reintenta el ciclo completo, con un limite de
            # reintentos para no quedar en un bucle infinito ante un
            # problema de permisos persistente.
            if attempts > MAX_STALE_LOCK_RETRIES:
                raise LuxionLockError(
                    f"No se pudo adquirir el lock tras "
                    f"{MAX_STALE_LOCK_RETRIES} intentos de limpiar locks "
                    f"obsoletos en {lock_path}. Revisa los permisos de "
                    "ese archivo/directorio."
                )
            _remove_lock_file(lock_path)
            continue
        except OSError as exc:
            raise LuxionLockError(
                f"No se pudo crear el lock file en {lock_path}: {exc}"
            ) from exc
        else:
            # Adquisicion exitosa: se escribe el propio PID adentro,
            # tanto para que otros procesos puedan diagnosticar quien lo
            # tiene, como para la verificacion de "no borrar un lock que
            # no es mio" en _release() mas abajo.
            with os.fdopen(fd, "w") as f:
                f.write(str(my_pid))
            break

    try:
        yield
    finally:
        _release(lock_path, my_pid)


def _release(path: str, expected_pid: int) -> None:
    """
    Libera el lock, pero SOLO si el archivo todavia contiene el PID que
    nosotros mismos escribimos al adquirirlo.

    Esta verificacion protege contra un escenario extremadamente
    improbable pero posible en teoria: que el lock haya sido considerado
    "obsoleto" y limpiado por otro proceso mientras nosotros creiamos
    seguir siendo los dueños (por ejemplo, alguien borro el lock file a
    mano desde fuera de Luxion mientras la operacion estaba en curso, y
    otro proceso de Luxion lo adquirio legitimamente despues). Sin esta
    verificacion, nuestro "finally" borraria el lock de ESE OTRO
    proceso, dejandolo sin proteccion mientras aun esta trabajando — lo
    opuesto de lo que este modulo existe para garantizar.

    Si el contenido no coincide con nuestro propio PID, simplemente no
    se hace nada (no se borra el archivo).
    """
    owner_pid = _read_lock_pid(path)
    if owner_pid is not None and owner_pid != expected_pid:
        return
    _remove_lock_file(path)


# ---------------------------------------------------------------------------
# Self-test (real, entre procesos de verdad — no requiere X11 ni bspwm)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.lockfile
    #
    # A diferencia de x11_utils.py y bspc_client.py (que necesitaron
    # mocks porque dependen de xdotool/bspc reales, no disponibles en
    # este entorno de desarrollo), este modulo SOLO depende del sistema
    # de archivos y de os.kill(pid, 0) — ambos disponibles aqui mismo.
    # Por eso este self-test es una prueba real de extremo a extremo,
    # incluyendo exclusion mutua GENUINA entre procesos distintos
    # (se lanzan subprocesos de Python reales que intentan adquirir el
    # mismo lock, no se simula con mocks).

    import subprocess
    import sys
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

    # Limpieza previa por si quedo un lock de una ejecucion anterior de
    # este mismo self-test.
    if os.path.exists(config.LOCK_PATH):
        os.remove(config.LOCK_PATH)

    # --- Test 1: adquisicion y liberacion basica ---
    with acquire():
        check("el lock file existe durante el bloque with", os.path.exists(config.LOCK_PATH))
        check(
            "el lock file contiene nuestro propio PID",
            _read_lock_pid(config.LOCK_PATH) == os.getpid(),
        )
    check("el lock file se elimina al salir del bloque with", not os.path.exists(config.LOCK_PATH))

    # --- Test 2: exclusion mutua real entre procesos ---
    # Mientras este proceso mantiene el lock, se lanza un subproceso de
    # Python real que intenta adquirirlo tambien. Debe fallar con
    # LuxionBusyError.
    checker_snippet = (
        "import sys; sys.path.insert(0, '.'); "
        "from core import lockfile; "
        "exec("
        "'try:\\n"
        "    with lockfile.acquire():\\n"
        "        print(\"UNEXPECTED_SUCCESS\")\\n"
        "except lockfile.LuxionBusyError:\\n"
        "    print(\"BUSY\")\\n"
        "except Exception as e:\\n"
        "    print(f\"OTHER_ERROR:{type(e).__name__}\")'"
        ")"
    )

    with acquire():
        result = subprocess.run(
            [sys.executable, "-c", checker_snippet],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        check(
            "un subproceso distinto detecta LuxionBusyError mientras el lock esta tomado",
            result.stdout.strip() == "BUSY",
        )

    # --- Test 3: una vez liberado, un subproceso nuevo SI puede adquirirlo ---
    result = subprocess.run(
        [sys.executable, "-c", checker_snippet.replace("BUSY", "BUSY")],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Nota: el snippet de arriba, al ya no estar el lock tomado, entrara
    # al bloque `with` sin excepcion e imprimira UNEXPECTED_SUCCESS (el
    # nombre es enganoso fuera de contexto de Test 2, pero aqui SI es el
    # resultado correcto: significa que pudo adquirir el lock con
    # normalidad).
    check(
        "un subproceso nuevo puede adquirir el lock una vez liberado",
        result.stdout.strip() == "UNEXPECTED_SUCCESS",
    )
    check("no queda ningun lock huerfano tras el test anterior", not os.path.exists(config.LOCK_PATH))

    # --- Test 4: deteccion de lock obsoleto (proceso muerto) ---
    dead_proc = subprocess.Popen(["true"])
    dead_proc.wait()
    dead_pid = dead_proc.pid
    check("el proceso de prueba para el PID muerto efectivamente termino", dead_proc.poll() is not None)

    with open(config.LOCK_PATH, "w") as f:
        f.write(str(dead_pid))

    with acquire():
        check(
            "acquire() no lanza LuxionBusyError ante un lock con PID muerto (stale)",
            True,  # si llegamos aqui sin excepcion, ya se cumplio
        )
        check(
            "tras descartar el lock obsoleto, el archivo pasa a tener NUESTRO pid",
            _read_lock_pid(config.LOCK_PATH) == os.getpid(),
        )
    check("el lock se libera normalmente despues de haber sido stale", not os.path.exists(config.LOCK_PATH))

    # --- Test 5: contenido corrupto (no numerico) tambien se trata como obsoleto ---
    with open(config.LOCK_PATH, "w") as f:
        f.write("esto-no-es-un-pid")

    with acquire():
        check(
            "acquire() tolera contenido corrupto en el lock file (lo trata como stale)",
            _read_lock_pid(config.LOCK_PATH) == os.getpid(),
        )
    check("el lock se libera normalmente tras contenido corrupto", not os.path.exists(config.LOCK_PATH))

    # --- Test 6: _release() no borra un lock que pertenece a otro PID vivo ---
    with open(config.LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))  # nuestro propio PID real, esta vivo de verdad

    _release(config.LOCK_PATH, expected_pid=999999999)  # PID distinto, a propósito
    check(
        "_release() NO borra el lock si el PID adentro no coincide con expected_pid",
        os.path.exists(config.LOCK_PATH),
    )
    # Limpieza manual (aqui SI corresponde borrarlo, ya terminamos de probar)
    os.remove(config.LOCK_PATH)

    # --- Test 7: LuxionLockError ante MAX_STALE_LOCK_RETRIES agotados ---
    # Se simula forzando que _remove_lock_file no pueda limpiar nunca el
    # lock (monkeypatching temporal), para confirmar que el bucle
    # termina con un error claro en vez de colgarse indefinidamente.
    import unittest.mock as mock

    with open(config.LOCK_PATH, "w") as f:
        f.write(str(dead_pid))  # PID muerto, pero "no se puede limpiar"

    with mock.patch(
        f"{__name__}._remove_lock_file",  # __name__ es "__main__" al correr con -m;
        # usar la ruta fija "core.lockfile._remove_lock_file" aqui NO
        # interceptaria nada, porque bajo `python3 -m core.lockfile` el
        # codigo de acquire() esta corriendo como parte del modulo
        # __main__, no de un modulo "core.lockfile" separado — parchear
        # ese otro nombre estaria parcheando un objeto de modulo
        # distinto al que realmente se ejecuta.
        side_effect=lambda p: None,  # no hace nada: simula que nunca se limpia
    ):
        try:
            with acquire():
                check("LuxionLockError ante limite de reintentos agotado", False)
        except LuxionLockError:
            check("LuxionLockError ante limite de reintentos agotado", True)

    if os.path.exists(config.LOCK_PATH):
        os.remove(config.LOCK_PATH)

    print(f"\n{passed} pruebas OK, {failed} pruebas fallidas.")
    if failed:
        raise SystemExit(1)
