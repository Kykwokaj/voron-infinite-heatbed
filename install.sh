#!/usr/bin/env bash
# Voron Infinite Heatbed — Interactive Installer
# Copies configs to printer_data/config for Fluidd visibility.
# Asks only: motor count and sensor type.
# User edits pins via Fluidd after installation.
#
# Usage: bash install.sh [options]
#   -u / --uninstall     Uninstall the mod
#   -w / --web-ui        Only (re)choose the web interface: Mainsail / Fluidd / both

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${HOME}/klipper"
MOONRAKER_DIR="${HOME}/moonraker"
KLIPPER_EXTRAS="${KLIPPER_DIR}/klippy/extras"
MOONRAKER_COMPONENTS="${MOONRAKER_DIR}/moonraker/components"
PRINTER_CFG="${HOME}/printer_data/config/printer.cfg"
MOONRAKER_CFG="${HOME}/printer_data/config/moonraker.conf"
IHB_CONFIG_DIR="${HOME}/printer_data/config/ihb"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[IHB]${NC} $*"; }
prompt()  { echo -e "${BLUE}[IHB]${NC} $*"; }
warn()    { echo -e "${YELLOW}[IHB WARN]${NC} $*"; }
error()   { echo -e "${RED}[IHB ERROR]${NC} $*" >&2; exit 1; }

UNINSTALL=0
WEB_UI_ONLY=0
WEB_UI=""        # mainsail | fluidd | both  (asked during install)
while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--uninstall) UNINSTALL=1 ;;
        -w|--web-ui)    WEB_UI_ONLY=1 ;;
    esac
    shift
done

check_dirs() {
    [[ -d "$KLIPPER_DIR" ]]    || error "Klipper not found at $KLIPPER_DIR"
    [[ -d "$MOONRAKER_DIR" ]]  || error "Moonraker not found at $MOONRAKER_DIR"
    [[ -d "$KLIPPER_EXTRAS" ]] || error "Klipper extras dir not found at $KLIPPER_EXTRAS"
    [[ -d "$MOONRAKER_COMPONENTS" ]] || error "Moonraker components dir not found at $MOONRAKER_COMPONENTS"
    [[ -f "$PRINTER_CFG" ]]     || error "printer.cfg not found at $PRINTER_CFG"
    [[ -f "$MOONRAKER_CFG" ]]   || error "moonraker.conf not found at $MOONRAKER_CFG"
}

link_klipper() {
    local src="${SCRIPT_DIR}/klippy_modules/infinite_heatbed"
    local dst="${KLIPPER_EXTRAS}/infinite_heatbed"
    if [[ -L "$dst" ]]; then
        info "Klipper extra already linked"
    else
        ln -s "$src" "$dst"
        info "Linked Klipper extra"
    fi
}

link_moonraker() {
    local src="${SCRIPT_DIR}/moonraker/infinite_heatbed_server.py"
    local dst="${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py"
    if [[ -L "$dst" ]]; then
        info "Moonraker component already linked"
    else
        ln -s "$src" "$dst"
        info "Linked Moonraker component"
    fi
}

copy_configs() {
    # Create IHB config directory in printer_data/config
    mkdir -p "${IHB_CONFIG_DIR}/base"
    mkdir -p "${IHB_CONFIG_DIR}/optional"

    # Copy config files
    cp "${SCRIPT_DIR}/config/base/infinite_heatbed.cfg" "${IHB_CONFIG_DIR}/base/"
    cp "${SCRIPT_DIR}/config/base/infinite_heatbed_params.cfg" "${IHB_CONFIG_DIR}/base/"
    cp "${SCRIPT_DIR}/config/base/infinite_heatbed_macros.cfg" "${IHB_CONFIG_DIR}/base/"
    cp "${SCRIPT_DIR}/config/optional/client_macros.cfg" "${IHB_CONFIG_DIR}/optional/"
    cp "${SCRIPT_DIR}/config/infinite_heatbed_master.cfg" "${IHB_CONFIG_DIR}/"

    info "Copied config files to ${IHB_CONFIG_DIR}"
}

