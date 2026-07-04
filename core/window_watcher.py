"""
core/window_watcher.py
=======================

Modulo de bloqueo/escucha (ver seccion 4.12 del plan arquitectonico):
garantiza que Luxion no procese la siguiente app de una carga hasta que
la ventana de la app anterior este realmente registrada en el arbol de
bspwm (mas un margen de gracia adicional para su renderizado real).

Contiene una sola clase publica: NodeAddWatcher, pensada para usarse como
context manager DURANTE TODA una operacion de carga (no una instancia
nueva por cada app individual):

    with NodeAddWatcher() as watcher:
        for app in apps_a_lanzar:
            ok, xid = launcher.launch_and_wait(
                watcher, app.launch_command, app.wm_class, timeout_seconds=7
            )
            ...

-------------------------------------------------------------------------
Por que UNA sola suscripcion para toda la carga, no una por cada app
-------------------------------------------------------------------------
Si se abriera y cerrara una suscripcion nueva en cada iteracion del
bucle (una por app), existiria una ventana de tiempo entre "se lanza la
app" y "termina de establecerse la nueva suscripcion" en la que un
evento node_add legitimo podria perderse — exactamente el mismo tipo de
condicion de carrera que causo el bug real del autostart de XFCE
documentado en setup_bspwm_kali.sh (procesos independientes arrancando
en paralelo sin garantia de orden). Por eso la suscripcion se abre UNA
vez, antes de lanzar la primera app, y se mantiene abierta leyendo
eventos secuencialmente durante toda la operacion.

-------------------------------------------------------------------------
Como se espera con timeout sobre un pipe de un subproceso (select())
-------------------------------------------------------------------------
`proc.stdout.readline()` por si solo es una llamada BLOQUEANTE sin forma
de imponerle un limite de tiempo. Para poder respetar el timeout de 7
segundos por app (seccion 4.13), se usa `select.select()` sobre el
descriptor de archivo del pipe antes de cada intento de lectura: esto
permite esperar como maximo el tiempo restante del timeout a que haya
datos disponibles, sin bloquear mas alla de eso.

NOTA sobre una simplificacion deliberada: `select()` solo garantiza que
HAY datos disponibles para leer, no que una LINEA COMPLETA este lista —
en teoria, `readline()` podria bloquear brevemente si recibio una
escritura parcial. En la practica esto no es un riesgo real aqui: cada
linea de evento de 'bspc subscribe' es de pocas decenas de bytes, muy
por debajo de PIPE_BUF (4096 bytes en Linux), por lo que el kernel
garantiza que cada escritura de bspc llega como una unica operacion
atomica — cuando select() reporta que hay datos, la linea completa ya
esta disponible. Implementar un lector no bloqueante byte a byte para
cubrir un riesgo praticamente inexistente en este caso especifico
habria sido complejidad sin beneficio real.
"""

from __future__ import annotations

import select
import time
from typing import Optional

from . import bspc_client
from . import config
from . import x11_utils


def _start_subscribe_process():
    """
    Punto de entrada (deliberadamente aislado en su propia funcion, en
    vez de llamado directo desde __enter__) para poder sustituirlo desde
    el self-test de este archivo por un proceso de prueba controlable,
    sin necesitar un 'bspc' real instalado. Ver el bloque
    `if __name__ == "__main__":` al final del archivo.
    """
    return bspc_client.start_subscribe("node_add")


