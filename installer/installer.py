"""One-click setup for the MIDI Bridge on Windows.

Installs everything the phone controller needs on a laptop:

  * the bridge itself (a self-contained exe, no Python required),
  * the Ableton remote script, in Live's user library,
  * a firewall rule for the phone connection,
  * a Start Menu shortcut and an entry that starts the bridge with Windows,
  * a running bridge using the virtual MIDI port "AbletonBridge".

The only manual step left is picking the control surface inside Live.
"""

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import messagebox, ttk

APP_NAME = "MIDI Bridge"
INSTALL_DIR = os.environ.get(
    "MIDI_BRIDGE_INSTALL_DIR",
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "MidiBridge"),
)
BRIDGE_EXE = "MidiBridge.exe"
PORT_NAME = "AbletonBridge"
PORT = 8765
FIREWALL_RULE = "MIDI Bridge (TCP 8765)"
FIREWALL_PROGRAM_RULE = "MIDI Bridge (program)"
SCRIPT_FOLDER = "AbletonBridgeMixer"
LOOPMIDI_URL = "https://www.tobias-erichsen.de/wp-content/uploads/2020/01/loopMIDISetup_1_0_16_27.zip"
LOOPMIDI_PAGE = "https://www.tobias-erichsen.de/software/loopmidi.html"
DRIVER_DLL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "teVirtualMIDI64.dll"
)


# ----------------------------------------------------------------------
# Paths and Windows helpers
# ----------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_FOLDERID_DOCUMENTS = _GUID(
    0xFDD39AD0,
    0x238F,
    0x46AF,
    (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
)


