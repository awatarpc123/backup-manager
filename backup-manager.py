#!/usr/bin/env python3
"""
Backup Manager — rsync/tar+zstd backup tool with PyQt6 GUI
"""

import sys
import os
import json
import subprocess
import argparse
import shutil
from pathlib import Path
from datetime import datetime

APP_NAME = "Backup Manager"
APP_VERSION = "1.0.0"
BACKEND = str(Path(__file__).parent / "backup-manager-backend.sh")
BACKEND_INSTALLED = "/usr/local/bin/backup-manager-backend"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "backup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"


def get_backend() -> str:
    if Path(BACKEND_INSTALLED).exists():
        return BACKEND_INSTALLED
    if Path(BACKEND).exists():
        return BACKEND
    return BACKEND_INSTALLED


def run_backend(cmd: list[str], stdin_data: str | None = None, timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            [get_backend()] + cmd,
            capture_output=True, text=True, timeout=timeout,
            input=stdin_data
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 1, "", "Backend not found. Run install.sh first."
    except subprocess.TimeoutExpired:
        return 1, "", "Timeout"


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        run_backend(["config-init"])
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"profiles": {}, "notifications": True}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return []


def human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# --- CLI ---

def run_cli(args):
    if args.command == "status":
        cfg = load_config()
        history = load_history()
        print(f"Profiles: {', '.join(cfg.get('profiles', {}).keys()) or 'none'}")
        if history:
            last = history[-1]
            print(f"Last backup: {last.get('date', 'N/A')} [{last.get('profile')}] — {last.get('status')}")
        else:
            print("Last backup: never")
        _, out, _ = run_backend(["schedule-status"])
        if out.strip():
            print(f"\nScheduled timers:\n{out.strip()}")
        return

    if args.command == "backup":
        profile = args.profile or "home"
        print(f"Starting backup: {profile}")
        proc = subprocess.Popen(
            [get_backend(), "backup", profile],
            stdout=sys.stdout, stderr=sys.stderr
        )
        sys.exit(proc.wait())

    if args.command == "restore":
        if not args.source:
            print("Error: --source required", file=sys.stderr)
            sys.exit(1)
        dest = args.dest or "."
        proc = subprocess.Popen(
            [get_backend(), "restore", args.source, dest],
            stdout=sys.stdout, stderr=sys.stderr
        )
        sys.exit(proc.wait())

    if args.command == "schedule":
        profile = args.profile or "home"
        if args.remove:
            _, out, _ = run_backend(["schedule-remove", profile])
        else:
            interval = args.interval or "daily"
            _, out, _ = run_backend(["schedule-install", profile, interval])
        print(out.strip())
        return

    # Default: launch GUI
    run_gui()


# --- GUI ---