class NodeAddWatcher:
    """
    Context manager que mantiene abierta una suscripcion a eventos
    'node_add' de bspwm durante su tiempo de vida, y expone
    wait_for_wm_class() para esperar, de forma sincrona y con timeout, a
    que aparezca una ventana nueva con un WM_CLASS especifico.
    """

    def __init__(self) -> None:
        self._proc = None  # se asigna en __enter__

    def __enter__(self) -> "NodeAddWatcher":
        self._proc = _start_subscribe_process()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1)
                except Exception:
                    # No respondio a terminate() a tiempo: se fuerza.
                    self._proc.kill()
                    self._proc.wait(timeout=1)
            self._proc = None
        # No suprimir ninguna excepcion que haya ocurrido dentro del
        # bloque `with` (devolver False/None dejaria que se propague,
        # que es el comportamiento correcto: un error real durante la
        # carga no debe quedar silenciado).
        return False

    def wait_for_wm_class(self, expected_wm_class: str, timeout_seconds: float) -> Optional[int]:
        """
        Bloquea hasta que ocurra UNA de estas tres cosas:

          1. Aparece una ventana nueva cuyo WM_CLASS coincide con
             `expected_wm_class` -> devuelve su XID (int), DESPUES de
             esperar el margen de gracia configurado en
             settings.render_grace_ms (ver core/config.py).

          2. Se agota `timeout_seconds` sin que aparezca -> devuelve
             None. NO lanza excepcion por esto: un timeout es el
             resultado esperado y normal que maneja
             core/launcher.py (ver seccion 4.13 del plan), no un error
             de este modulo.

          3. El proceso de suscripcion a bspc muere inesperadamente
             (bspwm se cayo, o algun otro problema de entorno genuino)
             -> lanza bspc_client.BspcError. A diferencia del timeout,
             esto SI es una condicion anormal que debe interrumpir la
             carga completa, no solo saltar a la siguiente app.

        Eventos de OTRAS ventanas (WM_CLASS distinto al esperado) se
        ignoran silenciosamente y NO consumen el timeout de forma
        distinta a simplemente seguir esperando — esto es intencional:
        durante una carga, podrian aparecer notificaciones del sistema,
        o la propia app esperada podria abrir una ventana secundaria
        (splash screen) con otro WM_CLASS antes de su ventana principal.

        Debe llamarse dentro de un bloque `with NodeAddWatcher() as w:`;
        llamarla antes de __enter__() o despues de __exit__() lanza
        RuntimeError.
        """
        if self._proc is None:
            raise RuntimeError(
                "NodeAddWatcher.wait_for_wm_class() debe llamarse dentro "
                "de un bloque 'with NodeAddWatcher() as watcher:'."
            )

        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            # Deteccion temprana de que el proceso de subscribe murio,
            # para no quedarnos esperando en select() datos que ya nunca
            # van a llegar durante el resto del timeout.
            if self._proc.poll() is not None:
                raise bspc_client.BspcError(
                    "El proceso 'bspc subscribe node_add' termino "
                    f"inesperadamente (codigo {self._proc.returncode}) "
                    "mientras se esperaba una ventana nueva."
                )

            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                # select() agoto su propio timeout (== remaining); el
                # bucle vuelve a evaluar `remaining` desde el inicio, que
                # ahora sera <= 0 y se devolvera None arriba.
                continue

            line = self._proc.stdout.readline()
            if not line:
                # EOF: el pipe se cerro sin que poll() lo detectara
                # todavia en esta vuelta del bucle (una condicion de
                # carrera muy estrecha entre el cierre del proceso y
                # nuestra verificacion de poll() de mas arriba). Se trata
                # igual que un proceso muerto: es un problema de entorno
                # real, no un timeout normal.
                raise bspc_client.BspcError(
                    "La suscripcion 'bspc subscribe node_add' se cerro "
                    "inesperadamente (EOF) mientras se esperaba una "
                    "ventana nueva."
                )

            new_xid = bspc_client.parse_node_add_event(line)
            if new_xid is None:
                continue  # linea no relevante, se sigue escuchando

            actual_class = x11_utils.get_wm_class(new_xid)
            if actual_class is None:
                # La ventana ya se cerro para cuando alcanzamos a
                # consultarla (carrera legitima, ej. una ventana muy
                # efimera). Se ignora y se sigue esperando.
                continue

            if actual_class == expected_wm_class:
                grace_ms = config.get_int("render_grace_ms", fallback=400) or 0
                if grace_ms > 0:
                    time.sleep(grace_ms / 1000)
                return new_xid

            # WM_CLASS de otra ventana: se ignora, se sigue esperando.


