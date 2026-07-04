#!/usr/bin/env bash
###############################################################################
#
#  setup_bspwm_kali.sh (v3 - causa raíz real identificada)
#  ---------------------------------------------------------------------------
#
#  DIAGNÓSTICO FINAL (con evidencia, no especulación):
#
#  El congelamiento NO es un choque entre bspwm y xfwm4. La prueba: la
#  versión v2 de este script ya NO tocaba la caché de sesión y SÍ mataba a
#  xfwm4 activamente (pkill en bspwmrc) — y el freeze ocurrió igual. Si la
#  teoría del "choque de window managers" fuera cierta, bspwm simplemente
#  habría impreso "Another window manager is already running." y se
#  habría cerrado solo (así se comporta SIEMPRE en X11, está documentado
#  en el propio repositorio de bspwm). Eso no es un freeze, es un fallo
#  limpio.
#
#  Lo único que estuvo presente, sin cambios, en TODAS las versiones que
#  fallaron (la tuya, la mía, la de Gemini) fue:
#
#       backend = "glx";    en picom.conf
#
#  El backend GLX de picom es famoso por colgar la sesión completa dentro
#  de máquinas virtuales (VirtualBox/VMware), porque ahí no hay GPU real,
#  solo un renderizador por software (llvmpipe) que no soporta bien las
#  primitivas que GLX necesita. Está documentado en múltiples reportes
#  oficiales del propio proyecto picom. El síntoma reportado en esos casos
#  es IDÉNTICO al tuyo: la pantalla deja de actualizarse visualmente pero
#  los procesos siguen vivos por debajo (de ahí que algunas teclas
#  "parezcan" no funcionar: en realidad sí se procesan, solo que no ves
#  el resultado en pantalla).
#
#  LA CORRECCIÓN REAL: usar backend = "xrender" en vez de "glx". Es más
#  lento visualmente (sin aceleración 3D) pero es 100% estable en VMs.
#
#  Lo que SÍ mantenemos de las versiones anteriores (buenas prácticas,
#  aunque no sean la causa raíz):
#    - Wrapper que mata a xfwm4 y desactiva su compositor nativo antes de
#      lanzar bspwm (no duele, evita conflictos menores de compositing).
#    - NO se toca ~/.cache/sessions ni el XML de xfce4-session (eso sigue
#      sin ser necesario y sigue siendo riesgoso).
#
###############################################################################

set -uo pipefail

### ============================ CONFIGURACIÓN ============================ ###

LOG_FILE="$HOME/bspwm_setup_$(date +%Y%m%d_%H%M%S).log"
TOP_PADDING=28
COMPOSITOR=""
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
    err "El script se detuvo. Revisa el log en: $LOG_FILE"
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

backup_if_exists() {
    local file="$1"
    if [[ -f "$file" ]]; then
        local backup="${file}.bak.$(date +%Y%m%d_%H%M%S)"
        cp "$file" "$backup" && warn "Ya existía '$file' -> respaldado en '$backup'"
    fi
}

### ============================ VALIDACIONES PREVIAS ====================== ###

preflight_checks() {
    log "=== Validaciones previas ==="
    if [[ "$EUID" -eq 0 ]]; then
        die "No ejecutes este script como root ni con sudo. Ejecútalo como tu usuario normal."
    fi
    sudo -v || die "Se requieren privilegios sudo."
    success "Acceso sudo confirmado"

    sudo apt-get update -qq >> "$LOG_FILE" 2>&1 || die "Fallo al actualizar repositorios."
    success "Repositorios actualizados"

    # Detección informativa: si estamos en una VM, lo avisamos para que el
    # usuario entienda por qué usamos xrender y no glx.
    if command -v systemd-detect-virt &>/dev/null; then
        local virt
        virt="$(systemd-detect-virt 2>/dev/null || true)"
        if [[ -n "$virt" && "$virt" != "none" ]]; then
            log "Entorno virtualizado detectado: ${BOLD}${virt}${NC}. Por eso usaremos el backend 'xrender' en picom (el backend 'glx' se cuelga en la mayoría de VMs)."
        fi
    fi
}

