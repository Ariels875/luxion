"""
core/database.py
=================

Unico modulo que habla SQL directamente en todo el proyecto. El resto del
codigo (reconciler.py, launcher.py, workspace_service.py, hotkey_manager.py,
y eventualmente ui/) SIEMPRE debe pasar por las funciones de este archivo
en vez de abrir sus propias conexiones a sqlite3 o escribir sus propias
queries. Esto centraliza en un solo lugar:

  - El modo WAL y el PRAGMA foreign_keys=ON (ver seccion 4.18 del plan:
    sin foreign_keys=ON, TODAS las clausulas ON DELETE CASCADE / SET NULL
    del esquema quedarian sin efecto de forma silenciosa).
  - La forma exacta en que se abren/cierran conexiones (para no dejar
    conexiones huerfanas abiertas por descuido en otros modulos).
  - El esquema completo, en un solo lugar, en vez de disperso.

Este archivo NO conoce nada sobre bspwm, X11, ni Gtk. Es persistencia
pura.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from typing import Iterable, Optional

from . import config

# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------
#
# Se define como un unico bloque de SQL ejecutado con executescript() en
# init_db(). Cada CREATE TABLE usa "IF NOT EXISTS" para que llamar a
# init_db() en cada arranque de Luxion sea seguro y no falle si las
# tablas ya existen de una ejecucion anterior.
#
# El detalle de que hace cada columna esta explicado a fondo en la
# seccion 3 del plan arquitectonico (luxion_plan_arquitectonico.txt); acá
# se deja solo un resumen breve en comentarios para no duplicar todo ese
# documento, pero SI se explica cualquier decision que no sea obvia
# leyendo unicamente el SQL.

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    hotkey TEXT UNIQUE,
    close_unmatched_windows INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    launch_order INTEGER NOT NULL,
    wm_class TEXT NOT NULL,
    instance_index INTEGER NOT NULL DEFAULT 0,
    launch_command TEXT NOT NULL,
    is_floating INTEGER NOT NULL DEFAULT 0,
    geom_x INTEGER,
    geom_y INTEGER,
    geom_w INTEGER,
    geom_h INTEGER,
    UNIQUE(workspace_id, wm_class, instance_index)
);

CREATE TABLE IF NOT EXISTS desktop_state (
    bspwm_desktop_name TEXT PRIMARY KEY,
    active_workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    loaded_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Conexion
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """
    Abre una conexion nueva a la base de datos con la configuracion
    correcta ya aplicada:

      - journal_mode=WAL: permite que un proceso escriba (por ejemplo la
        GUI guardando un workspace) mientras otro proceso lee (por
        ejemplo el CLI leyendo settings al arrancar) sin bloquearse
        mutuamente. Necesario porque la GUI y el CLI disparado por sxhkd
        son procesos de sistema operativo independientes que pueden
        tocar el mismo archivo .db al mismo tiempo.

      - foreign_keys=ON: SQLite trae esta verificacion DESACTIVADA por
        defecto en cada conexion nueva, sin importar como se creo la
        tabla. Sin esta linea, las clausulas "ON DELETE CASCADE" (en
        workspace_apps) y "ON DELETE SET NULL" (en desktop_state) NO se
        aplicarian, y borrar un workspace dejaria filas huerfanas en
        workspace_apps.

      - row_factory=sqlite3.Row: permite acceder a las columnas del
        resultado por nombre (row["name"]) ademas de por indice, lo cual
        hace el resto del codigo mucho mas legible y menos fragil ante
        cambios de orden de columnas.

    `timeout=5.0` le da a SQLite hasta 5 segundos para reintentar una
    escritura si encuentra el archivo bloqueado por otra conexion, antes
    de lanzar `sqlite3.OperationalError: database is locked`. En
    condiciones normales con WAL esto casi nunca deberia dispararse, pero
    es una red de seguridad barata.
    """
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def connect():
    """
    Context manager de conveniencia: abre una conexion, la entrega al
    bloque `with`, hace commit() si todo salio bien, hace rollback() si
    se lanzo una excepcion dentro del bloque, y SIEMPRE cierra la
    conexion al final.

    Es el patron que usa el resto de las funciones de este archivo para
    no repetir manejo de errores/commit/close en cada una. Ejemplo de
    uso:

        with connect() as conn:
            conn.execute("UPDATE workspaces SET name = ? WHERE id = ?",
                         (new_name, workspace_id))
            # si esta linea no lanza excepcion, se hace commit()
            # automaticamente al salir del bloque `with`.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """
    Crea el esquema completo (si no existe todavia) y siembra los
    valores por defecto de `settings` (si no existen todavia).

    Debe llamarse una vez al arrancar tanto luxion_cli.py como
    luxion_gui.py, ANTES de cualquier otra operacion sobre la base de
    datos. Es idempotente: llamarla en cada arranque del programa no
    causa ningun problema ni sobreescribe datos existentes.
    """
    with connect() as conn:
        conn.executescript(SCHEMA)
        _seed_default_settings(conn)


def _seed_default_settings(conn: sqlite3.Connection) -> None:
    """
    Inserta los valores de config.DEFAULT_SETTINGS que todavia no
    existan en la tabla `settings`.

    Usa "INSERT OR IGNORE" especificamente para NUNCA pisar un valor que
    el usuario ya haya cambiado desde ui/settings_dialog.py en una
    ejecucion anterior. Recibe la conexion como parametro (en vez de
    abrir la suya propia) para poder ejecutarse dentro de la misma
    transaccion que crea las tablas en init_db().
    """
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------


def create_workspace(
    name: Optional[str] = None,
    hotkey: Optional[str] = None,
    close_unmatched_windows: Optional[bool] = None,
) -> int:
    """
    Crea un workspace nuevo y devuelve su id.

    Si `name` es None o esta vacio, se autogenera "Workspace {id}" DESPUES
    de insertar (porque el id solo se conoce una vez hecho el INSERT).
    Como la columna `name` es NOT NULL UNIQUE, no se puede insertar con
    NULL directamente ni con un valor fijo repetible: se usa un nombre
    temporal unico (UUID) como placeholder para la fila, y se reemplaza
    por el nombre definitivo "Workspace {id}" en la misma transaccion.

    Si `name` SI viene con contenido, se garantiza que sea unico llamando
    a ensure_unique_name() (le agrega un sufijo numerico si ya existe
    otro workspace con ese nombre exacto).

    `close_unmatched_windows` acepta None (usar el ajuste global, ver
    core/config.py), True (forzar cierre) o False (forzar que no se
    cierre nada) — se guarda tal cual en la columna del mismo nombre.
    """
    with connect() as conn:
        clean_name = name.strip() if name else ""
        if clean_name:
            final_name = _ensure_unique_name(conn, clean_name, exclude_id=None)
        else:
            # Placeholder temporal, garantizado unico por ser un UUID.
            # Se reemplaza abajo apenas se conoce el id real.
            final_name = f"__pending_new_workspace_{uuid.uuid4().hex}"

        close_value = None if close_unmatched_windows is None else int(bool(close_unmatched_windows))

        cursor = conn.execute(
            """
            INSERT INTO workspaces (name, hotkey, close_unmatched_windows)
            VALUES (?, ?, ?)
            """,
            (final_name, hotkey, close_value),
        )
        workspace_id = cursor.lastrowid

        if not clean_name:
            default_name = f"Workspace {workspace_id}"
            conn.execute(
                "UPDATE workspaces SET name = ? WHERE id = ?",
                (default_name, workspace_id),
            )

        return workspace_id


def get_workspace(workspace_id: int) -> Optional[sqlite3.Row]:
    """Devuelve la fila del workspace, o None si no existe."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()


