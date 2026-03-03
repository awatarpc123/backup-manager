# Backup Manager v2.0

Backup tool with PyQt6 GUI for Arch Linux + KDE Plasma.
Rsync incremental backups, tar+zstd archives, Google Drive upload via rclone.

## Features

- **Rsync incremental** — fast backups with hardlinks
- **tar+zstd archives** — compressed single-file backups
- **Google Drive** — upload backups via rclone
- **Auto schedule** — systemd user timers (hourly/daily/weekly/monthly)
- **3-tab GUI** — Backup, History, Settings
- **System tray** — quick backup from tray
- **CLI** — full control from command line
- **Single file** — no bash backend, pure Python

## Installation

```bash
git clone https://github.com/awatarpc123/backup-manager.git
cd backup-manager
chmod +x install.sh
./install.sh
```

## Google Drive Setup

After installation, set up Google Drive (one-time):

```bash
rclone config
```

1. Choose `n` (new remote)
2. Name it `gdrive`
3. Choose `Google Drive`
4. Follow the browser OAuth flow
5. Done — enable Google Drive in the Backup tab

## Usage

### GUI
```bash
backup-manager
```

### CLI
```bash
backup-manager status                          # show status
backup-manager backup home                     # run backup
backup-manager restore /path/to/backup --dest ~/restored
backup-manager schedule home --interval daily  # auto schedule
backup-manager schedule home --remove          # remove schedule
```

## Architecture

```
backup-manager.py (single file)
  ├── BackupEngine class
  │   ├── rsync / tar+zstd (subprocess)
  │   ├── rclone (Google Drive upload)
  │   └── systemd user timers
  ├── PyQt6 GUI (3 tabs)
  └── CLI
```

Config: `~/.config/backup-manager/config.json`

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