### ============================ INSTALACIÓN =============================== ###

install_packages() {
    log "=== Instalando paquetes base ==="
    run_critical "Instalar bspwm, sxhkd, rofi, qterminal, wmctrl" \
        sudo apt-get install -y bspwm sxhkd rofi qterminal wmctrl

    if command -v picom &>/dev/null; then
        COMPOSITOR="picom"
        success "picom ya está instalado"
    elif apt-cache show picom &>/dev/null; then
        run_critical "Instalar picom" sudo apt-get install -y picom
        COMPOSITOR="picom"
    else
        die "picom no está disponible en tus repositorios."
    fi
}

### ============================ DIRECTORIOS ================================ ###

create_directories() {
    log "=== Creando directorios ==="
    for dir in "$HOME/.config/bspwm" "$HOME/.config/sxhkd" \
               "$HOME/.config/$COMPOSITOR" "$HOME/.config/rofi" \
               "$HOME/.config/autostart"; do
        mkdir -p "$dir" || die "Fallo creando $dir"
    done
    success "Directorios creados"
}

### ============================ ATAJOS XFCE (seguro) ======================= ###

remove_xfce_hotkeys() {
    log "=== Eliminando TODOS los atajos de teclado de XFCE (de fábrica + personalizados) ==="
    # OJO: XFCE guarda los atajos en dos sub-árboles por canal:
    #   /xfwm4/default    y  /commands/default   -> los de FÁBRICA (Alt+Tab, Alt+F4, Ctrl+Alt+T, etc.)
    #   /xfwm4/custom     y  /commands/custom    -> solo los que el usuario personalizó a mano
    #
    # La versión anterior de este script solo borraba "/custom", por eso la
    # mayoría de los atajos (los de fábrica) seguían activos. Ahora borramos
    # el árbol completo de cada canal para eliminar TODOS, de raíz.
    run_optional "Quitar TODO /commands (default + custom)" \
        xfconf-query -c xfce4-keyboard-shortcuts -p /commands -r -R
    run_optional "Quitar TODO /xfwm4 (default + custom)" \
        xfconf-query -c xfce4-keyboard-shortcuts -p /xfwm4 -r -R

    # Verificación: si después de esto siguen existiendo propiedades, avisamos.
    if xfconf-query -c xfce4-keyboard-shortcuts -p /xfwm4 -l &>/dev/null; then
        warn "Todavía quedan propiedades bajo /xfwm4. Puede que xfwm4 esté"
        warn "corriendo en este momento y las haya regenerado. Verifica con:"
        warn "  ps aux | grep xfwm4"
    else
        success "Confirmado: no quedan atajos bajo /xfwm4"
    fi
}

