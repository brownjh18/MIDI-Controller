"""Exercises the installer's steps without the window (development aid).

Runs the same functions the Install button calls, into a temporary folder, and
checks the pieces that do not need administrator rights.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MIDI_BRIDGE_INSTALL_DIR"] = os.path.join(tempfile.gettempdir(), "MidiBridgeTest")

import installer as setup  # noqa: E402


def log(message):
    print("   " + message)


def main():
    print("payload bridge:", setup.payload(setup.BRIDGE_EXE))
    exe = setup.step_copy_bridge(log)
    print("   exe copied:", os.path.isfile(exe))

    scripts = setup.step_copy_script(log)
    print("   script file:", os.path.isfile(os.path.join(scripts, "__init__.py")))

    shortcut = os.path.join(tempfile.gettempdir(), "MidiBridgeTest.lnk")
    ok = setup.make_shortcut(shortcut, exe, setup.INSTALL_DIR)
    print("   shortcut:", ok, os.path.isfile(shortcut))

    print("   driver present:", setup.driver_installed())
    print("   port already in use:", setup.port_open())
    print("   start:", setup.step_start(log))

    shutil.rmtree(shortcut, ignore_errors=True)


if __name__ == "__main__":
    main()
