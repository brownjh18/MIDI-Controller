import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  StyleSheet,
  Text,
  View,
  TouchableOpacity,
  TextInput,
  PanResponder,
  Platform,
  StatusBar,
  ScrollView,
  useWindowDimensions,
  AppState,
} from 'react-native';
import * as Battery from 'expo-battery';
import * as Network from 'expo-network';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import AsyncStorage from '@react-native-async-storage/async-storage';

const STORAGE_KEY = '@midi_controller:serverIp';
const SERVER_PORT = '8765';
const IS_WEB = Platform.OS === 'web';
/* Zero-config name the bridge announces over mDNS (midi-bridge.local). */
const MDNS_HOST = 'midi-bridge.local';
/* On the web the page's own host is the bridge; native builds have to find
   it on the LAN. */
const PAGE_HOST =
  IS_WEB && typeof window !== 'undefined' && window.location
    ? window.location.hostname || ''
    : '';
const DEFAULT_SERVER_HOST = PAGE_HOST || MDNS_HOST;
/* How long a single LAN probe may take before that address is written off. */
const PROBE_TIMEOUT = 1200;

/* Opens a throwaway WebSocket just long enough to learn whether a bridge
   answers at this address. Resolves with the host (or null). */
function probeHost(host) {
  return new Promise((resolve) => {
    let socket = null;
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        if (socket) {
          socket.onopen = null;
          socket.onerror = null;
          socket.onclose = null;
          socket.close();
        }
      } catch {
        /* already gone */
      }
      resolve(ok ? host : null);
    };
    const timer = setTimeout(() => finish(false), PROBE_TIMEOUT);
    try {
      socket = new WebSocket(`ws://${host}:${SERVER_PORT}`);
    } catch {
      finish(false);
      return;
    }
    socket.onopen = () => finish(true);
    socket.onerror = () => finish(false);
    socket.onclose = () => finish(false);
  });
}

function isIPv4(value) {
  const parts = String(value || '').split('.');
  return (
    parts.length === 4 &&
    parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  );
}

function subnetBase(address) {
  return String(address).split('.').slice(0, 3).join('.');
}

/* Probes every host on one /24, a batch at a time, and stops as soon as
   something answers. */
async function sweepSubnet(base, skip) {
  const hosts = [];
  for (let host = 1; host <= 254; host += 1) {
    const candidate = `${base}.${host}`;
    if (candidate !== skip) hosts.push(candidate);
  }
  const batchSize = 32;
  for (let start = 0; start < hosts.length; start += batchSize) {
    const answers = await Promise.all(
      hosts.slice(start, start + batchSize).map(probeHost)
    );
    const found = answers.filter(Boolean);
    if (found.length) return found;
  }
  return [];
}

const FADER_LABELS = ['REVERB', 'DELAY', 'MASTER'];
const FADER_CCS = [91, 94, 7];
const FADER_COLORS = ['#00e0ff', '#b06bff', '#31e06a'];
const INITIAL_FADER_VALUES = [47, 90, 100];
/* The effects section mirrors Live's MAIN section: the reverb return, the
   delay return and the master fader. All three always use this fixed MIDI
   channel (9), independent of the selected mixer voice. */
const MAIN_FX_CHANNEL = 8; // zero-based -> MIDI channel 9

/* Volume readouts are shown in dB, exactly like Ableton's mixer. Ableton's
   track volume is a 0..1 parameter whose fader reads 0 dB at 0.8 (a new
   track's default = unity gain) and +6 dB at 1.0; the app's 0-127 fader
   bytes are that parameter scaled by 127, so the same log taper applies. */
function volumeToDb(byte) {
  const v = Math.max(0, Math.min(127, Math.round(byte))) / 127;
  if (v <= 0) return '-inf';
  const db = (6.0 * Math.log(v / 0.8)) / Math.log(1.0 / 0.8);
  return `${db >= 0 ? '+' : ''}${db.toFixed(1)} dB`;
}

/* Mixer: channel strips (pan knob + volume fader) */
const MIXER_CHANNELS = [
  { name: 'CH 1', voice: 'BASS', color: '#2f9bff' },
  { name: 'CH 2', voice: 'LEAD', color: '#00e0ff' },
  { name: 'CH 3', voice: 'PAD', color: '#31e06a' },
  { name: 'CH 4', voice: 'STRINGS', color: '#ffd23f' },
  { name: 'CH 5', voice: 'BRASS', color: '#ff9f1c' },
  { name: 'CH 6', voice: 'BELL', color: '#ff4d8d' },
  { name: 'CH 7', voice: 'PLUCK', color: '#b06bff' },
  { name: 'CH 8', voice: 'DRUM', color: '#ff5c5c' },
];

/* ------------------------------------------------------------------ */
/*  Palette                                                           */
/* ------------------------------------------------------------------ */
const C = {
  bg: '#0d0d0d',
  chassis: '#191919',
  chassisEdge: '#2b2b2b',
  panel: '#1f1f1f',
  panelEdge: '#2e2e2e',
  inset: '#141414',
  surface: '#2b2b2b',
  surfaceEdge: '#3a3a3a',
  textWhite: '#f2f2f2',
  textGray: '#9a9a9a',
  textDim: '#5f5f5f',
  blue: '#1e9bff',
  cyan: '#00e0ff',
  green: '#31e06a',
  red: '#ff4d4d',
};

/* ------------------------------------------------------------------ */
/*  Bluetooth glyph                                                   */
/* ------------------------------------------------------------------ */
function BluetoothGlyph({ color, size = 18 }) {
  const stroke = Math.max(2, size * 0.12);
  const diag = size * 0.5;
  const offset = size * 0.24;
  return (
    <View style={{ width: size, height: size * 1.25 }}>
      <View style={[styles.glyphStem, { backgroundColor: color, width: stroke }]} />
      <View
        style={[
          styles.glyphDiag,
          { backgroundColor: color, width: diag, height: stroke, top: size * 0.2, left: offset, transform: [{ rotate: '55deg' }] },
        ]}
      />
      <View
        style={[
          styles.glyphDiag,
          { backgroundColor: color, width: diag, height: stroke, top: size * 0.2, left: offset - diag + stroke, transform: [{ rotate: '-55deg' }] },
        ]}
      />
      <View
        style={[
          styles.glyphDiag,
          { backgroundColor: color, width: diag, height: stroke, bottom: size * 0.2, left: offset, transform: [{ rotate: '-55deg' }] },
        ]}
      />
      <View
        style={[
          styles.glyphDiag,
          { backgroundColor: color, width: diag, height: stroke, bottom: size * 0.2, left: offset - diag + stroke, transform: [{ rotate: '55deg' }] },
        ]}
      />
    </View>
  );
}

/* ------------------------------------------------------------------ */
/*  PanResponder helper                                               */
/* ------------------------------------------------------------------ */
function usePanResponder({
  onStartShouldSet = () => true,
  onMoveShouldSet = () => false,
  onGrant,
  onMove,
  onRelease,
  onTerminate,
}) {
  const cbRef = useRef({
    onStartShouldSet,
    onMoveShouldSet,
    onGrant,
    onMove,
    onRelease,
    onTerminate,
  });
  // eslint-disable-next-line react-hooks/refs
  cbRef.current = {
    onStartShouldSet,
    onMoveShouldSet,
    onGrant,
    onMove,
    onRelease,
    onTerminate,
  };

  const ref = useRef(null);
  if (ref.current == null) {
    // eslint-disable-next-line react-hooks/refs
    ref.current = PanResponder.create({
      onStartShouldSetPanResponder: (e, gs) => cbRef.current.onStartShouldSet(e, gs),
      onMoveShouldSetPanResponder: (e, gs) => cbRef.current.onMoveShouldSet(e, gs),
      onPanResponderGrant: (e, gs) => cbRef.current.onGrant?.(e, gs),
      onPanResponderMove: (e, gs) => cbRef.current.onMove?.(e, gs),
      onPanResponderRelease: (e, gs) => cbRef.current.onRelease?.(e, gs),
      onPanResponderTerminate: (e, gs) => cbRef.current.onTerminate?.(e, gs),
    }).panHandlers;
  }
  // eslint-disable-next-line react-hooks/refs
  return ref.current;
}

/* ------------------------------------------------------------------ */
/*  Physical knob face (shared by pitch/mod and mixer pan)             */
/* ------------------------------------------------------------------ */
function KnobFace({
  size,
  angle,
  color,
  well = 0,
  ticks = 0,
  ring,
  ringWidth = 1,
  glow,
}) {
  const accent = color || C.blue;
  const ridges = size >= 34 ? 18 : 12;

  const body = (
    <View
      style={[
        styles.knobFace,
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          borderWidth: ringWidth,
          borderColor: ring || 'rgba(0,0,0,0.65)',
          shadowColor: glow || '#000',
          shadowOpacity: glow ? 0.9 : 0.55,
        },
      ]}
    >
      {/* knurled grip around the rim */}
      {[...Array(ridges)].map((_, i) => (
        <View
          key={`k${i}`}
          pointerEvents="none"
          style={[
            styles.knobKnurlWrap,
            { transform: [{ rotate: `${(360 / ridges) * i}deg` }] },
          ]}
        >
          <View
            style={[
              styles.knobKnurl,
              {
                width: Math.max(2, Math.round(size * 0.05)),
                height: Math.max(3, Math.round(size * 0.16)),
              },
            ]}
          />
        </View>
      ))}

      {/* specular highlight */}
      <View
        pointerEvents="none"
        style={[
          styles.knobGloss,
          {
            width: size * 0.56,
            height: size * 0.3,
            borderRadius: size * 0.3,
            top: size * 0.08,
            left: size * 0.22,
          },
        ]}
      />

      {/* pointer */}
      <View
        pointerEvents="none"
        style={[styles.knobPointerWrap, { transform: [{ rotate: `${angle}deg` }] }]}
      >
        <View
          style={[
            styles.knobPointerLine,
            {
              width: Math.max(2, Math.round(size * 0.07)),
              height: Math.max(4, Math.round(size * 0.3)),
              marginTop: Math.max(2, Math.round(size * 0.06)),
              backgroundColor: accent,
              shadowColor: accent,
            },
          ]}
        />
      </View>

      {/* centre hub */}
      <View
        pointerEvents="none"
        style={[
          styles.knobHub,
          { width: size * 0.4, height: size * 0.4, borderRadius: size * 0.2 },
        ]}
      />
    </View>
  );

  if (!well && !ticks) return body;

  const outer = size + well * 2;

  return (
    <View
      style={[
        styles.knobWell,
        { width: outer, height: outer, borderRadius: outer / 2, padding: well },
      ]}
    >
      {ticks > 1 &&
        [...Array(ticks)].map((_, i) => (
          <View
            key={`t${i}`}
            pointerEvents="none"
            style={[
              styles.knobTickWrap,
              { transform: [{ rotate: `${-135 + (i / (ticks - 1)) * 270}deg` }] },
            ]}
          >
            <View
              style={[
                styles.knobTick,
                {
                  backgroundColor:
                    i === 0 || i === ticks - 1 ? `${accent}dd` : `${accent}4d`,
                },
              ]}
            />
          </View>
        ))}
      {body}
    </View>
  );
}