add_includes_to_config() {
    local include_printer="[include ihb/infinite_heatbed_master.cfg]"
    local include_moonraker="[include ihb/infinite_heatbed.conf]"

    # Add to printer.cfg
    if ! grep -q "voron-infinite-heatbed\|infinite_heatbed" "$PRINTER_CFG"; then
        echo "" >> "$PRINTER_CFG"
        echo "# Voron Infinite Heatbed" >> "$PRINTER_CFG"
        echo "$include_printer" >> "$PRINTER_CFG"
        info "Added include to printer.cfg"
    else
        info "Include already in printer.cfg"
    fi

    # Add to moonraker.conf
    if ! grep -q "voron-infinite-heatbed\|infinite_heatbed" "$MOONRAKER_CFG"; then
        echo "" >> "$MOONRAKER_CFG"
        echo "# Voron Infinite Heatbed" >> "$MOONRAKER_CFG"
        echo "$include_moonraker" >> "$MOONRAKER_CFG"
        info "Added include to moonraker.conf"
    else
        info "Include already in moonraker.conf"
    fi
}

create_moonraker_conf() {
    # Moonraker config: component, job queue, update manager
    local dst="${IHB_CONFIG_DIR}/infinite_heatbed.conf"
    if grep -q "^\[job_queue\]" "$MOONRAKER_CFG"; then
        # moonraker.conf already has [job_queue]: don't define it twice
        sed '/^\[job_queue\]/,/^automatic_transition/d'             "${SCRIPT_DIR}/moonraker/infinite_heatbed.conf" > "$dst"
        warn "moonraker.conf already has [job_queue]. Set 'automatic_transition: False' there."
    else
        cp "${SCRIPT_DIR}/moonraker/infinite_heatbed.conf" "$dst"
    fi
    info "Created Moonraker config in ${dst}"
}

link_web_ui() {
    # Serve the belt bed UI next to Mainsail/Fluidd at http://<printer>/ihb/
    # (the Moonraker component also re-creates these links after web updates)
    local web
    for web in "${HOME}/mainsail" "${HOME}/fluidd"; do
        [[ -d "$web" ]] || continue
        if [[ -L "${web}/ihb" || ! -e "${web}/ihb" ]]; then
            ln -sfn "${SCRIPT_DIR}/ui" "${web}/ihb"
            info "Linked web UI into ${web}/ihb"
        fi
    done

}


add_mainsail_sidebar() {
    # Add a "Belt Bed" entry to Mainsail's sidebar
    local navi="${HOME}/printer_data/config/.theme/navi.json"
    if [[ ! -f "$navi" ]]; then
        mkdir -p "$(dirname "$navi")"
        cat > "$navi" <<'EOF'
[
  {
    "title": "Belt Bed",
    "href": "/ihb/",
    "target": "_self",
    "icon": "M2 12a4 4 0 0 1 4-4h12a4 4 0 0 1 0 8H6a4 4 0 0 1-4-4m4-2a2 2 0 1 0 0 4a2 2 0 0 0 0-4m12 0a2 2 0 1 0 0 4a2 2 0 0 0 0-4",
    "position": 25
  }
]
EOF
        info "Added 'Belt Bed' to the Mainsail sidebar"
    elif ! grep -q '"/ihb/"' "$navi"; then
        warn "Add a link to /ihb/ in ${navi} to show 'Belt Bed' in the Mainsail sidebar."
    fi
}

prompt_web_ui() {
    # First question of the installer: which web interface gets the belt bed UI?
    # The default is guessed from the web folders found on this machine.
    local default=1
    [[ -d "${HOME}/fluidd" && ! -d "${HOME}/mainsail" ]] && default=2
    [[ -d "${HOME}/fluidd" && -d "${HOME}/mainsail" ]] && default=3
    echo ""
    prompt "UI installation"
    echo "  1) Mainsail  (Belt Bed page + sidebar link)"
    echo "  2) Fluidd    (Belt Bed page + belt card on the Fluidd dashboard)"
    echo "  3) Both"
    read -p "  Enter choice [${default}]: " ui_choice
    ui_choice="${ui_choice:-$default}"
    case "$ui_choice" in
        2) WEB_UI="fluidd" ;;
        3) WEB_UI="both" ;;
        *) WEB_UI="mainsail" ;;
    esac
    info "UI: ${WEB_UI}"
}

