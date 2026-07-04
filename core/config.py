"""
core/config.py
===============

Responsabilidades de este modulo (y SOLO estas):

  1. Definir las rutas de archivos que usa Luxion en tiempo de ejecucion
     (base de datos, lock file, sxhkdrc del usuario).
  2. Definir los valores por defecto de la tabla `settings` (ver seccion 3
     del plan arquitectonico) para que database.py los pueda sembrar la
     primera vez que se crea la base de datos.
  3. Exponer funciones de conveniencia (get_str/get_int/get_bool/set_value)
     para leer y escribir esos ajustes sin que el resto del codigo tenga
     que escribir SQL directamente ni conocer el nombre exacto de la
     tabla.

Este modulo NO abre conexiones a SQLite por si mismo mas alla de lo que
delega en database.py (ver nota sobre el import diferido mas abajo).

-------------------------------------------------------------------------
Nota tecnica sobre el import diferido de `database`
-------------------------------------------------------------------------
database.py necesita las constantes de rutas definidas aqui (DB_PATH),
asi que hace `from . import config` en su parte superior. Si este archivo
importara `database` de la misma forma a nivel de modulo, se formaria un
import circular (config -> database -> config) que en algunos ordenes de
carga puede fallar.

La solucion estandar en Python para este caso es hacer el import DENTRO
de las funciones que realmente lo necesitan (import diferido / "lazy
import"). Para cuando esas funciones se ejecutan, ambos modulos ya
terminaron de cargarse por completo, asi que no hay ningun problema real
de circularidad. Se deja documentado aqui porque es la clase de detalle
que, si se "corrige" moviendo el import arriba sin entender el motivo,
rompe el arranque del programa.
"""

from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

APP_NAME = "luxion"

# Sigue la convencion XDG habitual para datos de aplicaciones de usuario:
# ~/.local/share/<app>/. No usamos variables de entorno XDG_DATA_HOME por
# simplicidad (Kali/Debian con XFCE siempre tiene ~/.local/share
# disponible), pero si en el futuro se quisiera respetar XDG_DATA_HOME,
# este es el unico lugar que habria que tocar.
DATA_DIR: str = os.path.expanduser(os.path.join("~", ".local", "share", APP_NAME))

DB_PATH: str = os.path.join(DATA_DIR, "luxion.db")
LOCK_PATH: str = os.path.join(DATA_DIR, "luxion.lock")

# Archivo real de configuracion de sxhkd del usuario. hotkey_manager.py
# escribe un bloque delimitado dentro de este archivo (ver seccion 4.8
# del plan). Se centraliza aqui para que ningun otro modulo tenga la ruta
# hardcodeada por su cuenta.
SXHKDRC_PATH: str = os.path.expanduser("~/.config/sxhkd/sxhkdrc")


def ensure_data_dir() -> None:
    """
    Crea ~/.local/share/luxion/ si todavia no existe.

    Se llama antes de cualquier operacion que necesite escribir en
    DATA_DIR (abrir la base de datos, crear el lock file). Es idempotente
    y segura de llamar multiples veces (os.makedirs con exist_ok=True no
    falla si el directorio ya esta).
    """
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Valores por defecto de la tabla `settings`
# ---------------------------------------------------------------------------
#
# IMPORTANTE: todos los valores se guardan como TEXT en la base de datos
# (asi es como esta definida la tabla `settings` en el esquema, ver
# seccion 3 del plan). Por eso aqui tambien se definen como strings,
# aunque conceptualmente algunos sean enteros o booleanos: la conversion
# de tipo ocurre en las funciones get_int()/get_bool() de este mismo
# archivo, no en la base de datos.
#
# Estos valores se siembran UNA sola vez, la primera vez que se crea la
# base de datos (ver database.py -> init_db() -> _seed_default_settings).
# Si el usuario ya cambio un valor desde ui/settings_dialog.py, la
# siembra usa "INSERT OR IGNORE", asi que nunca pisa un valor ya
# existente.

