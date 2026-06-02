#!/usr/bin/env bash
# Voron Infinite Heatbed — Interactive Installer
# Handles everything: symlinks, path substitution, config includes, pin assignment.
# Usage: bash install.sh [options]
#   -u / --uninstall     Uninstall the mod

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${HOME}/klipper"
MOONRAKER_DIR="${HOME}/moonraker"
KLIPPER_EXTRAS="${KLIPPER_DIR}/klippy/extras"
MOONRAKER_COMPONENTS="${MOONRAKER_DIR}/moonraker/components"
PRINTER_CFG="${HOME}/printer_data/config/printer.cfg"
MOONRAKER_CFG="${HOME}/printer_data/config/moonraker.conf"

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
while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--uninstall) UNINSTALL=1 ;;
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

update_config_paths() {
    # Replace placeholder /home/biqu with actual HOME
    local config_main="${SCRIPT_DIR}/config/infinite_heatbed.cfg"
    local moonraker_main="${SCRIPT_DIR}/moonraker/infinite_heatbed.conf"

    if [[ -f "$config_main" ]]; then
        sed -i "s|/home/biqu|${HOME}|g" "$config_main"
        info "Updated config paths for $HOME"
    fi
    if [[ -f "$moonraker_main" ]]; then
        sed -i "s|/home/biqu|${HOME}|g" "$moonraker_main"
    fi
}

add_includes_to_config() {
    local include_printer="[include ${HOME}/voron-infinite-heatbed/config/infinite_heatbed.cfg]"
    local include_moonraker="[include ${HOME}/voron-infinite-heatbed/moonraker/infinite_heatbed.conf]"

    # Check if already included
    if ! grep -q "voron-infinite-heatbed" "$PRINTER_CFG"; then
        echo "" >> "$PRINTER_CFG"
        echo "# Voron Infinite Heatbed" >> "$PRINTER_CFG"
        echo "$include_printer" >> "$PRINTER_CFG"
        info "Added include to printer.cfg"
    else
        info "Include already in printer.cfg"
    fi

    if ! grep -q "voron-infinite-heatbed" "$MOONRAKER_CFG"; then
        echo "" >> "$MOONRAKER_CFG"
        echo "# Voron Infinite Heatbed" >> "$MOONRAKER_CFG"
        echo "$include_moonraker" >> "$MOONRAKER_CFG"
        info "Added include to moonraker.conf"
    else
        info "Include already in moonraker.conf"
    fi
}

prompt_stepper_pins() {
    prompt "Configure belt stepper motor pins"
    echo "  (Check your printer.cfg for available pins)"
    echo ""
    read -p "  Step pin [PE2]: " STEP_PIN
    STEP_PIN="${STEP_PIN:-PE2}"

    read -p "  Dir pin [PE3]: " DIR_PIN
    DIR_PIN="${DIR_PIN:-PE3}"

    read -p "  Enable pin [!PE4]: " ENABLE_PIN
    ENABLE_PIN="${ENABLE_PIN:-!PE4}"

    read -p "  Rotation distance in mm [40]: " ROT_DIST
    ROT_DIST="${ROT_DIST:-40}"

    # Update the config file
    local cfg="${SCRIPT_DIR}/config/base/infinite_heatbed.cfg"
    sed -i "s/step_pin: PE2/step_pin: $STEP_PIN/" "$cfg"
    sed -i "s/dir_pin: PE3/dir_pin: $DIR_PIN/" "$cfg"
    sed -i "s/enable_pin: !PE4/enable_pin: $ENABLE_PIN/" "$cfg"
    sed -i "s/rotation_distance: 40/rotation_distance: $ROT_DIST/" "$cfg"

    info "Stepper pins configured"
}

