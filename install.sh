#!/usr/bin/env bash
# Voron Infinite Heatbed — Installer
# Symlinks the mod's Python code into Klipper and Moonraker directories.
# Run: bash install.sh
# Run with -u to uninstall: bash install.sh -u

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${HOME}/klipper"
MOONRAKER_DIR="${HOME}/moonraker"
KLIPPER_EXTRAS="${KLIPPER_DIR}/klippy/extras"
MOONRAKER_COMPONENTS="${MOONRAKER_DIR}/moonraker/components"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[IHB]${NC} $*"; }
warn()    { echo -e "${YELLOW}[IHB WARN]${NC} $*"; }
error()   { echo -e "${RED}[IHB ERROR]${NC} $*" >&2; exit 1; }

UNINSTALL=0
while getopts "u" opt; do
    case $opt in u) UNINSTALL=1 ;; esac
done

check_dirs() {
    [[ -d "$KLIPPER_DIR" ]]    || error "Klipper not found at $KLIPPER_DIR"
    [[ -d "$MOONRAKER_DIR" ]]  || error "Moonraker not found at $MOONRAKER_DIR"
    [[ -d "$KLIPPER_EXTRAS" ]] || error "Klipper extras dir not found at $KLIPPER_EXTRAS"
    [[ -d "$MOONRAKER_COMPONENTS" ]] || error "Moonraker components dir not found at $MOONRAKER_COMPONENTS"
}

link_klipper() {
    local src="${SCRIPT_DIR}/klippy_modules/infinite_heatbed"
    local dst="${KLIPPER_EXTRAS}/infinite_heatbed"
    if [[ -L "$dst" ]]; then
        info "Klipper extra already linked: $dst"
    else
        ln -s "$src" "$dst"
        info "Linked Klipper extra: $dst -> $src"
    fi
}

link_moonraker() {
    local src="${SCRIPT_DIR}/moonraker/infinite_heatbed_server.py"
    local dst="${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py"
    if [[ -L "$dst" ]]; then
        info "Moonraker component already linked: $dst"
    else
        ln -s "$src" "$dst"
        info "Linked Moonraker component: $dst -> $src"
    fi
}

update_config_paths() {
    # Replace /home/biqu with actual HOME in config files
    # This allows portable config includes
    local config_main="${SCRIPT_DIR}/config/infinite_heatbed.cfg"
    local moonraker_main="${SCRIPT_DIR}/moonraker/infinite_heatbed.conf"

    if [[ -f "$config_main" ]]; then
        sed -i "s|/home/biqu|${HOME}|g" "$config_main"
        info "Updated paths in config/infinite_heatbed.cfg"
    fi
    if [[ -f "$moonraker_main" ]]; then
        sed -i "s|/home/biqu|${HOME}|g" "$moonraker_main"
        info "Updated paths in moonraker/infinite_heatbed.conf"
    fi
}

unlink_klipper() {
    local dst="${KLIPPER_EXTRAS}/infinite_heatbed"
    [[ -L "$dst" ]] && rm "$dst" && info "Removed Klipper extra link: $dst" || true
}

unlink_moonraker() {
    local dst="${MOONRAKER_COMPONENTS}/infinite_heatbed_server.py"
    [[ -L "$dst" ]] && rm "$dst" && info "Removed Moonraker component link: $dst" || true
}

print_printer_cfg_instructions() {
    cat <<EOF

${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${GREEN}Next step: add this line to your printer.cfg${NC}

[include ~/voron-infinite-heatbed/config/infinite_heatbed.cfg]

${GREEN}And add this line to your moonraker.conf${NC}

[include ~/voron-infinite-heatbed/moonraker/infinite_heatbed.conf]

${YELLOW}Then edit config/base/infinite_heatbed.cfg to set your pin numbers.${NC}
${YELLOW}See README.md for full configuration instructions.${NC}
${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

EOF
}

restart_services() {
    info "Restarting Klipper and Moonraker..."
    sudo systemctl restart klipper   || warn "Could not restart klipper — do it manually"
    sudo systemctl restart moonraker || warn "Could not restart moonraker — do it manually"
}

main() {
    if [[ $UNINSTALL -eq 1 ]]; then
        info "Uninstalling Voron Infinite Heatbed..."
        check_dirs
        unlink_klipper
        unlink_moonraker
        restart_services
        info "Uninstall complete."
    else
        info "Installing Voron Infinite Heatbed..."
        check_dirs
        update_config_paths
        link_klipper
        link_moonraker
        print_printer_cfg_instructions
        restart_services
        info "Installation complete!"
    fi
}

main