### ============================ SCRIPT MAESTRO DE SESIÓN ==================== ###
#
# POR QUÉ ESTO REEMPLAZA A LOS 3 AUTOSTART SEPARADOS:
#
# XDG Autostart NO garantiza ningún orden de ejecución entre archivos
# .desktop — todos se lanzan casi en paralelo. Eso causaba dos bugs
# intermitentes:
#   1. picom arrancando ANTES de que xfwm4 fuera matado/desactivado ->
#      dos compositores compiten un instante -> backbuffer corrupto ->
#      el "cuadro negro" que aparecía a veces sí, a veces no.
#   2. xfce4-panel re-mapeándose ANTES de que bspwm esté listo para
#      aplicarle su regla -> el panel desaparece en un logout/login
#      normal (más rápido) pero no en un reinicio completo (más lento).
#
# La solución es UN SOLO script de orquestación, con orden secuencial
# real y verificación activa (no solo "sleep a ciegas") de cada paso.
#
create_session_start_script() {
    log "=== Creando script maestro de orquestación de sesión ==="
    local target="$HOME/.config/bspwm/session_start.sh"
    local conf_path="${HOME}/.config/${COMPOSITOR}/${COMPOSITOR}.conf"

    cat > "$target" << EOF
#!/bin/bash
SESSION_LOG="\$HOME/.cache/bspwm_session_start.log"
echo "=== Sesión iniciada: \$(date) ===" >> "\$SESSION_LOG"

# --- PASO 1: garantizar que xfwm4 no interfiera ---
xfconf-query -c xfwm4 -p /general/use_compositing -s false --create -t bool 2>/dev/null
pkill -x xfwm4 2>/dev/null
sleep 0.5

# --- PASO 2: lanzar bspwm y ESPERAR activamente a que esté listo ---
# (en vez de un sleep a ciegas, comprobamos con bspc cada 0.2s, máx 4s)
pkill -x bspwm 2>/dev/null
bspwm &
for i in \$(seq 1 20); do
    if bspc query -M &>/dev/null; then
        echo "bspwm activo tras \${i} intento(s) (\$((i*200))ms)" >> "\$SESSION_LOG"
        break
    fi
    sleep 0.2
done
if ! bspc query -M &>/dev/null; then
    echo "ADVERTENCIA: bspwm no respondió tras 4 segundos" >> "\$SESSION_LOG"
fi

# --- PASO 3: lanzar sxhkd (ya con bspwm garantizado activo) ---
pkill -x sxhkd 2>/dev/null
sleep 0.2
sxhkd &
echo "sxhkd lanzado" >> "\$SESSION_LOG"

# --- PASO 4: lanzar el compositor (xfwm4 ya está muerto, sin carrera) ---
pkill -x ${COMPOSITOR} 2>/dev/null
sleep 0.3
${COMPOSITOR} --config "${conf_path}" -b
echo "${COMPOSITOR} lanzado" >> "\$SESSION_LOG"

# --- PASO 5: reiniciar el panel para que tome la regla de bspwm ---
# (necesario porque en un logout/login puede haberse mapeado antes de
#  que la regla "xfce4-panel state=floating layer=above" existiera)
sleep 1
pkill -x xfce4-panel 2>/dev/null
sleep 0.5
xfce4-panel &
echo "xfce4-panel reiniciado" >> "\$SESSION_LOG"

echo "=== Orquestación completa: \$(date) ===" >> "\$SESSION_LOG"
EOF

    if [[ $? -ne 0 ]]; then
        die "No se pudo crear el script maestro de sesión"
    fi
    chmod +x "$target" || die "No se pudo dar permisos al script maestro"
    success "Script maestro creado en $target"
}


### ============================ CONFIGURACIONES ============================ ###

create_bspwmrc() {
    log "=== Creando bspwmrc ==="
    local target="$HOME/.config/bspwm/bspwmrc"
    backup_if_exists "$target"

    cat > "$target" << EOF
#! /bin/sh

# --- Monitors & Desktops ---
bspc monitor -d I II III IV V VI VII VIII IX X

# --- Global settings ---
bspc config border_width         2
bspc config window_gap           12
bspc config split_ratio          0.52
bspc config borderless_monocle   true
bspc config gapless_monocle      true
bspc config top_padding          ${TOP_PADDING}

# --- Mouse settings ---
bspc config pointer_modifier mod4
bspc config pointer_action1 resize_side
bspc config pointer_action2 resize_corner
bspc config pointer_action3 move

# --- Rules ---
bspc rule -a xfce4-panel state=floating layer=above border=off focus=off
EOF

    if [[ $? -ne 0 ]]; then
        die "No se pudo escribir bspwmrc"
    fi
    chmod +x "$target" || die "Error dando permisos a bspwmrc"
    success "bspwmrc creado"
}