def run_gui():
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QFrame, QSystemTrayIcon, QMenu, QMessageBox,
        QTabWidget, QGroupBox, QGridLayout, QLineEdit, QComboBox,
        QTextEdit, QFileDialog, QListWidget, QListWidgetItem, QCheckBox,
        QStyleFactory, QPlainTextEdit, QSplitter, QProgressBar
    )
    from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QProcess
    from PyQt6.QtGui import QIcon, QAction, QFont

    class BackupWorker(QThread):
        output = pyqtSignal(str)
        finished_signal = pyqtSignal(bool)

        def __init__(self, profile: str):
            super().__init__()
            self.profile = profile

        def run(self):
            try:
                proc = subprocess.Popen(
                    [get_backend(), "backup", self.profile],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True
                )
                for line in proc.stdout:
                    self.output.emit(line.rstrip())
                proc.wait()
                self.finished_signal.emit(proc.returncode == 0)
            except Exception as e:
                self.output.emit(f"ERROR: {e}")
                self.finished_signal.emit(False)

    class BackupManagerWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.setMinimumSize(600, 500)
            self.resize(640, 540)
            self.setWindowIcon(QIcon.fromTheme("folder-sync"))

            self.config = load_config()
            self.worker = None

            tabs = QTabWidget()
            self.setCentralWidget(tabs)

            tabs.addTab(self._build_dashboard(), QIcon.fromTheme("go-home"), "Dashboard")
            tabs.addTab(self._build_configure(), QIcon.fromTheme("configure"), "Configure")
            tabs.addTab(self._build_schedule(), QIcon.fromTheme("chronometer"), "Schedule")
            tabs.addTab(self._build_history(), QIcon.fromTheme("view-history"), "History")
            tabs.addTab(self._build_restore(), QIcon.fromTheme("edit-undo"), "Restore")

            self.statusBar().showMessage("Ready")

            # Tray
            self.tray = None
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray = QSystemTrayIcon(QIcon.fromTheme("folder-sync"), self)
                m = QMenu()
                a_backup = QAction("Backup Now (home)", self)
                a_backup.triggered.connect(lambda: self._run_backup("home"))
                m.addAction(a_backup)
                m.addSeparator()
                a_show = QAction("Show", self)
                a_show.triggered.connect(self._show_raise)
                m.addAction(a_show)
                a_quit = QAction("Quit", self)
                a_quit.triggered.connect(QApplication.quit)
                m.addAction(a_quit)
                self.tray.setContextMenu(m)
                self.tray.activated.connect(self._tray_click)
                self.tray.show()

            # Refresh timer
            self.timer = QTimer()
            self.timer.timeout.connect(self._refresh_dashboard)
            self.timer.start(10000)
            self._refresh_dashboard()

        # --- Dashboard ---

        def _build_dashboard(self) -> QWidget:
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(12)

            title = QLabel("Backup Manager")
            f = QFont(); f.setPointSize(16); f.setBold(True)
            title.setFont(f)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)

            # Status group
            sg = QGroupBox("Status")
            sl = QGridLayout(sg)

            self.dash_last_backup = QLabel("Last backup: ---")
            self.dash_last_status = QLabel("Status: ---")
            self.dash_next = QLabel("Next scheduled: ---")
            self.dash_dest_space = QLabel("Destination space: ---")

            for i, lbl in enumerate([self.dash_last_backup, self.dash_last_status,
                                     self.dash_next, self.dash_dest_space]):
                lf = QFont(); lf.setPointSize(11)
                lbl.setFont(lf)
                sl.addWidget(lbl, i, 0)
            layout.addWidget(sg)

            # Quick backup
            bg = QGroupBox("Quick Backup")
            bl = QHBoxLayout(bg)

            self.dash_profile_combo = QComboBox()
            self._populate_profiles(self.dash_profile_combo)
            bl.addWidget(QLabel("Profile:"))
            bl.addWidget(self.dash_profile_combo)

            self.btn_backup_now = QPushButton("  Backup Now  ")
            bf = QFont(); bf.setPointSize(12); bf.setBold(True)
            self.btn_backup_now.setFont(bf)
            self.btn_backup_now.setFixedHeight(48)
            self.btn_backup_now.setIcon(QIcon.fromTheme("document-save"))
            self.btn_backup_now.clicked.connect(self._on_backup_now)
            bl.addWidget(self.btn_backup_now)

            layout.addWidget(bg)

            # Progress / log
            self.dash_progress = QProgressBar()
            self.dash_progress.setRange(0, 0)  # indeterminate
            self.dash_progress.setVisible(False)
            layout.addWidget(self.dash_progress)

            self.dash_log = QPlainTextEdit()
            self.dash_log.setReadOnly(True)
            self.dash_log.setMaximumHeight(150)
            self.dash_log.setVisible(False)
            layout.addWidget(self.dash_log)

            layout.addStretch()
            return w

        def _populate_profiles(self, combo: 'QComboBox'):
            combo.clear()
            for name in self.config.get("profiles", {}):
                combo.addItem(name)

        def _refresh_dashboard(self):
            history = load_history()
            if history:
                last = history[-1]
                self.dash_last_backup.setText(f"Last backup: {last.get('date', 'N/A')} [{last.get('profile')}]")
                status = last.get("status", "unknown")
                color = "#27ae60" if status == "success" else "#e74c3c"
                self.dash_last_status.setText(f"Status: <span style='color:{color}'><b>{status}</b></span>")
            else:
                self.dash_last_backup.setText("Last backup: never")
                self.dash_last_status.setText("Status: ---")

            # Check scheduled timers
            code, out, _ = run_backend(["schedule-status"])
            if out.strip() and "backup-manager" in out:
                lines = [l for l in out.strip().splitlines() if "backup-manager" in l]
                if lines:
                    self.dash_next.setText(f"Next scheduled: {lines[0][:50]}...")
                else:
                    self.dash_next.setText("Next scheduled: ---")
            else:
                self.dash_next.setText("Next scheduled: not configured")

            # Destination space
            profiles = self.config.get("profiles", {})
            if profiles:
                first = list(profiles.values())[0]
                dest = first.get("destination", "")
                if dest:
                    dest_expanded = dest.replace("~", str(Path.home()))
                    try:
                        usage = shutil.disk_usage(dest_expanded)
                        self.dash_dest_space.setText(
                            f"Destination space: {human_size(usage.free)} free / {human_size(usage.total)}"
                        )
                    except OSError:
                        self.dash_dest_space.setText(f"Destination: {dest} (not accessible)")
                else:
                    self.dash_dest_space.setText("Destination: not configured")

        def _on_backup_now(self):
            profile = self.dash_profile_combo.currentText()
            if not profile:
                QMessageBox.warning(self, "No Profile", "Create a backup profile first in Configure tab.")
                return

            dest = self.config.get("profiles", {}).get(profile, {}).get("destination", "")
            if not dest:
                QMessageBox.warning(self, "No Destination",
                                    f"Profile '{profile}' has no destination.\nConfigure it first.")
                return

            self._run_backup(profile)

        def _run_backup(self, profile: str):
            if self.worker and self.worker.isRunning():
                QMessageBox.information(self, "Busy", "A backup is already running.")
                return

            self.dash_log.clear()
            self.dash_log.setVisible(True)
            self.dash_progress.setVisible(True)
            self.btn_backup_now.setEnabled(False)
            self.statusBar().showMessage(f"Backing up: {profile}...")

            self.worker = BackupWorker(profile)
            self.worker.output.connect(self._on_backup_output)
            self.worker.finished_signal.connect(self._on_backup_finished)
            self.worker.start()

        def _on_backup_output(self, line: str):
            self.dash_log.appendPlainText(line)
            # Auto-scroll
            sb = self.dash_log.verticalScrollBar()
            sb.setValue(sb.maximum())

        def _on_backup_finished(self, success: bool):
            self.dash_progress.setVisible(False)
            self.btn_backup_now.setEnabled(True)
            if success:
                self.statusBar().showMessage("Backup completed successfully!", 10000)
                if self.tray:
                    self.tray.showMessage(APP_NAME, "Backup completed!",
                                          QSystemTrayIcon.MessageIcon.Information, 5000)
            else:
                self.statusBar().showMessage("Backup FAILED — check log", 10000)
                if self.tray:
                    self.tray.showMessage(APP_NAME, "Backup failed!",
                                          QSystemTrayIcon.MessageIcon.Critical, 5000)
            self._refresh_dashboard()
            self._refresh_history()

        # --- Configure ---

        def _build_configure(self) -> QWidget:
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(8)

            # Profile selector
            hl = QHBoxLayout()
            hl.addWidget(QLabel("Profile:"))
            self.cfg_profile_combo = QComboBox()
            self._populate_profiles(self.cfg_profile_combo)
            self.cfg_profile_combo.currentTextChanged.connect(self._load_profile_form)
            hl.addWidget(self.cfg_profile_combo, 1)

            btn_new = QPushButton("New")
            btn_new.clicked.connect(self._new_profile)
            hl.addWidget(btn_new)

            btn_del = QPushButton("Delete")
            btn_del.clicked.connect(self._delete_profile)
            hl.addWidget(btn_del)

            layout.addLayout(hl)

            # Profile form
            fg = QGroupBox("Profile Settings")
            fl = QGridLayout(fg)

            fl.addWidget(QLabel("Name:"), 0, 0)
            self.cfg_name = QLineEdit()
            fl.addWidget(self.cfg_name, 0, 1)

            fl.addWidget(QLabel("Destination:"), 1, 0)
            dest_layout = QHBoxLayout()
            self.cfg_dest = QLineEdit()
            self.cfg_dest.setPlaceholderText("/mnt/backup or ~/Backups")
            dest_layout.addWidget(self.cfg_dest)
            btn_browse = QPushButton("Browse...")
            btn_browse.clicked.connect(self._browse_dest)
            dest_layout.addWidget(btn_browse)
            fl.addLayout(dest_layout, 1, 1)

            fl.addWidget(QLabel("Type:"), 2, 0)
            self.cfg_type = QComboBox()
            self.cfg_type.addItems(["rsync", "archive"])
            fl.addWidget(self.cfg_type, 2, 1)

            fl.addWidget(QLabel("Keep versions:"), 3, 0)
            self.cfg_keep = QComboBox()
            self.cfg_keep.addItems(["3", "5", "10", "20", "50"])
            self.cfg_keep.setCurrentText("5")
            fl.addWidget(self.cfg_keep, 3, 1)

            layout.addWidget(fg)

            # Sources
            sg = QGroupBox("Sources")
            sl = QVBoxLayout(sg)
            self.cfg_sources = QListWidget()
            self.cfg_sources.setMaximumHeight(100)
            sl.addWidget(self.cfg_sources)
            src_btns = QHBoxLayout()
            btn_add_src = QPushButton("Add Source...")
            btn_add_src.clicked.connect(self._add_source)
            src_btns.addWidget(btn_add_src)
            btn_rm_src = QPushButton("Remove")
            btn_rm_src.clicked.connect(lambda: self.cfg_sources.takeItem(self.cfg_sources.currentRow()))
            src_btns.addWidget(btn_rm_src)
            sl.addLayout(src_btns)
            layout.addWidget(sg)

            # Excludes
            eg = QGroupBox("Exclude Patterns (one per line)")
            el = QVBoxLayout(eg)
            self.cfg_excludes = QPlainTextEdit()
            self.cfg_excludes.setMaximumHeight(100)
            el.addWidget(self.cfg_excludes)
            layout.addWidget(eg)

            # Save button
            btn_save = QPushButton("Save Profile")
            f = QFont(); f.setBold(True)
            btn_save.setFont(f)
            btn_save.clicked.connect(self._save_profile)
            layout.addWidget(btn_save)

            # Load first profile
            if self.cfg_profile_combo.count() > 0:
                self._load_profile_form(self.cfg_profile_combo.currentText())

            return w

        def _load_profile_form(self, name: str):
            if not name:
                return
            p = self.config.get("profiles", {}).get(name, {})
            self.cfg_name.setText(p.get("name", name))
            self.cfg_dest.setText(p.get("destination", ""))
            idx = self.cfg_type.findText(p.get("type", "rsync"))
            if idx >= 0:
                self.cfg_type.setCurrentIndex(idx)
            self.cfg_keep.setCurrentText(str(p.get("keep_versions", 5)))

            self.cfg_sources.clear()
            for s in p.get("sources", []):
                self.cfg_sources.addItem(s)

            excludes = p.get("exclude", [])
            self.cfg_excludes.setPlainText("\n".join(excludes))

        def _save_profile(self):
            profile_key = self.cfg_profile_combo.currentText()
            if not profile_key:
                return

            excludes_text = self.cfg_excludes.toPlainText().strip()
            excludes = [e.strip() for e in excludes_text.splitlines() if e.strip()]
            sources = [self.cfg_sources.item(i).text() for i in range(self.cfg_sources.count())]

            self.config.setdefault("profiles", {})[profile_key] = {
                "name": self.cfg_name.text() or profile_key,
                "sources": sources,
                "destination": self.cfg_dest.text(),
                "type": self.cfg_type.currentText(),
                "exclude": excludes,
                "keep_versions": int(self.cfg_keep.currentText()),
                "schedule": self.config.get("profiles", {}).get(profile_key, {}).get("schedule", "none"),
                "compress": self.cfg_type.currentText() == "archive",
                "enabled": True
            }
            save_config(self.config)
            self.statusBar().showMessage(f"Profile '{profile_key}' saved", 5000)

        def _new_profile(self):
            from PyQt6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "New Profile", "Profile name (no spaces):")
            if ok and name:
                name = name.strip().replace(" ", "_")
                self.config.setdefault("profiles", {})[name] = {
                    "name": name,
                    "sources": [str(Path.home())],
                    "destination": "",
                    "type": "rsync",
                    "exclude": [".cache/", ".local/share/Trash/", "__pycache__/", "node_modules/"],
                    "keep_versions": 5,
                    "schedule": "none",
                    "compress": False,
                    "enabled": True
                }
                save_config(self.config)
                self.cfg_profile_combo.addItem(name)
                self.cfg_profile_combo.setCurrentText(name)
                self.dash_profile_combo.addItem(name)

        def _delete_profile(self):
            name = self.cfg_profile_combo.currentText()
            if not name:
                return
            r = QMessageBox.question(self, "Delete Profile", f"Delete profile '{name}'?")
            if r == QMessageBox.StandardButton.Yes:
                self.config.get("profiles", {}).pop(name, None)
                save_config(self.config)
                idx = self.cfg_profile_combo.findText(name)
                if idx >= 0:
                    self.cfg_profile_combo.removeItem(idx)
                idx2 = self.dash_profile_combo.findText(name)
                if idx2 >= 0:
                    self.dash_profile_combo.removeItem(idx2)

        def _browse_dest(self):
            d = QFileDialog.getExistingDirectory(self, "Select Backup Destination")
            if d:
                self.cfg_dest.setText(d)

        def _add_source(self):
            d = QFileDialog.getExistingDirectory(self, "Select Source Directory")
            if d:
                # Store with ~ for portability
                home = str(Path.home())
                if d.startswith(home):
                    d = "~" + d[len(home):]
                self.cfg_sources.addItem(d)

        # --- Schedule ---

        def _build_schedule(self) -> QWidget:
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(12)

            g = QGroupBox("Configure Schedule")
            gl = QGridLayout(g)

            gl.addWidget(QLabel("Profile:"), 0, 0)
            self.sched_profile = QComboBox()
            self._populate_profiles(self.sched_profile)
            gl.addWidget(self.sched_profile, 0, 1)

            gl.addWidget(QLabel("Interval:"), 1, 0)
            self.sched_interval = QComboBox()
            self.sched_interval.addItems(["hourly", "daily", "weekly", "monthly"])
            self.sched_interval.setCurrentText("daily")
            gl.addWidget(self.sched_interval, 1, 1)

            btn_enable = QPushButton("Enable Schedule")
            btn_enable.clicked.connect(self._enable_schedule)
            gl.addWidget(btn_enable, 2, 0)

            btn_disable = QPushButton("Disable Schedule")
            btn_disable.clicked.connect(self._disable_schedule)
            gl.addWidget(btn_disable, 2, 1)

            layout.addWidget(g)

            # Current timers
            tg = QGroupBox("Active Timers")
            tl = QVBoxLayout(tg)
            self.sched_status = QPlainTextEdit()
            self.sched_status.setReadOnly(True)
            self.sched_status.setMaximumHeight(120)
            tl.addWidget(self.sched_status)

            btn_refresh = QPushButton("Refresh")
            btn_refresh.clicked.connect(self._refresh_schedule_status)
            tl.addWidget(btn_refresh)
            layout.addWidget(tg)

            layout.addStretch()

            self._refresh_schedule_status()
            return w

        def _enable_schedule(self):
            profile = self.sched_profile.currentText()
            interval = self.sched_interval.currentText()
            if not profile:
                return

            # Check destination is configured
            dest = self.config.get("profiles", {}).get(profile, {}).get("destination", "")
            if not dest:
                QMessageBox.warning(self, "No Destination",
                                    f"Configure a destination for '{profile}' first.")
                return

            code, out, err = run_backend(["schedule-install", profile, interval], timeout=15)
            if code == 0:
                self.statusBar().showMessage(f"Schedule enabled: {profile} ({interval})", 5000)
            else:
                QMessageBox.warning(self, "Error", f"Failed to set schedule:\n{err or out}")
            self._refresh_schedule_status()

        def _disable_schedule(self):
            profile = self.sched_profile.currentText()
            if not profile:
                return
            run_backend(["schedule-remove", profile], timeout=10)
            self.statusBar().showMessage(f"Schedule disabled: {profile}", 5000)
            self._refresh_schedule_status()

        def _refresh_schedule_status(self):
            code, out, _ = run_backend(["schedule-status"])
            self.sched_status.setPlainText(out.strip() if out.strip() else "No active timers")

        # --- History ---

        def _build_history(self) -> QWidget:
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(16, 12, 16, 12)

            self.history_list = QListWidget()
            self.history_list.itemClicked.connect(self._on_history_item_clicked)
            layout.addWidget(self.history_list)

            self.history_detail = QPlainTextEdit()
            self.history_detail.setReadOnly(True)
            self.history_detail.setMaximumHeight(120)
            layout.addWidget(self.history_detail)

            btns = QHBoxLayout()
            btn_refresh = QPushButton("Refresh")
            btn_refresh.clicked.connect(self._refresh_history)
            btns.addWidget(btn_refresh)
            btn_clear = QPushButton("Clear History")
            btn_clear.clicked.connect(self._clear_history)
            btns.addWidget(btn_clear)
            layout.addLayout(btns)

            self._refresh_history()
            return w

        def _refresh_history(self):
            self.history_list.clear()
            history = load_history()
            for entry in reversed(history):
                status = entry.get("status", "?")
                icon = "dialog-ok" if status == "success" else "dialog-error"
                size = human_size(entry.get("bytes", 0))
                text = f"{entry.get('date', '?')}  [{entry.get('profile', '?')}]  {status}  {size}  ({entry.get('duration_s', 0)}s)"
                item = QListWidgetItem(QIcon.fromTheme(icon), text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.history_list.addItem(item)

        def _on_history_item_clicked(self, item):
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry:
                self.history_detail.setPlainText(json.dumps(entry, indent=2))

        def _clear_history(self):
            r = QMessageBox.question(self, "Clear History", "Delete all backup history?")
            if r == QMessageBox.StandardButton.Yes:
                run_backend(["history-clear"])
                self._refresh_history()

        # --- Restore ---

        def _build_restore(self) -> QWidget:
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(12)

            info = QLabel(
                "Select a backup source (directory or .tar.zst archive)\n"
                "and a destination to restore files to."
            )
            info.setWordWrap(True)
            layout.addWidget(info)

            g = QGroupBox("Restore")
            gl = QGridLayout(g)

            gl.addWidget(QLabel("Backup source:"), 0, 0)
            src_l = QHBoxLayout()
            self.restore_src = QLineEdit()
            self.restore_src.setPlaceholderText("/path/to/backup or .tar.zst file")
            src_l.addWidget(self.restore_src)
            btn_src_dir = QPushButton("Dir...")
            btn_src_dir.clicked.connect(lambda: self._browse_field(self.restore_src, mode="dir"))
            src_l.addWidget(btn_src_dir)
            btn_src_file = QPushButton("File...")
            btn_src_file.clicked.connect(lambda: self._browse_field(self.restore_src, mode="file"))
            src_l.addWidget(btn_src_file)
            gl.addLayout(src_l, 0, 1)

            gl.addWidget(QLabel("Restore to:"), 1, 0)
            dst_l = QHBoxLayout()
            self.restore_dst = QLineEdit()
            self.restore_dst.setPlaceholderText("Target directory")
            dst_l.addWidget(self.restore_dst)
            btn_dst = QPushButton("Browse...")
            btn_dst.clicked.connect(lambda: self._browse_field(self.restore_dst, mode="dir"))
            dst_l.addWidget(btn_dst)
            gl.addLayout(dst_l, 1, 1)

            layout.addWidget(g)

            self.restore_log = QPlainTextEdit()
            self.restore_log.setReadOnly(True)
            self.restore_log.setMaximumHeight(150)
            self.restore_log.setVisible(False)
            layout.addWidget(self.restore_log)

            btn_restore = QPushButton("  Restore  ")
            f = QFont(); f.setPointSize(12); f.setBold(True)
            btn_restore.setFont(f)
            btn_restore.setFixedHeight(48)
            btn_restore.setIcon(QIcon.fromTheme("edit-undo"))
            btn_restore.clicked.connect(self._do_restore)
            layout.addWidget(btn_restore)

            # Quick restore from history
            hg = QGroupBox("Quick Restore from Last Backups")
            hl = QVBoxLayout(hg)
            self.restore_quick_list = QComboBox()
            self._populate_restore_quick()
            hl.addWidget(self.restore_quick_list)
            btn_quick = QPushButton("Use Selected")
            btn_quick.clicked.connect(self._use_quick_restore)
            hl.addWidget(btn_quick)
            layout.addWidget(hg)

            layout.addStretch()
            return w

        def _populate_restore_quick(self):
            self.restore_quick_list.clear()
            history = load_history()
            seen = set()
            for entry in reversed(history):
                if entry.get("status") == "success":
                    dest = entry.get("destination", "")
                    profile = entry.get("profile", "")
                    key = f"{profile}:{dest}"
                    if key not in seen and dest:
                        self.restore_quick_list.addItem(
                            f"{entry.get('date', '?')} [{profile}] — {dest}",
                            entry
                        )
                        seen.add(key)

        def _use_quick_restore(self):
            entry = self.restore_quick_list.currentData()
            if entry:
                dest = entry.get("destination", "")
                profile = entry.get("profile", "")
                btype = entry.get("type", "rsync")
                if btype == "archive":
                    # Find latest archive
                    self.restore_src.setText(dest)
                else:
                    self.restore_src.setText(f"{dest}/{profile}_latest")

        def _browse_field(self, field: QLineEdit, mode: str):
            if mode == "dir":
                d = QFileDialog.getExistingDirectory(self, "Select Directory")
            else:
                d, _ = QFileDialog.getOpenFileName(self, "Select File", "", "Archives (*.tar.zst);;All (*)")
            if d:
                field.setText(d)

        def _do_restore(self):
            src = self.restore_src.text().strip()
            dst = self.restore_dst.text().strip()
            if not src:
                QMessageBox.warning(self, "Missing Source", "Select a backup source.")
                return
            if not dst:
                QMessageBox.warning(self, "Missing Destination", "Select a restore destination.")
                return

            r = QMessageBox.question(
                self, "Confirm Restore",
                f"Restore from:\n  {src}\n\nTo:\n  {dst}\n\nExisting files may be overwritten. Continue?"
            )
            if r != QMessageBox.StandardButton.Yes:
                return

            self.restore_log.clear()
            self.restore_log.setVisible(True)
            self.statusBar().showMessage("Restoring...")

            try:
                proc = subprocess.Popen(
                    [get_backend(), "restore", src, dst],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )
                for line in proc.stdout:
                    self.restore_log.appendPlainText(line.rstrip())
                    QApplication.processEvents()
                proc.wait()
                if proc.returncode == 0:
                    self.statusBar().showMessage("Restore completed!", 10000)
                    QMessageBox.information(self, "Done", "Restore completed successfully.")
                else:
                    self.statusBar().showMessage("Restore failed", 10000)
                    QMessageBox.warning(self, "Error", "Restore failed. Check the log.")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

        # --- Tray ---

        def _show_raise(self):
            self.show()
            self.raise_()
            self.activateWindow()

        def _tray_click(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                if self.isVisible():
                    self.hide()
                else:
                    self._show_raise()

        def closeEvent(self, event):
            if self.tray and self.tray.isVisible():
                self.hide()
                event.ignore()
            else:
                event.accept()

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

    win = BackupManagerWindow()
    win.show()
    sys.exit(app.exec())


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show backup status")

    bp = sub.add_parser("backup", help="Run backup")
    bp.add_argument("profile", nargs="?", default="home")

    rp = sub.add_parser("restore", help="Restore from backup")
    rp.add_argument("--source", required=False, help="Backup source path")
    rp.add_argument("--dest", default=".", help="Restore destination")

    sp = sub.add_parser("schedule", help="Manage schedule")
    sp.add_argument("profile", nargs="?", default="home")
    sp.add_argument("--interval", choices=["hourly", "daily", "weekly", "monthly"])
    sp.add_argument("--remove", action="store_true")

    args = parser.parse_args()
    run_cli(args)


if __name__ == "__main__":
    main()
