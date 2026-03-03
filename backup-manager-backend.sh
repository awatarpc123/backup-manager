#!/bin/bash
# backup-manager-backend.sh — rsync/tar+zstd backup engine
# Handles: backup, restore, schedule, history, config
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/backup-manager"
CONFIG_FILE="$CONFIG_DIR/config.json"
HISTORY_FILE="$CONFIG_DIR/history.json"
LOG_DIR="$CONFIG_DIR/logs"
LOCK_FILE="/tmp/backup-manager.lock"

# --- Helpers ---

ensure_config_dir() {
    mkdir -p "$CONFIG_DIR" "$LOG_DIR"
}

json_get() {
    # json_get <file> <key> [default]
    python3 -c "
import json, sys
try:
    d = json.load(open('$1'))
    keys = '$2'.split('.')
    v = d
    for k in keys:
        v = v[k]
    print(v if not isinstance(v, (list, dict)) else json.dumps(v))
except:
    print('${3:-}')
" 2>/dev/null
}

timestamp() {
    date '+%Y-%m-%d_%H-%M-%S'
}

human_date() {
    date '+%Y-%m-%d %H:%M:%S'
}

filesize_human() {
    numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "${1} bytes"
}

# --- Config management ---

cmd_config_init() {
    ensure_config_dir
    if [ -f "$CONFIG_FILE" ]; then
        echo "Config already exists: $CONFIG_FILE"
        return 0
    fi
    cat > "$CONFIG_FILE" << 'DEFAULTCFG'
{
    "profiles": {
        "home": {
            "name": "Home Directory",
            "sources": ["~/"],
            "destination": "",
            "type": "rsync",
            "exclude": [
                ".cache/",
                ".local/share/Trash/",
                ".thumbnails/",
                "*.tmp",
                "__pycache__/",
                "node_modules/",
                ".steam/",
                "Steam/",
                ".mozilla/firefox/*/storage/",
                "snap/",
                ".local/share/flatpak/"
            ],
            "schedule": "none",
            "keep_versions": 5,
            "compress": false,
            "enabled": true
        }
    },
    "notifications": true
}
DEFAULTCFG
    echo '[]' > "$HISTORY_FILE"
    echo "Config initialized: $CONFIG_FILE"
}

cmd_config_get() {
    if [ ! -f "$CONFIG_FILE" ]; then
        cmd_config_init >/dev/null
    fi
    cat "$CONFIG_FILE"
}

cmd_config_set() {
    # Accepts full JSON on stdin
    ensure_config_dir
    local tmpfile
    tmpfile=$(mktemp)
    cat > "$tmpfile"
    # Validate JSON
    if python3 -c "import json; json.load(open('$tmpfile'))" 2>/dev/null; then
        mv "$tmpfile" "$CONFIG_FILE"
        echo "OK: config saved"
    else
        rm -f "$tmpfile"
        echo "ERROR: invalid JSON" >&2
        return 1
    fi
}

# --- Backup ---

