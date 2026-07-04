#!/usr/bin/env bash
###############################################################################
#
#  install_library.sh
#  ---------------------------------------------------------------------------
#  Clona/actualiza, compila e instala libxfce4windowing-for-bspwm (la
#  biblioteca modificada para que xfce4-panel entienda bspwm), reemplazando
#  la version de sistema que usa xfce4-panel.
#
#  Pipeline validado durante el desarrollo real de la biblioteca. Cada
#  decision de este script corresponde a un bug REAL encontrado y
#  corregido en ese proceso, documentado en el comentario junto a cada
#  paso — no son precauciones teoricas.
#
###############################################################################

set -uo pipefail

### ============================ CONFIGURACION ============================ ###

LOG_FILE="$HOME/luxion_install_library_$(date +%Y%m%d_%H%M%S).log"

# Repositorio con la biblioteca modificada. Overridable via variable de
# entorno por si en algun momento se quiere apuntar a un fork distinto
# sin tener que editar el script.
REPO_URL="${LUXION_LIBRARY_REPO_URL:-https://github.com/Ariels875/libxfce4windowing-for-bspwm.git}"
INSTALL_DIR="$HOME/libxfce4windowing-for-bspwm"
BUILD_DIR="$INSTALL_DIR/build"

# Simbolo que confirma que el .so INSTALADO realmente contiene el
# soporte de bspwm (el nombre de la clase GObject que se agrego,
# XfwWindowX11Bspwm — ver xfw-window-x11-bspwm.c). Se usa en
# verify_installation() al final del script, con la MISMA tecnica
# (strings | grep) que se uso manualmente durante el desarrollo real
# para diagnosticar cuando el .so instalado no era el que se acababa de
# compilar.
BSPWM_SUPPORT_SYMBOL="XfwWindowX11Bspwm"

STEPS_OK=0
STEPS_WARN=0
STEPS_FAIL=0

### ============================ COLORES / LOG ============================= ###

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()     { echo -e "${BLUE}[INFO]${NC} $*"   | tee -a "$LOG_FILE"; }
success() { echo -e "${GREEN}[OK]${NC} $*"     | tee -a "$LOG_FILE"; STEPS_OK=$((STEPS_OK+1)); }
warn()    { echo -e "${YELLOW}[AVISO]${NC} $*" | tee -a "$LOG_FILE"; STEPS_WARN=$((STEPS_WARN+1)); }
err()     { echo -e "${RED}[ERROR]${NC} $*"    | tee -a "$LOG_FILE" >&2; STEPS_FAIL=$((STEPS_FAIL+1)); }

die() {
    err "$1"
    err "El script se detuvo. Revisa el log completo en: $LOG_FILE"
    exit 1
}

run_critical() {
    local desc="$1"; shift
    log "Ejecutando: $desc"
    if "$@" >> "$LOG_FILE" 2>&1; then
        success "$desc"
    else
        die "Falló (paso crítico): $desc  ->  comando: $*"
    fi
}

run_optional() {
    local desc="$1"; shift
    log "Ejecutando (opcional): $desc"
    if "$@" >> "$LOG_FILE" 2>&1; then
        success "$desc"
    else
        warn "Falló (no crítico, se continúa): $desc  ->  comando: $*"
    fi
}

### ============================ VALIDACIONES PREVIAS ====================== ###

preflight_checks() {
    log "=== Validaciones previas ==="

    if [[ "$EUID" -eq 0 ]]; then
        die "No ejecutes este script como root ni con sudo directo. Se pedirá sudo únicamente para el paso de instalación."
    fi

    # Verificacion LIGERA de las dependencias de compilacion — NO
    # instala nada por si mismo (esa es responsabilidad exclusiva de
    # installer/dependency_checker.py). El objetivo aqui es fallar
    # temprano con un mensaje claro y accionable, en vez de que
    # 'meson setup' truene 40 líneas después con un "command not found"
    # críptico si falta algo básico como git o meson.
    local missing=()
    for cmd in git meson ninja pkg-config; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Faltan herramientas necesarias para compilar: ${missing[*]}. " \
            "Ejecuta primero: python3 installer/dependency_checker.py --scope build --install"
    fi
    success "Herramientas de compilación básicas presentes (git, meson, ninja, pkg-config)"

    if ! command -v xfce4-panel &>/dev/null; then
        die "No se encontró 'xfce4-panel' en el PATH. ¿Está instalado XFCE? Este script necesita" \
            " que xfce4-panel ya esté instalado para poder detectar dónde reemplazar la biblioteca."
    fi
    success "xfce4-panel encontrado en el sistema"
}