def get_workspace_by_name(name: str) -> Optional[sqlite3.Row]:
    """Devuelve la fila del workspace con ese nombre exacto, o None."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM workspaces WHERE name = ?", (name,)
        ).fetchone()


def list_workspaces(order_by: str = "name") -> list[sqlite3.Row]:
    """
    Devuelve todos los workspaces.

    `order_by`:
      - "name"       (por defecto) orden alfabetico, el mas predecible
                      para una lista en la GUI.
      - "updated_at" mas recientemente modificado primero, util si se
                      quiere mostrar "usado recientemente" en la GUI.

    Cualquier otro valor cae de vuelta a "name" (no se lanza excepcion
    por un parametro invalido en una funcion de solo lectura como esta).
    """
    column = "updated_at DESC" if order_by == "updated_at" else "name ASC"
    with connect() as conn:
        return conn.execute(f"SELECT * FROM workspaces ORDER BY {column}").fetchall()


def get_workspaces_with_hotkey() -> list[sqlite3.Row]:
    """
    Devuelve solo los workspaces que tienen un atajo de teclado asignado
    (hotkey IS NOT NULL). Es la fuente de datos exacta que usa
    core/hotkey_manager.py para reconstruir el bloque gestionado dentro
    de ~/.config/sxhkd/sxhkdrc (ver seccion 4.8 del plan).
    """
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM workspaces WHERE hotkey IS NOT NULL ORDER BY name ASC"
        ).fetchall()


def update_workspace_name(workspace_id: int, new_name: str) -> None:
    """
    Actualiza el nombre de un workspace. NO valida unicidad por si misma
    — se espera que el llamador (tipicamente
    core/workspace_service.py -> rename()) ya haya resuelto un nombre
    unico con ensure_unique_name() antes de llamar a esta funcion. Se
    mantiene asi para que esta funcion siga siendo un UPDATE simple y
    predecible.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE workspaces SET name = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (new_name, workspace_id),
        )


