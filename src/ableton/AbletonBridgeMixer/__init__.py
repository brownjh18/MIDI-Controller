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

BUILD = "2026-11-06 normalised ranges 11"
LINK_HOST = "127.0.0.1"
LINK_PORT = 8767
# 16 midi channels total, channel 9 (index 8) reserved for the MAIN section,
# so the app can draw up to 15 channel strips.
MAX_TRACKS = 15
# Zero-based midi channel that always carries the MAIN (returns/master) CCs.
MAIN_MIDI_CHANNEL = 8
# Session-view clip slots exposed per channel strip in the app's clip panel.
CLIP_SLOTS = 8
# Effects mirror: the first device on each return track (A-Reverb, B-Delay)
# is streamed to the app and driven back over these CC numbers on the MAIN
# channel. The range never collides with the mixer CCs (7/10) or the main
# faders (7/91/94).
FX_RETURN_A_CC = 20  # CC 20..27 -> return A device parameter 0..7
FX_RETURN_B_CC = 30  # CC 30..37 -> return B device parameter 0..7
FX_PARAMS_PER_DEVICE = 8
FX_SKIP_PARAMS = ("Device On", "Chain Selector")
STATE_POLL_SECONDS = 0.0  # kept for compatibility; listeners drive the push
LINK_RETRY_SECONDS = 5.0  # failed connects are retried this often
# A silently dead loopback link (bridge restarted, socket reset) never reports
# itself: with no state changes nothing is ever sent, so the stale socket is
# reused forever and Live->app updates stop. A tiny keepalive at this interval
# proves the link and forces a reconnect within LINK_RETRY_SECONDS.
LINK_KEEPALIVE_SECONDS = 2.0
SCHEDULER_TICKS = 10  # scheduler delay between polls
# Meter sampling: 20 Hz is plenty for a level meter and keeps the JSON link
# light. When only the socket is available the file transport is skipped.
METER_INTERVAL = 0.05
FILE_METER_INTERVAL = 0.10
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
        self._last_link_send = 0.0
        self._last_file_state = None
        self._last_full_state = None
        self._last_file_write = 0.0
        self._meters = []
        self._master_meter = None
        self._meter_time = 0.0
        # (value, name) -> Live's formatted display string, keyed by parameter
        # identity. str_for_value() is a C++ round trip, so formatting every
        # parameter on every tick would tax Live's display thread; only the
        # knob that actually moved is re-formatted.
        self._text_cache = {}
        self._state_errors = set()
        self._set_listener_bound = False
        self._selection_listener = None
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
            self._last_link_send = time.time()
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
            try:
                song.view.add_selected_track_listener(self._on_selection_changed)
                self._selection_listener = self._on_selection_changed
            except Exception:
                self._selection_listener = None
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
        """Installs name/volume/pan listeners on one mixer and watches the first
        device in the chain so Ableton device param changes propagate to the app
        immediately (via _on_track_changed -> _tick -> snapshot push)."""
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
        # Watch ALL devices in the chain so every parameter change pushes to the
        # app immediately (via _on_track_changed -> _tick -> snapshot).
        try:
            chain = list(track.devices)
            for device in chain:
                params = self._enabled_params(device)
                for _name, parameter in params:
                    try:
                        parameter.add_value_listener(self._on_track_changed)
                        bound.append((parameter.remove_value_listener,
                                       self._on_track_changed))
                    except Exception:
                        pass
        except Exception:
            pass
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

    def _on_selection_changed(self, *args):
        """Live's selected track changed — push it so the app follows."""
        self._tick(from_listener=True)

    def _meter_snapshot(self, track):
        """Live's stereo output meter for one track, 0.0-1.0 per side."""
        try:
            return [
                float(track.output_meter_left),
                float(track.output_meter_right),
            ]
        except Exception:
            # Very old Live versions only expose the mono level.
            try:
                level = float(track.output_meter_level)
            except Exception:
                return [0.0, 0.0]
            return [level, level]

    def _refresh_meters(self, now):
        """Samples track meters at a fixed rate.

        Reading them is cheap, but the display timer fires far faster than a
        meter needs; streaming every strip at that rate would flood the link.
        """
        if now - self._meter_time < METER_INTERVAL:
            return
        self._meter_time = now
        song = self.song()
        meters = []
        for index in range(min(MAX_TRACKS, len(song.tracks))):
            meters.append(self._meter_snapshot(song.tracks[index]))
        self._meters = meters
        master_track = getattr(song, "master_track", None)
        self._master_meter = (
            self._meter_snapshot(master_track) if master_track is not None else None
        )

    def _read_state(self):
        """Full snapshot: strips, return tracks, master and metronome."""
        song = self.song()
        tracks = []
        for index in range(min(MAX_TRACKS, len(song.tracks))):
            track = song.tracks[index]
            snapshot = self._track_snapshot(track, index)
            snapshot["slots"] = [
                self._slot_snapshot(track.clip_slots[slot])
                for slot in range(min(CLIP_SLOTS, len(track.clip_slots)))
            ]
            try:
                snapshot["armed"] = bool(track.arm)
            except Exception:
                snapshot["armed"] = False
            # Per-track device chain (instrument/effects) mirrored to the app
            try:
                snapshot["devices"] = self._device_snapshot(track)
            except Exception:
                snapshot["devices"] = []
            tracks.append(snapshot)

        returns = []
        for return_track in getattr(song, "return_tracks", [])[:2]:
            snapshot = self._track_snapshot(return_track, None)
            snapshot["devices"] = self._device_snapshot(return_track)
            returns.append(snapshot)

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

        record_mode = None
        try:
            record_mode = bool(song.record_mode)
        except Exception:
            record_mode = None

        tempo = None
        try:
            tempo = float(song.tempo)
        except Exception:
            tempo = None

        # Which track Live has selected, so the app's channel selection can
        # follow the mouse (and vice versa through the select_track command).
        selected = None
        try:
            current = song.view.selected_track
            selected = next(
                (
                    index
                    for index in range(min(MAX_TRACKS, len(song.tracks)))
                    if song.tracks[index] == current
                ),
                None,
            )
        except Exception:
            selected = None

        return {
            "type": "mixer_state",
            "tracks": tracks,
            "returns": returns,
            "master": master,
            "meters": list(self._meters),
            "master_meter": self._master_meter,
            "metronome": metronome,
            "playing": playing,
            "record_mode": record_mode,
            "tempo": tempo,
            "selected": selected,
            "script_build": BUILD,
        }

    def _slot_snapshot(self, slot):
        """Play/record state of one session clip slot for the app's clip panel."""
        try:
            has_clip = bool(slot.has_clip)
        except Exception:
            has_clip = False
        playing = False
        recording = False
        try:
            clip = slot.clip
            if clip is not None:
                playing = bool(clip.is_playing)
                recording = bool(clip.is_recording)
        except Exception:
            pass
        return {
            "has_clip": has_clip,
            "playing": playing,
            "recording": recording,
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

    @staticmethod
    def _enabled_params(device):
        """Parameters mirrored to the app: the continuous knobs, not toggles.

        The index into this list is the CC offset, so the snapshot and the
        write path must filter identically. Continuous (non-quantised)
        parameters come first because those are the ones worth a slider in
        the app; switches and choosers only fill leftover slots. Dry/Wet is
        hoisted to the front - it is the first thing you reach for on a
        return effect.
        """
        continuous = []
        quantized = []
        try:
            for parameter in device.parameters:
                try:
                    if not parameter.is_enabled:
                        continue
                    name = str(parameter.name)
                    is_quantized = bool(getattr(parameter, "is_quantized", False))
                except Exception:
                    continue
                if name in FX_SKIP_PARAMS:
                    continue
                if is_quantized:
                    quantized.append((name, parameter))
                else:
                    continuous.append((name, parameter))
        except Exception:
            pass
        params = continuous + quantized
        # Stable sort: Dry/Wet jumps the queue, everything else keeps order.
        params.sort(key=lambda item: 0 if item[0] == "Dry/Wet" else 1)
        return params

    @staticmethod
    def _set_normalised(parameter, value):
        """Writes a 0..1 position into a parameter with its own real range.

        Rack macros (and many plugin parameters) are *not* 0..1: a Kontakt
        macro's range is 0..127, a volume is in dB, a release is in
        milliseconds. Writing the app's 0..1 straight in left every knob
        pinned to the bottom 1/127th of its travel, which looked like "the
        value only moves from 0 to 1".
        """
        try:
            low = float(parameter.min)
            high = float(parameter.max)
        except Exception:
            low, high = 0.0, 1.0
        position = max(0.0, min(1.0, float(value)))
        span = high - low
        parameter.value = (low + position * span) if span > 0.0 else low

    @staticmethod
    def _position(parameter):
        """Returns (normalised 0..1 position, real value, min, max)."""
        try:
            raw = float(parameter.value)
        except Exception:
            return 0.0, 0.0, 0.0, 1.0
        try:
            low = float(parameter.min)
            high = float(parameter.max)
        except Exception:
            return max(0.0, min(1.0, raw)), raw, 0.0, 1.0
        span = high - low
        position = (raw - low) / span if span > 0.0 else 0.0
        return max(0.0, min(1.0, position)), raw, low, high

    def _device_snapshot(self, track):
        """Full device chain on the selected track.

        Each parameter carries Live's normalised 0..1 position *and* the exact
        display string Live itself shows (``str_for_value``), so the app can
        mirror the real measurement ("1.20 kHz", "-6.0 dB") instead of a
        7-bit guess.
        """
        devices = []
        try:
            chain = list(track.devices)
        except Exception as error:
            self.log_message("_device_snapshot error: %s" % error)
            return devices
        for device in chain:
            params = []
            for name, parameter in self._enabled_params(device):
                position, raw, low, high = self._position(parameter)
                cache_key = id(parameter)
                cached = self._text_cache.get(cache_key)
                if cached is not None and cached[0] == raw and cached[1] == name:
                    text = cached[2]
                else:
                    try:
                        text = str(parameter.str_for_value(parameter.value))
                    except Exception:
                        text = ""
                    if len(self._text_cache) > 20000:
                        self._text_cache.clear()
                    self._text_cache[cache_key] = (raw, name, text)
                params.append(
                    {
                        "name": name,
                        "value": round(position, 6),
                        "text": text,
                        # The parameter's own range, so the app can show the
                        # real value when Live's display string is useless
                        # (Kontakt reports a bare "0"/"1" for a 0..127 macro).
                        "min": low,
                        "max": high,
                        "raw": raw,
                    }
                )
            devices.append(
                {"name": str(getattr(device, "name", "")), "params": params}
            )
        return devices

    def _set_return_param(self, return_index, param_index, value):
        """Writes one mirrored effect parameter (0..1) back into Live."""
        try:
            returns = self.song().return_tracks
            if return_index >= len(returns):
                return
            chain = list(returns[return_index].devices)
            if not chain:
                return
            params = self._enabled_params(chain[0])
            if param_index >= len(params):
                return
            self._set_normalised(params[param_index][1], value)
        except Exception as error:
            self.log_message("AbletonBridgeMixer fx write failed: %s" % error)

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
            self._refresh_meters(now)
            state = self._read_state()
            # Meter values change on every sample. While the socket is up they
            # only travel over the socket, so the file (and its once-a-second
            # rebroadcast) never overwrites fresh meters with stale ones. If
            # the socket is down, the file carries them at a slower rate.
            base_state = {
                key: value
                for key, value in state.items()
                if key not in ("meters", "master_meter")
            }
            if (
                base_state != self._last_file_state
                or (
                    self._link is None
                    and state != self._last_full_state
                    and now - self._last_file_write >= FILE_METER_INTERVAL
                )
            ):
                # Socket up: keep meters out of the file so its once-a-second
                # rebroadcast can't replace fresh levels with stale ones.
                self._write_state_file(state if self._link is None else base_state)
                self._last_file_state = base_state
                self._last_file_write = now
            self._last_full_state = state
            if self._link is None and now >= self._next_connect:
                self._next_connect = now + LINK_RETRY_SECONDS
                self._connect()
            if self._link is not None:
                if state != self._last_state:
                    payload = json.dumps(state)
                    self._link.sendall(payload.encode("utf-8") + b"\n")
                    self._last_state = state
                    self._last_link_send = now
                elif now - self._last_link_send >= LINK_KEEPALIVE_SECONDS:
                    # Nothing changed, but the socket still has to be proven
                    # alive. A failed send raises into the outer handler, which
                    # drops the link so the next tick reconnects immediately.
                    self._link.sendall(b'{"type":"heartbeat"}\n')
                    self._last_link_send = now
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
                if key in ("metronome", "transport", "tempo", "record"):
                    inner = target
                    # The bridge's field-nested form wraps the real payload
                    # one level deeper: {"value": {...}} instead of
                    # {"value": X, "seq": N} — without unwrapping, the seq
                    # guard read 0 and silently skipped every transport
                    # command.
                    nested = inner.get("value")
                    if (
                        "seq" not in inner
                        and isinstance(nested, dict)
                        and "seq" in nested
                    ):
                        inner = nested
                    sequence = int(inner.get("seq", 0))
                    if sequence <= self._command_seq.get(key, 0):
                        continue
                    if key == "metronome":
                        self.song().metronome = bool(inner.get("value"))
                    elif key == "record":
                        self.song().record_mode = bool(inner.get("value"))
                    elif key == "transport":
                        if str(inner.get("value")) == "start":
                            self.song().start_playing()
                        else:
                            self.song().stop_playing()
                    else:
                        self.song().tempo = float(inner.get("value"))
                    self._command_seq[key] = sequence
                    continue
                if key == "select_track":
                    inner = target
                    nested = inner.get("value")
                    if (
                        "seq" not in inner
                        and isinstance(nested, dict)
                        and "seq" in nested
                    ):
                        inner = nested
                    sequence = int(inner.get("seq", 0))
                    if sequence <= self._command_seq.get(key, 0):
                        continue
                    index = int(inner.get("value", 0))
                    tracks = self.song().tracks
                    if 0 <= index < len(tracks):
                        self.song().view.selected_track = tracks[index]
                    self._command_seq[key] = sequence
                    continue
                if key.startswith("clip:"):
                    inner = target
                    nested = inner.get("value")
                    if (
                        "seq" not in inner
                        and isinstance(nested, dict)
                        and "seq" in nested
                    ):
                        inner = nested
                    sequence = int(inner.get("seq", 0))
                    if sequence <= self._command_seq.get(key, 0):
                        continue
                    self._apply_clip_action(key, inner.get("action"))
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

        # Mirrored device parameters ride the same return/track targets as
        # "param:N" (legacy, device 0) and "device:D:param:P" (full chain)
        # fields, so every app slider reaches Live's real device parameters.
        for field in list(target.keys()):
            if not isinstance(field, str):
                continue
            if not (field.startswith("param:") or field.startswith("device:")):
                continue
            entry = target.get(field)
            if not isinstance(entry, dict):
                continue
            sequence = int(entry.get("seq", 0))
            marker = (key, field)
            if sequence <= self._command_seq.get(marker, 0):
                continue
            device_index = 0
            if field.startswith("device:"):
                parts = field.split(":")
                if len(parts) != 4 or parts[2] != "param":
                    continue
                try:
                    device_index = int(parts[1])
                    param_index = int(parts[3])
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    param_index = int(field.split(":", 1)[1])
                except Exception:
                    continue
            value = max(0.0, min(1.0, float(entry.get("value"))))
            if key == "return:0":
                self._set_return_param(0, param_index, value)
            elif key == "return:1":
                self._set_return_param(1, param_index, value)
            elif key.startswith("track:"):
                try:
                    track_index = int(key.split(":", 1)[1])
                    tracks = self.song().tracks
                    if 0 <= track_index < len(tracks):
                        chain = list(tracks[track_index].devices)
                        if 0 <= device_index < len(chain):
                            params = self._enabled_params(chain[device_index])
                            if param_index < len(params):
                                self._set_normalised(params[param_index][1], value)
                except Exception as error:
                    self.log_message(
                        "AbletonBridgeMixer track param write failed: %s" % error
                    )
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

    def _apply_clip_action(self, key, action):
        """Fires / stops / records one session clip slot from the app."""
        _, _, rest = key.partition(":")
        track_raw, _, slot_raw = rest.partition(":")
        try:
            track_index = int(track_raw)
            slot_index = int(slot_raw)
        except (TypeError, ValueError):
            return
        song = self.song()
        if not 0 <= track_index < len(song.tracks):
            return
        track = song.tracks[track_index]
        if action == "stop_track":
            track.stop_all_clips()
            return
        if action == "play_track":
            # Fire the first slot that holds a clip, like clicking the track
            # play button in Session view.
            for candidate in track.clip_slots:
                try:
                    if candidate.has_clip:
                        candidate.fire()
                        return
                except Exception:
                    continue
            return
        if action == "record_track":
            # Arm the track and start recording into its first empty slot.
            try:
                track.arm = True
            except Exception:
                pass
            for candidate in track.clip_slots:
                try:
                    if not candidate.has_clip:
                        candidate.fire()
                        return
                except Exception:
                    continue
            return
        if not 0 <= slot_index < len(track.clip_slots):
            return
        slot = track.clip_slots[slot_index]
        if action == "play":
            slot.fire()
        elif action == "stop":
            slot.stop()
        elif action == "record":
            # An empty slot starts recording when the track is armed.
            try:
                track.arm = True
            except Exception:
                pass
            slot.fire()

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
            index = self._track_index_for_channel(channel)
            if index is None:
                # Channel 9 (index 8) carries the main section: reverb send,
                # delay send and the master fader.
                if channel == MAIN_MIDI_CHANNEL:
                    self._receive_main_midi(data1, data2)
                    self._tick()
                return
            tracks = self.song().tracks
            if index >= len(tracks):
                return
            mixer_device = tracks[index].mixer_device
            if data1 == 7:
                mixer_device.volume.value = max(0.0, min(1.0, data2 / 127.0))
            elif data1 == 10:
                pan_parameter = getattr(mixer_device, "panning", None)
                if pan_parameter is None:
                    pan_parameter = getattr(mixer_device, "pan", None)
                if pan_parameter is not None:
                    pan_parameter.value = max(-1.0, min(1.0, (data2 - 64) / 64.0))
            # Mirror device parameters (CC 20..27 / 30..37) for this track
            if FX_RETURN_A_CC <= data1 < FX_RETURN_A_CC + FX_PARAMS_PER_DEVICE:
                try:
                    chain = list(tracks[index].devices)
                    if chain:
                        params = self._enabled_params(chain[0])
                        param_index = data1 - FX_RETURN_A_CC
                        if param_index < len(params):
                            self._set_normalised(params[param_index][1], data2 / 127.0)
                except Exception:
                    pass
            if FX_RETURN_B_CC <= data1 < FX_RETURN_B_CC + FX_PARAMS_PER_DEVICE:
                try:
                    chain = list(tracks[index].devices)
                    if chain:
                        params = self._enabled_params(chain[0])
                        param_index = data1 - FX_RETURN_B_CC
                        if param_index < len(params):
                            self._set_normalised(params[param_index][1], data2 / 127.0)
                except Exception:
                    pass
            self._tick()  # echo the change back to the app immediately
        except Exception as error:
            self.log_message("AbletonBridgeMixer error: %s" % error)

    @staticmethod
    def _track_index_for_channel(channel):
        """Midi channel -> strip index, skipping the reserved MAIN channel.

        Tracks 0-7 sit on channels 1-8; the MAIN section owns channel 9;
        tracks 8 and up continue on channels 10-16. Returns None for the
        MAIN channel (handled separately) and for anything unassigned.
        """
        if channel == MAIN_MIDI_CHANNEL:
            return None
        if 0 <= channel <= 7:
            return channel
        if channel > MAIN_MIDI_CHANNEL:
            return channel - 1
        return None

    def _receive_main_midi(self, data1, data2):
        """CC 91 / CC 94 / CC 7 on the main channel: reverb, delay, master.

        CC 20..27 and CC 30..37 mirror the first device on return A / B, so
        the app's effects panel drives Live's real device parameters.
        """
        song = self.song()
        level = max(0.0, min(1.0, data2 / 127.0))
        if FX_RETURN_A_CC <= data1 < FX_RETURN_A_CC + FX_PARAMS_PER_DEVICE:
            self._set_return_param(0, data1 - FX_RETURN_A_CC, level)
            return
        if FX_RETURN_B_CC <= data1 < FX_RETURN_B_CC + FX_PARAMS_PER_DEVICE:
            self._set_return_param(1, data1 - FX_RETURN_B_CC, level)
            return
        if data1 == 7:
            master = getattr(song, "master_track", None)
            if master is not None:
                master.mixer_device.volume.value = level
            return

        # Handle CC 20-27 (return A) and 30-37 (return B) on per-track channels
        # (the per-track path lives in receive_midi; this function is the MAIN
        # channel only, so those CCs were already handled above).

        index = 0 if data1 == 91 else 1 if data1 == 94 else None
        if index is None:
            return
        returns = song.return_tracks
        if index < len(returns):
            returns[index].mixer_device.volume.value = level

    def disconnect(self):
        try:
            self._unbind_track_listeners()
            if self._selection_listener is not None:
                try:
                    self.song().view.remove_selected_track_listener(
                        self._selection_listener
                    )
                except Exception:
                    pass
                self._selection_listener = None
            if self._set_listener_bound:
                self.song().remove_tracks_listener(self._on_tracks_changed)
                self._set_listener_bound = False
        except Exception:
            pass
        self._drop_link()
        super(AbletonBridgeMixer, self).disconnect()


def create_instance(c_instance):
    return AbletonBridgeMixer(c_instance)