prompt_motor_count() {
    prompt "How many belt drive motors?"
    echo "  1) Single motor (simpler)"
    echo "  2) Dual motors (better belt tracking)"
    read -p "  Choice [1]: " motor_count
    motor_count="${motor_count:-1}"

    local cfg="${SCRIPT_DIR}/config/base/infinite_heatbed.cfg"
    sed -i "s/motor_count: 1/motor_count: $motor_count/" "$cfg"

    if [[ $motor_count -eq 2 ]]; then
        prompt "Configure second motor pins"
        read -p "  Step pin [PE6]: " STEP_PIN2
        STEP_PIN2="${STEP_PIN2:-PE6}"
        read -p "  Dir pin [PA14]: " DIR_PIN2
        DIR_PIN2="${DIR_PIN2:-PA14}"
        read -p "  Enable pin [!PE0]: " ENABLE_PIN2
        ENABLE_PIN2="${ENABLE_PIN2:-!PE0}"

        # Uncomment and update motor 2
        sed -i '/^#\[manual_stepper infinite_heatbed_motor2\]/,/^#accel: 200/s/^#//' "$cfg"
        sed -i "s/step_pin: PE6/step_pin: $STEP_PIN2/" "$cfg"
        sed -i "s/dir_pin: PA14/dir_pin: $DIR_PIN2/" "$cfg"
        sed -i "s/enable_pin: !PE0/enable_pin: $ENABLE_PIN2/" "$cfg"

        info "Dual-motor mode configured"
    fi
}

prompt_sensor_type() {
    prompt "Which ejection sensor?"
    echo "  1) TOF (VL53L0X)"
    echo "  2) Camera"
    echo "  3) Both"
    echo "  4) None (manual eject only)"
    read -p "  Choice [1]: " sensor_choice
    sensor_choice="${sensor_choice:-1}"

    local sensor_type="none"
    [[ $sensor_choice -eq 1 ]] && sensor_type="tof"
    [[ $sensor_choice -eq 2 ]] && sensor_type="camera"
    [[ $sensor_choice -eq 3 ]] && sensor_type="both"

    local cfg="${SCRIPT_DIR}/config/base/infinite_heatbed.cfg"
    sed -i "s/ejection_sensor: tof/ejection_sensor: $sensor_type/" "$cfg"

    info "Sensor type: $sensor_type"
}

prompt_door_type() {
    prompt "Door actuator type?"
    echo "  1) Servo"
    echo "  2) Solenoid"
    echo "  3) Stepper/linear"
    echo "  4) None (manual door)"
    read -p "  Choice [4]: " door_choice
    door_choice="${door_choice:-4}"

    local door_type="none"
    [[ $door_choice -eq 1 ]] && door_type="servo"
    [[ $door_choice -eq 2 ]] && door_type="solenoid"
    [[ $door_choice -eq 3 ]] && door_type="stepper"

    local cfg="${SCRIPT_DIR}/config/base/infinite_heatbed.cfg"
    sed -i "s/door_type: none/door_type: $door_type/" "$cfg"

    if [[ $door_type != "none" ]]; then
        prompt "Configure door actuator pin"
        read -p "  Pin [PB6]: " door_pin
        door_pin="${door_pin:-PB6}"
        sed -i "s/pin: PB6/pin: $door_pin/" "$cfg"
    fi

    info "Door type: $door_type"
}

uninstall() {
    info "Uninstalling Voron Infinite Heatbed..."

    [[ -L "${KLIPPER_EXTRAS}/infinite_heatbed" ]] && rm "${KLIPPER_EXTRAS}/infinite_heatbed"
    [[ -L "${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py" ]] && rm "${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py"

    # Remove includes from config files
    sed -i '/voron-infinite-heatbed/d' "$PRINTER_CFG"
    sed -i '/voron-infinite-heatbed/d' "$MOONRAKER_CFG"

    info "Uninstall complete. Restart Klipper/Moonraker."
}

restart_services() {
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
  2. Go to Mainsail/Fluidd console
  3. Type: HEATBED_STATUS
  4. If successful, the mod is loaded!

${YELLOW}Configuration files:${NC}
  • Hardware pins: ${HOME}/voron-infinite-heatbed/config/base/infinite_heatbed.cfg
  • Tuning params: ${HOME}/voron-infinite-heatbed/config/base/infinite_heatbed_params.cfg
  • Custom hooks: ${HOME}/voron-infinite-heatbed/config/optional/client_macros.cfg

${BLUE}For help, see: ${HOME}/voron-infinite-heatbed/README.md${NC}

EOF
}

main() {
    if [[ $UNINSTALL -eq 1 ]]; then
        check_dirs
        uninstall
        restart_services
    else
        info "Installing Voron Infinite Heatbed..."
        check_dirs
        update_config_paths
        link_klipper
        link_moonraker
        prompt_motor_count
        prompt_stepper_pins
        prompt_sensor_type
        prompt_door_type
        add_includes_to_config
        restart_services
        print_completion_message
    fi
}

main