/* ------------------------------------------------------------------ */
/*  Vertical fader                                                    */
/* ------------------------------------------------------------------ */
function LogicFader({ value, onValueChange, trackHeight, label, color, db, dbText }) {
  const startNorm = useRef(0);
  const valueRef = useRef(value);
  const capHeight = Math.max(24, Math.min(42, trackHeight * 0.32));
  const accent = color || C.blue;

  useEffect(() => {
    valueRef.current = value;
  });

  const fillHeight = (value / 127) * trackHeight;
  const capTop = trackHeight - fillHeight - capHeight / 2;
  const pct = Math.round((value / 127) * 100);
  const readout = dbText || (db ? volumeToDb(value) : `${pct}%`);

  const panResponder = usePanResponder({
    onMoveShouldSet: () => true,
    onGrant: () => {
      startNorm.current = valueRef.current / 127;
    },
    onMove: (_e, gs) => {
      const next = startNorm.current - gs.dy / trackHeight;
      const clamped = Math.max(0, Math.min(1, next));
      onValueChange(Math.round(clamped * 127));
    },
  });

  return (
    <View style={styles.faderCol}>
      <Text style={[styles.sectionLabel, { color: accent }]}>{label}</Text>
      <View
        style={[
          styles.faderTrack,
          { height: trackHeight, borderColor: `${accent}66` },
        ]}
      >
        <View style={[styles.trackFill, { height: fillHeight, backgroundColor: accent }]}>
          <View style={styles.trackGloss} />
        </View>
        <View style={[styles.capWrap, { top: capTop, height: capHeight }]}>
          <View {...panResponder} style={[styles.cap, { shadowColor: accent }]}>
            <View style={styles.capInner} />
            <View style={[styles.capLine, { backgroundColor: accent }]} />
          </View>
        </View>
      </View>
      <View style={[styles.pctBox, { borderColor: `${accent}66` }]}>
        <Text
          style={[styles.pctText, { color: accent }]}
          numberOfLines={1}
        >
          {readout}
        </Text>
      </View>
    </View>
  );
}

/* ------------------------------------------------------------------ */
/*  Rotary pan knob                                                   */
/* ------------------------------------------------------------------ */
function PanKnob({ value, color, onChange, onTap, size }) {
  const startRef = useRef(0);
  const valueRef = useRef(value);

  useEffect(() => {
    valueRef.current = value;
  });

  const panResponder = usePanResponder({
    onMoveShouldSet: () => true,
    onGrant: () => {
      startRef.current = valueRef.current;
    },
    onMove: (_e, gs) => {
      const next = startRef.current - gs.dy / 1.1;
      onChange(Math.max(0, Math.min(127, Math.round(next))));
    },
    onRelease: (_e, gs) => {
      // Treat a tap (no meaningful drag) on the knob as selecting the voice.
      if (Math.abs(gs.dx) < 5 && Math.abs(gs.dy) < 5) onTap?.();
    },
  });

  const angle = -135 + (value / 127) * 270;

  return (
    <View style={[styles.knobOuter, { width: size, height: size, borderRadius: size / 2 }]}>
      <View {...panResponder} style={{ width: size, height: size }}>
        <KnobFace
          size={size}
          angle={angle}
          color={color}
          ring={`${color}99`}
          ringWidth={2}
          glow={color}
        />
      </View>
    </View>
  );
}

/* ------------------------------------------------------------------ */
/*  Mixer channel strip (pan knob + volume fader)                     */
/* ------------------------------------------------------------------ */
function ChannelStrip({
  name,
  voice,
  color,
  volume,
  pan,
  db,
  onVolume,
  onPan,
  onSelect,
  selected,
  faderHeight,
  knobSize,
}) {
  const startNorm = useRef(0);
  const valueRef = useRef(volume);

  useEffect(() => {
    valueRef.current = volume;
  });

  const capHeight = Math.max(16, Math.min(36, faderHeight * 0.3));
  const fillHeight = (volume / 127) * faderHeight;
  const capTop = faderHeight - fillHeight - capHeight / 2;
  const dbLabel = db || volumeToDb(volume);
  const panOffset = pan - 64;
  const panLabel =
    panOffset === 0
      ? 'C'
      : panOffset < 0
        ? `L${Math.round((-panOffset / 64) * 100)}`
        : `R${Math.round((panOffset / 64) * 100)}`;

  const volResponder = usePanResponder({
    onMoveShouldSet: () => true,
    onGrant: () => {
      startNorm.current = valueRef.current / 127;
    },
    onMove: (_e, gs) => {
      const next = startNorm.current - gs.dy / faderHeight;
      onVolume(Math.round(Math.max(0, Math.min(1, next)) * 127));
    },
    onRelease: (_e, gs) => {
      // Treat a tap (no meaningful drag) on the fader as selecting the voice.
      if (Math.abs(gs.dx) < 5 && Math.abs(gs.dy) < 5) onSelect();
    },
  });

  return (
    <TouchableOpacity
      style={[
        styles.stripCol,
        selected && { backgroundColor: `${color}1f`, borderColor: `${color}99` },
      ]}
      onPress={onSelect}
      activeOpacity={0.9}
    >
      <View
        style={[
          styles.stripNameBtn,
          selected && { backgroundColor: color, borderColor: color },
        ]}
      >
        <Text
          style={[styles.stripName, { color: selected ? '#0d0d0d' : color }]}
          numberOfLines={1}
        >
          {name}
        </Text>
      </View>
      <Text style={[styles.stripVoice, { color }]} numberOfLines={1}>
        {voice}
      </Text>
      <PanKnob value={pan} color={color} onChange={onPan} onTap={onSelect} size={knobSize} />
      <Text style={styles.stripPan}>{panLabel}</Text>
      <View style={[styles.stripTrack, { height: faderHeight, borderColor: `${color}66` }]}>
        <View style={[styles.trackFill, { height: fillHeight, backgroundColor: color }]}>
          <View style={styles.trackGloss} />
        </View>
        <View style={[styles.capWrap, { top: capTop, height: capHeight }]}>
          <View {...volResponder} style={[styles.cap, styles.stripCap, { shadowColor: color }]}>
            <View style={styles.capInner} />
            <View style={[styles.capLine, { backgroundColor: color }]} />
          </View>
        </View>
      </View>
      <View style={[styles.stripPct, { borderColor: `${color}66` }]}>
        <Text style={[styles.stripPctText, { color }]}>{dbLabel}</Text>
      </View>
    </TouchableOpacity>
  );
}

/* ------------------------------------------------------------------ */
/*  Device battery                                                     */
/* ------------------------------------------------------------------ */
function useDeviceBattery() {
  const [level, setLevel] = useState(null); // 0..1, or null when unknown
  const [charging, setCharging] = useState(false);

  useEffect(() => {
    let mounted = true;
    let releaseWeb = () => {};
    let levelSub = null;
    let stateSub = null;

    if (Platform.OS === 'web') {
      // Browsers expose the Battery Status API directly; expo-battery's web
      // shim always reports a full charge, so it is not used here.
      const nav = typeof navigator !== 'undefined' ? navigator : null;
      if (nav && typeof nav.getBattery === 'function') {
        nav
          .getBattery()
          .then((battery) => {
            if (!mounted) return;
            const sync = () => {
              setLevel(battery.level);
              setCharging(!!battery.charging);
            };
            sync();
            battery.addEventListener('levelchange', sync);
            battery.addEventListener('chargingchange', sync);
            releaseWeb = () => {
              battery.removeEventListener('levelchange', sync);
              battery.removeEventListener('chargingchange', sync);
            };
          })
          .catch(() => {
            /* battery API unavailable (e.g. Safari / Firefox) */
          });
      }
    } else {
      Battery.getBatteryLevelAsync()
        .then((value) => {
          if (mounted && value >= 0) setLevel(value);
        })
        .catch(() => {});
      Battery.getBatteryStateAsync()
        .then((state) => {
          if (mounted) setCharging(state === Battery.BatteryState.CHARGING);
        })
        .catch(() => {});
      try {
        levelSub = Battery.addBatteryLevelListener(({ batteryLevel: value }) => {
          if (mounted && value >= 0) setLevel(value);
        });
        stateSub = Battery.addBatteryStateListener(({ batteryState }) => {
          if (mounted) setCharging(batteryState === Battery.BatteryState.CHARGING);
        });
      } catch (error) {
        console.log('Battery API unavailable:', error);
      }
    }

    return () => {
      mounted = false;
      releaseWeb();
      if (levelSub) levelSub.remove();
      if (stateSub) stateSub.remove();
    };
  }, []);

  return { level, charging };
}