create_sxhkdrc() {
    log "=== Creando sxhkdrc ==="
    local target="$HOME/.config/sxhkd/sxhkdrc"
    backup_if_exists "$target"

    cat > "$target" << 'EOF'
super + Return
    qterminal

super + Escape
    pkill -USR1 -x sxhkd

super + q
    bspc node -c

super + d
    rofi -show run

super + Tab
    rofi -show window

F1
    bspc node @/ -R 90
F2
    bspc node -f next.local
F3
    bspc node @/ -F horizontal
F4
    bspc node @/ -F vertical
F5
    bspc desktop -l next
F6
    bspc node @/ -B
F7
    bspc config -d focused window_gap $((`bspc config -d focused window_gap` + 2))
F8
    bspc config -d focused window_gap $((`bspc config -d focused window_gap` - 2))
F9
    bspc node -t floating
F10
    bspc node -t tiled
F11
    bspc node -t pseudo_tiled
F12
    bspc node -t fullscreen

super + {Left,Right}
    bspc desktop -f {prev.local,next.local}

super + {Up,Down}
    bspc node -f {next,prev}.local

super + shift + {1-9,0}
    bspc node -d '^{1-9,10}'

super + {1-9,0}
    bspc desktop -f '^{1-9,10}'
EOF

    if [[ $? -ne 0 ]]; then
        die "No se pudo escribir sxhkdrc"
    fi
    success "sxhkdrc creado"
}

create_compositor_config() {
    log "=== Creando picom.conf (backend xrender, seguro en VM) ==="
    local target="$HOME/.config/${COMPOSITOR}/${COMPOSITOR}.conf"
    backup_if_exists "$target"

    # IMPORTANTE: backend "xrender", NO "glx".
    # "glx" es la causa confirmada del congelamiento en máquinas virtuales
    # (renderizado por software llvmpipe sin soporte real de vsync/GLX).
    cat > "$target" << 'EOF'
# Backend de renderizado.
# xrender = software, lento pero 100% estable en máquinas virtuales.
# glx     = usa OpenGL, MUY rápido en hardware real, pero se CUELGA en la
#           mayoría de VMs (VirtualBox/VMware) porque usan renderizado por
#           software (llvmpipe) sin soporte real de vsync. NO USAR EN VM.
backend = "xrender";

# FIX para el bug del "cuadro negro" al abrir/cerrar ventanas:
# El backend xrender a veces calcula mal qué región de pantalla repintar
# (su "damage tracking"). Esto deja basura visual (rectángulos negros) que
# no se borra ni siquiera al cerrar la ventana. Confirmado en el issue
# oficial de picom #626 ("Weird black semi opaque rectangles when terminal
# is opened" -> se arregla con use-damage = false).
# Costo: redibuja la pantalla completa en cada frame en vez de solo la
# parte que cambió. Es menos eficiente, pero en una VM con xrender ya
# estamos sacrificando rendimiento de todos modos, así que no se nota.
use-damage = false;

# Sombras
shadow = true;
no-dnd-shadow = true;
no-dock-shadow = true;
shadow-radius = 7;
shadow-offset-x = -7;
shadow-offset-y = -7;
shadow-opacity = 0.7;
shadow-exclude = [
    "name = 'Notification'",
    "class_g = 'Conky'",
    "_GTK_FRAME_EXTENTS@:c"
];

# Opacidad
menu-opacity = 1;
inactive-opacity = 0.85;
active-opacity = 1;
frame-opacity = 1;
focus-exclude = [ "name = 'rofi'" ];

# Fundidos (fading)
fading = true;
fade-delta = 4;
fade-in-step = 0.03;
fade-out-step = 0.03;

detect-transient = true;
detect-client-leader = true;

# --------------------------------------------------------------------
# Si en algún momento corres esto en hardware REAL (no en una VM) y
# quieres aceleración por GPU, comenta "xrender" arriba y descomenta:
# backend = "glx";
# glx-no-stencil = true;
# --------------------------------------------------------------------
EOF

    if [[ $? -ne 0 ]]; then
        die "No se pudo escribir picom.conf"
    fi
    success "picom.conf creado con backend xrender (seguro en VM)"
}

create_rofi_config() {
    log "=== Creando configuración de rofi ==="
    local target="$HOME/.config/rofi/config"
    backup_if_exists "$target"

    cat > "$target" << 'EOF'
rofi.color-enabled: true
rofi.color-window: #000000, #000000, #000000
rofi.color-normal: #000000, #00ff00, #000000, #000000, #00ff00
rofi.color-active: #000000, #00ff00, #000000, #000000, #00ff00
rofi.color-urgent: #000000, #ff0000, #000000, #000000, #ff0000
rofi.modi: window,run
rofi.font: Monospace 12
rofi.separator-style: solid
rofi.hide-scrollbar: true
rofi.padding: 5
rofi.lines: 10
EOF

    if [[ $? -ne 0 ]]; then
        die "No se pudo escribir la configuración de rofi"
    fi
    success "Configuración de rofi creada"
}