### ============================ DETECCION DE LIBDIR ======================== ###
#
# EL BUG REAL QUE ESTO EVITA: durante el desarrollo, un primer intento
# usó 'meson setup build --prefix=/usr' (sin --libdir), lo que por
# defecto instaló la biblioteca en /usr/local/lib/... — una ruta
# DISTINTA a la que xfce4-panel realmente carga
# (/usr/lib/x86_64-linux-gnu/...). El resultado fue "compilé e instalé,
# pero nada cambió", porque literalmente se estaba actualizando un
# archivo que el sistema nunca leía. El diagnóstico y la solución fueron:
#
#     ldd $(which xfce4-panel) | grep windowing
#
# — que muestra la ruta EXACTA que el sistema realmente usa. Esta
# función automatiza exactamente esa misma técnica de diagnóstico, en
# vez de asumir una ruta fija (que además solo sería correcta en
# x86_64, rompiendo el script en cualquier otra arquitectura como ARM64).
#
detect_system_libdir() {
    local xfce_panel_path
    xfce_panel_path="$(command -v xfce4-panel)"

    local lib_line
    lib_line="$(ldd "$xfce_panel_path" 2>/dev/null | grep 'libxfce4windowing-0\.so' | head -1 || true)"

    if [[ -n "$lib_line" ]]; then
        # Formato típico de una línea de ldd:
        #   libxfce4windowing-0.so.0 => /usr/lib/x86_64-linux-gnu/libxfce4windowing-0.so.0 (0x...)
        # El campo 3 (separado por espacios) es la ruta completa.
        local full_path
        full_path="$(echo "$lib_line" | awk '{print $3}')"

        # Si la biblioteca del sistema ya existe y ldd pudo resolverla,
        # $full_path es una ruta real y absoluta (empieza con '/').
        # Si en cambio dice "=> not found", el campo 3 sería la palabra
        # "not" — no una ruta — así que se descarta ese caso aquí.
        if [[ "$full_path" == /* ]]; then
            dirname "$full_path"
            return 0
        fi
    fi

    # Fallback: libxfce4windowing todavía no está instalado en el
    # sistema (poco común, ya que normalmente viene como dependencia de
    # xfce4-panel vía apt), o ldd no pudo resolverlo por algún otro
    # motivo. Se usa el triplet multiarch CANÓNICO de Debian
    # (dpkg-architecture), que es la fuente de verdad real del propio
    # sistema de paquetes — más confiable que mantener a mano una lista
    # de "x86_64 -> ..., arm64 -> ..." que podría quedar incompleta.
    local triplet
    triplet="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || true)"
    if [[ -n "$triplet" ]]; then
        echo "/usr/lib/$triplet"
    else
        echo "/usr/lib"
    fi
}

### ============================ CLONADO / ACTUALIZACION ==================== ###

clone_or_update_repo() {
    log "=== Obteniendo el código fuente de la biblioteca ==="

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "El repositorio ya existe en $INSTALL_DIR, actualizando..."
        run_critical "git pull" git -C "$INSTALL_DIR" pull
    else
        run_critical "Clonar repositorio" git clone "$REPO_URL" "$INSTALL_DIR"
    fi

    run_critical "Actualizar submódulos" git -C "$INSTALL_DIR" submodule update --init --recursive
}

### ============================ BUILD ======================================= ###

configure_and_build() {
    local libdir="$1"

    log "=== Configurando y compilando (prefix=/usr, libdir=$libdir) ==="

    # Se borra el directorio de build por completo (en vez de usar
    # 'meson setup --wipe') para no depender de los distintos casos
    # límite de --wipe (falla si el directorio existe pero tiene
    # contenido parcial de una corrida interrumpida, ver la
    # investigación en la documentación de Meson). Empezar SIEMPRE desde
    # cero es más simple y 100% predecible, al costo de perder el cache
    # de compilación incremental entre corridas — un costo aceptable
    # para un script de instalación que no se ejecuta con frecuencia.
    if [[ -d "$BUILD_DIR" ]]; then
        log "Eliminando build/ previo para garantizar una configuración limpia..."
        rm -rf "$BUILD_DIR"
    fi

    run_critical "meson setup" meson setup "$BUILD_DIR" \
        --prefix=/usr \
        --libdir="$libdir" \
        -Dx11=enabled \
        -C "$INSTALL_DIR"

    run_critical "meson compile" meson compile -C "$BUILD_DIR"
}

### ============================ INSTALACION ================================= ###

install_library() {
    log "=== Instalando la biblioteca compilada ==="

    # Se mata xfce4-panel ANTES de instalar para liberar el archivo .so
    # que tiene cargado en memoria — en Linux, sobrescribir un archivo
    # que un proceso tiene abierto normalmente SÍ funciona (el proceso
    # viejo sigue usando el inodo antiguo hasta que termina), pero matar
    # el panel primero evita cualquier ambigüedad y además fuerza un
    # reinicio limpio que carga la versión nueva de inmediato, en vez de
    # esperar a que el usuario cierre sesión.
    run_optional "Cerrar xfce4-panel para liberar la biblioteca en uso" pkill -x xfce4-panel

    run_critical "sudo meson install" sudo meson install -C "$BUILD_DIR"
    run_critical "sudo ldconfig" sudo ldconfig
}

### ============================ REINICIO DEL PANEL ========================== ###

restart_panel() {
    log "=== Reiniciando xfce4-panel ==="

    # --disable-wm-check: bspwm solo soporta EWMH parcialmente, y la
    # verificación propia de xfce4-panel de "hay un WM registrado en la
    # pantalla" puede fallar de forma no determinista bajo bspwm (bug
    # real encontrado durante el desarrollo). Se salta esa verificación
    # explícitamente.
    #
    # Esto es un paso OPCIONAL (run_optional, no run_critical) porque
    # este script puede ejecutarse fuera de una sesión gráfica activa
    # (por ejemplo, antes de haber iniciado sesión en XFCE por primera
    # vez, o vía SSH sin reenvío de X) — en ese caso no hay ningún
    # DISPLAY al cual conectarse, y eso NO significa que la instalación
    # en sí haya fallado.
    if [[ -z "${DISPLAY:-}" ]]; then
        warn "No hay ninguna sesión gráfica activa (\$DISPLAY vacío); no se reinicia xfce4-panel ahora. Se cargará la biblioteca nueva la próxima vez que inicies sesión."
        return
    fi

    run_optional "Relanzar xfce4-panel con --disable-wm-check" \
        bash -c "nohup xfce4-panel --disable-wm-check >> '$LOG_FILE' 2>&1 & disown"
}

### ============================ VERIFICACION ================================ ###
#
# Automatiza la MISMA verificación manual que se usó durante el
# desarrollo real para confirmar que la instalación efectivamente
# "tomó" (en vez de silenciosamente seguir usando una versión vieja de
# la biblioteca, como pasó la primera vez por el problema de --libdir
# descrito más arriba).
#
verify_installation() {
    log "=== Verificando la instalación ==="

    local libdir="$1"
    local installed_so="$libdir/libxfce4windowing-0.so.0"

    if [[ ! -f "$installed_so" ]]; then
        warn "No se encontró $installed_so tras la instalación. La verificación automática no puede continuar; revisa manualmente."
        return
    fi

    if strings "$installed_so" 2>/dev/null | grep -q "$BSPWM_SUPPORT_SYMBOL"; then
        success "Verificado: la biblioteca instalada en $installed_so contiene el soporte de bspwm (símbolo '$BSPWM_SUPPORT_SYMBOL' encontrado)"
    else
        warn "La biblioteca en $installed_so NO contiene el símbolo '$BSPWM_SUPPORT_SYMBOL'." \
             " La instalación pudo haber ido a un directorio distinto al que realmente usa el sistema." \
             " Verifica manualmente con: ldd \$(which xfce4-panel) | grep windowing"
    fi

    # Confirmación cruzada: ¿el binario xfce4-panel realmente carga la
    # biblioteca desde ESTE directorio, y no desde otro lado?
    local xfce_panel_path
    xfce_panel_path="$(command -v xfce4-panel)"
    local loaded_path
    loaded_path="$(ldd "$xfce_panel_path" 2>/dev/null | grep 'libxfce4windowing-0\.so' | awk '{print $3}' || true)"

    if [[ "$loaded_path" == "$installed_so" ]]; then
        success "Confirmado: xfce4-panel carga la biblioteca exactamente desde donde se instaló ($installed_so)"
    else
        warn "xfce4-panel parece estar cargando la biblioteca desde una ruta distinta a la instalada" \
             " (instalada en: $installed_so — cargada desde: ${loaded_path:-desconocido})." \
             " Ejecuta 'sudo ldconfig' manualmente y reinicia xfce4-panel."
    fi
}

### ============================ RESUMEN FINAL =============================== ###

print_summary() {
    echo
    echo -e "${BOLD}============================================================${NC}"
    echo -e "${BOLD}                     RESUMEN DE EJECUCIÓN                    ${NC}"
    echo -e "${BOLD}============================================================${NC}"
    echo -e "  ${GREEN}Pasos exitosos:${NC}        $STEPS_OK"
    echo -e "  ${YELLOW}Avisos (no críticos):${NC}  $STEPS_WARN"
    echo -e "  ${RED}Fallos:${NC}                 $STEPS_FAIL"
    echo "  Log completo en: $LOG_FILE"
    echo -e "${BOLD}============================================================${NC}"
    echo
}

### ============================ MAIN ======================================== ###

main() {
    log "Iniciando instalación de libxfce4windowing-for-bspwm"
    log "Log de esta ejecución: $LOG_FILE"

    preflight_checks

    local libdir
    libdir="$(detect_system_libdir)"
    log "libdir detectado para este sistema: ${BOLD}${libdir}${NC}"

    clone_or_update_repo
    configure_and_build "$libdir"
    install_library
    restart_panel
    verify_installation "$libdir"
    print_summary

    if [[ "$STEPS_FAIL" -gt 0 ]]; then
        warn "El script terminó con algunos fallos. Revisa el log: $LOG_FILE"
        exit 1
    fi

    success "¡Instalación completada!"
}

main "$@"