cmd_backup() {
    local profile="${1:-home}"
    ensure_config_dir

    if [ -f "$LOCK_FILE" ]; then
        local lock_pid
        lock_pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$lock_pid" 2>/dev/null; then
            echo "ERROR: Another backup is running (PID $lock_pid)" >&2
            return 1
        else
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT

    if [ ! -f "$CONFIG_FILE" ]; then
        cmd_config_init >/dev/null
    fi

    # Read profile config
    local profile_json
    profile_json=$(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG_FILE'))
p = cfg.get('profiles', {}).get('$profile')
if p:
    print(json.dumps(p))
else:
    print('null')
")
    if [ "$profile_json" = "null" ]; then
        echo "ERROR: Profile '$profile' not found" >&2
        return 1
    fi

    local name sources_json destination backup_type excludes_json compress keep_versions
    name=$(echo "$profile_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
    sources_json=$(echo "$profile_json" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['sources']))")
    destination=$(echo "$profile_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['destination'])")
    backup_type=$(echo "$profile_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('type','rsync'))")
    excludes_json=$(echo "$profile_json" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('exclude',[])))")
    compress=$(echo "$profile_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('compress', False))")
    keep_versions=$(echo "$profile_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('keep_versions', 5))")

    # Expand ~ in destination
    destination="${destination/#\~/$HOME}"

    if [ -z "$destination" ]; then
        echo "ERROR: No destination set for profile '$profile'" >&2
        return 1
    fi

    # Check destination
    if [[ "$destination" != *":"* ]]; then
        # Local destination
        if [ ! -d "$(dirname "$destination")" ]; then
            echo "ERROR: Destination parent directory does not exist: $destination" >&2
            return 1
        fi
        mkdir -p "$destination"
    fi

    local ts
    ts=$(timestamp)
    local log_file="$LOG_DIR/backup_${profile}_${ts}.log"
    local start_time
    start_time=$(date +%s)

    echo "=== Backup: $name ==="
    echo "Profile:     $profile"
    echo "Type:        $backup_type"
    echo "Destination: $destination"
    echo "Started:     $(human_date)"
    echo "Log:         $log_file"
    echo ""

    local status="success"
    local bytes_transferred=0

    if [ "$backup_type" = "archive" ] || [ "$compress" = "True" ]; then
        # tar+zstd archive
        local archive_name="backup_${profile}_${ts}.tar.zst"
        local archive_path="$destination/$archive_name"

        # Build sources list
        local sources_expanded
        sources_expanded=$(echo "$sources_json" | python3 -c "
import json, sys, os
sources = json.load(sys.stdin)
for s in sources:
    print(os.path.expanduser(s))
")
        # Build exclude args
        local exclude_args=""
        exclude_args=$(echo "$excludes_json" | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    print(f'--exclude={e}')
")

        echo "Creating archive: $archive_path"
        if echo "$sources_expanded" | tr '\n' '\0' | \
            xargs -0 tar --create --zstd $exclude_args -f "$archive_path" 2>"$log_file"; then
            bytes_transferred=$(stat -c%s "$archive_path" 2>/dev/null || echo 0)
            echo "Archive created: $(filesize_human "$bytes_transferred")"
        else
            status="failed"
            echo "ERROR: Archive creation failed. See $log_file" >&2
        fi
    else
        # rsync incremental
        local dest_path="$destination/${profile}_latest"
        mkdir -p "$dest_path"

        # Build sources
        local sources_expanded
        sources_expanded=$(echo "$sources_json" | python3 -c "
import json, sys, os
for s in json.load(sys.stdin):
    p = os.path.expanduser(s)
    if not p.endswith('/'):
        p += '/'
    print(p)
")

        # Build exclude args for rsync
        local -a rsync_args=(
            --archive
            --human-readable
            --delete
            --info=progress2
            --stats
        )

        while IFS= read -r excl; do
            [ -n "$excl" ] && rsync_args+=(--exclude="$excl")
        done < <(echo "$excludes_json" | python3 -c "import json,sys;[print(e) for e in json.load(sys.stdin)]")

        # Link-dest for incremental (previous backup)
        local prev_backup="$destination/${profile}_previous"
        if [ -d "$prev_backup" ]; then
            rsync_args+=(--link-dest="$prev_backup")
        fi

        echo "Running rsync..."
        while IFS= read -r src; do
            [ -z "$src" ] && continue
            if [ ! -d "$src" ] && [ ! -f "$src" ]; then
                echo "  Skipping (not found): $src"
                continue
            fi
            echo "  Syncing: $src -> $dest_path"
            if rsync "${rsync_args[@]}" "$src" "$dest_path/" 2>&1 | tee -a "$log_file"; then
                true
            else
                status="failed"
                echo "ERROR: rsync failed for $src" >&2
            fi
        done <<< "$sources_expanded"

        # Rotate: previous -> delete old, latest -> previous
        if [ "$status" = "success" ]; then
            rm -rf "$prev_backup"
            if [ -d "$dest_path" ]; then
                cp -al "$dest_path" "$prev_backup" 2>/dev/null || true
            fi
        fi

        bytes_transferred=$(du -sb "$dest_path" 2>/dev/null | cut -f1 || echo 0)
    fi

    local end_time
    end_time=$(date +%s)
    local duration=$((end_time - start_time))

    echo ""
    echo "Status:   $status"
    echo "Duration: ${duration}s"
    echo "Size:     $(filesize_human "$bytes_transferred")"

    # Record in history
    python3 -c "
import json, os
hist_file = '$HISTORY_FILE'
try:
    history = json.load(open(hist_file))
except:
    history = []
history.append({
    'profile': '$profile',
    'name': '$name',
    'timestamp': '$ts',
    'date': '$(human_date)',
    'status': '$status',
    'duration_s': $duration,
    'bytes': $bytes_transferred,
    'destination': '$destination',
    'type': '$backup_type',
    'log': '$log_file'
})
# Keep last 100 entries
history = history[-100:]
with open(hist_file, 'w') as f:
    json.dump(history, f, indent=2)
"

    # Cleanup old versions
    if [ "$backup_type" = "archive" ] || [ "$compress" = "True" ]; then
        local count
        count=$(ls -1 "$destination"/backup_${profile}_*.tar.zst 2>/dev/null | wc -l)
        if [ "$count" -gt "$keep_versions" ]; then
            local to_delete=$((count - keep_versions))
            ls -1t "$destination"/backup_${profile}_*.tar.zst | tail -n "$to_delete" | while read -r old; do
                echo "Removing old archive: $old"
                rm -f "$old"
            done
        fi
    fi

    [ "$status" = "success" ] && return 0 || return 1
}

# --- Restore ---

cmd_restore() {
    local source_path="$1"
    local restore_to="${2:-.}"

    if [ ! -e "$source_path" ]; then
        echo "ERROR: Source not found: $source_path" >&2
        return 1
    fi

    if [[ "$source_path" == *.tar.zst ]]; then
        echo "Extracting archive to: $restore_to"
        mkdir -p "$restore_to"
        tar --zstd -xf "$source_path" -C "$restore_to"
        echo "Restore complete"
    elif [ -d "$source_path" ]; then
        echo "Restoring from rsync backup to: $restore_to"
        mkdir -p "$restore_to"
        rsync --archive --human-readable --info=progress2 "$source_path/" "$restore_to/"
        echo "Restore complete"
    else
        echo "ERROR: Unknown backup format: $source_path" >&2
        return 1
    fi
}

# --- History ---

cmd_history() {
    ensure_config_dir
    if [ ! -f "$HISTORY_FILE" ]; then
        echo "[]"
        return
    fi
    cat "$HISTORY_FILE"
}

cmd_history_clear() {
    echo "[]" > "$HISTORY_FILE"
    echo "History cleared"
}

# --- Schedule ---

cmd_schedule_install() {
    local profile="${1:-home}"
    local interval="${2:-daily}"

    # Read destination from config to validate
    local dest
    dest=$(python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
print(cfg.get('profiles',{}).get('$profile',{}).get('destination',''))
")
    if [ -z "$dest" ]; then
        echo "ERROR: Profile '$profile' has no destination. Configure it first." >&2
        return 1
    fi

    local service_name="backup-manager-${profile}"
    local timer_file="$HOME/.config/systemd/user/${service_name}.timer"
    local service_file="$HOME/.config/systemd/user/${service_name}.service"

    mkdir -p "$HOME/.config/systemd/user"

    # Determine OnCalendar value
    local calendar
    case "$interval" in
        hourly) calendar="*-*-* *:00:00" ;;
        daily)  calendar="*-*-* 12:00:00" ;;
        weekly) calendar="Mon *-*-* 12:00:00" ;;
        monthly) calendar="*-*-01 12:00:00" ;;
        *)
            echo "ERROR: interval must be hourly/daily/weekly/monthly" >&2
            return 1
            ;;
    esac

    cat > "$service_file" << EOF