def update_workspace_hotkey(workspace_id: int, hotkey: Optional[str]) -> None:
    """
    Asigna (o quita, si hotkey=None) el atajo de teclado de un workspace.

    Esta funcion SOLO toca la base de datos. NO reescribe sxhkdrc ni
    reinicia sxhkd por si misma — eso es responsabilidad de
    core/hotkey_manager.py, que debe llamarse por separado despues
    (workspace_service.set_hotkey() es quien coordina ambos pasos, ver
    seccion 4.8 del plan).
    """
    with connect() as conn:
        conn.execute(
            "UPDATE workspaces SET hotkey = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (hotkey, workspace_id),
        )


def update_workspace_close_setting(
    workspace_id: int, close_unmatched_windows: Optional[bool]
) -> None:
    """
    Actualiza el override por-workspace del comportamiento de cierre al
    cargar (ver seccion 4.11 del plan). None = usar el ajuste global.
    """
    value = None if close_unmatched_windows is None else int(bool(close_unmatched_windows))
    with connect() as conn:
        conn.execute(
            "UPDATE workspaces SET close_unmatched_windows = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (value, workspace_id),
        )


def touch_workspace_updated_at(workspace_id: int) -> None:
    """
    Actualiza solamente `updated_at` a la hora actual, sin cambiar
    ningun otro campo. Se usa despues de operaciones que modifican datos
    relacionados (como reemplazar las apps de un workspace) pero que no
    pasan por update_workspace_name()/update_workspace_hotkey().
    """
    with connect() as conn:
        conn.execute(
            "UPDATE workspaces SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (workspace_id,),
        )


def delete_workspace(workspace_id: int) -> None:
    """
    Elimina un workspace.

    Gracias a "ON DELETE CASCADE" en workspace_apps.workspace_id y a
    "ON DELETE SET NULL" en desktop_state.active_workspace_id (ambas
    solo activas porque get_connection() siempre ejecuta
    "PRAGMA foreign_keys = ON"), un solo DELETE aqui es suficiente: no
    hace falta borrar manualmente las filas de workspace_apps ni limpiar
    desktop_state por separado.

    Esta funcion NO toca sxhkdrc. Si el workspace eliminado tenia un
    atajo asignado, es responsabilidad del llamador
    (core/workspace_service.py -> delete()) invocar despues a
    core/hotkey_manager.py para resincronizar sxhkdrc (ver seccion 4.14
    del plan).
    """
    with connect() as conn:
        conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))


def ensure_unique_name(candidate_name: str, exclude_id: Optional[int] = None) -> str:
    """
    Version publica (abre su propia conexion) de _ensure_unique_name().
    Ver esa funcion para el detalle del algoritmo.
    """
    with connect() as conn:
        return _ensure_unique_name(conn, candidate_name, exclude_id)


def _ensure_unique_name(
    conn: sqlite3.Connection, candidate_name: str, exclude_id: Optional[int]
) -> str:
    """
    Dado un nombre candidato, devuelve un nombre garantizado unico dentro
    de la tabla workspaces:

      - Si `candidate_name` no esta en uso (o solo lo usa el propio
        workspace que se esta editando, via `exclude_id`), se devuelve
        tal cual.
      - Si ya esta en uso por OTRO workspace, se le agrega un sufijo
        numerico incremental: "Tesis", "Tesis (2)", "Tesis (3)", etc.,
        hasta encontrar uno libre.

    Recibe la conexion como parametro (en vez de abrir la suya propia)
    para poder llamarse desde dentro de otras transacciones (por ejemplo
    create_workspace(), que la usa antes de hacer el INSERT).

    exclude_id se usa cuando se esta RENOMBRANDO un workspace existente:
    si el usuario simplemente vuelve a guardar el mismo nombre que ya
    tenia, no debe considerarse una colision consigo mismo.
    """
    query = "SELECT id FROM workspaces WHERE name = ?"
    params: list = [candidate_name]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)

    if conn.execute(query, params).fetchone() is None:
        return candidate_name

    suffix = 2
    while True:
        attempt = f"{candidate_name} ({suffix})"
        query2 = "SELECT id FROM workspaces WHERE name = ?"
        params2: list = [attempt]
        if exclude_id is not None:
            query2 += " AND id != ?"
            params2.append(exclude_id)
        if conn.execute(query2, params2).fetchone() is None:
            return attempt
        suffix += 1


