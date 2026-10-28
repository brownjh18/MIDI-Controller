"""AbletonBridgeMixer - control surface for the MIDI bridge app.

Attach this script to the "loopMIDI Port" MIDI input port
(Preferences > Link/Tempo/MIDI). It converts controller faders into live
track mixer changes, no MIDI mapping required:

    CC 7  (volume) on MIDI channels 1-8 -> track 1-8 volume
    CC 10 (pan)    on MIDI channels 1-8 -> track 1-8 pan

It also streams a snapshot of the Live set to the bridge over a small
loopback TCP link, so the app mirrors Ableton exactly:

    {"type": "mixer_state", "tracks": [
        {"name": "Bass", "volume": 0.79, "pan": -0.25}, ... ]}

* the number of entries = the number of tracks in the set (capped at 8),
  so the app draws exactly as many channel strips as Ableton has tracks;
* volume is Live's 0..1 and pan is -1..1 (the app converts both to its
  0-127 display bytes);
* a snapshot is pushed the instant anything changes: the script subscribes to
  Live's own name/volume/pan/tracks listeners, so renames, fader and pan moves
  and newly added tracks follow live without any polling timer.

Every other message is ignored, so the app can keep sending notes and
clock through the main "AbletonBridge" port at the same time.
"""

from __future__ import absolute_import, division, print_function

import codecs
import json
import os
import socket
import tempfile
import time

from _Framework.ControlSurface import ControlSurface

BUILD = "2026-10-27 15:50 field-seq"
LINK_HOST = "127.0.0.1"
LINK_PORT = 8767
MAX_TRACKS = 8  # the app draws at most eight channel strips
STATE_POLL_SECONDS = 0.0  # kept for compatibility; listeners drive the push
LINK_RETRY_SECONDS = 5.0  # failed connects are retried this often
SCHEDULER_TICKS = 10  # scheduler delay between polls
STATE_DIR = os.path.join(tempfile.gettempdir(), "AbletonBridgeMixer")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
COMMAND_FILE = os.path.join(STATE_DIR, "commands.json")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "tick.txt")
IDNA_STATE = "unknown"


def _ensure_idna_codec():
    """Live's embedded Python ships without the idna codec, so any socket name
    resolution - even for 127.0.0.1 - dies with "unknown encoding: idna".
    Register a pass-through shim; it is only used when the real one is absent.
    """
    global IDNA_STATE
    try:
        codecs.lookup("idna")
        IDNA_STATE = "native"
        return
    except LookupError:
        pass

    def _search(encoding):
        if encoding != "idna":
            return None

        def _encode(text, errors="strict"):
            return text.encode("ascii", errors), len(text)

        def _decode(data, errors="strict"):
            return data.decode("ascii", errors), len(data)

        return codecs.CodecInfo(name="idna", encode=_encode, decode=_decode)

    codecs.register(_search)
    try:
        codecs.lookup("idna")
        IDNA_STATE = "shim"
    except LookupError:
        IDNA_STATE = "shim-failed"


_ensure_idna_codec()


