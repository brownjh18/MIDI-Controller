# MIDI Bridge

MIDI Bridge connects the MIDI Controller app (Android/iOS) to supported DAWs on Windows through a local bridge.

## Finished DAWs (setup & connection)

- **Ableton Live** (Windows)
- **Studio One** (Windows)

For other DAWs, support may be in progress; use the DAW selector in the app only for DAWs that are configured and reporting state.

## 1) Install the Windows bridge (recommended)

Use the single all-in-one installer: **`MidiBridgeSetup.exe`**

1. Download `MidiBridgeSetup.exe` from the [GitHub release](https://github.com/brownjh18/MIDI-Controller/releases).
2. Run it on the Windows PC that runs your DAW. It requests **administrator** permission (UAC) so it can install/configure required components.
3. The installer sets up the bridge and supported DAW integrations/components (including virtual MIDI/driver components such as **loopMIDI** when needed, firewall access for the app connection, DAW scripts/devices/surface data for supported DAWs, and starts/configures the bridge as applicable).
4. When installation completes, keep the bridge running (it listens for the app on **TCP port 8765**).

> If Windows Firewall or security software blocks the connection, allow **MidiBridge** / inbound **TCP 8765** on the networks you use (the installer attempts to configure this).

## 2) Install the mobile app

- **Android:** install the release `.apk` (e.g. `MIDIController-v1.2.0.apk`).
- **iOS:** install the release `.ipa` (e.g. `MidiController-v1.2.0.ipa`) using your preferred sideload/install method.

## 3) Connect the app to the bridge

1. Ensure the **bridge is running** on the PC.
2. Connect the phone and PC so they can reach each other:
   - **Same Wi-Fi/LAN (recommended):** phone and PC on the same network. The app can auto-discover the bridge (port 8765).
   - **USB / Personal Hotspot tether:** supported by the app/bridge for direct connection where Wi-Fi is unavailable/restricted.
3. Open the app → **Settings**:
   - Confirm the app is connected to the bridge (status/latency/MIDI info updates when connected).
   - If auto-discovery does not find the bridge, enter the PC’s IP address (server address) and connect manually.
4. In the app, select the target **DAW** (**Ableton Live** or **Studio One**) so the bridge/app exchange the correct DAW state and controls.

---

## Ableton Live — setup and connection (finished)

### Install/configure (Windows)
1. Run **`MidiBridgeSetup.exe`** on the PC (recommended). It installs the bridge and the Ableton integration components (including the **`AbletonBridgeMixer`** control surface script).
2. Ensure the virtual MIDI port used by the integration is available and running (commonly **`AbletonBridge`** via **loopMIDI**). The installer can set up/install required MIDI components.
3. Start or **restart Ableton Live** after installation if Live was open (Live scans control surfaces at startup).

### Ableton Live settings
1. Open **Options/Preferences → Link, Tempo & MIDI**.
2. In **Control Surface**, select **`AbletonBridgeMixer`**.
3. Set **Input** and **Output** to the bridge MIDI port (commonly **`AbletonBridge`**).
4. Enable the appropriate MIDI port options (typically **Track** and/or **Remote**, depending on your setup).

### Connect and verify in the app
1. Start the bridge and open Ableton Live with the control surface configured.
2. In the app, connect to the bridge and select **Ableton Live**.
3. The app should sync Ableton’s mixer/transport/state (tracks, volumes/pans, devices/parameters where available).  
   - If the app shows **“waiting for Ableton Live”**, verify: bridge is running, app is connected to the correct PC, **AbletonBridgeMixer** is selected as Control Surface with correct **Input/Output**, and restart Ableton Live if needed.

---

## Studio One — setup and connection (finished)

### Install/configure (Windows)
1. Run **`MidiBridgeSetup.exe`** on the PC (recommended). It installs the bridge and the Studio One integration (e.g. **Jarvis Bridge** device/component and required surface/control-link data).
2. Ensure the virtual MIDI port used by the integration exists and is running (commonly **`loopMIDI Port`**).
3. Start or **restart Studio One** after installation so it loads the installed device/surface data.

### Studio One settings
1. Open **Studio One → Options/Preferences → External Devices** (or MIDI device/surface configuration).
2. Add/select the installed **Jarvis Bridge** / Mackie Control device for this integration.
3. Set **Receive From** and **Send To** to the **same loopMIDI port** used by the bridge (commonly **`loopMIDI Port`**).
4. Save/apply settings and restart Studio One if the device/surface does not load correctly.

### Connect and verify in the app
1. Start the bridge and open Studio One with the Jarvis Bridge device configured on the correct loopMIDI port.
2. In the app, connect to the bridge and select **Studio One**.
3. The app should sync Studio One mixer/state (track names, levels/pans, devices/inserts/sends and parameters as exposed).
4. For detailed plugin/insert parameter control, create/enable the required **Control Link** mappings in Studio One for the target parameters, then focus/reselect the device in the app if needed.

---

## Troubleshooting
- **App can’t connect:** ensure the bridge is running, phone/PC can reach each other, and inbound **TCP 8765** is allowed. Try entering the PC IP manually in app Settings.
- **Connected but waiting for the DAW:** select the correct DAW in the app, ensure the DAW is open, and confirm the DAW-side control surface/device/script is configured on the correct MIDI port(s).
- **Ableton:** verify `AbletonBridgeMixer` + correct Input/Output in *Link, Tempo & MIDI*; restart Ableton after install.
- **Studio One:** verify Jarvis Bridge/Mackie Control **Receive/Send** use the same loopMIDI port; restart Studio One after changes.

## Build/release artifacts
- **Windows (all-in-one installer):** `MidiBridgeSetup.exe` (installs bridge and performs DAW/setup configuration with admin elevation)
- **Android:** release `.apk`
- **iOS:** release `.ipa`
- Latest assets: https://github.com/brownjh18/MIDI-Controller/releases

## Developer notes (from source)

### Run the MIDI bridge
```powershell
python -m pip install -r requirements.txt
python src\main.py
```

### Build the Windows installer
```powershell
Set-Location installer
python -m pip install pyinstaller
.\build.ps1
```
`build.ps1` produces `installer\dist\MidiBridgeSetup.exe`. It requires `installer\payload\loopMIDISetup.zip`.

### Build Android (local)
```powershell
Set-Location mobile
npx expo prebuild --platform android
Set-Location android
.\gradlew assembleRelease
```
APK output: `mobile/android/app/build/outputs/apk/release/app-release.apk`.

### Build iOS (xtool)
```bash
cd /home/brown/MidiController
xtool dev build -c release -s -i
```
IPA output: `xtool/MidiController.ipa`.
