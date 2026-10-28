# Using the controller with a different laptop

The APK needs nothing new. Everything below happens **once, on the new laptop**.
After that, opening the app on the same Wi-Fi connects by itself.

## 1. Copy the project

Copy the whole `midi_bridge` folder to the new laptop (it contains the bridge and
the Ableton remote script).

## 2. Install Python + bridge dependencies

Install Python 3.11–3.13 from python.org (tick "Add python.exe to PATH"), then:

```powershell
cd C:\path\to\midi_bridge
python -m pip install -r requirements.txt
```

If `python-rtmidi` fails to build, that is fine — install the other two and the
bridge falls back to the built-in Windows MIDI API:

```powershell
python -m pip install mido websockets
```

## 3. Install a MIDI loopback port

Install **loopMIDI**, open it, and add a port. Name it `AbletonBridge`.
Leave loopMIDI running (it must be up before Live starts).

## 4. Install the remote script into Ableton Live

Copy the folder `src\ableton\AbletonBridgeMixer` into Live's user library:

```
C:\Users\<you>\Documents\Ableton\User Library\MIDI Remote Scripts\AbletonBridgeMixer\
```

(or, if the user library does not exist yet, into
`C:\ProgramData\Ableton\Live <version>\Resources\MIDI Remote Scripts\` — that needs
admin rights).

Result: `...\MIDI Remote Scripts\AbletonBridgeMixer\__init__.py`.

## 5. Select it in Live

Live → **Preferences / Settings → Link, Tempo & MIDI → MIDI**:

Add a control surface row:

| Control Surface | Input | Output |
| --- | --- | --- |
| `AbletonBridgeMixer` | `AbletonBridge` | `AbletonBridge` |

Also tick **Remote** for the `AbletonBridge` *input* port.

Live loads the script immediately — no restart needed the first time.

## 6. Start the bridge

```powershell
cd C:\path\to\midi_bridge
$env:MIDI_OUTPUT_PORT = "AbletonBridge"
python src\main.py
```

Leave that window open. The log must show:

```
Connected to MIDI output 'AbletonBridge' ...
Starting WebSocket server on 0.0.0.0:8765
Ableton script connected ...
```

## 7. Allow it through Windows Firewall

The first time it starts, Windows asks about Python — allow it on **Private
networks**. If the prompt was dismissed, the phone cannot reach the laptop.
Fix it in an admin PowerShell:

```powershell
New-NetFirewallRule -DisplayName "MIDI Bridge 8765" -Direction Inbound `
  -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
```

## 8. Use the phone

1. Phone and laptop on the **same Wi-Fi** (not a guest network — those often
   block device-to-device traffic).
2. Open the app. It sweeps the Wi-Fi network, finds the laptop, connects, and
   remembers the address. Nothing to type.

## If it does not connect

Open the app's **Settings** — it now says what happened:

- `THIS PHONE 192.168.x.y` + `Found the bridge at 192.168.x.z` → it found it.
- `No bridge answered on this Wi-Fi` → the bridge is not running, or the
  firewall blocked port 8765.
- `Could not read this phone's Wi-Fi address` → type the laptop's IP instead.

Typing the address: on the laptop run `ipconfig` and take the IPv4 address of
the Wi-Fi adapter, then in the app's Settings enter it under **SERVER ADDRESS**
and press **CONNECT**. The app stores it and reuses it from then on.

**Important when both laptops are around:** the app keeps the *last* address and
races it against the scan. If the old laptop is still on the same Wi-Fi with its
bridge running, the app will happily connect to the old one. Shut the old bridge
down, or type the new laptop's IP once in Settings.