# ---------------------------------------------------------------------------
# workspace_apps
# ---------------------------------------------------------------------------


def replace_workspace_apps(workspace_id: int, rows: Iterable[dict]) -> None:
    """
    Reemplaza POR COMPLETO las apps guardadas de un workspace.

    Semantica de "guardar" (ver seccion 4.6 del plan): guardar un
    workspace que ya existia NO acumula historico, siempre representa
    una foto nueva y completa del estado actual. Por eso esta funcion
    hace DELETE de todas las filas previas de ese workspace_id y despues
    INSERT de las nuevas, dentro de la MISMA transaccion (si el INSERT
    fallara a mitad de camino, el rollback automatico de connect()
    revierte tambien el DELETE — nunca se queda el workspace con las
    apps viejas borradas pero las nuevas incompletas).

    Cada elemento de `rows` es un dict con estas claves:
        launch_order     (int, obligatorio)
        wm_class          (str, obligatorio)
        instance_index    (int, opcional, default 0)
        launch_command    (str, obligatorio)
        is_floating       (bool/int, opcional, default False/0)
        geom_x, geom_y, geom_w, geom_h  (int o None, opcionales)

    Al final tambien actualiza workspaces.updated_at, para no requerir
    una llamada separada a touch_workspace_updated_at() desde el
    llamador.
    """
    with connect() as conn:
        conn.execute(
            "DELETE FROM workspace_apps WHERE workspace_id = ?", (workspace_id,)
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO workspace_apps (
                    workspace_id, launch_order, wm_class, instance_index,
                    launch_command, is_floating,
                    geom_x, geom_y, geom_w, geom_h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    row["launch_order"],
                    row["wm_class"],
                    row.get("instance_index", 0),
                    row["launch_command"],
                    1 if row.get("is_floating") else 0,
                    row.get("geom_x"),
                    row.get("geom_y"),
                    row.get("geom_w"),
                    row.get("geom_h"),
                ),
            )
        conn.execute(
            "UPDATE workspaces SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (workspace_id,),
        )


def get_workspace_apps(workspace_id: int) -> list[sqlite3.Row]:
    """
    Devuelve las apps guardadas de un workspace, ordenadas por
    launch_order ascendente — el mismo orden en que deben lanzarse al
    cargar (ver seccion 4.9 del plan) y en el que tiene sentido
    mostrarlas en la GUI.
    """
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM workspace_apps WHERE workspace_id = ? "
            "ORDER BY launch_order ASC",
            (workspace_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# desktop_state
# ---------------------------------------------------------------------------


def set_desktop_state(desktop_name: str, workspace_id: Optional[int]) -> None:
    """
    Registra que `workspace_id` es el workspace actualmente cargado en
    el desktop de bspwm `desktop_name` (ej. "II"), con la marca de
    tiempo de este momento.

    Usa la sintaxis UPSERT de SQLite ("ON CONFLICT ... DO UPDATE") para
    insertar la fila si es la primera vez que se registra ese desktop, o
    actualizarla si ya existia — sin tener que hacer un SELECT previo
    para decidir cual de las dos operaciones corresponde.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO desktop_state (bspwm_desktop_name, active_workspace_id, loaded_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(bspwm_desktop_name) DO UPDATE SET
                active_workspace_id = excluded.active_workspace_id,
                loaded_at = excluded.loaded_at
            """,
            (desktop_name, workspace_id),
        )


