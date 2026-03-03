#!/bin/bash
set -euo pipefail

echo "=== Backup Manager — Uninstaller ==="
echo ""

# Remove scheduled timers
for timer in "$HOME/.config/systemd/user"/backup-manager-*.timer; do
    [ -f "$timer" ] && {
        name=$(basename "$timer" .timer)
        systemctl --user disable --now "$name" 2>/dev/null || true
        rm -fv "$timer"
        rm -fv "$HOME/.config/systemd/user/${name}.service"
    }
done
systemctl --user daemon-reload 2>/dev/null || true

sudo rm -fv /usr/local/bin/backup-manager
sudo rm -fv /usr/local/bin/backup-manager-backend
rm -fv "$HOME/.local/share/applications/backup-manager.desktop"

echo ""
echo "Uninstalled. Config kept in: ~/.config/backup-manager/"
echo "To remove config: rm -rf ~/.config/backup-manager"