def documents_dir():
    """The real Documents folder (OneDrive may have redirected it)."""
    path = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(_FOLDERID_DOCUMENTS), 0, None, ctypes.byref(path)
    )
    if result != 0:
        return os.path.join(os.path.expanduser("~"), "Documents")
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def payload(*parts):
    """A file shipped inside this exe."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload")
    return os.path.join(base, *parts)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def port_open(port=PORT):
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def run(command):
    """Runs a console command quietly; returns (ok, output)."""
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        return False, str(error)
    return done.returncode == 0, (done.stdout or "") + (done.stderr or "")


def make_shortcut(path, target, working_dir, minimized=True):
    window_style = 7 if minimized else 1
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath = '%s';"
        "$s.WorkingDirectory = '%s';"
        "$s.WindowStyle = %d;"
        "$s.Description = 'MIDI Bridge';"
        "$s.Save()" % (path, target, working_dir, window_style)
    )
    ok, _ = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return ok and os.path.isfile(path)


def start_bridge(exe):
    env = dict(os.environ)
    env["MIDI_OUTPUT_PORT"] = PORT_NAME
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 6  # minimised: running, but out of the way
    subprocess.Popen([exe], env=env, startupinfo=info, close_fds=True)


def shorten(text, limit=64):
    """Trims a long path for the checklist, keeping its meaningful tail."""
    text = str(text)
    if len(text) <= limit:
        return text
    return "\u2026" + text[-(limit - 1):]


# ----------------------------------------------------------------------
# Install steps
# ----------------------------------------------------------------------
def step_copy_bridge(log):
    os.makedirs(INSTALL_DIR, exist_ok=True)
    target = os.path.join(INSTALL_DIR, BRIDGE_EXE)
    shutil.copy2(payload(BRIDGE_EXE), target)
    log("Bridge installed to %s" % target)
    return target


def step_copy_script(log):
    scripts = os.path.join(documents_dir(), "Ableton", "User Library", "MIDI Remote Scripts")
    target = os.path.join(scripts, SCRIPT_FOLDER)
    os.makedirs(target, exist_ok=True)
    source = payload(SCRIPT_FOLDER)
    shutil.copy2(os.path.join(source, "__init__.py"), os.path.join(target, "__init__.py"))
    log("Ableton script installed to %s" % target)
    return target


def step_firewall(log, exe=None):
    if not is_admin():
        log("Skipped the firewall rule (needs administrator).")
        return False
    run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + FIREWALL_RULE])
    ok, output = run(
        [
            "netsh",
            "advfirewall",
            "firewall",
            "add",
            "rule",
            "name=" + FIREWALL_RULE,
            "dir=in",
            "action=allow",
            "protocol=TCP",
            "localport=" + str(PORT),
            "profile=private",
        ]
    )
    if exe and os.path.isfile(exe):
        # Also allow the program itself, so Windows never shows its own prompt.
        run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "delete",
                "rule",
                "name=" + FIREWALL_PROGRAM_RULE,
            ]
        )
        run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                "name=" + FIREWALL_PROGRAM_RULE,
                "dir=in",
                "action=allow",
                "program=" + exe,
                "profile=private",
            ]
        )
    if ok:
        log("Firewall open for the phone on port %d (private networks)." % PORT)
    else:
        log("Firewall rule failed: %s" % output.strip()[:200])
    return ok


def step_shortcuts(exe, log):
    start_menu = os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
    )
    menu_ok = make_shortcut(
        os.path.join(start_menu, APP_NAME + ".lnk"), exe, INSTALL_DIR
    )
    if menu_ok:
        log("Start Menu shortcut created.")
    else:
        log("Start Menu shortcut skipped (needs administrator).")

    startup_ok = False
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
    )
    if os.path.isdir(startup):
        startup_ok = make_shortcut(
            os.path.join(startup, APP_NAME + ".lnk"), exe, INSTALL_DIR
        )
        if startup_ok:
            log("Bridge will start with Windows.")
    return menu_ok, startup_ok


def stop_old_bridge(log):
    """Closes a bridge from an earlier copy so the installed one can take over."""
    if not port_open():
        return True
    log("An older bridge is running - restarting it from the installed copy.")
    run(["taskkill", "/IM", BRIDGE_EXE, "/F"])
    for _ in range(20):
        if not port_open():
            return True
        time.sleep(0.5)
    return False


def step_start(log):
    if not stop_old_bridge(log):
        log("Port %d is still held by another program - left it alone." % PORT)
        return True
    start_bridge(os.path.join(INSTALL_DIR, BRIDGE_EXE))
    for _ in range(40):
        time.sleep(0.5)
        if port_open():
            log("Bridge started and listening on port %d." % PORT)
            return True
    log("The bridge did not answer on port %d yet - check the log window." % PORT)
    return False


def driver_installed():
    return os.path.isfile(DRIVER_DLL)


def ensure_midi_driver(log):
    """Installs loopMIDI's virtual MIDI driver when the laptop has none.

    The bridge creates the "AbletonBridge" port itself through that driver, so
    this is the only third-party piece the setup needs.
    """
    if driver_installed():
        log("Virtual MIDI driver already installed.")
        return True

    log("Installing the virtual MIDI driver (loopMIDI)...")
    work = tempfile.mkdtemp(prefix="midibridge-driver-")
    try:
        archive = payload("loopMIDISetup.zip")
        if not os.path.isfile(archive):
            archive = os.path.join(work, "loopMIDISetup.zip")
            urllib.request.urlretrieve(LOOPMIDI_URL, archive)
        with zipfile.ZipFile(archive) as package:
            package.extractall(work)

        setup_exe = ""
        for root, _dirs, files in os.walk(work):
            for name in files:
                if name.lower().endswith(".exe"):
                    setup_exe = os.path.join(root, name)
                    break
            if setup_exe:
                break
        if not setup_exe:
            log("The driver package did not contain an installer.")
            return False

        # loopMIDI ships a WiX bundle: /quiet is its silent switch.
        subprocess.run([setup_exe, "/quiet", "/norestart"], check=False)
        for _ in range(60):
            if driver_installed():
                log("Virtual MIDI driver installed.")
                return True
            time.sleep(1)
        log("The driver has not appeared yet - a reboot may be required.")
        return False
    except Exception as error:
        log("Driver installation failed: %s" % error)
        log("You can install it by hand: %s" % LOOPMIDI_PAGE)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------
# Window
# ----------------------------------------------------------------------
class Setup(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME + " Setup")
        self.geometry("780x720")
        self.minsize(700, 640)
        self.configure(bg="#14161a")
        self.busy = False
        self.checklist = []

        style = ttk.Style(self)
        style.theme_use("clam")

        # The clam theme paints its own pale buttons; restyle them for the
        # dark window so the labels stay readable in every state.
        style.configure(
            "TButton",
            background="#252b36",
            foreground="#e7ecf3",
            bordercolor="#3c4553",
            lightcolor="#252b36",
            darkcolor="#252b36",
            focuscolor="#252b36",
            relief="flat",
            padding=(14, 7),
            font=("Segoe UI", 9),
        )
        style.map(
            "TButton",
            background=[("pressed", "#1b212b"), ("active", "#303846")],
            foreground=[("disabled", "#6c7686"), ("active", "#ffffff")],
            bordercolor=[("active", "#4d5869")],
        )
        style.configure(
            "Install.TButton",
            background="#2f6fed",
            foreground="#ffffff",
            bordercolor="#2f6fed",
            lightcolor="#2f6fed",
            darkcolor="#2f6fed",
            font=("Segoe UI", 11, "bold"),
            padding=(20, 10),
        )
        style.map(
            "Install.TButton",
            background=[
                ("disabled", "#2c3d5c"),
                ("pressed", "#2459c4"),
                ("active", "#3f7dff"),
            ],
            foreground=[("disabled", "#8fa3c0"), ("active", "#ffffff")],
            bordercolor=[("active", "#3f7dff")],
        )
        style.configure(
            "TProgressbar",
            background="#2f6fed",
            troughcolor="#1b2029",
            bordercolor="#1b2029",
            lightcolor="#2f6fed",
            darkcolor="#2f6fed",
        )

        # The action bar is packed first, anchored to the bottom: whatever the
        # checklist below the log adds, the buttons keep their room.
        bar = tk.Frame(self, bg="#14161a")
        bar.pack(side="bottom", fill="x", padx=24, pady=(0, 18))
        self.bar = bar

        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="left")

        self.install_button = ttk.Button(
            bar, text="Install", style="Install.TButton", command=self.install
        )
        self.install_button.pack(side="right")
        self.close_button = ttk.Button(bar, text="Close", command=self.destroy)
        self.close_button.pack(side="right", padx=(0, 10))

        head = tk.Frame(self, bg="#14161a")
        head.pack(side="top", fill="x", padx=24, pady=(22, 8))
        tk.Label(
            head,
            text="MIDI Bridge",
            bg="#14161a",
            fg="#f2f5f9",
            font=("Segoe UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            head,
            text="Lets the controller app on your phone drive Ableton Live over Wi-Fi.",
            bg="#14161a",
            fg="#8d97a6",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        status = tk.Frame(self, bg="#14161a")
        status.pack(side="top", fill="x", padx=24, pady=(10, 0))
        self.driver_label = tk.Label(
            status, text="", bg="#14161a", font=("Segoe UI", 9, "bold")
        )
        self.driver_label.pack(side="left")
        ttk.Button(status, text="Install MIDI driver", command=self.install_driver).pack(
            side="right"
        )
        ttk.Button(status, text="Check again", command=self.refresh_driver).pack(
            side="right", padx=(0, 8)
        )
        if not is_admin():
            ttk.Button(
                status,
                text="Restart as administrator",
                command=self.relaunch_admin,
            ).pack(side="right", padx=(0, 8))

        # Filled in when the install finishes: one line per thing that was set up.
        self.checklist_frame = tk.Frame(self, bg="#0d0f12")

        self.log_view = tk.Text(
            self,
            bg="#0d0f12",
            fg="#c7d0dc",
            insertbackground="#c7d0dc",
            font=("Consolas", 9),
            relief="flat",
            padx=14,
            pady=12,
            wrap="word",
            height=7,
        )
        self.log_view.pack(side="top", fill="both", expand=True, padx=24, pady=10)
        self.log_view.configure(state="disabled")

        self.log("Ready. Everything is installed automatically.")
        self.refresh_driver()
        if not is_admin():
            self.log(
                "Not running as administrator: the firewall rule and the "
                "Program Files install will be skipped."
            )
        if "--auto" in sys.argv:
            self.after(600, self.install)

    # -- helpers -------------------------------------------------------
    def refresh_driver(self):
        if driver_installed():
            self.driver_label.configure(
                text="MIDI driver: installed", fg="#5bd08a"
            )
        else:
            self.driver_label.configure(
                text="MIDI driver: not installed - the setup will add it", fg="#e2705f"
            )

    def relaunch_admin(self):
        """Same exe, this time with the UAC prompt, so every step can run."""
        if getattr(sys, "frozen", False):
            target, params = sys.executable, None
        else:
            target, params = sys.executable, '"%s"' % os.path.abspath(__file__)
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, None, 1
        )
        if result > 32:
            self.destroy()

    def install_driver(self):
        """Manual button: same step the install runs, on demand."""
        if self.busy:
            return
        self.busy = True
        self.install_button.configure(state="disabled")
        self.progress.start(12)
        threading.Thread(target=self._driver_worker, daemon=True).start()

    def _driver_worker(self):
        try:
            ensure_midi_driver(self.log)
        except Exception as error:
            self.log("Driver installation failed: %s" % error)
        finally:
            self.after(0, self.finished)

    def open_driver(self):
        os.startfile(LOOPMIDI_PAGE)

    def log(self, message):
        self.log_view.configure(state="normal")
        self.log_view.insert("end", message + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")
        self.update_idletasks()

    def install(self):
        if self.busy:
            return
        self.busy = True
        self.clear_checklist()
        self.install_button.configure(state="disabled")
        self.progress.start(12)
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        results = []
        try:
            self.log("")
            driver_ok = ensure_midi_driver(self.log)
            results.append(
                (
                    "Virtual MIDI driver",
                    "installed (loopMIDI)" if driver_ok else "could not be installed",
                    driver_ok,
                )
            )

            exe = step_copy_bridge(self.log)
            results.append(("Bridge program", shorten(exe), True))

            script = step_copy_script(self.log)
            results.append(("Ableton remote script", shorten(script), True))

            firewall_ok = step_firewall(self.log, exe)
            results.append(
                (
                    "Firewall",
                    "port %d open on private networks" % PORT
                    if firewall_ok
                    else "skipped - needs administrator",
                    firewall_ok,
                )
            )

            menu_ok, startup_ok = step_shortcuts(exe, self.log)
            results.append(("Start Menu shortcut", APP_NAME, menu_ok))
            results.append(("Starts with Windows", "runs at sign-in", startup_ok))

            running_ok = step_start(self.log)
            results.append(
                (
                    "Bridge running",
                    "listening on port %d" % PORT
                    if running_ok
                    else "not answering yet",
                    running_ok,
                )
            )
        except Exception as error:  # keep the window usable on any failure
            self.log("Failed: %s" % error)
            results.append(("Setup", "failed: %s" % error, False))
        self.checklist = results
        self.after(0, self.finished)

    def finished(self):
        self.busy = False
        self.progress.stop()
        self.install_button.configure(state="normal", text="Run again")
        self.refresh_driver()
        self.show_checklist()

    # -- checklist -----------------------------------------------------
    def clear_checklist(self):
        for child in self.checklist_frame.winfo_children():
            child.destroy()
        self.checklist_frame.pack_forget()
        self.checklist = []

    def show_checklist(self):
        for child in self.checklist_frame.winfo_children():
            child.destroy()

        items = self.checklist
        if not items:
            return

        failures = [item for item in items if not item[2]]
        header = "Everything is installed" if not failures else "Installed with warnings"
        tk.Label(
            self.checklist_frame,
            text=header,
            bg="#0d0f12",
            fg="#5bd08a" if not failures else "#e2b25f",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 2))

        if failures:
            tk.Label(
                self.checklist_frame,
                text="Restart the setup as administrator to add the crossed-out items.",
                bg="#0d0f12",
                fg="#95897a",
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(fill="x", padx=14, pady=(0, 6))
        else:
            tk.Label(
                self.checklist_frame,
                text="",
                bg="#0d0f12",
                height=1,
            ).pack(fill="x")

        for label, detail, ok in items:
            row = tk.Frame(self.checklist_frame, bg="#0d0f12")
            row.pack(fill="x", padx=14, pady=1)
            tk.Label(
                row,
                text="\u2713" if ok else "\u2717",
                bg="#0d0f12",
                fg="#5bd08a" if ok else "#e2705f",
                font=("Segoe UI", 11, "bold"),
                width=2,
            ).pack(side="left")
            tk.Label(
                row,
                text=label,
                bg="#0d0f12",
                fg="#e7ecf3",
                font=("Segoe UI", 9, "bold"),
                width=22,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=detail,
                bg="#0d0f12",
                fg="#8d97a6",
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)

        turn = tk.Frame(self.checklist_frame, bg="#1e1a10")
        turn.pack(fill="x", padx=8, pady=(12, 10))
        tk.Label(
            turn,
            text="One step left, inside Ableton Live",
            bg="#1e1a10",
            fg="#e2b25f",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 2))
        for line in (
            "Preferences > Link, Tempo & MIDI",
            "Control Surface:  AbletonBridgeMixer",
            "Input / Output:   %s" % PORT_NAME,
        ):
            tk.Label(
                turn,
                text=line,
                bg="#1e1a10",
                fg="#d8c9a6",
                font=("Consolas", 9),
                anchor="w",
            ).pack(fill="x", padx=8)
        tk.Label(
            turn,
            text="Then open the app on your phone - it finds the bridge by itself.",
            bg="#1e1a10",
            fg="#95897a",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=8, pady=(4, 8))

        self.checklist_frame.pack(
            side="bottom", fill="x", padx=24, pady=(0, 10), before=self.log_view
        )


def main():
    if not is_admin() and "--no-elevate" not in sys.argv:
        # Re-launch with the UAC prompt so the firewall rule can be added.
        if getattr(sys, "frozen", False):
            target, params = sys.executable, None
        else:
            target, params = sys.executable, '"%s"' % os.path.abspath(__file__)
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, None, 1
        )
        if result > 32:
            return
        # The user declined the prompt: carry on unsigned rather than refuse.
    if not os.path.isfile(payload(BRIDGE_EXE)):
        messagebox.showerror(APP_NAME, "The installer is missing its bridge payload.")
        return
    Setup().mainloop()


if __name__ == "__main__":
    main()