def get_desktop_state(desktop_name: str) -> Optional[sqlite3.Row]:
    """Devuelve el estado registrado de un desktop, o None si nunca se cargo nada ahi."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM desktop_state WHERE bspwm_desktop_name = ?",
            (desktop_name,),
        ).fetchone()


def list_desktop_states() -> list[sqlite3.Row]:
    """
    Devuelve el estado de todos los desktops que Luxion ha tocado alguna
    vez. Pensado para un futuro panel de estado en la GUI (ej. "Desktop
    II: Desarrollo"), no usado todavia por ninguna otra pieza del
    sistema en el MVP.
    """
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM desktop_state ORDER BY bspwm_desktop_name ASC"
        ).fetchall()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def get_setting(key: str) -> Optional[str]:
    """
    Devuelve el valor crudo (string) de un ajuste, o None si la clave no
    existe en la base de datos todavia. La resolucion contra
    DEFAULT_SETTINGS ocurre en core/config.py -> get_str(), no aqui: esta
    funcion es deliberadamente "tonta", solo lee lo que hay en la tabla.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    """
    Guarda o actualiza un ajuste. Usa UPSERT para no tener que
    distinguir entre "es la primera vez que se guarda esta clave" y
    "ya existia y hay que actualizarla".
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_all_settings() -> dict[str, str]:
    """
    Devuelve todos los ajustes actualmente guardados en la base de datos
    como un diccionario simple. Pensado para poblar
    ui/settings_dialog.py de una sola consulta en vez de una por cada
    campo.
    """
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


# ---------------------------------------------------------------------------
# Self-test manual
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ejecutar con: python3 -m core.database
    #
    # Smoke test end-to-end de este modulo, sin depender de ningun otro
    # componente de Luxion (ni bspwm, ni X11, ni la GUI). Crea un
    # workspace de prueba con nombre unico, le agrega un par de apps
    # falsas, las lee de vuelta, prueba el reemplazo completo, y al
    # final limpia todo lo que creo — es seguro correrlo varias veces
    # seguidas sin ir dejando basura acumulada en la base de datos real.

    print("Inicializando base de datos en:", config.DB_PATH)
    init_db()
    print("OK: init_db() completado sin errores.\n")

    print("Ajustes actuales:", get_all_settings())
    print()

    test_name = f"__selftest_{uuid.uuid4().hex[:8]}"
    ws_id = create_workspace(name=test_name)
    print(f"Workspace de prueba creado: id={ws_id}, name={test_name}")

    fetched = get_workspace(ws_id)
    assert fetched is not None and fetched["name"] == test_name
    print("OK: get_workspace() devuelve la fila esperada.")

    replace_workspace_apps(
        ws_id,
        [
            {
                "launch_order": 0,
                "wm_class": "Navigator.firefox",
                "instance_index": 0,
                "launch_command": "firefox",
                "is_floating": False,
            },
            {
                "launch_order": 1,
                "wm_class": "Navigator.firefox",
                "instance_index": 1,
                "launch_command": "firefox --new-window https://example.com",
                "is_floating": False,
            },
            {
                "launch_order": 2,
                "wm_class": "Code.Code",
                "instance_index": 0,
                "launch_command": "code ~/proyecto",
                "is_floating": True,
                "geom_x": 100,
                "geom_y": 50,
                "geom_w": 1200,
                "geom_h": 800,
            },
        ],
    )
    apps = get_workspace_apps(ws_id)
    assert len(apps) == 3
    print(f"OK: replace_workspace_apps() + get_workspace_apps() -> {len(apps)} filas")
    for app in apps:
        print(
            f"    order={app['launch_order']} wm_class={app['wm_class']!r} "
            f"instance={app['instance_index']} floating={bool(app['is_floating'])} "
            f"cmd={app['launch_command']!r}"
        )

    # Probar que re-guardar reemplaza en vez de acumular.
    replace_workspace_apps(
        ws_id,
        [
            {
                "launch_order": 0,
                "wm_class": "Navigator.firefox",
                "instance_index": 0,
                "launch_command": "firefox",
                "is_floating": False,
            }
        ],
    )
    apps_after = get_workspace_apps(ws_id)
    assert len(apps_after) == 1
    print("OK: un segundo replace_workspace_apps() reemplaza, no acumula.\n")

    set_desktop_state("SELFTEST", ws_id)
    state = get_desktop_state("SELFTEST")
    assert state is not None and state["active_workspace_id"] == ws_id
    print("OK: set_desktop_state()/get_desktop_state() funcionan.")

    dup_name = _ensure_unique_name.__wrapped__ if False else None  # placeholder no usado
    unique = ensure_unique_name(test_name)
    assert unique != test_name  # ya existe, debe devolver algo con sufijo
    print(f"OK: ensure_unique_name() ante colision -> {unique!r}")

    # Limpieza: elimina el workspace de prueba y su entrada de desktop_state.
    delete_workspace(ws_id)
    assert get_workspace(ws_id) is None
    with connect() as _conn:
        _conn.execute(
            "DELETE FROM desktop_state WHERE bspwm_desktop_name = 'SELFTEST'"
        )
    print("\nOK: limpieza completa. Self-test de database.py terminado sin errores.")