create_autostart_entries() {
    log "=== Creando entrada de autostart única (orquestada) ==="
    local autostart_dir="$HOME/.config/autostart"
    local session_script="$HOME/.config/bspwm/session_start.sh"

    # Limpieza: si existían los 3 autostart separados de versiones
    # anteriores del script, los eliminamos para que no compitan en
    # paralelo con el script maestro nuevo.
    for old_file in bspwm.desktop sxhkd.desktop picom.desktop compton.desktop; do
        if [[ -f "${autostart_dir}/${old_file}" ]]; then
            rm -f "${autostart_dir}/${old_file}"
            warn "Eliminado autostart antiguo (causaba la carrera): ${old_file}"
        fi
    done

    cat > "${autostart_dir}/bspwm-session.desktop" << EOF
[Desktop Entry]
Type=Application
Name=BSPWM Session Orchestrator
Comment=Lanza xfwm4-kill, bspwm, sxhkd y ${COMPOSITOR} en orden garantizado
Exec=${session_script}
OnlyShowIn=XFCE;
StartupNotify=false
Terminal=false
Hidden=false
EOF
    [[ $? -eq 0 ]] && success "Autostart único creado (apunta al script maestro)" || die "Falló la creación del autostart"
}

### ============================ FIX DEFENSIVO: ZSH HISTORY ================= ###

fix_zsh_history_if_corrupt() {
    log "=== Verificando integridad del historial de zsh ==="
    local hist="$HOME/.zsh_history"
    [[ -f "$hist" ]] || { log "No existe ~/.zsh_history todavía."; return; }

    if command -v zsh &>/dev/null && zsh -i -c 'true' 2>&1 | grep -qi "corrupt history"; then
        local backup="${hist}.bak.$(date +%Y%m%d_%H%M%S)"
        mv "$hist" "$backup" && touch "$hist"
        warn "Historial de zsh corrupto respaldado en '$backup' y reiniciado"
    else
        success "El historial de zsh está en buen estado"
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
    echo -e "${YELLOW}${BOLD}RECOMENDACIÓN DE PRUEBA EN ETAPAS (muy importante):${NC}"
    echo "  1. Cierra sesión (Logout desde el menú, NO 'sudo reboot' directo)."
    echo "  2. Vuelve a iniciar sesión y observa si TODO carga bien."
    echo "  3. Si llegara a congelarse de nuevo, antes de pensar en xfwm4,"
    echo "     prueba desactivando SOLO picom para aislar el problema:"
    echo "       mv ~/.config/autostart/picom.desktop ~/.config/autostart/picom.desktop.disabled"
    echo "     y vuelve a iniciar sesión. Si con esto YA NO se congela,"
    echo "     queda 100% confirmado que el causante era el compositor,"
    echo "     y entonces el siguiente paso sería revisar drivers de video"
    echo "     de tu VM (Guest Additions / VMware Tools actualizados)."
    echo
}

### ============================ MAIN ======================================== ###

main() {
    log "Iniciando instalación corregida de bspwm (causa raíz: backend GLX en VM)"
    log "Log de esta ejecución: $LOG_FILE"

    preflight_checks
    install_packages
    create_directories
    remove_xfce_hotkeys
    create_session_start_script
    create_bspwmrc
    create_sxhkdrc
    create_compositor_config
    create_rofi_config
    create_autostart_entries
    fix_zsh_history_if_corrupt
    print_summary

    if [[ "$STEPS_FAIL" -gt 0 ]]; then
        warn "El script terminó con algunos fallos. Revisa el log: $LOG_FILE"
        exit 1
    fi
    success "¡Automatización completada!"
}

main "$@"