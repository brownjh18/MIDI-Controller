# name=JarvisBridge
# url=
import os, tempfile, time, json

_last = 0.0

def OnInit():
    pass

def OnDeInit():
    pass

def OnIdle():
    global _last
    now = time.time()
    if now - _last < 0.5:
        return
    _last = now
    try:
        d = os.path.join(tempfile.gettempdir(), "AbletonBridgeMixer")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "fl_jarvis_heartbeat.txt"), "w", encoding="utf-8") as f:
            f.write(str(now) + "|fl-simple")
        p = {
            "type": "mixer_state",
            "daw": "flstudio",
            "script_build": "fl-simple",
            "tracks": [{"name": "Insert 1", "volume": 0.8, "pan": 0.0, "db": "0.0 dB", "armed": False, "slots": [], "devices": []}],
            "returns": [],
            "master": {"volume": 0.8, "pan": 0.0, "db": "0.0 dB"},
            "meters": [[0.0, 0.0]],
            "master_meter": [0.0, 0.0],
            "metronome": False,
            "playing": False,
            "record_mode": False,
            "tempo": 120.0,
            "selected": 0,
        }
        t = os.path.join(d, "state.json.tmp")
        with open(t, "w", encoding="utf-8") as f:
            json.dump(p, f, separators=(",", ":"))
        os.replace(t, os.path.join(d, "state.json"))
    except Exception:
        pass
