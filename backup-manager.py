#!/usr/bin/env python3
"""
Backup Manager v2.0 — rsync/tar+zstd backup tool with Google Drive support
Single-file app: BackupEngine + PyQt6 GUI (3 tabs) + CLI
"""

import sys
import os
import json
import subprocess
import argparse
import shutil
import signal
from pathlib import Path
from datetime import datetime

APP_NAME = "Backup Manager"
APP_VERSION = "2.0.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "backup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
LOG_DIR = CONFIG_DIR / "logs"
LOCK_FILE = CONFIG_DIR / ".lock"

DEFAULT_EXCLUDES = [
    ".cache/", ".local/share/Trash/", ".thumbnails/", "*.tmp",
    "__pycache__/", "node_modules/", ".steam/", "Steam/",
    ".mozilla/firefox/*/storage/", "snap/", ".local/share/flatpak/",
]


def human_size(b) -> str:
    try:
        b = float(b)
    except (ValueError, TypeError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def human_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════
#  BackupEngine — all logic, no GUI dependencies
# ══════════════════════════════════════════════════════════════

class BackupEngine:

    # --- Config / History ---

    @staticmethod
    def load_config() -> dict:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            default = {
                "profiles": {
                    "home": {
                        "name": "Home Directory",
                        "sources": ["~/"],
                        "destination": "",
                        "type": "rsync",
                        "exclude": list(DEFAULT_EXCLUDES),
                        "schedule": "none",
                        "keep_versions": 5,
                        "gdrive_remote": "",
                        "gdrive_path": "Backups/",
                    }
                },
            }
            CONFIG_FILE.write_text(json.dumps(default, indent=2))
        if not HISTORY_FILE.exists():
            HISTORY_FILE.write_text("[]")
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"profiles": {}}

    @staticmethod
    def save_config(cfg: dict):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    @staticmethod
    def load_history() -> list:
        try:
            return json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            return []

    @staticmethod
    def _save_history(history: list):
        HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2))

    # --- Lock ---

    @staticmethod
    def is_locked() -> tuple[bool, int | None]:
        if LOCK_FILE.exists():
            try:
                pid = int(LOCK_FILE.read_text().strip())
                os.kill(pid, 0)
                return True, pid
            except (ValueError, OSError):
                LOCK_FILE.unlink(missing_ok=True)
        return False, None

    @staticmethod
    def _lock():
        LOCK_FILE.write_text(str(os.getpid()))

    @staticmethod
    def _unlock():
        LOCK_FILE.unlink(missing_ok=True)

    # --- Backup ---

    @staticmethod
    def run_backup(profile_name: str, cfg: dict | None = None,
                   log=None) -> dict:
        """Run backup. `log` is a callable(str) for progress lines."""
        if cfg is None:
            cfg = BackupEngine.load_config()

        profile = cfg.get("profiles", {}).get(profile_name)
        if not profile:
            raise ValueError(f"Profile '{profile_name}' not found")

        dest = profile.get("destination", "")
        if not dest:
            raise ValueError(f"No destination for profile '{profile_name}'")
        dest = str(Path(dest).expanduser())

        locked, pid = BackupEngine.is_locked()
        if locked:
            raise RuntimeError(f"Another backup running (PID {pid})")

        BackupEngine._lock()
        try:
            return BackupEngine._do_backup(profile_name, profile, dest, log)
        finally:
            BackupEngine._unlock()

    @staticmethod
    def _do_backup(profile_name, profile, dest, log) -> dict:
        p = log or (lambda s: None)
        sources = [str(Path(s).expanduser()) for s in profile.get("sources", [])]
        excludes = profile.get("exclude", [])
        btype = profile.get("type", "rsync")
        keep = profile.get("keep_versions", 5)
        ts = timestamp()
        log_file = str(LOG_DIR / f"backup_{profile_name}_{ts}.log")
        start = datetime.now()

        p(f"=== Backup: {profile.get('name', profile_name)} ===")
        p(f"Type: {btype} | Destination: {dest}")
        p(f"Started: {human_now()}")

        status = "success"
        dest_path = ""
        total_bytes = 0

        try:
            Path(dest).mkdir(parents=True, exist_ok=True)

            if btype == "archive":
                archive = f"{dest}/backup_{profile_name}_{ts}.tar.zst"
                dest_path = archive
                cmd = ["tar", "--create", "--zstd"]
                for ex in excludes:
                    cmd += [f"--exclude={ex}"]
                cmd += ["-f", archive] + [s for s in sources if Path(s).exists()]
                p(f"Creating archive: {archive}")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                with open(log_file, "w") as lf:
                    for line in proc.stdout:
                        lf.write(line)
                        p(line.rstrip())
                proc.wait()
                if proc.returncode != 0:
                    status = "failed"
                else:
                    total_bytes = Path(archive).stat().st_size
                    p(f"Archive: {human_size(total_bytes)}")
                # Rotate old archives
                archives = sorted(Path(dest).glob(f"backup_{profile_name}_*.tar.zst"))
                while len(archives) > keep:
                    old = archives.pop(0)
                    p(f"Removing old: {old.name}")
                    old.unlink()
            else:
                # rsync incremental
                latest = f"{dest}/{profile_name}_latest"
                previous = f"{dest}/{profile_name}_previous"
                dest_path = latest
                Path(latest).mkdir(parents=True, exist_ok=True)

                for src in sources:
                    if not Path(src).exists():
                        p(f"  Skip (not found): {src}")
                        continue
                    src_path = src.rstrip("/") + "/"
                    cmd = ["rsync", "--archive", "--human-readable",
                           "--delete", "--info=progress2"]
                    for ex in excludes:
                        cmd += [f"--exclude={ex}"]
                    if Path(previous).is_dir():
                        cmd += [f"--link-dest={previous}"]
                    cmd += [src_path, latest + "/"]

                    p(f"  Syncing: {src_path}")
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
                    with open(log_file, "a") as lf:
                        for line in proc.stdout:
                            lf.write(line)
                            stripped = line.rstrip()
                            if stripped:
                                p(stripped)
                    proc.wait()
                    if proc.returncode != 0:
                        status = "failed"

                # Rotate
                if status == "success" and Path(latest).is_dir():
                    if Path(previous).exists():
                        shutil.rmtree(previous, ignore_errors=True)
                    try:
                        subprocess.run(["cp", "-al", latest, previous],
                                       capture_output=True, timeout=120)
                    except Exception:
                        pass

                try:
                    r = subprocess.run(["du", "-sb", latest],
                                       capture_output=True, text=True, timeout=30)
                    total_bytes = int(r.stdout.split()[0])
                except Exception:
                    pass
        except Exception as e:
            status = "failed"
            p(f"ERROR: {e}")

        duration = int((datetime.now() - start).total_seconds())
        p(f"\nStatus: {status} | Duration: {duration}s | Size: {human_size(total_bytes)}")

        entry = {
            "profile": profile_name,
            "name": profile.get("name", profile_name),
            "timestamp": ts,
            "date": human_now(),
            "status": status,
            "duration_s": duration,
            "bytes": total_bytes,
            "destination": dest,
            "destination_path": dest_path,
            "type": btype,
            "log": log_file,
        }
        history = BackupEngine.load_history()
        history.append(entry)
        BackupEngine._save_history(history)

        return entry

    # --- Restore ---

    @staticmethod
    def run_restore(source: str, dest: str, log=None) -> bool:
        p = log or (lambda s: None)
        source = str(Path(source).expanduser())
        dest = str(Path(dest).expanduser())

        if not Path(source).exists():
            p(f"ERROR: Source not found: {source}")
            return False

        Path(dest).mkdir(parents=True, exist_ok=True)

        if source.endswith(".tar.zst"):
            p(f"Extracting archive to: {dest}")
            proc = subprocess.Popen(
                ["tar", "--zstd", "-xf", source, "-C", dest],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        else:
            p(f"Restoring via rsync to: {dest}")
            src = source.rstrip("/") + "/"
            proc = subprocess.Popen(
                ["rsync", "--archive", "--human-readable",
                 "--info=progress2", src, dest + "/"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )

        for line in proc.stdout:
            p(line.rstrip())
        proc.wait()
        ok = proc.returncode == 0
        p("Restore complete!" if ok else "Restore FAILED")
        return ok

    # --- Google Drive / rclone ---

    @staticmethod
    def detect_rclone() -> dict:
        result = {"installed": False, "remotes": [], "configured": False}
        if not shutil.which("rclone"):
            return result
        result["installed"] = True
        try:
            r = subprocess.run(["rclone", "listremotes"],
                               capture_output=True, text=True, timeout=10)
            for name in r.stdout.strip().splitlines():
                name = name.strip().rstrip(":")
                if not name:
                    continue
                r2 = subprocess.run(["rclone", "config", "show", name],
                                    capture_output=True, text=True, timeout=5)
                rtype = "unknown"
                for line in r2.stdout.splitlines():
                    if line.strip().startswith("type"):
                        rtype = line.split("=", 1)[1].strip()
                result["remotes"].append({"name": name, "type": rtype})
            result["configured"] = any(r["type"] == "drive" for r in result["remotes"])
        except Exception:
            pass
        return result

    @staticmethod
    def upload_to_gdrive(local_path: str, remote: str, remote_path: str,
                         log=None) -> bool:
        p = log or (lambda s: None)
        if not shutil.which("rclone"):
            p("ERROR: rclone not installed")
            return False
        p(f"Uploading to {remote}:{remote_path} ...")
        cmd = ["rclone", "copy", local_path, f"{remote}:{remote_path}",
               "--progress", "--stats-one-line", "--stats", "2s"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    p(stripped)
            proc.wait()
            ok = proc.returncode == 0
            p("Upload complete!" if ok else "Upload FAILED")
            return ok
        except Exception as e:
            p(f"ERROR: {e}")
            return False

    # --- Schedule (systemd user timers) ---

    @staticmethod
    def install_schedule(profile: str, interval: str) -> str:
        calendar_map = {
            "hourly": "*-*-* *:00:00",
            "daily": "*-*-* 12:00:00",
            "weekly": "Mon *-*-* 12:00:00",
            "monthly": "*-*-01 12:00:00",
        }
        if interval not in calendar_map:
            return f"ERROR: interval must be one of {list(calendar_map.keys())}"

        user_dir = Path.home() / ".config/systemd/user"
        user_dir.mkdir(parents=True, exist_ok=True)
        name = f"backup-manager-{profile}"

        service = f"""[Unit]
Description=Backup Manager — {profile}

[Service]
Type=oneshot
ExecStart=/usr/local/bin/backup-manager backup {profile}
Environment=HOME={Path.home()}
Environment=XDG_CONFIG_HOME={os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')}
"""
        timer = f"""[Unit]
Description=Backup Manager timer — {profile} ({interval})

[Timer]
OnCalendar={calendar_map[interval]}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""
        (user_dir / f"{name}.service").write_text(service)
        (user_dir / f"{name}.timer").write_text(timer)

        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"],
                       capture_output=True)

        # Update config
        cfg = BackupEngine.load_config()
        if profile in cfg.get("profiles", {}):
            cfg["profiles"][profile]["schedule"] = interval
            BackupEngine.save_config(cfg)

        return f"Schedule set: {profile} ({interval})"

    @staticmethod
    def remove_schedule(profile: str) -> str:
        name = f"backup-manager-{profile}"
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.timer"],
                       capture_output=True)
        user_dir = Path.home() / ".config/systemd/user"
        (user_dir / f"{name}.timer").unlink(missing_ok=True)
        (user_dir / f"{name}.service").unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

        cfg = BackupEngine.load_config()
        if profile in cfg.get("profiles", {}):
            cfg["profiles"][profile]["schedule"] = "none"
            BackupEngine.save_config(cfg)

        return f"Schedule removed: {profile}"

    @staticmethod
    def get_next_run(profile: str) -> str:
        name = f"backup-manager-{profile}"
        try:
            r = subprocess.run(
                ["systemctl", "--user", "show", f"{name}.timer",
                 "--property=NextElapseUSecRealtime"],
                capture_output=True, text=True, timeout=5
            )
            val = r.stdout.strip().split("=", 1)[-1].strip()
            if val and val != "n/a":
                return val
        except Exception:
            pass
        return ""


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def run_cli(args):
    if args.command == "status":
        cfg = BackupEngine.load_config()
        history = BackupEngine.load_history()
        print(f"Profiles: {', '.join(cfg.get('profiles', {}).keys()) or 'none'}")
        if history:
            last = history[-1]
            print(f"Last:     {last.get('date')} [{last.get('profile')}] — {last.get('status')}")
        else:
            print("Last:     never")
        for pname in cfg.get("profiles", {}):
            sched = cfg["profiles"][pname].get("schedule", "none")
            if sched != "none":
                nxt = BackupEngine.get_next_run(pname)
                print(f"Schedule: {pname} every {sched}" + (f" (next: {nxt})" if nxt else ""))
        rclone = BackupEngine.detect_rclone()
        if rclone["installed"]:
            remotes = ", ".join(f"{r['name']} ({r['type']})" for r in rclone["remotes"]) or "none"
            print(f"rclone:   installed, remotes: {remotes}")
        return

    if args.command == "backup":
        profile = args.profile or "home"
        cfg = BackupEngine.load_config()
        result = BackupEngine.run_backup(profile, cfg, log=print)
        # Upload to Google Drive if configured
        p = cfg.get("profiles", {}).get(profile, {})
        gdrive = p.get("gdrive_remote", "")
        if gdrive and result["status"] == "success":
            print("\n--- Uploading to Google Drive ---")
            BackupEngine.upload_to_gdrive(
                result["destination_path"], gdrive,
                p.get("gdrive_path", "Backups/"), log=print
            )
        sys.exit(0 if result["status"] == "success" else 1)

    if args.command == "restore":
        ok = BackupEngine.run_restore(args.source, args.dest, log=print)
        sys.exit(0 if ok else 1)

    if args.command == "schedule":
        profile = args.profile or "home"
        if args.remove:
            print(BackupEngine.remove_schedule(profile))
        else:
            print(BackupEngine.install_schedule(profile, args.interval or "daily"))
        return

    # No command → GUI
    run_gui()


# ══════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════

def run_gui():
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QFrame, QSystemTrayIcon, QMenu, QMessageBox,
        QTabWidget, QGroupBox, QGridLayout, QLineEdit, QComboBox,
        QFileDialog, QListWidget, QCheckBox, QStyleFactory,
        QPlainTextEdit, QProgressBar, QInputDialog
    )
    from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
    from PyQt6.QtGui import QIcon, QAction, QFont

    # --- Worker thread ---

    class BackupWorker(QThread):
        output = pyqtSignal(str)
        finished_signal = pyqtSignal(bool)

        def __init__(self, profile_name: str, cfg: dict):
            super().__init__()
            self.profile_name = profile_name
            self.cfg = cfg

        def run(self):
            try:
                self.output.emit("--- Starting backup ---")
                result = BackupEngine.run_backup(
                    self.profile_name, self.cfg,
                    log=lambda s: self.output.emit(s)
                )
                if result["status"] != "success":
                    self.finished_signal.emit(False)
                    return

                # Google Drive upload
                p = self.cfg.get("profiles", {}).get(self.profile_name, {})
                gdrive = p.get("gdrive_remote", "")
                if gdrive:
                    self.output.emit("")
                    self.output.emit("--- Uploading to Google Drive ---")
                    BackupEngine.upload_to_gdrive(
                        result["destination_path"], gdrive,
                        p.get("gdrive_path", "Backups/"),
                        log=lambda s: self.output.emit(s)
                    )

                self.finished_signal.emit(True)
            except Exception as e:
                self.output.emit(f"ERROR: {e}")
                self.finished_signal.emit(False)

    # --- Main window ---

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.setMinimumSize(560, 480)
            self.resize(580, 520)
            self.setWindowIcon(QIcon.fromTheme("folder-sync"))
            self.cfg = BackupEngine.load_config()
            self.worker = None
            self.rclone_info = BackupEngine.detect_rclone()

            tabs = QTabWidget()
            self.setCentralWidget(tabs)
            tabs.addTab(self._build_backup_tab(), QIcon.fromTheme("document-save"), "Backup")
            tabs.addTab(self._build_history_tab(), QIcon.fromTheme("view-history"), "History")
            tabs.addTab(self._build_settings_tab(), QIcon.fromTheme("configure"), "Settings")

            self.statusBar().showMessage("Ready")

            # Tray
            self.tray = None
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray = QSystemTrayIcon(QIcon.fromTheme("folder-sync"), self)
                m = QMenu()
                a = QAction("Backup Now", self)
                a.triggered.connect(self._on_backup_now)
                m.addAction(a)
                m.addSeparator()
                a2 = QAction("Show", self)
                a2.triggered.connect(self._show_raise)
                m.addAction(a2)
                a3 = QAction("Quit", self)
                a3.triggered.connect(QApplication.quit)
                m.addAction(a3)
                self.tray.setContextMenu(m)
                self.tray.activated.connect(self._tray_click)
                self.tray.show()

            # Refresh timer
            self._timer = QTimer()
            self._timer.timeout.connect(self._refresh_status)
            self._timer.start(10000)
            self._refresh_status()

        # ═══ Tab 1: Backup ═══

        def _build_backup_tab(self) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(18, 14, 18, 14)
            lay.setSpacing(10)

            # Title
            title = QLabel("Backup Manager")
            f = QFont(); f.setPointSize(15); f.setBold(True)
            title.setFont(f)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(title)

            # Status
            sg = QGroupBox("Status")
            sl = QGridLayout(sg)
            self.st_last = QLabel("Last backup: ---")
            self.st_next = QLabel("Next scheduled: ---")
            self.st_space = QLabel("Destination: ---")
            for i, lbl in enumerate([self.st_last, self.st_next, self.st_space]):
                lf = QFont(); lf.setPointSize(10)
                lbl.setFont(lf)
                sl.addWidget(lbl, i, 0)
            lay.addWidget(sg)

            # Destination
            dg = QGroupBox("Destination")
            dl = QGridLayout(dg)

            dl.addWidget(QLabel("Profile:"), 0, 0)
            self.bk_profile = QComboBox()
            for name in self.cfg.get("profiles", {}):
                self.bk_profile.addItem(name)
            self.bk_profile.currentTextChanged.connect(self._on_profile_changed)
            dl.addWidget(self.bk_profile, 0, 1, 1, 2)

            dl.addWidget(QLabel("Local path:"), 1, 0)
            self.bk_dest = QLineEdit()
            self.bk_dest.setPlaceholderText("/mnt/backup or ~/Backups")
            dl.addWidget(self.bk_dest, 1, 1)
            btn_browse = QPushButton("Browse...")
            btn_browse.clicked.connect(self._browse_dest)
            dl.addWidget(btn_browse, 1, 2)

            # Google Drive row
            self.bk_gdrive_chk = QCheckBox("Upload to Google Drive")
            dl.addWidget(self.bk_gdrive_chk, 2, 0, 1, 1)
            self.bk_gdrive_remote = QComboBox()
            self.bk_gdrive_remote.setMinimumWidth(120)
            self._populate_remotes()
            dl.addWidget(self.bk_gdrive_remote, 2, 1)
            btn_rclone = QPushButton("Setup rclone...")
            btn_rclone.clicked.connect(self._open_rclone_config)
            dl.addWidget(btn_rclone, 2, 2)

            self.bk_gdrive_path = QLineEdit("Backups/")
            self.bk_gdrive_path.setPlaceholderText("Remote path, e.g. Backups/")
            dl.addWidget(QLabel("Drive path:"), 3, 0)
            dl.addWidget(self.bk_gdrive_path, 3, 1, 1, 2)

            lay.addWidget(dg)

            # Auto schedule
            sched_lay = QHBoxLayout()
            self.bk_sched_chk = QCheckBox("Auto backup every:")
            self.bk_sched_chk.toggled.connect(self._on_schedule_toggled)
            sched_lay.addWidget(self.bk_sched_chk)
            self.bk_sched_interval = QComboBox()
            self.bk_sched_interval.addItems(["hourly", "daily", "weekly", "monthly"])
            self.bk_sched_interval.setCurrentText("daily")
            self.bk_sched_interval.currentTextChanged.connect(self._on_interval_changed)
            sched_lay.addWidget(self.bk_sched_interval)
            self.bk_sched_next = QLabel("")
            sched_lay.addWidget(self.bk_sched_next, 1)
            lay.addLayout(sched_lay)

            # Backup Now button
            self.btn_backup = QPushButton("  Backup Now  ")
            bf = QFont(); bf.setPointSize(13); bf.setBold(True)
            self.btn_backup.setFont(bf)
            self.btn_backup.setFixedHeight(50)
            self.btn_backup.setIcon(QIcon.fromTheme("document-save"))
            self.btn_backup.clicked.connect(self._on_backup_now)
            lay.addWidget(self.btn_backup)

            # Progress
            self.bk_progress = QProgressBar()
            self.bk_progress.setRange(0, 0)
            self.bk_progress.setVisible(False)
            lay.addWidget(self.bk_progress)

            # Log
            self.bk_log = QPlainTextEdit()
            self.bk_log.setReadOnly(True)
            self.bk_log.setMaximumHeight(140)
            self.bk_log.setVisible(False)
            lay.addWidget(self.bk_log)

            # Load first profile
            if self.bk_profile.count() > 0:
                self._on_profile_changed(self.bk_profile.currentText())

            return w

        def _populate_remotes(self):
            self.bk_gdrive_remote.clear()
            self.bk_gdrive_remote.addItem("(none)")
            for r in self.rclone_info.get("remotes", []):
                label = f"{r['name']} ({r['type']})"
                self.bk_gdrive_remote.addItem(label, r["name"])

        def _on_profile_changed(self, name):
            p = self.cfg.get("profiles", {}).get(name, {})
            self.bk_dest.setText(p.get("destination", ""))
            gdrive = p.get("gdrive_remote", "")
            self.bk_gdrive_chk.setChecked(bool(gdrive))
            self.bk_gdrive_path.setText(p.get("gdrive_path", "Backups/"))
            # Select remote in combo
            idx = 0
            for i in range(self.bk_gdrive_remote.count()):
                if self.bk_gdrive_remote.itemData(i) == gdrive:
                    idx = i
                    break
            self.bk_gdrive_remote.setCurrentIndex(idx)
            # Schedule
            sched = p.get("schedule", "none")
            self.bk_sched_chk.blockSignals(True)
            self.bk_sched_chk.setChecked(sched != "none")
            self.bk_sched_chk.blockSignals(False)
            if sched != "none":
                self.bk_sched_interval.setCurrentText(sched)
            self._refresh_status()

        def _browse_dest(self):
            d = QFileDialog.getExistingDirectory(self, "Select Destination")
            if d:
                self.bk_dest.setText(d)

        def _save_current_profile(self):
            name = self.bk_profile.currentText()
            if not name:
                return
            p = self.cfg.setdefault("profiles", {}).setdefault(name, {})
            p["destination"] = self.bk_dest.text()
            if self.bk_gdrive_chk.isChecked():
                p["gdrive_remote"] = self.bk_gdrive_remote.currentData() or ""
            else:
                p["gdrive_remote"] = ""
            p["gdrive_path"] = self.bk_gdrive_path.text() or "Backups/"
            BackupEngine.save_config(self.cfg)

        def _on_backup_now(self):
            profile = self.bk_profile.currentText()
            if not profile:
                return
            self._save_current_profile()
            dest = self.bk_dest.text().strip()
            if not dest:
                QMessageBox.warning(self, "No Destination",
                                    "Set a backup destination first.")
                return
            if self.worker and self.worker.isRunning():
                QMessageBox.information(self, "Busy", "A backup is already running.")
                return

            self.cfg = BackupEngine.load_config()
            self.bk_log.clear()
            self.bk_log.setVisible(True)
            self.bk_progress.setVisible(True)
            self.btn_backup.setEnabled(False)
            self.statusBar().showMessage(f"Backing up: {profile}...")

            self.worker = BackupWorker(profile, self.cfg)
            self.worker.output.connect(self._on_output)
            self.worker.finished_signal.connect(self._on_finished)
            self.worker.start()

        def _on_output(self, line):
            self.bk_log.appendPlainText(line)
            sb = self.bk_log.verticalScrollBar()
            sb.setValue(sb.maximum())

        def _on_finished(self, success):
            self.bk_progress.setVisible(False)
            self.btn_backup.setEnabled(True)
            msg = "Backup completed!" if success else "Backup FAILED"
            self.statusBar().showMessage(msg, 10000)
            if self.tray:
                icon = (QSystemTrayIcon.MessageIcon.Information if success
                        else QSystemTrayIcon.MessageIcon.Critical)
                self.tray.showMessage(APP_NAME, msg, icon, 5000)
            self._refresh_status()
            self._refresh_history()

        def _on_schedule_toggled(self, checked):
            profile = self.bk_profile.currentText()
            if not profile:
                return
            self._save_current_profile()
            if checked:
                interval = self.bk_sched_interval.currentText()
                msg = BackupEngine.install_schedule(profile, interval)
            else:
                msg = BackupEngine.remove_schedule(profile)
            self.statusBar().showMessage(msg, 5000)
            self.cfg = BackupEngine.load_config()
            self._refresh_status()

        def _on_interval_changed(self, interval):
            if not self.bk_sched_chk.isChecked():
                return
            profile = self.bk_profile.currentText()
            if not profile:
                return
            BackupEngine.remove_schedule(profile)
            BackupEngine.install_schedule(profile, interval)
            self.cfg = BackupEngine.load_config()
            self._refresh_status()

        def _open_rclone_config(self):
            if not shutil.which("rclone"):
                QMessageBox.warning(self, "rclone not installed",
                                    "Install rclone first:\n\nsudo pacman -S rclone")
                return
            for term_cmd in [
                ["konsole", "-e", "rclone", "config"],
                ["xterm", "-e", "rclone", "config"],
            ]:
                if shutil.which(term_cmd[0]):
                    subprocess.Popen(term_cmd)
                    QMessageBox.information(self, "rclone Setup",
                        "rclone config is running in a terminal.\n\n"
                        "Follow the prompts to set up Google Drive.\n"
                        "When done, close this dialog to refresh remotes.")
                    self.rclone_info = BackupEngine.detect_rclone()
                    self._populate_remotes()
                    self._refresh_rclone_status()
                    return
            QMessageBox.warning(self, "Error",
                                "No terminal found. Run 'rclone config' manually.")

        def _refresh_status(self):
            history = BackupEngine.load_history()
            if history:
                last = history[-1]
                status = last.get("status", "?")
                color = "#27ae60" if status == "success" else "#e74c3c"
                self.st_last.setText(
                    f"Last backup: {last.get('date')} [{last.get('profile')}] "
                    f"— <b style='color:{color}'>{status}</b>"
                )
            else:
                self.st_last.setText("Last backup: never")

            profile = self.bk_profile.currentText()
            if profile:
                nxt = BackupEngine.get_next_run(profile)
                sched = self.cfg.get("profiles", {}).get(profile, {}).get("schedule", "none")
                if nxt:
                    self.st_next.setText(f"Next scheduled: {nxt}")
                    self.bk_sched_next.setText(f"Next: {nxt}")
                elif sched != "none":
                    self.st_next.setText(f"Scheduled: {sched} (calculating...)")
                    self.bk_sched_next.setText("")
                else:
                    self.st_next.setText("Next scheduled: not configured")
                    self.bk_sched_next.setText("")

            dest = self.bk_dest.text().strip()
            if dest:
                try:
                    dest_exp = str(Path(dest).expanduser())
                    usage = shutil.disk_usage(dest_exp)
                    self.st_space.setText(
                        f"Destination: {human_size(usage.free)} free / {human_size(usage.total)}"
                    )
                except OSError:
                    self.st_space.setText(f"Destination: {dest} (not accessible)")
            else:
                self.st_space.setText("Destination: not configured")

            if self.tray:
                self.tray.setToolTip(self.st_last.text().replace("<b style='color:#27ae60'>", "")
                                     .replace("<b style='color:#e74c3c'>", "").replace("</b>", ""))

        # ═══ Tab 2: History ═══

        def _build_history_tab(self) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(8)

            self.hist_list = QListWidget()
            self.hist_list.itemClicked.connect(self._on_hist_click)
            lay.addWidget(self.hist_list)

            self.hist_detail = QPlainTextEdit()
            self.hist_detail.setReadOnly(True)
            self.hist_detail.setMaximumHeight(90)
            lay.addWidget(self.hist_detail)

            # Restore section
            rg = QGroupBox("Restore")
            rl = QGridLayout(rg)
            rl.addWidget(QLabel("Source:"), 0, 0)
            self.rst_src = QLineEdit()
            self.rst_src.setPlaceholderText("Click history entry or browse")
            rl.addWidget(self.rst_src, 0, 1)
            btn_browse_src = QPushButton("Browse...")
            btn_browse_src.clicked.connect(
                lambda: self._browse_to(self.rst_src))
            rl.addWidget(btn_browse_src, 0, 2)

            rl.addWidget(QLabel("Restore to:"), 1, 0)
            self.rst_dst = QLineEdit()
            rl.addWidget(self.rst_dst, 1, 1)
            btn_browse_dst = QPushButton("Browse...")
            btn_browse_dst.clicked.connect(
                lambda: self._browse_to(self.rst_dst))
            rl.addWidget(btn_browse_dst, 1, 2)

            btn_restore = QPushButton("Restore")
            btn_restore.clicked.connect(self._do_restore)
            rl.addWidget(btn_restore, 2, 0, 1, 3)
            lay.addWidget(rg)

            self.rst_log = QPlainTextEdit()
            self.rst_log.setReadOnly(True)
            self.rst_log.setMaximumHeight(100)
            self.rst_log.setVisible(False)
            lay.addWidget(self.rst_log)

            btns = QHBoxLayout()
            btn_ref = QPushButton("Refresh")
            btn_ref.clicked.connect(self._refresh_history)
            btns.addWidget(btn_ref)
            btn_clr = QPushButton("Clear History")
            btn_clr.clicked.connect(self._clear_history)
            btns.addWidget(btn_clr)
            lay.addLayout(btns)

            self._refresh_history()
            return w

        def _refresh_history(self):
            self.hist_list.clear()
            from PyQt6.QtWidgets import QListWidgetItem
            for entry in reversed(BackupEngine.load_history()):
                ok = entry.get("status") == "success"
                icon = QIcon.fromTheme("dialog-ok" if ok else "dialog-error")
                size = human_size(entry.get("bytes", 0))
                text = (f"{entry.get('date', '?')}  [{entry.get('profile', '?')}]  "
                        f"{entry.get('status', '?')}  {size}  ({entry.get('duration_s', 0)}s)")
                item = QListWidgetItem(icon, text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.hist_list.addItem(item)

        def _on_hist_click(self, item):
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry:
                self.hist_detail.setPlainText(json.dumps(entry, indent=2))
                dp = entry.get("destination_path", "")
                if dp:
                    self.rst_src.setText(dp)

        def _browse_to(self, field):
            d = QFileDialog.getExistingDirectory(self, "Select Directory")
            if d:
                field.setText(d)

        def _do_restore(self):
            src = self.rst_src.text().strip()
            dst = self.rst_dst.text().strip()
            if not src or not dst:
                QMessageBox.warning(self, "Missing", "Set both source and destination.")
                return
            r = QMessageBox.question(
                self, "Confirm",
                f"Restore from:\n  {src}\nTo:\n  {dst}\n\nExisting files may be overwritten.")
            if r != QMessageBox.StandardButton.Yes:
                return

            self.rst_log.clear()
            self.rst_log.setVisible(True)
            self.statusBar().showMessage("Restoring...")
            QApplication.processEvents()

            ok = BackupEngine.run_restore(
                src, dst, log=lambda s: (self.rst_log.appendPlainText(s),
                                         QApplication.processEvents()))
            self.statusBar().showMessage(
                "Restore complete!" if ok else "Restore FAILED", 10000)

        def _clear_history(self):
            r = QMessageBox.question(self, "Clear", "Delete all backup history?")
            if r == QMessageBox.StandardButton.Yes:
                BackupEngine._save_history([])
                self._refresh_history()

        # ═══ Tab 3: Settings ═══

        def _build_settings_tab(self) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(8)

            # Profile selector
            hl = QHBoxLayout()
            hl.addWidget(QLabel("Profile:"))
            self.set_profile = QComboBox()
            for name in self.cfg.get("profiles", {}):
                self.set_profile.addItem(name)
            self.set_profile.currentTextChanged.connect(self._load_settings)
            hl.addWidget(self.set_profile, 1)
            btn_new = QPushButton("New")
            btn_new.clicked.connect(self._new_profile)
            hl.addWidget(btn_new)
            btn_del = QPushButton("Delete")
            btn_del.clicked.connect(self._del_profile)
            hl.addWidget(btn_del)
            lay.addLayout(hl)

            # Profile settings
            pg = QGroupBox("Profile Settings")
            pl = QGridLayout(pg)
            pl.addWidget(QLabel("Name:"), 0, 0)
            self.set_name = QLineEdit()
            pl.addWidget(self.set_name, 0, 1)
            pl.addWidget(QLabel("Type:"), 1, 0)
            self.set_type = QComboBox()
            self.set_type.addItems(["rsync", "archive"])
            pl.addWidget(self.set_type, 1, 1)
            pl.addWidget(QLabel("Keep versions:"), 2, 0)
            self.set_keep = QComboBox()
            self.set_keep.addItems(["3", "5", "10", "20"])
            pl.addWidget(self.set_keep, 2, 1)
            lay.addWidget(pg)

            # Sources
            sg = QGroupBox("Sources")
            sl = QVBoxLayout(sg)
            self.set_sources = QListWidget()
            self.set_sources.setMaximumHeight(80)
            sl.addWidget(self.set_sources)
            sb = QHBoxLayout()
            btn_add = QPushButton("Add...")
            btn_add.clicked.connect(self._add_source)
            sb.addWidget(btn_add)
            btn_rm = QPushButton("Remove")
            btn_rm.clicked.connect(lambda: self.set_sources.takeItem(
                self.set_sources.currentRow()))
            sb.addWidget(btn_rm)
            sl.addLayout(sb)
            lay.addWidget(sg)

            # Excludes
            eg = QGroupBox("Exclude Patterns (one per line)")
            el = QVBoxLayout(eg)
            self.set_excludes = QPlainTextEdit()
            self.set_excludes.setMaximumHeight(90)
            el.addWidget(self.set_excludes)
            lay.addWidget(eg)

            # rclone status
            rg = QGroupBox("Google Drive (rclone)")
            rl = QVBoxLayout(rg)
            self.set_rclone_label = QLabel("")
            rl.addWidget(self.set_rclone_label)
            btn_rc = QPushButton("Run rclone config...")
            btn_rc.clicked.connect(self._open_rclone_config)
            rl.addWidget(btn_rc)
            btn_rr = QPushButton("Refresh rclone status")
            btn_rr.clicked.connect(self._refresh_rclone_status)
            rl.addWidget(btn_rr)
            lay.addWidget(rg)
            self._refresh_rclone_status()

            # Save
            btn_save = QPushButton("Save Settings")
            sf = QFont(); sf.setBold(True)
            btn_save.setFont(sf)
            btn_save.clicked.connect(self._save_settings)
            lay.addWidget(btn_save)

            if self.set_profile.count() > 0:
                self._load_settings(self.set_profile.currentText())

            return w

        def _load_settings(self, name):
            if not name:
                return
            p = self.cfg.get("profiles", {}).get(name, {})
            self.set_name.setText(p.get("name", name))
            idx = self.set_type.findText(p.get("type", "rsync"))
            if idx >= 0:
                self.set_type.setCurrentIndex(idx)
            self.set_keep.setCurrentText(str(p.get("keep_versions", 5)))
            self.set_sources.clear()
            for s in p.get("sources", []):
                self.set_sources.addItem(s)
            self.set_excludes.setPlainText("\n".join(p.get("exclude", [])))

        def _save_settings(self):
            name = self.set_profile.currentText()
            if not name:
                return
            p = self.cfg.setdefault("profiles", {}).setdefault(name, {})
            p["name"] = self.set_name.text() or name
            p["type"] = self.set_type.currentText()
            p["keep_versions"] = int(self.set_keep.currentText())
            p["sources"] = [self.set_sources.item(i).text()
                            for i in range(self.set_sources.count())]
            excl = self.set_excludes.toPlainText().strip()
            p["exclude"] = [e.strip() for e in excl.splitlines() if e.strip()]
            BackupEngine.save_config(self.cfg)
            self.statusBar().showMessage(f"Settings saved: {name}", 5000)

        def _new_profile(self):
            name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
            if ok and name:
                name = name.strip().replace(" ", "_")
                self.cfg.setdefault("profiles", {})[name] = {
                    "name": name,
                    "sources": [str(Path.home())],
                    "destination": "",
                    "type": "rsync",
                    "exclude": list(DEFAULT_EXCLUDES),
                    "keep_versions": 5,
                    "schedule": "none",
                    "gdrive_remote": "",
                    "gdrive_path": "Backups/",
                }
                BackupEngine.save_config(self.cfg)
                self.set_profile.addItem(name)
                self.set_profile.setCurrentText(name)
                self.bk_profile.addItem(name)

        def _del_profile(self):
            name = self.set_profile.currentText()
            if not name:
                return
            if QMessageBox.question(self, "Delete", f"Delete profile '{name}'?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.cfg.get("profiles", {}).pop(name, None)
            BackupEngine.save_config(self.cfg)
            idx = self.set_profile.findText(name)
            if idx >= 0:
                self.set_profile.removeItem(idx)
            idx2 = self.bk_profile.findText(name)
            if idx2 >= 0:
                self.bk_profile.removeItem(idx2)

        def _add_source(self):
            d = QFileDialog.getExistingDirectory(self, "Add Source")
            if d:
                home = str(Path.home())
                if d.startswith(home):
                    d = "~" + d[len(home):]
                self.set_sources.addItem(d)

        def _refresh_rclone_status(self):
            self.rclone_info = BackupEngine.detect_rclone()
            if not self.rclone_info["installed"]:
                self.set_rclone_label.setText(
                    "rclone: <b style='color:#e74c3c'>NOT INSTALLED</b><br>"
                    "Install: <code>sudo pacman -S rclone</code>"
                )
            elif not self.rclone_info["configured"]:
                remotes = ", ".join(r["name"] for r in self.rclone_info["remotes"]) or "none"
                self.set_rclone_label.setText(
                    f"rclone: installed | Remotes: {remotes}<br>"
                    "<b>No Google Drive remote configured.</b> Click 'Run rclone config...' to set up."
                )
            else:
                drives = [r["name"] for r in self.rclone_info["remotes"] if r["type"] == "drive"]
                self.set_rclone_label.setText(
                    f"rclone: installed | Google Drive: <b style='color:#27ae60'>"
                    f"{', '.join(drives)}</b>"
                )

        # ═══ Common ═══

        def _show_raise(self):
            self.show(); self.raise_(); self.activateWindow()

        def _tray_click(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self.hide() if self.isVisible() else self._show_raise()

        def closeEvent(self, event):
            if self.tray and self.tray.isVisible():
                self.hide()
                event.ignore()
            else:
                event.accept()

    # --- Launch ---
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName("backup-manager")
    app.setWindowIcon(QIcon.fromTheme("folder-sync"))

    for style in ("Breeze", "Fusion"):
        if style in QStyleFactory.keys():
            app.setStyle(style)
            break

    app.setStyleSheet("""
        QPushButton:hover { background-color: rgba(39, 174, 96, 20); }
        QProgressBar { border: 1px solid palette(mid); border-radius: 4px; text-align: center; }
        QProgressBar::chunk { background-color: #3498db; border-radius: 3px; }
    """)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show backup status")

    bp = sub.add_parser("backup", help="Run backup")
    bp.add_argument("profile", nargs="?", default="home")

    rp = sub.add_parser("restore", help="Restore from backup")
    rp.add_argument("source", help="Backup source path")
    rp.add_argument("--dest", default=".", help="Restore destination")

    sp = sub.add_parser("schedule", help="Manage schedule")
    sp.add_argument("profile", nargs="?", default="home")
    sp.add_argument("--interval", choices=["hourly", "daily", "weekly", "monthly"])
    sp.add_argument("--remove", action="store_true")

    args = parser.parse_args()

    if args.command:
        run_cli(args)
    else:
        run_gui()


if __name__ == "__main__":
    main()