apply_web_ui() {
    # Write the chosen UI into the Moonraker config (needs create_moonraker_conf first)
    local conf="${IHB_CONFIG_DIR}/infinite_heatbed.conf"
    local theme="mainsail"
    [[ $WEB_UI == "fluidd" ]] && theme="fluidd"
    # UI look, used by the Moonraker component
    if grep -q "^ui_theme:" "$conf"; then
        sed -i "s/^ui_theme:.*/ui_theme: ${theme}/" "$conf"
    else
        sed -i "s/^\[infinite_heatbed_server\]/[infinite_heatbed_server]
ui_theme: ${theme}/" "$conf"
    fi

    # Fluidd: show the compact belt card on the dashboard as an "HTTP page"
    # (iframe) camera. Remove any previous copy first.
    sed -i '/^\[webcam infinite_heatbed\]/,/^$/d' "$conf"
    if [[ $WEB_UI != "mainsail" ]]; then
        cat >> "$conf" <<'EOF'

[webcam infinite_heatbed]
# Belt bed card on the Fluidd dashboard (Cameras panel)
service: iframe
stream_url: /ihb/?embed=1&theme=fluidd
aspect_ratio: 4:3

EOF
        info "Added the belt bed card to the Fluidd dashboard (Cameras panel)"
    fi

    if [[ $WEB_UI != "fluidd" ]]; then
        add_mainsail_sidebar
    fi
    info "Web interface: ${WEB_UI} (UI style: ${theme})"
}

prompt_motor_count() {
    echo ""
    prompt "How many belt drive motors?"
    echo "  1) Single motor (simpler, cheaper)"
    echo "  2) Dual motors (better belt tracking)"
    read -p "  Enter choice [1]: " motor_choice
    motor_choice="${motor_choice:-1}"

    if [[ $motor_choice -eq 2 ]]; then
        # Uncomment motor2 in the copied config
        sed -i '/^#\[manual_stepper infinite_heatbed_motor2\]/,/^#accel: 200/s/^#//' "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"
        info "Motor count: 2 (motor2 enabled)"
    else
        info "Motor count: 1"
    fi
}

prompt_sensor_type() {
    echo ""
    prompt "Ejection sensor (optional)?"
    echo "  1) TOF (VL53L0X) - most reliable"
    echo "  2) Camera detection"
    echo "  3) Both (TOF + Camera)"
    echo "  4) None (manual eject only) [default]"
    read -p "  Enter choice [4]: " sensor_choice
    sensor_choice="${sensor_choice:-4}"

    local sensor_type="none"
    [[ $sensor_choice -eq 1 ]] && sensor_type="tof"
    [[ $sensor_choice -eq 2 ]] && sensor_type="camera"
    [[ $sensor_choice -eq 3 ]] && sensor_type="both"

    sed -i "s/ejection_sensor: tof/ejection_sensor: $sensor_type/" "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"

    # Remove TOF-specific parameters if TOF is not used
    if [[ $sensor_type != "tof" && $sensor_type != "both" ]]; then
        sed -i '/tof_threshold_mm/d' "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"
    fi

    info "Sensor type: $sensor_type"
}

prompt_door_type() {
    echo ""
    prompt "Door opener (optional)?"
    echo "  1) Servo"
    echo "  2) Solenoid"
    echo "  3) Stepper/Linear actuator"
    echo "  4) None (manual door) [default]"
    read -p "  Enter choice [4]: " door_choice
    door_choice="${door_choice:-4}"

    local door_type="none"
    [[ $door_choice -eq 1 ]] && door_type="servo"
    [[ $door_choice -eq 2 ]] && door_type="solenoid"
    [[ $door_choice -eq 3 ]] && door_type="stepper"

    sed -i "s/door_type: none/door_type: $door_type/" "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"

    if [[ $door_type != "none" ]]; then
        # Uncomment door config sections based on type
        if [[ $door_type == "servo" ]]; then
            sed -i '/^#\[servo infinite_heatbed_door_servo\]/,/^#initial_angle: 0/s/^#//' "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"
        elif [[ $door_type == "solenoid" ]]; then
            sed -i '/^#\[output_pin infinite_heatbed_door_solenoid\]/,/^#shutdown_value: 0/s/^#//' "${IHB_CONFIG_DIR}/base/infinite_heatbed.cfg"
        fi
    fi

    info "Door type: $door_type"
}