/* ------------------------------------------------------------------ */
/*  Main App                                                          */
/* ------------------------------------------------------------------ */
export default function App() {
  const { width: SW, height: SH } = useWindowDimensions();
  const insets = useSafeAreaInsets();

  const [mixerOpen, setMixerOpen] = useState(true);
  const [mixerMaximized, setMixerMaximized] = useState(false);
  /* Live set mirrored from Ableton by the remote script: the set's track
     count, its track names, and each track's current volume/pan. Until
     the first snapshot arrives the app shows the 8 default strips. */
  const [trackNames, setTrackNames] = useState([]);
  /* 0 until the first Ableton snapshot arrives, so the app never invents
     channels that do not exist in the Live set. */
  const [trackCount, setTrackCount] = useState(0);
  /* Timestamp of the last fader/knob move made IN the app; incoming
     Ableton snapshots are ignored briefly after it so a mirror update
     can never stomp a drag that is still in progress. */
  const lastLocalMixerEditRef = useRef(0);

  const DISPLAY_H = Math.round(Math.max(40, Math.min(50, SH * 0.1)));
  const HEADER_H = Math.round(Math.max(54, Math.min(72, SH * 0.15)));
  /* The mixer always sits inline, RIGHT of the effects section (never below
     it) — its strips scroll horizontally when the screen is narrow. */
  const CONTROLS_H = Math.max(
    96,
    SH - insets.top - insets.bottom - 36 - HEADER_H - DISPLAY_H
  );
  const TRACK_H = Math.round(Math.max(56, CONTROLS_H - 84));
  const [connected, setConnected] = useState(false);
  /* False after the user presses DISCONNECT: auto-discovery then stays
     quiet until CONNECT is pressed again. */
  const [autoConnect, setAutoConnect] = useState(true);
  const [midiAvailable, setMidiAvailable] = useState(null);
  const [midiOutput, setMidiOutput] = useState('');
  const [midiError, setMidiError] = useState('');
  const [availableMidiOutputs, setAvailableMidiOutputs] = useState([]);
  const [midiInput, setMidiInput] = useState('');
  const [serverIp, setServerIp] = useState(DEFAULT_SERVER_HOST);
  const [serverAddressInput, setServerAddressInput] = useState(DEFAULT_SERVER_HOST);
  /* The main section mirrors Ableton: reverb return, delay return, master.
     Values below arrive from Live and are sent back when dragged. */
  const [mainFxVolumes, setMainFxVolumes] = useState([
    ...INITIAL_FADER_VALUES,
  ]);
  const [mainFxDbs, setMainFxDbs] = useState([null, null, null]);
  const [metronomeOn, setMetronomeOn] = useState(false);
  const [mixerVolumes, setMixerVolumes] = useState([100, 88, 76, 64, 52, 40, 96, 72]);
  const [mixerPans, setMixerPans] = useState([64, 64, 64, 64, 64, 64, 64, 64]);
  /* Exact dB text Live shows on each fader ("-5.1 dB"), so the app echoes
     Ableton verbatim instead of approximating the taper curve. */
  const [mixerDbs, setMixerDbs] = useState([]);
  const [selectedVoice, setSelectedVoice] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { level: batteryLevel, charging: batteryCharging } = useDeviceBattery();
  const [latency, setLatency] = useState(null); // round-trip ms, null until measured
  const [bpm, setBpm] = useState(120);
  const [clockRunning, setClockRunning] = useState(false);
  const [clockAngle, setClockAngle] = useState(0); // degrees, 0 = 12 o'clock

  /* Inline strips fill the controls row (~46px header/padding + fader +
     ~126px of name/voice/knob/pan/pct chrome). The maximized overlay is
     rendered at the chassis level, covering the ENTIRE app frame, so it
     gets its own taller fader sized to that bigger space. */
  const mixerInlineFader = Math.max(90, CONTROLS_H - 172);
  const mixerFullFader = Math.max(120, SH - insets.top - insets.bottom - 206);

  /* Landscape-only: app.json locks orientation for native builds; on the
     web we ask the browser too and block clearly-portrait viewports with
     a rotate prompt. Threshold 1.2 keeps near-square desktop windows
     usable. */
  const portraitLocked = SH > SW * 1.2;
  useEffect(() => {
    try {
      const orientation = globalThis.screen && globalThis.screen.orientation;
      if (orientation && typeof orientation.lock === 'function') {
        orientation.lock('landscape').catch(() => {});
      }
    } catch {
      /* Browsers may refuse the lock outside fullscreen; the guard above
         still keeps portrait viewports out. */
    }
  }, []);

  const ws = useRef(null);
  const autoConnectRef = useRef(true);
  const connectWebSocketRef = useRef(null);
  const reconnectTimer = useRef(null);
  /* Connection generation counter (late events from superseded sockets
     are ignored) and the last time the bridge answered a ping (used by
     the silent-drop watchdog below). */
  const attemptGenRef = useRef(0);
  const lastPongRef = useRef(0);
  /* Auto-connect: hosts are tried in order and cycled until one answers. */
  const hostCandidatesRef = useRef([DEFAULT_SERVER_HOST]);
  const [hostCandidates, setHostCandidates] = useState([DEFAULT_SERVER_HOST]);
  const activeHostRef = useRef(null);
  /* LAN scanning is throttled so a bridge that is switched off does not
     trigger a fresh sweep of 254 addresses every few seconds. */
  const scanInFlightRef = useRef(false);
  const lastScanRef = useRef(0);
  /* What the LAN sweep saw, shown in Settings so a phone that cannot find
     the bridge says why instead of just sitting on "offline". */
  const [localIp, setLocalIp] = useState('');
  const [scanNote, setScanNote] = useState('');

  /* The effective channel: the user's selection clamped to the tracks
     Ableton actually has, so the display and every send never point at a
     missing strip when the Live set shrinks. */
  const activeVoice =
    trackCount > 0 ? Math.min(selectedVoice, trackCount - 1) : 0;
  /* Before the first snapshot there are no real channels: show an explicit
     waiting state instead of invented strip names. */
  const noTracks = trackCount === 0;
  const selVoice = MIXER_CHANNELS[activeVoice] || MIXER_CHANNELS[0];
  const selVolume = mixerVolumes[activeVoice] ?? 100;
  const selPan = mixerPans[activeVoice] ?? 64;
  const activeFaderValues = mainFxVolumes;
  const activeFaderDbs = mainFxDbs;
  const selVolPct = Math.round((selVolume / 127) * 100);
  const selPanOffset = selPan - 64;
  const selPanLabel = noTracks
    ? '—'
    : selPanOffset === 0
      ? 'CENTER'
      : selPanOffset < 0
        ? `L ${Math.round((-selPanOffset / 64) * 100)}`
        : `R ${Math.round((selPanOffset / 64) * 100)}`;

  /* Applies a mixer_state snapshot from Ableton: how many tracks the set
     has (0-8), their names, and current volume (0..1) / pan (-1..1).
     The strip count therefore mirrors Ableton exactly, and moving a fader
     inside Ableton updates the app on the next ~1s poll. */
  const applyMixerState = useCallback((message) => {
    if (!Array.isArray(message.tracks)) return;
    // The metronome is transport state, not a fader: mirror it even while a
    // local fader move is being protected below.
    if (typeof message.metronome === 'boolean') {
      setMetronomeOn(message.metronome);
    }
    if (typeof message.playing === 'boolean') {
      setClockRunning(message.playing);
    }
    if (typeof message.tempo === 'number' && message.tempo > 0) {
      setBpm(Math.round(message.tempo * 10) / 10);
    }
    // Never fight a fader/knob move happening right now in the app.
    if (Date.now() - lastLocalMixerEditRef.current < 1500) return;
    const tracks = message.tracks
      .filter((t) => t && typeof t === 'object')
      .slice(0, MIXER_CHANNELS.length);
    const toByte = (v, lo, hi) => {
      if (v === null || v === undefined) return null;
      const n = Number(v);
      if (!Number.isFinite(n)) return null;
      const clamped = Math.max(lo, Math.min(hi, n));
      return Math.round(((clamped - lo) / (hi - lo)) * 127);
    };
    setTrackCount(tracks.length);
    setTrackNames(
      tracks.map((t) => (typeof t.name === 'string' ? t.name : ''))
    );
    setMixerVolumes((prev) => {
      const next = [...prev];
      tracks.forEach((t, i) => {
        const byte = toByte(t.volume, 0, 1);
        if (byte !== null) next[i] = byte;
      });
      return next;
    });
    setMixerPans((prev) => {
      const next = [...prev];
      tracks.forEach((t, i) => {
        const byte = toByte(t.pan, -1, 1);
        if (byte !== null) next[i] = byte;
      });
      return next;
    });
    setMixerDbs((prev) => {
      const next = [...prev];
      tracks.forEach((t, i) => {
        next[i] =
          typeof t.db === 'string' && /db/i.test(t.db) ? t.db : null;
      });
      return next;
    });
    // Main section: reverb return (A), delay return (B) and the master fader.
    const returns = Array.isArray(message.returns)
      ? message.returns
          .filter((r) => r && typeof r === 'object')
          .slice(0, 2)
      : [];
    const returnBytes = returns.map((r) => toByte(r.volume, 0, 1));
    const returnDbs = returns.map((r) =>
      typeof r.db === 'string' && /db/i.test(r.db) ? r.db : null
    );
    const master = message.master;
    const masterByte =
      master && typeof master === 'object'
        ? toByte(master.volume, 0, 1)
        : null;
    const masterDb =
      master && typeof master.db === 'string' && /db/i.test(master.db)
        ? master.db
        : null;
    setMainFxVolumes((prev) => {
      const next = [...prev];
      returnBytes.forEach((byte, i) => {
        if (byte !== null) next[i] = byte;
      });
      if (masterByte !== null) next[2] = masterByte;
      return next;
    });
    setMainFxDbs((prev) => {
      const next = [...prev];
      returnDbs.forEach((text, i) => {
        next[i] = text;
      });
      if (master) next[2] = masterDb;
      return next;
    });
  }, []);

  /* Hosts to try, most-likely first: last saved address, the address this page
     was opened from (web), the mDNS name the bridge announces, then a generic
   fallback. Deduplicated, empties dropped. */
  const buildCandidates = useCallback(async () => {
    const list = [];
    const push = (host) => {
      const trimmed = (host || '').trim();
      if (trimmed && !list.includes(trimmed)) list.push(trimmed);
    };
    try {
      push(await AsyncStorage.getItem(STORAGE_KEY));
    } catch (e) {
      console.log('Failed to load saved IP:', e);
    }
    if (typeof window !== 'undefined' && window.location) {
      push(window.location.hostname);
    }
    push(MDNS_HOST);
    push(DEFAULT_SERVER_HOST);
    return list;
  }, []);

  /* The Wi-Fi address is the one that matters: reading the "active" network
     could hand back a carrier address and send the sweep somewhere useless.
     The module can report 0.0.0.0 while Wi-Fi is still settling, so retry. */
  const readWifiAddress = useCallback(async () => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const address = await Network.getIpAddressAsync();
        if (isIPv4(address) && !address.startsWith('0.')) return address;
      } catch (error) {
        setScanNote(`Wi-Fi check failed: ${error?.message || error}`);
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    return '';
  }, []);

  /* Android has no mDNS resolver for apps, so `midi-bridge.local` usually
     fails there. Instead read the phone's own Wi-Fi address and probe that
     /24; failing that, sweep the network of the last address that worked
     (a laptop can move to a new host on the same Wi-Fi). */
  const scanLanHosts = useCallback(async () => {
    if (IS_WEB) return [];
    const bases = [];
    const address = await readWifiAddress();
    setLocalIp(address);
    if (address) bases.push(subnetBase(address));
    try {
      const saved = await AsyncStorage.getItem(STORAGE_KEY);
      if (isIPv4(saved)) bases.push(subnetBase(saved));
    } catch {
      /* storage is optional for scanning */
    }
    const subnets = [...new Set(bases.filter(Boolean))];
    for (const base of subnets) {
      setScanNote(`Scanning ${base}.0/24 for the bridge…`);
      const found = await sweepSubnet(base, address);
      if (found.length) {
        setScanNote(`Found the bridge at ${found[0]}`);
        return found;
      }
    }
    setScanNote(
      subnets.length
        ? 'No bridge answered on this Wi-Fi — is the bridge running on the laptop?'
        : 'Could not read this phone’s Wi-Fi address — type the laptop’s IP below.'
    );
    return [];
  }, [readWifiAddress]);

  /* Connection races EVERY candidate at once: whichever bridge answers
     first wins. A stale address (an old network's IP, a DNS blackhole)
     can never stall the others, so joining Wi-Fi connects near-instantly. */
  const connectWebSocket = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    // Supersede any previous attempt; late events from old sockets are
    // ignored because their generation no longer matches.
    const generation = (attemptGenRef.current += 1);
    const candidates = hostCandidatesRef.current;
    const scheduleRetry = (delay) => {
      if (!autoConnectRef.current) return;
      if (generation !== attemptGenRef.current) return;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = setTimeout(
        () => connectWebSocketRef.current?.(),
        delay
      );
    };
    if (!candidates.length) {
      scheduleRetry(1500);
      return;
    }

    /* Discovery runs alongside the name-based race: if this is a native
       build and nothing answers by name, sweep the phone's own subnet. */
    if (
      !IS_WEB &&
      !ws.current &&
      !scanInFlightRef.current &&
      Date.now() - lastScanRef.current > 8000
    ) {
      scanInFlightRef.current = true;
      lastScanRef.current = Date.now();
      scanLanHosts()
        .then((found) => {
          scanInFlightRef.current = false;
          if (!found.length || generation !== attemptGenRef.current || adopted) {
            return;
          }
          const merged = [
            ...found,
            ...hostCandidatesRef.current.filter(
              (host) => !found.includes(host)
            ),
          ];
          hostCandidatesRef.current = merged;
          setHostCandidates(merged);
          connectWebSocketRef.current?.();
        })
        .catch(() => {
          scanInFlightRef.current = false;
        });
    }

    const pending = new Set();
    let adopted = false;

    // If nothing opens within 6s (every candidate unreachable), close the
    // stragglers and run the whole race again.
    const raceTimer = setTimeout(() => {
      if (generation !== attemptGenRef.current || adopted) return;
      pending.forEach((s) => {
        try {
          s.close();
        } catch {
          /* already gone */
        }
      });
      scheduleRetry(1500);
    }, 6000);

    candidates.forEach((host) => {
      let socket;
      try {
        socket = new WebSocket(`ws://${host}:${SERVER_PORT}`);
      } catch (error) {
        console.log('Failed to create WebSocket for', host, error);
        return;
      }
      pending.add(socket);
      socket.onopen = () => {
        if (generation !== attemptGenRef.current || adopted) {
          try {
            socket.close();
          } catch {
            /* noop */
          }
          return;
        }
        adopted = true;
        clearTimeout(raceTimer);
        pending.forEach((other) => {
          if (other !== socket) {
            other.onopen = null;
            other.onmessage = null;
            other.onerror = null;
            other.onclose = null;
            try {
              other.close();
            } catch {
              /* noop */
            }
          }
        });
        pending.clear();
        ws.current = socket;
        activeHostRef.current = host;
        setServerIp(host);
        setConnected(true);
        setMidiAvailable(null);
        console.log('WebSocket Connected to', host);
        // Remember whatever address just worked so future launches are instant.
        AsyncStorage.setItem(STORAGE_KEY, host).catch(() => {});
        setServerAddressInput(host);
        lastPongRef.current = Date.now();
        socket.onmessage = (event) => {
          try {
            const message = JSON.parse(event.data);
            if (message.type === 'status') {
              setMidiAvailable(message.midi_available === true);
              setMidiOutput(message.midi_output || '');
              setMidiError(message.midi_error || '');
              setAvailableMidiOutputs(
                Array.isArray(message.available_outputs)
                  ? message.available_outputs
                  : []
              );
            } else if (message.type === 'pong') {
              lastPongRef.current = Date.now();
              const sentAt = Number(message.t);
              if (Number.isFinite(sentAt)) {
                setLatency(Math.max(0, Date.now() - sentAt));
              }
            } else if (message.type === 'track_names') {
              const names = Array.isArray(message.names) ? message.names : [];
              setTrackNames(
                names.map((name) => (typeof name === 'string' ? name : ''))
              );
            } else if (message.type === 'mixer_state') {
              applyMixerState(message);
            } else if (message.type === 'input') {
              setMidiInput(typeof message.name === 'string' ? message.name : '');
            }
          } catch (error) {
            console.log('Invalid message from MIDI bridge:', error);
          }
        };
        socket.onerror = (e) => {
          console.log('WebSocket Error: ', e.message);
        };
        socket.onclose = (e) => {
          if (ws.current !== socket) return;
          ws.current = null;
          if (generation !== attemptGenRef.current) return;
          setConnected(false);
          setMidiAvailable(false);
          setMidiOutput('');
          setMidiInput('');
          setMidiError('');
          setAvailableMidiOutputs([]);
          setLatency(null);
          console.log(
            'WebSocket Closed: ',
            e.code,
            e.reason,
            '- retrying via auto-discovery'
          );
          scheduleRetry(1500);
        };
      };
      socket.onerror = () => {
        /* onclose follows; the race keeps the other candidates going. */
      };
      socket.onclose = () => {
        pending.delete(socket);
        if (ws.current === socket) return;
        if (!adopted && !pending.size) {
          // Every candidate failed before any opened: try again shortly.
          clearTimeout(raceTimer);
          scheduleRetry(1500);
        }
      };
    });
    if (!pending.size && !adopted) {
      clearTimeout(raceTimer);
      scheduleRetry(1500);
    }
  }, [applyMixerState, scanLanHosts]);

  useEffect(() => {
    connectWebSocketRef.current = connectWebSocket;
  }, [connectWebSocket]);

  useEffect(() => {
    let cancelled = false;
    let bootTimer = null;
    (async () => {
      const candidates = await buildCandidates();
      if (cancelled) return;
      hostCandidatesRef.current = candidates;
      setHostCandidates(candidates);
      bootTimer = setTimeout(() => connectWebSocketRef.current?.(), 0);
    })();
    return () => {
      cancelled = true;
      if (bootTimer) clearTimeout(bootTimer);
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      if (ws.current) {
        const socket = ws.current;
        ws.current = null;
        socket.close();
      }
    };
  }, [buildCandidates]);

  const sendMessage = useCallback(
    (type, control, value, ch) => {
      const socket = ws.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({ type, control, value, channel: ch ?? activeVoice })
        );
      }
    },
    [activeVoice]
  );

  /* Round-trip latency measured against the bridge, plus a watchdog: a
     socket that stops answering pongs for ~9s is silently dead (Wi-Fi
     dropped or switched networks) - close it so auto-discovery takes over
     immediately instead of waiting for a TCP timeout minutes later. */
  useEffect(() => {
    if (!connected) return undefined;
    const ping = () => {
      const socket = ws.current;
      if (socket?.readyState === WebSocket.OPEN) {
        if (Date.now() - lastPongRef.current > 9000) {
          console.log('Bridge stopped answering - reconnecting');
          try {
            socket.close();
          } catch {
            /* already closing */
          }
          return;
        }
        socket.send(JSON.stringify({ type: 'ping', t: Date.now() }));
      }
    };
    lastPongRef.current = Date.now();
    ping();
    const timer = setInterval(ping, 3000);
    return () => clearInterval(timer);
  }, [connected]);

  /* Joining Wi-Fi should reconnect instantly: the browser 'online' event
     fires when a network comes back, and AppState 'active' when a native
     app returns to the foreground. A healthy socket (answered a ping in
     the last 6s) is left alone; anything dead or stale is re-raced now. */
  useEffect(() => {
    const reconnectIfStale = () => {
      if (!autoConnectRef.current) return;
      if (ws.current && Date.now() - lastPongRef.current < 6000) return;
      const socket = ws.current;
      if (socket) {
        ws.current = null;
        try {
          socket.close();
        } catch {
          /* noop */
        }
      }
      connectWebSocketRef.current?.();
    };
    const onAppState = (state) => {
      if (state === 'active') reconnectIfStale();
    };
    const appSub = AppState.addEventListener('change', onAppState);
    const onOnline = () => reconnectIfStale();
    if (typeof window !== 'undefined' && window.addEventListener) {
      window.addEventListener('online', onOnline);
    }
    return () => {
      appSub.remove();
      if (typeof window !== 'undefined' && window.removeEventListener) {
        window.removeEventListener('online', onOnline);
      }
    };
  }, []);

  const bpmRef = useRef(bpm);
  useEffect(() => {
    bpmRef.current = bpm;
  }, [bpm]);

  /* Streams MIDI clock (24 ppqn) + transport to the bridge while running. */
  useEffect(() => {
    if (!clockRunning) return undefined;
    const startedAt = Date.now();
    let sent = 0;
    let lastFrame = 0;
    const timer = setInterval(() => {
      const now = Date.now();
      const beatMs = 60000 / bpmRef.current;
      const pulseMs = beatMs / 24;
      const want = Math.floor((now - startedAt) / pulseMs);
      const socket = ws.current;
      if (socket?.readyState === WebSocket.OPEN) {
        let burst = Math.min(want - sent, 48); // cap bursts after a stall
        while (burst > 0) {
          socket.send(JSON.stringify({ type: 'clock' }));
          burst -= 1;
        }
      }
      sent = want;
      if (now - lastFrame > 80) {
        lastFrame = now;
        setClockAngle((((now - startedAt) % beatMs) / beatMs) * 360);
      }
    }, 40);
    return () => clearInterval(timer);
  }, [clockRunning]);

  const toggleClock = useCallback(() => {
    setClockRunning((running) => {
      const next = !running;
      const socket = ws.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: next ? 'start' : 'stop' }));
      }
      return next;
    });
  }, []);

  /* Metronome: the button mirrors Live's metronome state (arriving in every
     snapshot) and toggles Live's metronome through the bridge. */
  const toggleMetronome = useCallback(() => {
    setMetronomeOn((on) => {
      const next = !on;
      sendMessage('metronome', 0, next ? 1 : 0);
      return next;
    });
  }, [sendMessage]);

  const tapTimesRef = useRef([]);
  const handleTapTempo = useCallback(() => {
    const now = Date.now();
    const recent = tapTimesRef.current.filter((t) => now - t < 2400);
    recent.push(now);
    tapTimesRef.current = recent.slice(-5);
    const taps = tapTimesRef.current;
    if (taps.length >= 2) {
      const gaps = taps.slice(1).map((t, i) => t - taps[i]);
      const avg = gaps.reduce((sum, gap) => sum + gap, 0) / gaps.length;
      const next = Math.max(40, Math.min(240, 60000 / avg));
      const rounded = Math.round(next * 10) / 10;
      setBpm(rounded);
      // Set Live's tempo too, so the metronome and the app stay in time.
      sendMessage('tempo', 0, rounded);
    }
  }, [sendMessage]);

  const handleMixerVolume = useCallback(
    (index, val) => {
      lastLocalMixerEditRef.current = Date.now();
      setMixerVolumes((prev) => {
        const next = [...prev];
        next[index] = val;
        return next;
      });
      // The cached Ableton dB text no longer describes this fader.
      setMixerDbs((prev) => {
        const next = [...prev];
        next[index] = null;
        return next;
      });
      sendMessage('cc', 7, Math.round(val), index);
    },
    [sendMessage]
  );

  const handleMixerPan = useCallback(
    (index, val) => {
      lastLocalMixerEditRef.current = Date.now();
      setMixerPans((prev) => {
        const next = [...prev];
        next[index] = val;
        return next;
      });
      sendMessage('cc', 10, Math.round(val), index);
    },
    [sendMessage]
  );

  const handleFaderChange = useCallback(
    (index, val) => {
      lastLocalMixerEditRef.current = Date.now();
      setMainFxVolumes((prev) => {
        const next = [...prev];
        next[index] = val;
        return next;
      });
      // The cached Ableton dB text no longer describes this fader.
      setMainFxDbs((prev) => {
        const next = [...prev];
        next[index] = null;
        return next;
      });
      // Reverb/Delay/Master always target the fixed MAIN channel.
      sendMessage('cc', FADER_CCS[index], Math.round(val), MAIN_FX_CHANNEL);
    },
    [sendMessage]
  );

  /* CONNECT: saves any typed address and starts the discovery race.
     The sheet stays open so the status line can flip to connected. */
  const connectBridge = useCallback(async () => {
    autoConnectRef.current = true;
    setAutoConnect(true);
    const address = serverAddressInput.trim();
    if (address) {
      setServerAddressInput(address);
      setServerIp(address);
      try {
        await AsyncStorage.setItem(STORAGE_KEY, address);
      } catch (e) {
        console.log('Failed to save IP:', e);
      }
    }
    // Manual address wins the auto-connect order from now on.
    const candidates = await buildCandidates();
    hostCandidatesRef.current = candidates;
    setHostCandidates(candidates);
    connectWebSocketRef.current?.();
  }, [buildCandidates, serverAddressInput]);

  /* DISCONNECT: closes the socket and stops the automatic retries until
     CONNECT is pressed again. */
  const disconnectBridge = useCallback(() => {
    autoConnectRef.current = false;
    setAutoConnect(false);
    // Supersede the running attempt so none of its events reconnect us.
    attemptGenRef.current += 1;
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    const socket = ws.current;
    ws.current = null;
    if (socket) {
      try {
        socket.close();
      } catch {
        /* already gone */
      }
    }
    setConnected(false);
    setMidiAvailable(false);
    setMidiOutput('');
    setMidiInput('');
    setMidiError('');
    setAvailableMidiOutputs([]);
    setLatency(null);
  }, []);

  const selectMidiOutput = useCallback((name) => {
    const socket = ws.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'select_output', name }));
    }
  }, []);

  /* The mixer panel: always inline, RIGHT of the effects section (never
     below it); strips scroll horizontally on narrow screens. MAXIMIZE
     detaches it to the chassis so it covers the entire app space. */
  const mixerPanel = mixerOpen ? (
    <View
      style={
        mixerMaximized ? [styles.mixer, styles.mixerMax] : styles.mixerInline
      }
    >
      <View style={styles.mixerHeader}>
        <Text style={styles.mixerTitle}>MIXER</Text>
        <Text style={styles.mixerSub}>
          {trackCount} CH · TAP CH TO SELECT
        </Text>
        <View style={styles.mixerSpacer} />
        <TouchableOpacity
          style={styles.mixerBtn}
          onPress={() => setMixerMaximized((v) => !v)}
        >
          <Text style={styles.mixerBtnText}>
            {mixerMaximized ? 'MINIMIZE' : 'MAXIMIZE'}
          </Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.mixerClose}
          onPress={() => {
            setMixerOpen(false);
            setMixerMaximized(false);
          }}
        >
          <Text style={styles.mixerCloseText}>✕</Text>
        </TouchableOpacity>
      </View>

      {/* Strips scroll horizontally when the sheet is narrower than the
          channel list (small screens / phones). */}
      <ScrollView
        horizontal
        style={styles.mixerStrips}
        contentContainerStyle={styles.mixerStripsContent}
        showsHorizontalScrollIndicator={false}
      >
        {noTracks ? (
          <View style={styles.mixerWaiting}>
            <Text style={styles.mixerWaitingTitle}>WAITING FOR ABLETON</Text>
            <Text style={styles.mixerWaitingText}>
              No track data yet — assign AbletonBridgeMixer in the
              Link/Tempo/MIDI preferences of Live.
            </Text>
          </View>
        ) : (
          MIXER_CHANNELS.slice(0, trackCount).map((ch, i) => (
            <React.Fragment key={ch.name}>
              {i > 0 && <View style={styles.vSep} />}
              <ChannelStrip
                name={trackNames[i] || ch.name}
                voice={ch.voice}
                color={ch.color}
                volume={mixerVolumes[i] ?? 100}
                pan={mixerPans[i] ?? 64}
                db={mixerDbs[i] || null}
                onVolume={(v) => handleMixerVolume(i, v)}
                onPan={(v) => handleMixerPan(i, v)}
                onSelect={() => setSelectedVoice(i)}
                selected={activeVoice === i}
                faderHeight={mixerMaximized ? mixerFullFader : mixerInlineFader}
                knobSize={40}
              />
            </React.Fragment>
          ))
        )}
      </ScrollView>
    </View>
  ) : null;

  return (
    <>
      <StatusBar translucent backgroundColor="#0d0d0d" barStyle="light-content" />
      <View style={styles.container}>
        <View
          style={[
            styles.chassis,
            {
              marginTop: insets.top + 6,
              marginBottom: insets.bottom + 6,
              marginLeft: insets.left + 6,
              marginRight: insets.right + 6,
            },
          ]}
        >
          {/* ---------------- Header ---------------- */}
          <View style={[styles.header, { height: HEADER_H }]}>
            <TouchableOpacity
              style={[styles.settingsBtn, settingsOpen && styles.settingsBtnActive]}
              onPress={() => setSettingsOpen((v) => !v)}
            >
              <Text style={styles.settingsIcon}>⚙</Text>
              <Text style={styles.settingsText}>Settings</Text>
            </TouchableOpacity>

            <View style={styles.headerCenter}>
              <View style={styles.connBlock}>
                <BluetoothGlyph color={connected ? C.blue : C.textDim} />
                <View style={styles.connTextWrap}>
                  <Text style={styles.connTitle} numberOfLines={1}>
                  {connected ? 'Bridge Connected' : 'Bridge Offline'}
                  </Text>
                <Text style={styles.connSub} numberOfLines={1}>
                  {midiAvailable === true
                    ? midiOutput || 'MIDI Ready'
                    : midiAvailable === false
                      ? 'MIDI Unavailable'
                      : 'MIDI Bridge'}
                  {midiInput ? ` · ${midiInput}` : ''}
                </Text>
                </View>
                <View
                  style={[
                    styles.connDot,
                    { backgroundColor: connected ? C.green : C.red },
                  ]}
                />
              </View>

              <View style={styles.divider} />

              <View style={styles.infoCol}>
                <Text style={styles.infoValue}>{latency == null ? '--' : latency}</Text>
                <Text style={styles.infoLabel}>Latency ms</Text>
              </View>

              <View style={styles.divider} />

              <View style={styles.bpmBlock}>
                <TouchableOpacity
                  style={[styles.transportBtn, clockRunning && styles.transportBtnActive]}
                  onPress={toggleClock}
                  accessibilityRole="button"
                  accessibilityLabel={clockRunning ? 'Stop MIDI clock' : 'Start MIDI clock'}
                >
                  {clockRunning && (
                    <View
                      style={[
                        styles.transportHand,
                        { transform: [{ rotate: `${clockAngle}deg` }] },
                      ]}
                    >
                      <View style={styles.transportHandDot} />
                    </View>
                  )}
                  <Text
                    style={[
                      styles.transportText,
                      clockRunning && styles.transportTextActive,
                    ]}
                  >
                    {clockRunning ? '❚❚' : '▶'}
                  </Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[
                    styles.transportBtn,
                    metronomeOn && styles.transportBtnActive,
                  ]}
                  onPress={toggleMetronome}
                  accessibilityRole="button"
                  accessibilityLabel={
                    metronomeOn ? 'Metronome on' : 'Metronome off'
                  }
                >
                  <Text
                    style={[
                      styles.transportText,
                      metronomeOn && styles.transportTextActive,
                    ]}
                  >
                    ♩
                  </Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={styles.bpmTap}
                  onPress={handleTapTempo}
                  accessibilityRole="button"
                  accessibilityLabel="Tap tempo"
                >
                  <Text style={styles.bpmText}>{bpm.toFixed(1)} BPM</Text>
                  <Text style={styles.bpmHint}>TAP</Text>
                </TouchableOpacity>
              </View>

              <View style={styles.divider} />

              <View style={styles.batteryBlock}>
                <Text style={styles.batteryPct}>
                  {batteryLevel == null ? '--' : `${Math.round(batteryLevel * 100)}%`}
                </Text>
                {batteryCharging && <Text style={styles.batteryCharge}>⚡</Text>}
                <View style={styles.batteryBody}>
                  <View
                    style={[
                      styles.batteryFill,
                      {
                        width: `${Math.round(
                          Math.min(1, Math.max(0, batteryLevel ?? 0)) * 100
                        )}%`,
                        backgroundColor: batteryCharging
                          ? C.green
                          : batteryLevel != null && batteryLevel < 0.2
                            ? C.red
                            : C.blue,
                      },
                    ]}
                  />
                </View>
                <View style={styles.batteryNub} />
              </View>
            </View>

            <TouchableOpacity
              style={[styles.mixerOpenBtn, mixerOpen && styles.mixerOpenBtnActive]}
              onPress={() => setMixerOpen((v) => !v)}
            >
              <Text style={[styles.mixerOpenText, mixerOpen && styles.mixerOpenTextActive]}>
                MIXER
              </Text>
            </TouchableOpacity>
          </View>

          {/* ---------------- Main display: selected voice ---------------- */}
          <View style={[styles.display, { height: DISPLAY_H }]}>
            <View style={[styles.displayBadge, { borderColor: `${selVoice.color}99` }]}>
              <Text style={[styles.displayBadgeTitle, { color: selVoice.color }]}>
                VOICE CHANNEL
              </Text>
              <Text
                style={[styles.displayBadgeName, { color: selVoice.color }]}
                numberOfLines={1}
              >
                {noTracks ? 'ABLETON' : trackNames[activeVoice] || selVoice.name}
              </Text>
              <Text
                style={[styles.displayBadgeSub, { color: selVoice.color }]}
                numberOfLines={1}
              >
                {noTracks ? 'WAITING FOR LIVE' : selVoice.voice}
              </Text>
            </View>

            <View style={styles.displayFields}>
              <View style={styles.displayField}>
                <Text style={styles.displayFieldLabel}>VOLUME</Text>
                <Text style={[styles.displayFieldValue, { color: selVoice.color }]}>
                  {noTracks ? '—' : mixerDbs[activeVoice] || volumeToDb(selVolume)}
                </Text>
              </View>

              <View style={styles.displayDivider} />

              <View style={styles.displayField}>
                <Text style={styles.displayFieldLabel}>PAN</Text>
                <Text style={[styles.displayFieldValue, { color: selVoice.color }]}>
                  {selPanLabel}
                </Text>
              </View>

              <View style={styles.displayDivider} />

              <View style={styles.displayField}>
                <Text style={styles.displayFieldLabel}>MIDI CH</Text>
                <Text style={styles.displayFieldValue}>{activeVoice + 1}</Text>
              </View>

              <View style={styles.displayDivider} />

              <View style={styles.displayField}>
                <Text style={styles.displayFieldLabel}>CC</Text>
                <Text style={styles.displayFieldValue}>7 / 10</Text>
              </View>
            </View>

            <View style={styles.displayMeter}>
              <View
                style={[
                  styles.displayMeterFill,
                  { height: `${selVolPct}%`, backgroundColor: selVoice.color },
                ]}
              />
            </View>
          </View>

          {/* ---------------- Controls ---------------- */}
          <View style={[styles.controlsRow, { height: CONTROLS_H }]}>
            {/* Center: Ableton main-effect faders (Reverb, Delay, Volume) */}
            <View
              style={[
                styles.effectsPanel,
                mixerOpen
                  ? {
                      flexGrow: 0,
                      flexShrink: 1,
                      flexBasis: 260,
                      minWidth: SW < 520 ? 150 : 190,
                    }
                  : null,
              ]}
            >
              <Text style={styles.sectionLabel}>EFFECTS</Text>
              <Text style={styles.effectsSubLabel}>
                REVERB / DELAY / MASTER = LIVE MAIN
              </Text>
              <View style={styles.fadersRow}>
                {FADER_CCS.map((cc, i) => (
                  <React.Fragment key={cc}>
                    {i > 0 && <View style={styles.vSep} />}
                    <LogicFader
                      value={activeFaderValues[i]}
                      onValueChange={(val) => handleFaderChange(i, val)}
                      trackHeight={TRACK_H}
                      label={FADER_LABELS[i]}
                      color={FADER_COLORS[i]}
                      db
                      dbText={activeFaderDbs[i]}
                    />
                  </React.Fragment>
                ))}
              </View>
            </View>
            {/* Right: the mixer sits beside the effects section (always);
                when maximized it detaches to the chassis instead */}
            {!mixerMaximized && mixerPanel}
          </View>

          {/* Maximized mixer: anchored to the chassis, so it covers the
              entire app space - header, display and controls row. */}
          {mixerMaximized && mixerPanel}

          {/* ---------------- Settings ---------------- */}
          {settingsOpen && (
            <View style={styles.settingsOverlay}>
              <View style={styles.settingsCard}>
                <View style={styles.settingsHeader}>
                  <Text style={styles.settingsTitle}>SETTINGS</Text>
                  <TouchableOpacity
                    style={styles.mixerClose}
                    onPress={() => setSettingsOpen(false)}
                  >
                    <Text style={styles.mixerCloseText}>✕</Text>
                  </TouchableOpacity>
                </View>

                {/* The body scrolls, the header and the buttons stay put, so
                    the sheet fits any screen height. */}
                <ScrollView
                  style={styles.settingsScroll}
                  contentContainerStyle={styles.settingsScrollContent}
                  showsVerticalScrollIndicator
                  persistentScrollbar={Platform.OS === 'android'}
                >
                  <Text style={styles.settingsLabel}>SERVER ADDRESS</Text>
                  <TextInput
                    style={styles.settingsInput}
                    value={serverAddressInput}
                    onChangeText={setServerAddressInput}
                    placeholder={DEFAULT_SERVER_HOST}
                    placeholderTextColor={C.textDim}
                    autoCapitalize="none"
                    autoCorrect={false}
                  />
                  <Text style={styles.settingsAutoHint}>
                    AUTO - no typing needed: the app sweeps this phone&apos;s
                    Wi-Fi network for the bridge and reconnects on its own. Type
                    an address only for special setups.
                  </Text>

                  <Text style={styles.settingsLabel}>THIS PHONE</Text>
                  <Text style={styles.settingsPort}>{localIp || 'unknown'}</Text>
                  {scanNote ? (
                    <Text style={styles.settingsAutoHint}>{scanNote}</Text>
                  ) : null}

                  <Text style={styles.settingsLabel}>PORT</Text>
                  <Text style={styles.settingsPort}>{SERVER_PORT}</Text>

                  <View style={styles.settingsStatusRow}>
                    <View
                      style={[
                        styles.connDot,
                        { backgroundColor: connected ? C.green : C.red },
                      ]}
                    />
                    <Text style={styles.settingsStatusText}>
                      {connected
                        ? midiAvailable === true
                          ? 'Bridge and MIDI output connected'
                          : midiAvailable === false
                            ? 'Bridge connected, MIDI output unavailable'
                            : 'Bridge connected, checking MIDI output'
                        : 'Bridge disconnected'}
                    </Text>
                  </View>
                  <Text style={styles.settingsPortDetails}>
                    {connected
                      ? midiAvailable
                        ? `Connected via ${serverIp} to MIDI output "${midiOutput}".`
                        : midiError ||
                          `MIDI output "${midiOutput || 'AbletonBridge'}" is unavailable.`
                      : autoConnect
                        ? `Auto-connecting (tried: ${hostCandidates.join(', ')})…`
                        : 'Disconnected by hand. Press CONNECT to search again.'}
                  </Text>

                  <Text style={styles.settingsLabel}>MIDI OUTPUT</Text>
                  {availableMidiOutputs.length > 0 ? (
                    <View style={styles.outputList}>
                      {availableMidiOutputs.map((name) => {
                        const isSelected = name === midiOutput;
                        return (
                          <TouchableOpacity
                            key={name}
                            style={[styles.outputRow, isSelected && styles.outputRowActive]}
                            onPress={() => selectMidiOutput(name)}
                            accessibilityRole="button"
                            accessibilityLabel={`Use MIDI output ${name}`}
                          >
                            <View
                              style={[styles.outputDot, isSelected && styles.outputDotActive]}
                            />
                            <Text
                              style={[
                                styles.outputRowText,
                                isSelected && styles.outputRowTextActive,
                              ]}
                              numberOfLines={1}
                            >
                              {name}
                            </Text>
                          </TouchableOpacity>
                        );
                      })}
                    </View>
                  ) : (
                    <Text style={styles.settingsPortDetails}>
                      {connected
                        ? 'No workstation MIDI outputs are currently detected.'
                        : 'Outputs appear here once the bridge is connected.'}
                    </Text>
                  )}
                </ScrollView>

                <View style={styles.settingsActions}>
                  {connected ? (
                    <TouchableOpacity
                      style={styles.settingsDanger}
                      onPress={disconnectBridge}
                      accessibilityRole="button"
                      accessibilityLabel="Disconnect from the bridge"
                    >
                      <Text style={styles.settingsDangerText}>DISCONNECT</Text>
                    </TouchableOpacity>
                  ) : (
                    <TouchableOpacity
                      style={styles.settingsPrimary}
                      onPress={connectBridge}
                      accessibilityRole="button"
                      accessibilityLabel="Connect to the bridge"
                    >
                      <Text style={styles.settingsPrimaryText}>CONNECT</Text>
                    </TouchableOpacity>
                  )}
                  <TouchableOpacity
                    style={styles.settingsSecondary}
                    onPress={() => setSettingsOpen(false)}
                  >
                    <Text style={styles.settingsSecondaryText}>CLOSE</Text>
                  </TouchableOpacity>
                </View>
              </View>
            </View>
          )}

          {/* Landscape guard: app.json locks native rotation; on the web a
              clearly-portrait viewport gets a rotate prompt (near-square
              desktop windows stay usable). */}
          {portraitLocked && (
            <View style={styles.rotateOverlay}>
              <Text style={styles.rotateIcon}>↻</Text>
              <Text style={styles.rotateTitle}>ROTATE TO LANDSCAPE</Text>
              <Text style={styles.rotateHint}>
                The controller runs in landscape only.
              </Text>
            </View>
          )}
        </View>
      </View>
    </>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: C.bg,
  },
  chassis: {
    flex: 1,
    backgroundColor: C.chassis,
    borderWidth: 1,
    borderColor: C.chassisEdge,
    borderRadius: 20,
    padding: 8,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 12 },
    shadowOpacity: 0.7,
    shadowRadius: 20,
    elevation: 14,
    overflow: 'hidden',
  },

  /* ----- Glyphs ----- */
  glyphStem: {
    position: 'absolute',
    left: 0,
    top: 0,
    bottom: 0,
    borderRadius: 2,
  },
  glyphDiag: {
    position: 'absolute',
    borderRadius: 2,
  },

  /* ----- Header ----- */
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: C.inset,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: C.panelEdge,
    paddingHorizontal: 12,
    marginBottom: 8,
  },
  settingsBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingRight: 12,
  },
  settingsIcon: {
    color: C.textGray,
    fontSize: 15,
    marginRight: 7,
  },
  settingsText: {
    color: C.textWhite,
    fontSize: 14,
    fontWeight: '600',
  },
  headerCenter: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
  },
  connBlock: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  connTextWrap: {
    marginLeft: 8,
    marginRight: 8,
  },
  connTitle: {
    color: C.textWhite,
    fontSize: 14,
    fontWeight: '700',
  },
  connSub: {
    color: C.textDim,
    fontSize: 10,
  },
  connDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  divider: {
    width: 1,
    height: 26,
    backgroundColor: C.panelEdge,
  },
  infoCol: {
    alignItems: 'center',
    minWidth: 50,
  },
  infoValue: {
    color: C.textWhite,
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.4,
  },
  infoLabel: {
    color: C.textDim,
    fontSize: 9,
    marginTop: 1,
  },
  bpmBlock: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  transportBtn: {
    width: 22,
    height: 22,
    borderRadius: 11,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
    backgroundColor: C.surface,
    alignItems: 'center',
    justifyContent: 'center',
    marginRight: 8,
    overflow: 'hidden',
  },
  transportBtnActive: {
    borderColor: C.green,
    backgroundColor: `${C.green}1A`,
  },
  transportText: {
    color: C.textGray,
    fontSize: 9,
    lineHeight: 11,
    fontWeight: '700',
  },
  transportTextActive: {
    color: C.green,
  },
  transportHand: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: 'center',
  },
  transportHandDot: {
    width: 4,
    height: 4,
    borderRadius: 2,
    backgroundColor: C.green,
    marginTop: 1,
  },
  bpmTap: {
    alignItems: 'flex-start',
  },
  bpmText: {
    color: C.textWhite,
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  bpmHint: {
    color: C.textDim,
    fontSize: 8,
    fontWeight: '700',
    letterSpacing: 1,
  },
  batteryBlock: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  batteryPct: {
    color: C.textWhite,
    fontSize: 13,
    fontWeight: '700',
    marginRight: 7,
  },
  batteryCharge: {
    color: C.green,
    fontSize: 11,
    marginRight: 6,
  },
  batteryBody: {
    width: 36,
    height: 16,
    borderRadius: 4,
    borderWidth: 2,
    borderColor: C.textGray,
    padding: 2,
    justifyContent: 'center',
  },
  batteryFill: {
    height: '100%',
    borderRadius: 2,
    backgroundColor: C.green,
  },
  batteryNub: {
    width: 3,
    height: 7,
    borderRadius: 2,
    backgroundColor: C.textGray,
    marginLeft: 2,
  },

  /* ----- Layout ----- */
  controlsRow: {
    flexDirection: 'row',
    gap: 8,
  },
  effectsPanel: {
    flex: 1,
    backgroundColor: C.panel,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: C.panelEdge,
    padding: 10,
    overflow: 'hidden',
    alignItems: 'center',
    justifyContent: 'center',
  },

  sectionLabel: {
    color: C.textDim,
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 1,
    marginBottom: 6,
    textAlign: 'center',
  },

  /* ----- Physical knob face ----- */
  knobWell: {
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: C.inset,
    borderWidth: 1,
    borderColor: C.panelEdge,
  },
  knobFace: {
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: '#3b3b3b',
    shadowOffset: { width: 0, height: 3 },
    shadowRadius: 5,
    elevation: 6,
  },
  knobKnurlWrap: {
    ...StyleSheet.absoluteFillObject,
    alignItems: 'center',
  },
  knobKnurl: {
    borderRadius: 2,
    backgroundColor: 'rgba(255,255,255,0.10)',
  },
  knobGloss: {
    position: 'absolute',
    backgroundColor: 'rgba(255,255,255,0.07)',
  },
  knobPointerWrap: {
    ...StyleSheet.absoluteFillObject,
    alignItems: 'center',
    justifyContent: 'flex-start',
  },
  knobPointerLine: {
    borderRadius: 3,
    shadowOffset: { width: 0, height: 0 },
    shadowOpacity: 0.9,
    shadowRadius: 4,
    elevation: 4,
  },
  knobHub: {
    position: 'absolute',
    backgroundColor: '#2a2a2a',
    borderWidth: 1,
    borderColor: 'rgba(255,255,255,0.10)',
  },
  knobTickWrap: {
    ...StyleSheet.absoluteFillObject,
    alignItems: 'center',
  },
  knobTick: {
    width: 2,
    height: 4,
    borderRadius: 1,
    marginTop: 1,
  },
  trackFill: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 0,
  },
  capWrap: {
    position: 'absolute',
    left: -8,
    right: -8,
  },
  cap: {
    flex: 1,
    borderRadius: 9,
    backgroundColor: '#4a4a4a',
    borderWidth: 2,
    borderColor: '#ffffff',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.8,
    shadowRadius: 5,
    elevation: 5,
  },
  capInner: {
    position: 'absolute',
    left: 4,
    right: 4,
    top: 4,
    bottom: 4,
    borderRadius: 5,
    borderWidth: 1,
    borderColor: 'rgba(0,0,0,0.45)',
  },
  capLine: {
    position: 'absolute',
    left: 7,
    right: 7,
    top: '50%',
    height: 1,
    backgroundColor: 'rgba(255,255,255,0.28)',
  },

  /* ----- Faders ----- */
  effectsSubLabel: {
    color: C.textDim,
    fontSize: 8,
    fontWeight: '700',
    letterSpacing: 0.5,
    marginTop: -3,
    marginBottom: 4,
  },
  fadersRow: {
    flexDirection: 'row',
    justifyContent: 'center',
    gap: 6,
  },
  faderCol: {
    flex: 1,
    alignItems: 'center',
  },
  faderTrack: {
    width: 44,
    borderRadius: 14,
    backgroundColor: C.inset,
    borderWidth: 1,
    borderColor: C.panelEdge,
    overflow: 'hidden',
    position: 'relative',
  },
  pctBox: {
    marginTop: 6,
    paddingHorizontal: 4,
    paddingVertical: 2,
    borderRadius: 5,
    backgroundColor: C.inset,
    borderWidth: 1,
    borderColor: C.panelEdge,
  },
  pctText: {
    color: C.textGray,
    fontSize: 9,
    fontWeight: '700',
  },

  /* ----- Glossy fill highlight (faders / wheels / strip faders) ----- */
  trackGloss: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    height: 5,
    backgroundColor: 'rgba(255,255,255,0.32)',
  },

  /* ----- Mixer pan knob well ----- */
  knobOuter: {
    backgroundColor: C.inset,
    borderWidth: 1,
    borderColor: C.panelEdge,
    alignItems: 'center',
    justifyContent: 'center',
  },

  /* ----- Mixer open button (header) ----- */
  mixerOpenBtn: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 8,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
    marginLeft: 10,
  },
  mixerOpenBtnActive: {
    borderColor: `${C.blue}99`,
    backgroundColor: `${C.blue}22`,
  },
  mixerOpenText: {
    color: C.textGray,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.5,
  },
  mixerOpenTextActive: {
    color: C.blue,
  },

  /* ----- Landscape guard ----- */
  rotateOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: '#0a0a0a',
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 32,
    zIndex: 90,
    elevation: 90,
  },
  rotateIcon: {
    fontSize: 64,
    color: C.blue,
    marginBottom: 18,
  },
  rotateTitle: {
    color: C.textWhite,
    fontSize: 20,
    fontWeight: '800',
    letterSpacing: 4,
    textAlign: 'center',
  },
  rotateHint: {
    color: C.textDim,
    fontSize: 13,
    marginTop: 10,
    textAlign: 'center',
  },

  /* ----- Mixer sheet ----- */
  mixer: {
    position: 'absolute',
    left: 0,
    right: 0,
    backgroundColor: 'rgba(10,10,10,0.98)',
    borderColor: C.panelEdge,
    paddingHorizontal: 10,
    paddingVertical: 8,
    zIndex: 30,
    elevation: 30,
  },
  mixerInline: {
    flex: 1,
    /* Keeps at least one strip visible; the rest scrolls horizontally. */
    minWidth: 76,
    borderWidth: 1,
    borderRadius: 14,
    overflow: 'hidden',
    backgroundColor: 'rgba(10,10,10,0.98)',
    borderColor: C.panelEdge,
    paddingHorizontal: 10,
    paddingVertical: 8,
  },
  mixerMax: {
    top: 0,
    bottom: 0,
    borderWidth: 1,
    borderRadius: 16,
    overflow: 'hidden',
  },
  mixerHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 8,
  },
  mixerTitle: {
    color: C.textWhite,
    fontSize: 12,
    fontWeight: '800',
    letterSpacing: 2,
  },
  mixerSub: {
    color: C.textDim,
    fontSize: 9,
    marginLeft: 8,
    letterSpacing: 0.5,
    flexShrink: 1,
  },
  mixerSpacer: {
    flex: 1,
  },
  mixerBtn: {
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 7,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: `${C.blue}66`,
    marginRight: 6,
  },
  mixerBtnText: {
    color: C.blue,
    fontSize: 10,
    fontWeight: '800',
    letterSpacing: 1,
  },
  mixerClose: {
    width: 24,
    height: 24,
    borderRadius: 7,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
    alignItems: 'center',
    justifyContent: 'center',
  },
  mixerCloseText: {
    color: C.textGray,
    fontSize: 12,
    fontWeight: '800',
  },
  mixerStrips: {
    flex: 1,
  },
  mixerStripsContent: {
    /* At least one viewport wide so strips spread evenly when they fit, and
       wider (scrollable) when their min widths exceed the sheet. */
    minWidth: '100%',
    flexDirection: 'row',
    justifyContent: 'space-around',
    alignItems: 'flex-start',
    gap: 4,
  },
  mixerWaiting: {
    flex: 1,
    minWidth: 220,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 12,
    gap: 6,
  },
  mixerWaitingTitle: {
    color: '#2f9bff',
    fontSize: 13,
    fontWeight: '900',
    letterSpacing: 1,
  },
  mixerWaitingText: {
    color: C.textGray,
    fontSize: 11,
    textAlign: 'center',
  },
  stripCol: {
    flex: 1,
    minWidth: 96,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: 'transparent',
    borderRadius: 8,
    paddingHorizontal: 2,
  },
  stripNameBtn: {
    alignSelf: 'stretch',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: C.panelEdge,
    borderRadius: 5,
    paddingVertical: 0,
    marginBottom: 4,
  },
  stripName: {
    fontSize: 9,
    fontWeight: '800',
    letterSpacing: 1,
  },
  stripVoice: {
    fontSize: 7,
    fontWeight: '800',
    letterSpacing: 1,
    marginBottom: 2,
  },
  stripPan: {
    color: C.textDim,
    fontSize: 9,
    fontWeight: '700',
    marginVertical: 3,
  },
  stripTrack: {
    width: 44,
    borderRadius: 14,
    backgroundColor: C.inset,
    borderWidth: 1,
    overflow: 'hidden',
    position: 'relative',
    marginTop: 4,
  },
  stripCap: {
    borderRadius: 8,
  },
  stripPct: {
    marginTop: 5,
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 4,
    backgroundColor: C.inset,
    borderWidth: 1,
  },
  stripPctText: {
    fontSize: 9,
    fontWeight: '800',
  },

  /* ----- Separation line between faders / channel strips ----- */
  vSep: {
    width: 1,
    alignSelf: 'stretch',
    backgroundColor: C.panelEdge,
    marginHorizontal: 2,
  },

  /* ----- Main display (selected voice settings) ----- */
  display: {
    marginTop: 6,
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#0a0f0a',
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.panelEdge,
    paddingHorizontal: 8,
    gap: 8,
  },
  displayBadge: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 8,
    paddingVertical: 2,
    alignItems: 'center',
    minWidth: 96,
  },
  displayBadgeTitle: {
    fontSize: 7,
    fontWeight: '800',
    letterSpacing: 1,
  },
  displayBadgeName: {
    fontSize: 14,
    fontWeight: '800',
    letterSpacing: 1,
  },
  displayBadgeSub: {
    fontSize: 8,
    fontWeight: '800',
    letterSpacing: 2,
  },
  displayFields: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  displayField: {
    alignItems: 'center',
    minWidth: 56,
  },
  displayFieldLabel: {
    color: C.textDim,
    fontSize: 7,
    fontWeight: '700',
    letterSpacing: 1,
  },
  displayFieldValue: {
    color: C.textWhite,
    fontSize: 12,
    fontWeight: '800',
    letterSpacing: 0.5,
  },
  displayDivider: {
    width: 1,
    height: 22,
    backgroundColor: C.panelEdge,
  },
  displayMeter: {
    width: 8,
    height: '70%',
    backgroundColor: C.inset,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: C.panelEdge,
    justifyContent: 'flex-end',
    overflow: 'hidden',
  },
  displayMeterFill: {
    width: '100%',
    borderRadius: 3,
  },

  /* ----- Settings overlay ----- */
  settingsBtnActive: {
    borderColor: `${C.blue}99`,
    backgroundColor: `${C.blue}1a`,
  },
  settingsOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0,0,0,0.72)',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 40,
    elevation: 40,
  },
  settingsCard: {
    width: '88%',
    maxWidth: 560,
    height: '88%',
    maxHeight: 620,
    backgroundColor: '#141414',
    borderRadius: 16,
    borderWidth: 1,
    borderColor: C.panelEdge,
    padding: 14,
  },
  settingsScroll: {
    flex: 1,
  },
  settingsScrollContent: {
    paddingRight: 2,
  },
  settingsHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 8,
  },
  settingsTitle: {
    flex: 1,
    color: C.textWhite,
    fontSize: 13,
    fontWeight: '800',
    letterSpacing: 2,
  },
  settingsLabel: {
    color: C.textDim,
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 1,
    marginBottom: 4,
    marginTop: 6,
  },
  settingsInput: {
    backgroundColor: C.inset,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
    color: C.textWhite,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
  },
  settingsAutoHint: {
    color: C.textDim,
    fontSize: 10,
    lineHeight: 14,
    marginTop: 6,
    fontStyle: 'italic',
  },
  settingsPort: {
    color: C.textGray,
    fontSize: 12,
    fontWeight: '700',
  },
  settingsPortDetails: {
    color: C.textDim,
    fontSize: 10,
    lineHeight: 14,
    marginTop: 5,
  },
  settingsStatusRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    marginTop: 10,
  },
  settingsStatusText: {
    color: C.textGray,
    fontSize: 11,
    fontWeight: '700',
  },
  outputList: {
    gap: 6,
  },
  outputRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingVertical: 8,
    paddingHorizontal: 10,
    borderRadius: 8,
    backgroundColor: C.inset,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
  },
  outputRowActive: {
    borderColor: C.blue,
    backgroundColor: `${C.blue}14`,
  },
  outputDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: C.textDim,
  },
  outputDotActive: {
    borderColor: C.blue,
    backgroundColor: C.blue,
  },
  outputRowText: {
    color: C.textGray,
    fontSize: 12,
    fontWeight: '600',
    flexShrink: 1,
  },
  outputRowTextActive: {
    color: C.textWhite,
  },
  settingsActions: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    gap: 8,
    marginTop: 14,
  },
  settingsPrimary: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: `${C.blue}22`,
    borderWidth: 1,
    borderColor: C.blue,
  },
  settingsPrimaryText: {
    color: C.blue,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1,
  },
  settingsSecondary: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.surfaceEdge,
  },
  settingsSecondaryText: {
    color: C.textGray,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1,
  },
  settingsDanger: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: `${C.red}18`,
    borderWidth: 1,
    borderColor: C.red,
  },
  settingsDangerText: {
    color: C.red,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1,
  },
});