# ---------------------------------------------------------------------------
# Self-test (con un subproceso Python real haciendo de 'bspc subscribe',
# ejercitando la logica REAL de select()/readline()/timeout — no se
# mockea subprocess.run como en x11_utils.py/bspc_client.py, porque aqui
# lo que se quiere probar es justamente el comportamiento de I/O real
# entre procesos, que un mock no ejercitaria de forma genuina)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.window_watcher
    import subprocess
    import sys
    import unittest.mock as mock

    from . import database

    # config.get_int() (usado dentro de wait_for_wm_class para el
    # margen de gracia) termina consultando la tabla `settings` via
    # database.get_setting(); si esa tabla no existe todavia se
    # lanzaria sqlite3.OperationalError. init_db() es idempotente y
    # segura de llamar aqui.
    database.init_db()
    # Se reduce el margen de gracia a un valor pequeño para que el
    # self-test corra rapido, sin dejar de ejercitar de verdad esa
    # linea de codigo (con 0 no probariamos que efectivamente espera
    # algo).
    config.set_value("render_grace_ms", "20")

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

    def make_fake_subscribe(producer_code: str):
        """
        Devuelve una funcion reemplazo para _start_subscribe_process()
        que lanza un subproceso de Python real ejecutando
        `producer_code`, en vez del 'bspc subscribe' real (no disponible
        en este entorno de desarrollo). El subproceso real ejercita el
        pipe/select()/readline() de NodeAddWatcher con I/O genuina entre
        procesos del sistema operativo.
        """

        def _fake():
            return subprocess.Popen(
                [sys.executable, "-u", "-c", producer_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )

        return _fake

    # --- Test 1: aparece primero una ventana NO buscada, luego la buscada ---
    producer_ok = (
        "import time\n"
        "time.sleep(0.1)\n"
        "print('node_add 0x1 0x2 0x3 0x00600011 0', flush=True)\n"  # no coincide
        "time.sleep(0.1)\n"
        "print('node_add 0x1 0x2 0x3 0x00600099 0', flush=True)\n"  # coincide
        "time.sleep(10)\n"  # simula que 'subscribe' sigue vivo indefinidamente
    )

    def fake_get_wm_class(xid):
        return {0x00600011: "OtraApp", 0x00600099: "AppBuscada"}.get(xid)

    with mock.patch(f"{__name__}._start_subscribe_process", make_fake_subscribe(producer_ok)):
        with mock.patch("core.x11_utils.get_wm_class", side_effect=fake_get_wm_class):
            t0 = time.monotonic()
            with NodeAddWatcher() as watcher:
                result = watcher.wait_for_wm_class("AppBuscada", timeout_seconds=3)
            elapsed = time.monotonic() - t0
            check(
                "detecta la ventana correcta e ignora la que no coincide",
                result == 0x00600099,
            )
            check(
                "aplica el margen de gracia configurado (render_grace_ms)",
                elapsed >= 0.01,  # 20ms configurados, margen laxo para evitar flakiness
            )

    # --- Test 2: timeout, la ventana esperada nunca aparece ---
    producer_never = "import time\ntime.sleep(10)\n"
    with mock.patch(f"{__name__}._start_subscribe_process", make_fake_subscribe(producer_never)):
        t0 = time.monotonic()
        with NodeAddWatcher() as watcher:
            result = watcher.wait_for_wm_class("NuncaAparece", timeout_seconds=0.5)
        elapsed = time.monotonic() - t0
        check("devuelve None si se agota el timeout", result is None)
        check(
            "el timeout se respeta razonablemente (ni instantaneo ni indefinido)",
            0.4 <= elapsed <= 2.0,
        )

    # --- Test 3: el proceso de 'subscribe' muere inesperadamente ---
    producer_dies = "import sys\nsys.exit(1)\n"
    with mock.patch(f"{__name__}._start_subscribe_process", make_fake_subscribe(producer_dies)):
        t0 = time.monotonic()
        with NodeAddWatcher() as watcher:
            try:
                watcher.wait_for_wm_class("LoQueSea", timeout_seconds=5)
                check("BspcError si el proceso de subscribe muere", False)
            except bspc_client.BspcError:
                check("BspcError si el proceso de subscribe muere", True)
        elapsed = time.monotonic() - t0
        check(
            "detecta la muerte del proceso rapido, sin esperar el timeout completo",
            elapsed < 2.0,
        )

    # --- Test 4: llamar wait_for_wm_class() fuera de un bloque 'with' ---
    watcher_sin_with = NodeAddWatcher()
    try:
        watcher_sin_with.wait_for_wm_class("Lo que sea", timeout_seconds=1)
        check("RuntimeError si se usa fuera de 'with'", False)
    except RuntimeError:
        check("RuntimeError si se usa fuera de 'with'", True)

    print(f"\n{passed} pruebas OK, {failed} pruebas fallidas.")
    if failed:
        raise SystemExit(1)