class AbletonBridgeMixer(ControlSurface):
    def __init__(self, c_instance):
        super(AbletonBridgeMixer, self).__init__(c_instance)
        self._link = None
        self._last_state = None
        self._last_poll = 0.0
        self._next_connect = 0.0
        self._last_file_state = None
        self._state_errors = set()
        self._set_listener_bound = False
        self._track_listeners = []
        self._midi_seen = False
        self._command_seq = {}
        self._command_retry = {}
        self._command_errors = set()
        self.log_message(
            "AbletonBridgeMixer build %s (idna=%s, state file=%s)"
            % (BUILD, IDNA_STATE, STATE_FILE)
        )
        # Event driven: a bare ControlSurface script cannot rely on Live's
        # scheduler firing, so the mirror is driven by song/track listeners.
        try:
            self._bind_set_listeners()
            self._tick()
            self.log_message(
                "AbletonBridgeMixer watching %d tracks." % len(self.song().tracks)
            )
        except Exception as error:
            self.log_message("AbletonBridgeMixer listener setup failed: %s" % error)
        # Keep a slow fallback poll for anything the listeners miss.
        self.schedule_message(SCHEDULER_TICKS, self._poll_state)

    # ------------------------------------------------------------------
    # Set-state streaming (Ableton -> bridge -> app)
    # ------------------------------------------------------------------
    def _connect(self):
        """Opens the loopback link to the bridge; blocking but throttled."""
        try:
            link = socket.create_connection((LINK_HOST, LINK_PORT), timeout=0.25)
            link.settimeout(0.25)
            self._link = link
            self._last_state = None  # force a fresh push after (re)connect
            self.log_message(
                "AbletonBridgeMixer linked to bridge %s:%s." % (LINK_HOST, LINK_PORT)
            )
        except Exception as error:
            self._link = None
            self.log_message("AbletonBridgeMixer link connect failed: %s" % error)

    def _drop_link(self):
        if self._link is not None:
            try:
                self._link.close()
            except Exception:
                pass
            self._link = None

    # ------------------------------------------------------------------
    # Live listeners: the real source of updates
    # ------------------------------------------------------------------
    def _bind_set_listeners(self):
        """Watches the set so renames, fader/pan moves and new tracks push here."""
        self._unbind_track_listeners()
        song = self.song()
        if not self._set_listener_bound:
            song.add_tracks_listener(self._on_tracks_changed)
            self._set_listener_bound = True
        for track in song.tracks:
            self._track_listeners.extend(self._bind_track(track))
        for return_track in getattr(song, "return_tracks", []):
            self._track_listeners.extend(self._bind_track(return_track))
        master_track = getattr(song, "master_track", None)
        if master_track is not None:
            self._track_listeners.extend(
                self._bind_track(master_track, with_name=False)
            )

    def _bind_track(self, track, with_name=True):
        """Installs name/volume/pan listeners on one mixer."""
        bound = []
        if with_name:
            self._add_listener(bound, track, "name")
        try:
            mixer = track.mixer_device
            self._add_listener(bound, mixer.volume, "value")
            pan_parameter = getattr(mixer, "panning", None)
            if pan_parameter is None:
                pan_parameter = getattr(mixer, "pan", None)
            if pan_parameter is not None:
                self._add_listener(bound, pan_parameter, "value")
        except Exception as error:
            self.log_message("AbletonBridgeMixer mixer watch failed: %s" % error)
        return bound

    def _add_listener(self, bound, target, kind):
        """Registers one listener and remembers how to remove it again."""
        try:
            if kind == "name":
                target.add_name_listener(self._on_track_changed)
                bound.append((target.remove_name_listener, self._on_track_changed))
            else:
                target.add_value_listener(self._on_track_changed)
                bound.append((target.remove_value_listener, self._on_track_changed))
        except Exception:
            pass

    def _unbind_track_listeners(self):
        for remove, callback in self._track_listeners:
            try:
                remove(callback)
            except Exception:
                pass
        self._track_listeners = []

    def _on_track_changed(self, *args):
        self._tick(from_listener=True)

    def _on_tracks_changed(self, *args):
        try:
            self._bind_set_listeners()
        except Exception as error:
            self.log_message("AbletonBridgeMixer rebind failed: %s" % error)
        self._tick(from_listener=True)

    def _read_state(self):
        """Full snapshot: strips, return tracks, master and metronome."""
        song = self.song()
        tracks = []
        for index in range(min(MAX_TRACKS, len(song.tracks))):
            tracks.append(self._track_snapshot(song.tracks[index], index))

        returns = []
        for return_track in getattr(song, "return_tracks", [])[:2]:
            returns.append(self._track_snapshot(return_track, None))

        master = None
        master_track = getattr(song, "master_track", None)
        if master_track is not None:
            master = self._track_snapshot(master_track, None)

        metronome = None
        try:
            metronome = bool(song.metronome)
        except Exception:
            metronome = None

        playing = None
        try:
            playing = bool(song.is_playing)
        except Exception:
            playing = None

        tempo = None
        try:
            tempo = float(song.tempo)
        except Exception:
            tempo = None

        return {
            "type": "mixer_state",
            "tracks": tracks,
            "returns": returns,
            "master": master,
            "metronome": metronome,
            "playing": playing,
            "tempo": tempo,
        }

    def _track_snapshot(self, track, index):
        """Name + volume + Ableton's own dB text + pan for one mixer."""
        try:
            mixer = track.mixer_device
            # Live calls the pan parameter "panning", not "pan".
            pan_parameter = getattr(mixer, "panning", None)
            if pan_parameter is None:
                pan_parameter = getattr(mixer, "pan", None)
            # str() of the volume parameter is exactly the dB text Live
            # shows on the fader, so the app can echo Ableton verbatim.
            try:
                db_text = str(mixer.volume)
            except Exception:
                db_text = None
            return {
                "name": str(track.name),
                "volume": float(mixer.volume.value),
                "db": db_text,
                "pan": (
                    None if pan_parameter is None else float(pan_parameter.value)
                ),
            }
        except Exception as error:
            # Keep the slot (position stays stable) with no value.
            key = "track:%s" % index if index is not None else str(
                getattr(track, "name", "?")
            )
            if key not in self._state_errors:
                self._state_errors.add(key)
                self.log_message(
                    "AbletonBridgeMixer %s read failed: %s: %s"
                    % (key, type(error).__name__, error)
                )
            return {
                "name": str(getattr(track, "name", "")),
                "volume": None,
                "db": None,
                "pan": None,
            }

    def _tick(self, from_listener=False):
        """Reads the set and pushes a snapshot; every Live listener lands here."""
        try:
            now = time.time()
            self._last_poll = now
            # Live forbids parameter writes from inside a notification, so
            # commands are only applied on safe ticks (display timer / MIDI).
            if not from_listener:
                self._apply_commands()
            self._beat()
            state = self._read_state()
            if state != self._last_file_state:
                self._write_state_file(state)
                self._last_file_state = state
            if self._link is None and now >= self._next_connect:
                self._next_connect = now + LINK_RETRY_SECONDS
                self._connect()
            if self._link is not None and state != self._last_state:
                payload = json.dumps(state)
                self._link.sendall(payload.encode("utf-8") + b"\n")
                self._last_state = state
        except Exception as error:
            self.log_message("AbletonBridgeMixer sync error: %s" % error)
            self._drop_link()
            self._next_connect = 0.0

    def _beat(self):
        """Heartbeat file: proves to the bridge that Live is still ticking."""
        try:
            handle = open(HEARTBEAT_FILE, "w")
            try:
                handle.write("%.3f\n" % time.time())
            finally:
                handle.close()
        except Exception:
            pass

    def _apply_commands(self):
        """Applies absolute fader/pan/metronome targets from the bridge."""
        payload = self._take_command_payload()
        if not isinstance(payload, dict):
            payload = {}
        if self._command_retry:
            for key, value in self._command_retry.items():
                payload.setdefault(key, value)
            self._command_retry = {}
        if not payload:
            return

        for key in list(payload):
            target = payload[key]
            try:
                if not isinstance(target, dict):
                    continue
                if key in ("metronome", "transport", "tempo"):
                    sequence = int(target.get("seq", 0))
                    if sequence <= self._command_seq.get(key, 0):
                        continue
                    if key == "metronome":
                        self.song().metronome = bool(target.get("value"))
                    elif key == "transport":
                        if str(target.get("value")) == "start":
                            self.song().start_playing()
                        else:
                            self.song().stop_playing()
                    else:
                        self.song().tempo = float(target.get("value"))
                    self._command_seq[key] = sequence
                    continue
                owner = self._resolve_target(key)
                if owner is None:
                    continue
                self._apply_mixer_fields(key, owner.mixer_device, target)
            except Exception as error:
                # Live refuses parameter writes triggered from notifications;
                # hold the target so the next safe tick retries it.
                self._command_retry[key] = target
                if key not in self._command_errors:
                    self._command_errors.add(key)
                    self.log_message(
                        "AbletonBridgeMixer command deferred (%s): %s"
                        % (key, error)
                    )

    def _apply_mixer_fields(self, key, mixer, target):
        """Applies volume/pan, each only when its own sequence is newer.

        Values are sequenced per field: a pan move must never drag an older
        volume back onto the fader (and vice versa).
        """
        for field in ("volume", "pan"):
            entry = target.get(field)
            if not isinstance(entry, dict):
                continue
            sequence = int(entry.get("seq", 0))
            marker = (key, field)
            if sequence <= self._command_seq.get(marker, 0):
                continue
            value = float(entry.get("value"))
            if field == "volume":
                mixer.volume.value = max(0.0, min(1.0, value))
            else:
                pan_parameter = getattr(mixer, "panning", None)
                if pan_parameter is None:
                    pan_parameter = getattr(mixer, "pan", None)
                if pan_parameter is not None:
                    pan_parameter.value = max(-1.0, min(1.0, value))
            self._command_seq[marker] = sequence

    def _take_command_payload(self):
        """Atomically claims the bridge's command file and returns its JSON."""
        claim_file = COMMAND_FILE + ".reading"
        try:
            if not os.path.isfile(COMMAND_FILE):
                return None
            # Claim the file atomically so a command written during this read
            # is never swallowed by the cleanup below.
            os.replace(COMMAND_FILE, claim_file)
        except Exception:
            return None

        payload = None
        try:
            handle = open(claim_file, "r")
            try:
                payload = json.load(handle)
            finally:
                handle.close()
        except Exception:
            payload = None
        try:
            os.remove(claim_file)
        except Exception:
            pass
        return payload

    def _resolve_target(self, key):
        """Maps a command id ("track:2", "return:0", "master", "3") to a track."""
        song = self.song()
        try:
            if key == "master":
                return getattr(song, "master_track", None)
            if ":" in key:
                kind, _, raw = key.partition(":")
                index = int(raw)
                if kind == "return":
                    items = song.return_tracks
                elif kind == "track":
                    items = song.tracks
                else:
                    self._note_unknown_target(key)
                    return None
                return items[index] if 0 <= index < len(items) else None
            index = int(key)  # legacy: bare channel number == track index
            tracks = song.tracks
            return tracks[index] if 0 <= index < len(tracks) else None
        except Exception:
            return None

    def _note_unknown_target(self, key):
        """Logs a target the script cannot resolve, once per key."""
        if key in self._command_errors:
            return
        self._command_errors.add(key)
        self.log_message("AbletonBridgeMixer unknown command target: %s" % key)

    def _write_state_file(self, state):
        """Fallback transport: hand the snapshot to the bridge through a file.

        Live's embedded Python cannot open a socket when its stdlib lacks the
        idna codec, but plain file I/O always works.
        """
        try:
            if not os.path.isdir(STATE_DIR):
                os.makedirs(STATE_DIR)
            payload = json.dumps(state)
            temp_path = STATE_FILE + ".tmp"
            handle = open(temp_path, "w")
            try:
                handle.write(payload)
                handle.write("\n")
            finally:
                handle.close()
            os.replace(temp_path, STATE_FILE)
        except Exception as error:
            self.log_message("AbletonBridgeMixer state file failed: %s" % error)

    def _poll_state(self):
        """Scheduler-driven poll; re-scheduled forever."""
        self._tick()
        self.schedule_message(SCHEDULER_TICKS, self._poll_state)

    def update_display(self):
        """Live's display timer also drives the poll, so the mirror runs even
        when the control-surface scheduler stays dormant."""
        try:
            super(AbletonBridgeMixer, self).update_display()
        except Exception:
            pass
        self._tick()

    # ------------------------------------------------------------------
    # Incoming controller messages
    # ------------------------------------------------------------------
    def receive_midi(self, midi_bytes):
        if not midi_bytes or len(midi_bytes) < 3:
            return
        if not self._midi_seen:
            self._midi_seen = True
            self.log_message(
                "AbletonBridgeMixer first MIDI in: %s" % (list(midi_bytes),)
            )
        status, data1, data2 = midi_bytes[0], midi_bytes[1], midi_bytes[2]
        if status & 0xF0 != 0xB0:
            return  # not a control change
        channel = status & 0x0F
        try:
            tracks = self.song().tracks
            if channel >= len(tracks):
                # Channel 9 (index 8) carries the main section: reverb send,
                # delay send and the master fader.
                self._receive_main_midi(data1, data2)
                self._tick()
                return
            mixer_device = tracks[channel].mixer_device
            if data1 == 7:
                mixer_device.volume.value = max(0.0, min(1.0, data2 / 127.0))
            elif data1 == 10:
                pan_parameter = getattr(mixer_device, "panning", None)
                if pan_parameter is None:
                    pan_parameter = getattr(mixer_device, "pan", None)
                if pan_parameter is not None:
                    pan_parameter.value = max(-1.0, min(1.0, (data2 - 64) / 64.0))
            self._tick()  # echo the change back to the app immediately
        except Exception as error:
            self.log_message("AbletonBridgeMixer error: %s" % error)

    def _receive_main_midi(self, data1, data2):
        """CC 91 / CC 94 / CC 7 on the main channel: reverb, delay, master."""
        song = self.song()
        level = max(0.0, min(1.0, data2 / 127.0))
        if data1 == 7:
            master = getattr(song, "master_track", None)
            if master is not None:
                master.mixer_device.volume.value = level
            return
        index = 0 if data1 == 91 else 1 if data1 == 94 else None
        if index is None:
            return
        returns = song.return_tracks
        if index < len(returns):
            returns[index].mixer_device.volume.value = level

    def disconnect(self):
        try:
            self._unbind_track_listeners()
            if self._set_listener_bound:
                self.song().remove_tracks_listener(self._on_tracks_changed)
                self._set_listener_bound = False
        except Exception:
            pass
        self._drop_link()
        super(AbletonBridgeMixer, self).disconnect()


def create_instance(c_instance):
    return AbletonBridgeMixer(c_instance)