[Unit]
Description=Backup Manager — $profile

[Service]
Type=oneshot
ExecStart=$(realpath "$0") backup $profile
Environment=HOME=$HOME
Environment=XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
EOF

    cat > "$timer_file" << EOF
[Unit]
Description=Backup Manager timer — $profile ($interval)

[Timer]
OnCalendar=$calendar
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "${service_name}.timer"

    # Update config
    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
if '$profile' in cfg.get('profiles', {}):
    cfg['profiles']['$profile']['schedule'] = '$interval'
    json.dump(cfg, open('$CONFIG_FILE', 'w'), indent=2)
"

    echo "OK: Schedule set — $profile ($interval)"
    echo "Timer: $timer_file"
    systemctl --user status "${service_name}.timer" --no-pager 2>/dev/null || true
}

cmd_schedule_remove() {
    local profile="${1:-home}"
    local service_name="backup-manager-${profile}"

    systemctl --user disable --now "${service_name}.timer" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/${service_name}.timer"
    rm -f "$HOME/.config/systemd/user/${service_name}.service"
    systemctl --user daemon-reload

    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
if '$profile' in cfg.get('profiles', {}):
    cfg['profiles']['$profile']['schedule'] = 'none'
    json.dump(cfg, open('$CONFIG_FILE', 'w'), indent=2)
" 2>/dev/null || true

    echo "OK: Schedule removed for $profile"
}

