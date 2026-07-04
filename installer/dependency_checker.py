#!/usr/bin/env python3
"""
installer/dependency_checker.py
================================

Verifica que paquetes de sistema (apt) necesita Luxion, e instala los
que falten (ver seccion 4.1 del plan arquitectonico).

Este archivo es DELIBERADAMENTE independiente de Gtk, igual que todo lo
que vive en core/ (aunque installer/ no esta sujeto formalmente a esa
regla de arquitectura, mantenerlo asi permite usarlo de forma standalone
desde la terminal —ver el modo CLI al final del archivo— y probarlo sin
necesitar ningun entorno grafico, exactamente como se hizo con todo
core/). La forma en que se integrara con la GUI mas adelante (ver
seccion 4.1 del plan) es: ui/setup_tab.py usa build_install_command()
para construir el comando de apt, y se lo entrega a una Vte.Terminal
para que el usuario vea el progreso real de apt Y pueda escribir su
contraseña de sudo interactivamente — este archivo NO intenta capturar
ni redirigir esa salida hacia ningun widget por su cuenta.

Dos grupos de paquetes, con proposito distinto:

  LUXION_RUNTIME_PACKAGES  -> lo que Luxion EN SI necesita para correr
                              (la GUI, el CLI, y las herramientas de
                              linea de comandos que core/x11_utils.py y
                              core/bspc_client.py invocan via
                              subprocess: xdotool, wmctrl, ps).

  LIBRARY_BUILD_PACKAGES   -> lo que hace falta UNICAMENTE para
                              compilar libxfce4windowing-for-bspwm (ver
                              installer/install_library.sh). No hacen
                              falta para correr Luxion en si, solo para
                              (re)construir la biblioteca modificada.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Listas de paquetes
# ---------------------------------------------------------------------------

LUXION_RUNTIME_PACKAGES: list[str] = [
    "python3-gi",
    "gir1.2-gtk-3.0",
    "gir1.2-vte-2.91",
    "xdotool",
    "wmctrl",
    "procps",  # provee 'ps', usado por core.x11_utils.get_launch_command
    "bspwm",
    "sxhkd",
    "picom",
]

LIBRARY_BUILD_PACKAGES: list[str] = [
    "git",
    "meson",
    "ninja-build",
    "build-essential",
    "pkg-config",
    "libgtk-3-dev",
    "libwnck-3-dev",
    "libglib2.0-dev",
    "libx11-dev",
    "libxrandr-dev",
    "libwayland-dev",
    "wayland-protocols",
    "gobject-introspection",
    "libgirepository1.0-dev",
]


class DependencyCheckerError(Exception):
    """
    Problemas de ENTORNO genuinos: el sistema no parece ser
    Debian/apt (falta 'dpkg-query'), no un simple "falta este paquete"
    (eso se representa devolviendo una lista, no lanzando una excepcion).
    """


# ---------------------------------------------------------------------------
# Deteccion de paquetes instalados (via dpkg, sin necesitar sudo)
# ---------------------------------------------------------------------------


def is_package_installed(package_name: str) -> bool:
    """
    Verifica si un paquete apt esta REALMENTE instalado, usando
    'dpkg-query -W -f=${Status} <paquete>', que consulta la base de
    datos local de paquetes sin necesitar privilegios de root (a
    diferencia de intentar instalar/desinstalar algo para averiguarlo).

    Se exige que el campo Status contenga literalmente
    "install ok installed" — un paquete que fue removido pero dejo
    configuracion residual (estado "rc", 'dpkg -l' lo marca con "rc" en
    vez de "ii") NO cuenta como instalado para nuestros propositos: sus
    archivos ya no estan en el sistema.

    Lanza DependencyCheckerError si 'dpkg-query' no existe en absoluto
    en el sistema — Luxion esta pensado especificamente para
    Kali/Debian, y sin dpkg-query no hay forma confiable de continuar.
    """
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise DependencyCheckerError(
            "El comando 'dpkg-query' no esta disponible en este sistema. "
            "Luxion esta pensado para distribuciones basadas en "
            "Debian/apt (como Kali Linux)."
        ) from exc

    return result.returncode == 0 and "install ok installed" in result.stdout


def check_missing(packages: list[str]) -> list[str]:
    """
    Devuelve la sublista de `packages` que NO estan instalados
    actualmente, preservando el orden original de la lista de entrada.
    """
    return [pkg for pkg in packages if not is_package_installed(pkg)]


# ---------------------------------------------------------------------------
# Construccion/ejecucion del comando de instalacion
# ---------------------------------------------------------------------------


def build_install_command(missing_packages: list[str]) -> list[str]:
    """
    Arma el comando de instalacion (como lista de argv, SIN pasar por un
    shell) para los paquetes faltantes. Esta funcion NO lo ejecuta —
    solo lo construye. Quien lo ejecuta depende del contexto:

      - ui/setup_tab.py (cuando exista) se lo pasa a una Vte.Terminal
        para que el usuario vea la salida real de apt y pueda escribir
        su contraseña de sudo interactivamente.
      - El modo CLI standalone de este mismo archivo (ver main() mas
        abajo) lo ejecuta directo via install_missing(), heredando la
        terminal actual — util para instalar dependencias desde la
        linea de comandos antes de que exista la GUI.
    """
    return ["sudo", "apt-get", "install", "-y", *missing_packages]


def install_missing(missing_packages: list[str]) -> int:
    """
    Ejecuta 'sudo apt-get install -y <paquetes>' SIN capturar
    stdout/stderr (se heredan directo de la terminal actual): esto es
    intencional, para que el usuario vea el progreso real de apt (barras
    de descarga, etc.) y pueda escribir su contraseña de sudo de forma
    interactiva si hace falta — capturar la salida la ocultaria.

    Si `missing_packages` esta vacia, no ejecuta nada y devuelve 0
    inmediatamente (no hay nada que instalar, no tiene sentido invocar
    apt para una lista vacia de paquetes).

    Devuelve el returncode real del proceso de apt (0 = exito).

    NOTA: deliberadamente NO se establece DEBIAN_FRONTEND=noninteractive.
    Ninguno de los paquetes en LUXION_RUNTIME_PACKAGES ni
    LIBRARY_BUILD_PACKAGES tiene normalmente prompts de debconf
    interactivos, asi que no hace falta suprimirlos — y si algun paquete
    futuro SI llegara a tener uno, es preferible que el usuario lo vea y
    pueda responderlo, en vez de que apt elija un valor por defecto en
    silencio o falle de forma confusa.
    """
    if not missing_packages:
        return 0

    command = build_install_command(missing_packages)
    result = subprocess.run(command)
    return result.returncode


# ---------------------------------------------------------------------------
# Modo CLI standalone
# ---------------------------------------------------------------------------


def check_and_report(packages: list[str], label: str) -> list[str]:
    """
    Revisa `packages`, imprime un resumen legible por humanos (pensado
    para el modo CLI de mas abajo), y devuelve la lista de faltantes —
    igual que check_missing(), solo que ademas imprime.
    """
    missing = check_missing(packages)
    if missing:
        print(f"{label}: faltan {len(missing)} de {len(packages)} paquete(s):")
        for pkg in missing:
            print(f"  - {pkg}")
    else:
        print(f"{label}: los {len(packages)} paquetes ya estan instalados.")
    return missing


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dependency_checker",
        description="Verifica/instala las dependencias de sistema de Luxion.",
    )
    parser.add_argument(
        "--scope",
        choices=["runtime", "build", "all"],
        default="all",
        help="Que grupo de dependencias revisar: 'runtime' (Luxion en si), "
        "'build' (compilar la biblioteca modificada), o 'all' (ambos, por defecto).",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Instalar automaticamente los paquetes faltantes (pide contraseña de sudo).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    groups: list[tuple[str, list[str]]] = []
    if args.scope in ("runtime", "all"):
        groups.append(("Dependencias de Luxion", LUXION_RUNTIME_PACKAGES))
    if args.scope in ("build", "all"):
        groups.append(("Dependencias de compilacion de la biblioteca", LIBRARY_BUILD_PACKAGES))

    all_missing: list[str] = []
    for label, packages in groups:
        all_missing.extend(check_and_report(packages, label))

    if not all_missing:
        print("\nNo falta ningun paquete.")
        return 0

    if not args.install:
        print(
            f"\nFaltan {len(all_missing)} paquete(s) en total. "
            "Ejecuta de nuevo con --install para instalarlos, o hazlo manualmente con:"
        )
        print("  " + " ".join(build_install_command(all_missing)))
        return 1

    print(f"\nInstalando {len(all_missing)} paquete(s) faltante(s)...")
    returncode = install_missing(all_missing)
    if returncode != 0:
        print("La instalacion de paquetes fallo. Revisa la salida de apt arriba.", file=sys.stderr)
        return 1

    print("Instalacion completada.")
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__" and len(sys.argv) == 1:
    # Sin argumentos: correr el self-test (mismo criterio que
    # luxion_cli.py, ya que este archivo tampoco es parte del paquete
    # core/ y por lo tanto no se invoca con 'python3 -m').
    #
    # A diferencia de los modulos de core/, aqui NO hace falta mockear
    # dpkg — este sandbox de desarrollo SI tiene dpkg-query real (es un
    # contenedor basado en Debian/Ubuntu), asi que is_package_installed()
    # se prueba contra la base de datos de paquetes REAL de esta
    # maquina, no simulada. Se usan paquetes base casi con certeza
    # presentes (bash, coreutils) y un nombre inventado que casi con
    # certeza NO existe, para las dos ramas de la logica.

    passed = 0
    failed_count = 0

    def check(label: str, condition: bool) -> None:
        global passed, failed_count
        if condition:
            print(f"OK: {label}")
            passed += 1
        else:
            print(f"FALLO: {label}")
            failed_count += 1

    # --- is_package_installed contra el dpkg REAL de este sistema ---
    check("is_package_installed detecta 'bash' como instalado", is_package_installed("bash"))
    check(
        "is_package_installed detecta un paquete inventado como NO instalado",
        not is_package_installed("paquete-que-definitivamente-no-existe-xyz-123"),
    )

    # --- check_missing preserva el orden y filtra correctamente ---
    mixed = ["bash", "paquete-inventado-abc", "coreutils", "otro-paquete-inventado-def"]
    missing = check_missing(mixed)
    check(
        "check_missing devuelve solo los paquetes faltantes, en el orden original",
        missing == ["paquete-inventado-abc", "otro-paquete-inventado-def"],
    )

    # --- build_install_command ---
    cmd = build_install_command(["pkg1", "pkg2"])
    check(
        "build_install_command arma el argv correcto (sin pasar por shell)",
        cmd == ["sudo", "apt-get", "install", "-y", "pkg1", "pkg2"],
    )
    check(
        "build_install_command con lista vacia no incluye paquetes de mas",
        build_install_command([]) == ["sudo", "apt-get", "install", "-y"],
    )

    # --- install_missing con lista vacia: no debe intentar ejecutar nada ---
    import unittest.mock as mock

    with mock.patch("subprocess.run") as mock_run:
        returncode = install_missing([])
        check("install_missing([]) devuelve 0 sin ejecutar ningun comando", returncode == 0)
        check("install_missing([]) nunca llama a subprocess.run", mock_run.call_count == 0)

    # --- install_missing con paquetes: se ejecuta el comando esperado ---
    with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0)) as mock_run_2:
        returncode = install_missing(["pkg1"])
        check("install_missing devuelve el returncode real del proceso", returncode == 0)
        check(
            "install_missing invoca el comando construido por build_install_command",
            mock_run_2.call_args[0][0] == ["sudo", "apt-get", "install", "-y", "pkg1"],
        )

    # --- check_and_report: contenido impreso + valor devuelto consistentes ---
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reported = check_and_report(["bash", "paquete-inventado-xyz-999"], "Prueba")
    output = buf.getvalue()
    check("check_and_report devuelve la lista de faltantes", reported == ["paquete-inventado-xyz-999"])
    check("check_and_report imprime el paquete faltante", "paquete-inventado-xyz-999" in output)
    check("check_and_report NO menciona el paquete que si esta instalado como faltante", "  - bash" not in output)

    # --- main(): modo de solo verificacion (sin --install), con dependencias reales de LUXION_RUNTIME_PACKAGES ---
    # Se reemplaza temporalmente la lista real por una mezcla controlada
    # para no depender de si xdotool/bspwm/etc. estan realmente
    # instalados en ESTE sandbox de desarrollo (no lo estan, es un
    # contenedor sin entorno grafico).
    with mock.patch(f"{__name__}.LUXION_RUNTIME_PACKAGES", ["bash", "paquete-inventado-para-main-test"]), \
         mock.patch(f"{__name__}.LIBRARY_BUILD_PACKAGES", ["coreutils"]):
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code = main(["--scope", "all"])
        check("main() sin --install devuelve 1 si falta algo", code == 1)
        check(
            "main() sin --install sugiere el comando manual de instalacion",
            "sudo apt-get install -y paquete-inventado-para-main-test" in buf2.getvalue(),
        )

        # --scope runtime: NO debe ni mirar LIBRARY_BUILD_PACKAGES
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            main(["--scope", "runtime"])
        check(
            "main() --scope runtime no reporta nada del grupo de compilacion",
            "compilacion" not in buf3.getvalue().lower(),
        )

        # Todo instalado -> exit 0
        with mock.patch(f"{__name__}.LUXION_RUNTIME_PACKAGES", ["bash"]), \
             mock.patch(f"{__name__}.LIBRARY_BUILD_PACKAGES", ["coreutils"]):
            buf4 = io.StringIO()
            with contextlib.redirect_stdout(buf4):
                code = main(["--scope", "all"])
            check("main() devuelve 0 cuando no falta nada", code == 0)

    # --- main() con --install: se ejecuta install_missing con lo faltante ---
    with mock.patch(f"{__name__}.LUXION_RUNTIME_PACKAGES", ["paquete-inventado-para-install-test"]), \
         mock.patch(f"{__name__}.LIBRARY_BUILD_PACKAGES", []), \
         mock.patch(f"{__name__}.install_missing", return_value=0) as mock_install:
        buf5 = io.StringIO()
        with contextlib.redirect_stdout(buf5):
            code = main(["--scope", "all", "--install"])
        check("main() --install devuelve 0 si install_missing tuvo exito", code == 0)
        check(
            "main() --install llama a install_missing con la lista de faltantes correcta",
            mock_install.call_args[0][0] == ["paquete-inventado-para-install-test"],
        )

    with mock.patch(f"{__name__}.LUXION_RUNTIME_PACKAGES", ["paquete-inventado-otra-vez"]), \
         mock.patch(f"{__name__}.LIBRARY_BUILD_PACKAGES", []), \
         mock.patch(f"{__name__}.install_missing", return_value=1):
        code = main(["--scope", "all", "--install"])
        check("main() --install devuelve 1 si install_missing falla", code == 1)

    # --- DependencyCheckerError si dpkg-query no existe ---
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        try:
            is_package_installed("bash")
            check("DependencyCheckerError si falta dpkg-query", False)
        except DependencyCheckerError:
            check("DependencyCheckerError si falta dpkg-query", True)

    print(f"\n{passed} pruebas OK, {failed_count} pruebas fallidas.")
    if failed_count:
        sys.exit(1)

elif __name__ == "__main__":
    sys.exit(main())