uninstall() {
    info "Uninstalling Voron Infinite Heatbed..."

    [[ -L "${KLIPPER_EXTRAS}/infinite_heatbed" ]] && rm "${KLIPPER_EXTRAS}/infinite_heatbed"
    [[ -L "${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py" ]] && rm "${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py"
    [[ -L "${HOME}/mainsail/ihb" ]] && rm "${HOME}/mainsail/ihb"
    [[ -L "${HOME}/fluidd/ihb" ]] && rm "${HOME}/fluidd/ihb"
    [[ -d "${IHB_CONFIG_DIR}" ]] && rm -rf "${IHB_CONFIG_DIR}"

    # Remove includes from config files
    sed -i '/voron-infinite-heatbed\|infinite_heatbed/d' "$PRINTER_CFG"
    sed -i '/voron-infinite-heatbed\|infinite_heatbed/d' "$MOONRAKER_CFG"

    info "Uninstall complete. Restart Klipper/Moonraker."
}

restart_services() {
    echo ""
    prompt "Restarting Klipper and Moonraker..."
    if sudo systemctl restart klipper 2>/dev/null; then
        info "Klipper restarted"
    else
        warn "Could not restart klipper (try: sudo systemctl restart klipper)"
    fi

    if sudo systemctl restart moonraker 2>/dev/null; then
        info "Moonraker restarted"
    else
        warn "Could not restart moonraker (try: sudo systemctl restart moonraker)"
    fi
}

print_completion_message() {
    cat <<EOF

${GREEN}✓ Installation complete!${NC}

${BLUE}Next steps:${NC}
  1. Wait 30 seconds for Klipper to restart
  2. Open Fluidd/Mainsail → Config Files
  3. Look for "ihb" folder
  4. Edit "ihb/base/infinite_heatbed.cfg" and set your stepper pins
  5. Return to console and type: HEATBED_STATUS
  6. Open the belt bed UI: http://<printer>/ihb/
     Mainsail: "Belt Bed" in the sidebar. Fluidd: belt card in the Cameras panel.
     Change later with: bash install.sh --web-ui

${YELLOW}Config files now in Fluidd:${NC}
  📁 ihb/
    └── base/
        ├── infinite_heatbed.cfg          (← Edit pins here!)
        ├── infinite_heatbed_params.cfg   (← Tune speeds, margins here)
        └── infinite_heatbed_macros.cfg   (core macros, READ-ONLY)
    └── optional/
        └── client_macros.cfg             (← Add custom hooks here)

${BLUE}For detailed docs, see: ${SCRIPT_DIR}/README.md${NC}

EOF
}

main() {
    if [[ $WEB_UI_ONLY -eq 1 ]]; then
        check_dirs
        mkdir -p "$IHB_CONFIG_DIR"
        if ! grep -q "^\[infinite_heatbed_server\]" "${IHB_CONFIG_DIR}/infinite_heatbed.conf" 2>/dev/null; then
            create_moonraker_conf
        fi
        prompt_web_ui
        link_moonraker
        link_web_ui
        apply_web_ui
        add_includes_to_config
        sudo systemctl restart moonraker 2>/dev/null && info "Moonraker restarted"             || warn "Could not restart moonraker (try: sudo systemctl restart moonraker)"
        return
    fi
    if [[ $UNINSTALL -eq 1 ]]; then
        check_dirs
        uninstall
        restart_services
    else
        info "Installing Voron Infinite Heatbed..."
        check_dirs
        prompt_web_ui
        link_klipper
        link_moonraker
        copy_configs
        create_moonraker_conf
        link_web_ui
        apply_web_ui
        prompt_motor_count
        prompt_sensor_type
        prompt_door_type
        add_includes_to_config
        restart_services
        print_completion_message
    fi
}

main
