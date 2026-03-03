# Backup Manager

Incremental backup tool with PyQt6 GUI for Arch Linux + KDE Plasma.

Uses **rsync** for incremental backups and **tar+zstd** for compressed archives. Scheduled via **systemd user timers**.

## Features

| Feature | Description |
|---|---|
| **Rsync incremental** | Fast, deduplicating backups with hardlinks |
| **tar+zstd archives** | Compressed single-file backups |
| **Multiple profiles** | Different backup configs for different data |
| **Scheduling** | systemd user timers (hourly/daily/weekly/monthly) |
| **History** | Full backup log with status, size, duration |
| **Restore** | GUI-guided restore from any backup |
| **Exclude patterns** | Skip caches, trash, node_modules, etc. |
| **System tray** | Quick backup from tray menu |
| **CLI** | Full control from command line |

## Installation

```bash
git clone https://github.com/awatarpc123/backup-manager.git
cd backup-manager
chmod +x install.sh
./install.sh
```

## Usage

### GUI
```bash
backup-manager
```

### CLI
```bash
# Show status
backup-manager status

# Run backup
backup-manager backup home

# Schedule daily backups
backup-manager schedule home --interval daily

# Remove schedule
backup-manager schedule home --remove

# Restore
backup-manager restore --source /mnt/backup/home_latest --dest ~/restored
```

## Configuration

Config stored in `~/.config/backup-manager/config.json`.

Edit via GUI (Configure tab) or manually:

```json
{
  "profiles": {
    "home": {
      "name": "Home Directory",
      "sources": ["~/"],
      "destination": "/mnt/backup",
      "type": "rsync",
      "exclude": [".cache/", "node_modules/", ".local/share/Trash/"],
      "keep_versions": 5,
      "schedule": "daily"
    }
  }
}
```

## Architecture

```
backup-manager (PyQt6 GUI / CLI)
       │
       ▼ calls
backup-manager-backend (bash)
       │
       ├─ rsync --archive --delete --link-dest
       ├─ tar --zstd
       └─ systemd user timers
       │
       ▼ stores
~/.config/backup-manager/
  ├── config.json      (profiles & settings)
  ├── history.json     (backup log)
  └── logs/            (detailed rsync/tar output)
```

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
