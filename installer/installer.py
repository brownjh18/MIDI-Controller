"""One-click setup for the MIDI Bridge on Windows.

Installs everything the phone controller needs on a laptop:

  * the bridge itself (a self-contained exe, no Python required),
  * the Ableton remote script, in Live's user library,
  * the REAPER companion script, in REAPER's Scripts folder,
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
LAUNCHER = "launch_midibridge.ps1"
# Used when Program Files cannot be written (Windows sometimes keeps a handle
# on an old exe, so replacing it fails even with administrator rights).
FALLBACK_DIR = os.path.join(
    os.environ.get(
        "LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")
    ),
    "MidiBridge",
)
PORT_NAME = "AbletonBridge"
PORT = 8765
FIREWALL_RULE = "MIDI Bridge (TCP 8765)"
FIREWALL_PROGRAM_RULE = "MIDI Bridge (program)"
SCRIPT_FOLDER = "AbletonBridgeMixer"
REAPER_PAYLOAD = "ReaperScripts"
LOOPMIDI_URL = "https://www.tobias-erichsen.de/wp-content/uploads/2020/01/loopMIDISetup_1_0_16_27.zip"
LOOPMIDI_PAGE = "https://www.tobias-erichsen.de/software/loopmidi.html"
DRIVER_DLL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "teVirtualMIDI64.dll"
)
# Studio One side: where the companion script and the Control Link surface data live.
STUDIOONE_INSTALL_ROOT = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "PreSonus",
    "Studio One 6",
)
STUDIOONE_SCRIPTS_DIR = os.path.join(STUDIOONE_INSTALL_ROOT, "Scripts")
STUDIOONE_SURFACE_DATA_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming")),
    "PreSonus",
    "Studio One 6",
    "Surface Data[1]",
)
SURFACEDATA_NAME = "Jarvis Bridge.surfacedata"
STUDIOONE_EXE = os.path.join(STUDIOONE_INSTALL_ROOT, "Studio One.exe")


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


def live_install_roots():
    """Every Ableton Live install folder Windows knows about."""
    roots = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Ableton"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Ableton"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Ableton"
        ),
    ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            found.extend(
                os.path.join(root, name)
                for name in os.listdir(root)
                if name.lower().startswith("live")
            )
        except OSError:
            pass
    return found


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def port_open(port=PORT):
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def studioone_running():
    """True when Studio One is currently running."""
    try:
        import subprocess as _sp

        done = _sp.run(
            ["tasklist", "/FI", "IMAGENAME eq Studio One.exe", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    return "Studio One.exe" in (done.stdout or "")


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


def make_shortcut(path, target, working_dir, minimized=True, arguments=""):
    window_style = 7 if minimized else 1
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath = '%s';"
        "$s.Arguments = '%s';"
        "$s.WorkingDirectory = '%s';"
        "$s.WindowStyle = %d;"
        "$s.Description = 'MIDI Bridge';"
        "$s.Save()" % (path, target, arguments, working_dir, window_style)
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
def _copy_with_retry(source, target, attempts=3):
    """Copies a file, retrying around Windows file locks."""
    last_error = None
    for _ in range(attempts):
        try:
            shutil.copy2(source, target)
            return True, None
        except OSError as error:
            last_error = error
            # A stale copy may be what is holding the lock; clear it if we can.
            try:
                os.remove(target)
            except OSError:
                pass
            time.sleep(1.0)
    return False, last_error


def step_copy_launcher(directory, log):
    """Drops the newest-build launcher next to the installed bridge."""
    source = payload(LAUNCHER)
    if not os.path.isfile(source):
        # Older payloads shipped the file under its source name.
        source = payload("launcher.ps1")
    if not os.path.isfile(source):
        log("Launcher was not bundled - skipped.")
        return
    try:
        shutil.copy2(source, os.path.join(directory, LAUNCHER))
        log("Launcher installed next to the bridge.")
    except OSError as error:
        log("Launcher skipped (%s)" % error)


def step_copy_bridge(log):
    """Installs the bridge exe, falling back to a user-writable folder.

    Program Files is sometimes protected: Windows keeps a handle on an old exe
    even after its process exits, so replacing it fails with Access denied.
    In that case the bridge goes to %LOCALAPPDATA%\\MidiBridge instead, and the
    shortcuts and the running process use that copy.
    """
    source = payload(BRIDGE_EXE)
    for directory in (INSTALL_DIR, FALLBACK_DIR):
        target = os.path.join(directory, BRIDGE_EXE)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            log("Cannot create %s (%s)" % (shorten(directory), error))
            continue
        ok, error = _copy_with_retry(source, target)
        if not ok:
            log("Could not write %s (%s)" % (shorten(target), error))
            continue
        if directory == FALLBACK_DIR:
            log("Program Files is locked - installed to %s" % target)
        else:
            log("Bridge installed to %s" % target)
        step_copy_launcher(directory, log)
        return target
    raise RuntimeError("the bridge could not be copied to any writable folder")


def step_copy_script(log):
    """Drops the remote script where every Live version looks for it.

    Two places get the script:

    * Documents\\Ableton\\User Library\\MIDI Remote Scripts - the standard
      location, and the one that survives Live reinstalls;
    * every installed Live version's own Resources\\MIDI Remote Scripts
      folder, so even versions with User-Library scanning disabled, or
      oddities with OneDrive-redirected documents, still pick it up the
      moment Live restarts.
    """
    source = payload(SCRIPT_FOLDER)

    library = os.path.join(
        documents_dir(), "Ableton", "User Library", "MIDI Remote Scripts"
    )
    target = os.path.join(library, SCRIPT_FOLDER)
    os.makedirs(target, exist_ok=True)
    shutil.copy2(
        os.path.join(source, "__init__.py"), os.path.join(target, "__init__.py")
    )
    # A stale cache stops Live from noticing a replaced script.
    shutil.rmtree(os.path.join(target, "__pycache__"), ignore_errors=True)
    log("Ableton script installed to %s" % target)

    installed_into = []
    for live in live_install_roots():
        for scripts in (
            os.path.join(live, "Program", "Resources", "MIDI Remote Scripts"),
            os.path.join(live, "Resources", "MIDI Remote Scripts"),
        ):
            if not os.path.isdir(scripts):
                continue
            destination = os.path.join(scripts, SCRIPT_FOLDER)
            try:
                os.makedirs(destination, exist_ok=True)
                shutil.copy2(
                    os.path.join(source, "__init__.py"),
                    os.path.join(destination, "__init__.py"),
                )
                shutil.rmtree(
                    os.path.join(destination, "__pycache__"), ignore_errors=True
                )
                installed_into.append(destination)
            except OSError as error:
                log("Could not write to %s (%s)" % (shorten(destination), error))
    if installed_into:
        log(
            "Copied into %d Live installation(s): %s"
            % (len(installed_into), "; ".join(shorten(p, 40) for p in installed_into))
        )
    else:
        log("No extra Live install folders found (User Library copy is enough).")
    return target


def step_copy_reaper_script(log):
    """Installs the REAPER companion script and its startup loader."""
    source = payload(REAPER_PAYLOAD)
    if not os.path.isdir(source):
        log("REAPER script was not bundled - skipped.")
        return None
    scripts = os.path.join(os.environ.get("APPDATA", ""), "REAPER", "Scripts")
    target = os.path.join(scripts, "JarvisBridge")
    os.makedirs(target, exist_ok=True)
    shutil.copy2(
        os.path.join(source, "JarvisBridge.lua"),
        os.path.join(target, "JarvisBridge.lua"),
    )

    loader = (
        "-- Loads the MIDI Controller companion script when REAPER starts.\n"
        "local separator = package.config:sub(1, 1)\n"
        "local script_path = reaper.GetResourcePath() .. separator .. \"Scripts\""
        " .. separator .. \"JarvisBridge\" .. separator .. \"JarvisBridge.lua\"\n"
        "local handle = io.open(script_path, \"rb\")\n"
        "if handle then\n"
        "  handle:close()\n"
        "  dofile(script_path)\n"
        "end\n"
    )
    startup = os.path.join(scripts, "__startup.lua")
    try:
        existing = ""
        if os.path.isfile(startup):
            with open(startup, "r", encoding="utf-8", errors="replace") as handle:
                existing = handle.read()
        if "JarvisBridge" not in existing:
            with open(startup, "a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("\n" + loader)
            log("REAPER startup loader added.")
    except OSError as error:
        log("REAPER startup loader skipped (%s)" % error)

    log("REAPER script installed to %s" % target)
    return target


def step_copy_studioone_script(log):
    """Installs the Studio One companion script and its startup loader.

    It uses the FrameworkService category, which is auto-started when Studio
    One starts. The service reads commands.json and writes state.json, then
    exposes itself via Host.Objects.registerObject so other scripts can query
    the current DAW state.
    """
    source = payload("StudioOne")
    if not os.path.isdir(source):
        log("Studio One script was not bundled - skipped.")
        return None
    scripts = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        "PreSonus",
        "Studio One 6",
        "Scripts",
    )
    target = os.path.join(scripts, "JarvisBridge")
    os.makedirs(target, exist_ok=True)
    for file in (
        "metainfo.xml",
        "classfactory.xml",
        "jarvisbridge.device",
        "jarvisbridge.js",
        "jarvisbridge.surface.xml",
        "JarvisBridgeDevice.js",
        "JarvisBridgeComponent.js",
        "JarvisBridge.txt",
        "skin/skin.xml",
    ):
        src_path = os.path.join(source, file)
        dst_path = os.path.join(target, file)
        if os.path.isfile(src_path):
            shutil.copy2(src_path, dst_path)
    log("Studio One script installed to %s" % target)
    return target


def step_install_studioone_surfacdata(log):
    """Installs the Studio One Control Link surface data for the bridge.

    The surfacedata file carries the Mackie-Control surface associations that
    Studio One uses to fill the generic parameter strips (titles + values) of
    the focused plugin. Without a valid surfacedata the Control Link fill stays
    empty and the phone controller has no device parameters to show.
    """
    source = payload("StudioOne", SURFACEDATA_NAME)
    fallback = os.path.join(SURFACE_DATA_DIR, SURFACEDATA_NAME)
    if not os.path.isfile(source):
        source = fallback
    if not os.path.isfile(source):
        log("Studio One surface data was not bundled - skipped.")
        return False
    try:
        os.makedirs(SURFACE_DATA_DIR, exist_ok=True)
        shutil.copy2(source, os.path.join(SURFACE_DATA_DIR, SURFACEDATA_NAME))
        log(
            "Studio One Control Link surface data installed to %s"
            % SURFACE_DATA_DIR
        )
        return True
    except OSError as error:
        log("Studio One surface data install skipped (%s)" % error)
        return False


def step_restart_studioone(log):
    """Fully restarts Studio One so it reloads the bridge component and the
    freshly installed Control Link surface data."""
    if not os.path.isfile(STUDIOONE_EXE):
        log("Studio One exe not found - restart skipped.")
        return False
    log("Restarting Studio One...")
    run(["taskkill", "/IM", "Studio One.exe", "/F"])
    for _ in range(30):
        if not studioone_running():
            break
        time.sleep(1.0)
    os.startfile(STUDIOONE_EXE)
    for _ in range(30):
        if studioone_running():
            break
        time.sleep(1.0)
    if studioone_running():
        log("Studio One restarted.")
        return True
    log("Studio One did not come back up - restart may need a manual launch.")
    return False


def step_firewall(log, exe=None):
    if not is_admin():
        log("Skipped the firewall rule (needs administrator).")
        return False
    # Allow every Windows network profile. A USB-tethered iPhone often shows
    # up as an unidentified/Public network, and a Private-only rule silently
    # blocks the app even though the bridge is listening on 0.0.0.0.
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
            "profile=any",
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
                "profile=any",
            ]
        )
    if ok:
        log("Firewall open for the phone on port %d (all network profiles)." % PORT)
    else:
        log("Firewall rule failed: %s" % output.strip()[:200])
    return ok


def _launch_target(exe):
    """Shortcut target: the launcher when present, else the bridge itself."""
    launcher = os.path.join(os.path.dirname(exe), LAUNCHER)
    if os.path.isfile(launcher):
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        arguments = (
            '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%s"'
            % launcher
        )
        return powershell, arguments
    return exe, ""


def step_shortcuts(exe, log):
    target, arguments = _launch_target(exe)
    working_dir = os.path.dirname(exe)
    start_menu = os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
    )
    menu_ok = make_shortcut(
        os.path.join(start_menu, APP_NAME + ".lnk"),
        target,
        working_dir,
        arguments=arguments,
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
            os.path.join(startup, APP_NAME + ".lnk"),
            target,
            working_dir,
            arguments=arguments,
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


def step_start(exe, log):
    if not stop_old_bridge(log):
        log("Port %d is still held by another program - left it alone." % PORT)
        return True
    start_bridge(exe)
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

            # Stop the old bridge first: a running exe keeps a lock that makes
            # replacing it fail on Windows.
            stop_old_bridge(self.log)

            exe = step_copy_bridge(self.log)
            results.append(("Bridge program", shorten(exe), True))

            script = step_copy_script(self.log)
            results.append(("Ableton remote script", shorten(script), True))

            reaper_script = step_copy_reaper_script(self.log)
            if reaper_script:
                results.append(("REAPER script", shorten(reaper_script), True))

            studioone_script = step_copy_studioone_script(self.log)
            if studioone_script:
                results.append(("Studio One script", shorten(studioone_script), True))

            surfacedata_ok = step_install_studioone_surfacdata(self.log)
            results.append(
                (
                    "Studio One Control Link data",
                    "installed" if surfacedata_ok else "skipped",
                    surfacedata_ok,
                )
            )

            restart_ok = step_restart_studioone(self.log)
            results.append(
                (
                    "Studio One restart",
                    "done" if restart_ok else "skipped",
                    restart_ok,
                )
            )

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

            running_ok = step_start(exe, self.log)
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
            "Exit Ableton Live completely (if it is open), then reopen it:",
            "the control surface list is only scanned at startup.",
            "Preferences > Link, Tempo & MIDI",
            "Control Surface:  AbletonBridgeMixer",
            "(spelled like that - it sits under the A's, above Push)",
            "Input / Output:   AbletonBridge",
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