cmd_schedule_status() {
    systemctl --user list-timers "backup-manager-*" --no-pager 2>/dev/null
}

# --- List destinations ---

cmd_list_destinations() {
    echo "=== Mounted filesystems ==="
    lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL | grep -v loop
    echo ""
    echo "=== Potential backup targets ==="
    # List mount points with significant free space
    df -h --output=target,avail,fstype | grep -vE "tmpfs|devtmpfs|efivarfs|^/boot$|^/sys|^/proc|^/dev$|^/run" | tail -n +2
}

# --- Is running check ---

cmd_is_running() {
    if [ -f "$LOCK_FILE" ]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$pid" 2>/dev/null; then
            echo "running:$pid"
            return 0
        fi
    fi
    echo "idle"
}

# --- Main ---

case "${1:-}" in
    config-init)    cmd_config_init ;;
    config-get)     cmd_config_get ;;
    config-set)     cmd_config_set ;;
    backup)         cmd_backup "${2:-home}" ;;
    restore)        cmd_restore "${2:-}" "${3:-.}" ;;
    history)        cmd_history ;;
    history-clear)  cmd_history_clear ;;
    schedule-install)  cmd_schedule_install "${2:-home}" "${3:-daily}" ;;
    schedule-remove)   cmd_schedule_remove "${2:-home}" ;;
    schedule-status)   cmd_schedule_status ;;
    list-destinations) cmd_list_destinations ;;
    is-running)     cmd_is_running ;;
    *)
        echo "Backup Manager Backend v1.0"
        echo ""
        echo "Usage: $(basename "$0") <command> [args]"
        echo ""
        echo "Commands:"
        echo "  config-init               Initialize default config"
        echo "  config-get                Print config JSON"
        echo "  config-set                Write config JSON from stdin"
        echo "  backup [profile]          Run backup (default: home)"
        echo "  restore <source> [dest]   Restore backup"
        echo "  history                   Show backup history"
        echo "  history-clear             Clear history"
        echo "  schedule-install <p> <i>  Set timer (hourly/daily/weekly/monthly)"
        echo "  schedule-remove <p>       Remove timer"
        echo "  schedule-status           Show active timers"
        echo "  list-destinations         List available storage"
        echo "  is-running                Check if backup is in progress"
        exit 1
        ;;
esac
