#!/bin/bash
# Backup Manager — installer for Arch Linux + KDE Plasma
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

echo -e "${BOLD}=== Backup Manager — Installer ===${NC}"
echo ""

if [ "$(id -u)" -eq 0 ]; then
    echo "Do not run as root. The script will use sudo when needed." >&2
    exit 1
fi

# --- Dependencies ---
if ! python3 -c "import PyQt6" 2>/dev/null; then
    info "Installing python-pyqt6..."
    sudo pacman -S --needed --noconfirm python-pyqt6
else
    info "python-pyqt6 already installed"
fi

for pkg in rsync tar zstd; do
    if ! command -v "$pkg" &>/dev/null; then
        info "Installing $pkg..."
        sudo pacman -S --needed --noconfirm "$pkg"
    fi
done

# --- Install files ---
info "Installing backup-manager-backend -> /usr/local/bin/"
sudo install -m 755 "$SCRIPT_DIR/backup-manager-backend.sh" /usr/local/bin/backup-manager-backend

info "Installing backup-manager -> /usr/local/bin/"
sudo install -m 755 "$SCRIPT_DIR/backup-manager.py" /usr/local/bin/backup-manager

# --- Desktop entry ---
info "Installing KDE menu entry..."
install -Dm 644 "$SCRIPT_DIR/backup-manager.desktop" \
    "$HOME/.local/share/applications/backup-manager.desktop"
update-desktop-database "$HOME/.local/share/applications/" 2>/dev/null || true

# --- Initialize config ---
info "Initializing default config..."
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/backup-manager"
/usr/local/bin/backup-manager-backend config-init 2>/dev/null || true

echo ""
echo -e "${GREEN}${BOLD}Installation complete!${NC}"
echo ""
echo "  Usage:"
echo "    GUI:      backup-manager"
echo "    Status:   backup-manager status"
echo "    Backup:   backup-manager backup [profile]"
echo "    Restore:  backup-manager restore --source /path --dest /target"
echo "    Schedule: backup-manager schedule home --interval daily"
echo ""
echo "  Available in KDE menu: System -> Backup Manager"
echo ""
echo "  NEXT STEP: Open the app and configure a destination in the Configure tab."