DEFAULT_SETTINGS: dict[str, str] = {
    # Cuantos segundos esperar como maximo a que una app lanzada abra una
    # ventana con el WM_CLASS esperado antes de darla por fallida.
    # Usado por core/launcher.py (ver seccion 4.13 del plan).
    "app_open_timeout_seconds": "7",
    # Margen de gracia (en milisegundos) que se espera DESPUES de recibir
    # el evento node_add de bspc, antes de considerar la ventana
    # realmente lista para la siguiente operacion. Compensa que el mapeo
    # de la ventana en X11 no garantiza que ya haya pintado su primer
    # frame real (notorio en apps Electron/Java). Usado por
    # core/window_watcher.py (ver seccion 4.12).
    "render_grace_ms": "400",
    # Comportamiento GLOBAL por defecto al cargar un workspace: si se
    # deben cerrar (matar) las ventanas que no coinciden con el workspace
    # objetivo. Cada workspace individual puede sobreescribir este valor
    # via workspaces.close_unmatched_windows (NULL = usar este default).
    # Usado por core/reconciler.py (ver seccion 4.11).
    "default_close_unmatched_windows": "1",
}


# ---------------------------------------------------------------------------
# Lectura/escritura de settings (wrappers sobre database.py)
# ---------------------------------------------------------------------------


def get_str(key: str, fallback: Optional[str] = None) -> Optional[str]:
    """
    Devuelve el valor crudo (string) de un setting.

    Orden de resolucion:
      1. Si la clave existe en la tabla `settings` de la base de datos,
         se devuelve ese valor (el usuario pudo haberlo cambiado desde
         ui/settings_dialog.py).
      2. Si no existe en la base de datos todavia (por ejemplo, la base
         de datos se acaba de crear y aun no corrio la siembra, o se
         agrego una clave nueva en una version mas reciente de Luxion
         que el usuario todavia no actualizo), se usa el valor de
         DEFAULT_SETTINGS.
      3. Si tampoco existe ahi, se devuelve `fallback` (por defecto None).

    Esta doble red de seguridad (DB -> DEFAULT_SETTINGS -> fallback)
    evita que Luxion crashee por un KeyError si algun dia se agrega un
    ajuste nuevo y la base de datos de un usuario existente todavia no
    lo tiene sembrado.
    """
    from . import database  # import diferido, ver nota al inicio del archivo

    value = database.get_setting(key)
    if value is not None:
        return value
    return DEFAULT_SETTINGS.get(key, fallback)


def get_int(key: str, fallback: Optional[int] = None) -> Optional[int]:
    """
    Igual que get_str(), pero convierte el resultado a int.

    Si el valor almacenado no se puede convertir a int (dato corrupto o
    editado a mano de forma invalida), se devuelve `fallback` en vez de
    lanzar una excepcion — un ajuste mal formado no deberia poder tumbar
    una carga de workspace completa.
    """
    raw = get_str(key)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except (ValueError, TypeError):
        return fallback


def get_bool(key: str, fallback: Optional[bool] = None) -> Optional[bool]:
    """
    Igual que get_str(), pero convierte el resultado a bool.

    Se aceptan como "verdadero": "1", "true", "True", "yes", "on"
    (sin distinguir mayusculas/minusculas). Cualquier otra cosa no vacia
    se interpreta como False. Si no hay valor en absoluto, se devuelve
    `fallback`.
    """
    raw = get_str(key)
    if raw is None:
        return fallback
    return raw.strip().lower() in ("1", "true", "yes", "on")


def set_value(key: str, value) -> None:
    """
    Guarda (o actualiza) un ajuste en la tabla `settings`.

    `value` puede ser cualquier tipo con una representacion razonable en
    str() (int, bool, str) — se convierte explicitamente antes de
    guardar, ya que la columna `settings.value` es TEXT.

    Usado principalmente desde ui/settings_dialog.py cuando el usuario
    cambia un ajuste desde la interfaz grafica.
    """
    from . import database  # import diferido, ver nota al inicio del archivo

    database.set_setting(key, str(value))


if __name__ == "__main__":
    # Ejecutar con: python3 -m core.config
    # Sanity check rapido de las rutas resueltas, sin tocar la base de
    # datos. Util para confirmar que las rutas son las esperadas en la
    # maquina donde se esta desarrollando/probando.
    print("APP_NAME     =", APP_NAME)
    print("DATA_DIR     =", DATA_DIR)
    print("DB_PATH      =", DB_PATH)
    print("LOCK_PATH    =", LOCK_PATH)
    print("SXHKDRC_PATH =", SXHKDRC_PATH)
    ensure_data_dir()
    print("DATA_DIR creado/confirmado en disco:", os.path.isdir(DATA_DIR))
    print("DEFAULT_SETTINGS =", DEFAULT_SETTINGS)
