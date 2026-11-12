// Jarvis Bridge FrameworkService for Studio One
// This script runs as a background FrameworkService when Studio One starts.
// It polls commands.json and writes state.json, then exposes itself via Host.Objects.

include_file("resource://com.presonus.musicdevices/sdk/midiprotocol.js");
include_file("resource://com.presonus.musicdevices/sdk/controlsurfacecomponent.js");
include_file("resource://com.presonus.musicdevices/sdk/controlsurfacedevice.js");

function JarvisBridgeService() {
    this._hostComponent = null;
    this._connected = false;
    this._initialized = false;
    this._lastCommandTime = 0;
    this._lastStateTime = 0;
    this._seenSequences = {};
    this._commandsPath = "local://$TEMP/AbletonBridgeMixer/commands.json";
    this._statePath = "local://$TEMP/AbletonBridgeMixer/state.json";
}

JarvisBridgeService.prototype.initialize = function (context) {
    if (this._initialized) return;
    this._initialized = true;
    this._connected = true;
    this._running = true;
    try {
        Host.Console.writeLine("JarvisBridge: initialize - FrameworkService started");
    } catch (e) {
        // Ignore initialization errors
    }
    return Host.Results.kResultOk;
};

JarvisBridgeService.prototype.terminate = function () {
    this._running = false;
    try {
        Host.Console.writeLine("JarvisBridge: terminate - FrameworkService stopped");
    } catch (e) {
        // Ignore termination errors
    }
    return Host.Results.kResultOk;
};

// Poll commands.json at regular intervals
JarvisBridgeService.prototype.pollCommands = function (now) {
    if (!this._running) return false;
    // Host.onIdle(time) reports milliseconds, not seconds.
    if (now - this._lastCommandTime >= 150) { // 150ms interval
        this._lastCommandTime = now;
        var cmdText = this._readCommands();
        if (cmdText && cmdText !== this._lastCommandText) {
            this._lastCommandText = cmdText;
            var commands = this._parseJson(cmdText);
            if (commands) {
                this._applyCommands(commands);
            }
        }
    }
    return true;
};

// Read commands from file
JarvisBridgeService.prototype._readCommands = function () {
    try {
        var file = Host.IO.openTextFile(Host.Url(this._commandsPath));
        if (!file) return null;
        var lines = [];
        while (!file.endOfStream) {
            lines.push(file.readLine());
        }
        file.close();
        if (lines.length === 0) return null;
        return lines.join("\n");
    } catch (e) {
        return null;
    }
};

// Apply commands to Studio One
JarvisBridgeService.prototype._applyCommands = function (commands) {
    if (!commands || typeof commands !== "object") return;
    for (var key in commands) {
        if (!commands.hasOwnProperty(key)) continue;
        var target = commands[key];
        if (!target || typeof target !== "object") continue;
        var seq = target.seq;
        if (seq !== undefined && seq !== null) {
            var last = this._seenSequences[key];
            if (last !== undefined && seq <= last) continue;
            this._seenSequences[key] = seq;
        }
        // Transport commands
        if (key === "transport") {
            if (target.value === "start") {
                Host.GUI.Commands.interpretCommand("Transport", "Start");
            } else if (target.value === "stop") {
                Host.GUI.Commands.interpretCommand("Transport", "Stop");
            }
        } else if (key === "record") {
            var recording = false;
            try {
                var tp = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/TransportPanel");
                if (tp) {
                    var recParam = tp.findParameter("record");
                    if (recParam) recording = recParam.value !== 0;
                }
            } catch (e) {}
            if (target.value !== recording) {
                Host.GUI.Commands.interpretCommand("Transport", "Record");
            }
        } else if (key === "metronome") {
            var enabled = false;
            try {
                var tp = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/TransportPanel");
                if (tp) {
                    var clickParam = tp.findParameter("click");
                    if (clickParam) enabled = clickParam.value !== 0;
                }
            } catch (e) {}
            if (target.value !== enabled) {
                Host.GUI.Commands.interpretCommand("Transport", "Click");
            }
        } else if (key === "tempo") {
            var bpm = Number(target.value) || 0;
            if (bpm > 0) {
                try {
                    var tp = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/TransportPanel");
                    if (tp) {
                        var tempoParam = tp.findParameter("tempo");
                        if (tempoParam) tempoParam.setValue(bpm);
                    }
                } catch (e) {}
            }
        } else if (key === "select_track") {
            var trackIdx = Number(target.value) || -1;
            if (trackIdx >= 0) {
                try {
                    var mtl = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/MainTrackList");
                    if (mtl) {
                        var track = mtl.getTrack(trackIdx);
                        if (track) {
                            if (track.select) track.select();
                        }
                    }
                } catch (e) {}
            }
        } else if (key.indexOf("track:") === 0) {
            var trackIdx = Number(key.substring(6)) || -1;
            if (trackIdx >= 0) {
                this._applyTrackFields(trackIdx, target);
            }
        } else if (key === "master") {
            this._applyTrackFields("master", target);
        } else if (key.indexOf("return:") === 0) {
            var returnIdx = Number(key.substring(7)) || -1;
            if (returnIdx >= 0) {
                this._applyReturnFields(returnIdx, target);
            }
        }
    }
};

// Apply track fields (volume, pan, mute, solo, arm, FX)
JarvisBridgeService.prototype._applyTrackFields = function (trackRef, target) {
    try {
        var mc = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/MixerConsole");
        if (!mc) return;
        var channel = null;
        if (trackRef === "master") {
            var masterList = mc.getChannelList(3);
            if (masterList && masterList.numChannels > 0) {
                channel = masterList.getChannel(0);
            }
        } else {
            var list = mc.getChannelList(1);
            if (list && trackRef < list.numChannels) {
                channel = list.getChannel(trackRef);
            }
        }
        if (!channel) return;
        // Volume
        if (target.volume && target.volume.value !== undefined) {
            channel.volume = target.volume.value;
        }
        // Pan
        if (target.pan && target.pan.value !== undefined) {
            channel.pan = target.pan.value;
        }
        // Mute
        if (target.mute && target.mute.value !== undefined) {
            channel.mute = target.mute.value ? 1 : 0;
        }
        // Solo
        if (target.solo && target.solo.value !== undefined) {
            channel.solo = target.solo.value ? 1 : 0;
        }
        // Arm
        if (target.arm && target.arm.value !== undefined) {
            try {
                var chRecordUnit = channel.recordUnit;
                if (chRecordUnit) chRecordUnit.recordArmed = target.arm.value ? 1 : 0;
            } catch (e) {}
        }
        // FX parameters
        for (var field in target) {
            if (field.indexOf("device:") === 0 && field.indexOf(":param:") > 0) {
                var parts = field.split(":");
                if (parts.length >= 4) {
                    var deviceIdx = Number(parts[1]) || 0;
                    var paramIdx = Number(parts[3]) || 0;
                    this._setFxParam(channel, deviceIdx, paramIdx, target[field].value);
                }
            }
        }
    } catch (e) {
        // Ignore errors
    }
};

// Apply return bus fields
JarvisBridgeService.prototype._applyReturnFields = function (returnIdx, target) {
    try {
        var mc = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/MixerConsole");
        if (!mc) return;
        var list = mc.getChannelList(2); // sub-output busses
        if (!list || returnIdx >= list.numChannels) return;
        var channel = list.getChannel(returnIdx);
        if (!channel) return;
        if (target.volume && target.volume.value !== undefined) {
            channel.volume = target.volume.value;
        }
        if (target.pan && target.pan.value !== undefined) {
            channel.pan = target.pan.value;
        }
        if (target.mute && target.mute.value !== undefined) {
            channel.mute = target.mute.value ? 1 : 0;
        }
        if (target.solo && target.solo.value !== undefined) {
            channel.solo = target.solo.value ? 1 : 0;
        }
    } catch (e) {
        // Ignore errors
    }
};

// Set FX parameter (best effort)
JarvisBridgeService.prototype._setFxParam = function (channel, fxIndex, paramIndex, value) {
    try {
        // Studio One FX parameters are accessed via channel.inserts
        // For now, best-effort via transport commands or parameter setting
    } catch (e) {
        // Ignore errors
    }
};

// Build state snapshot
JarvisBridgeService.prototype._buildState = function () {
    var state = {
        type: "mixer_state",
        daw: "studioone",
        script_build: "studioone-1.0",
        tracks: [],
        returns: [],
        master: { volume: 0.0, db: "" },
        meters: [],
        master_meter: [0.0, 0.0],
        metronome: false,
        playing: false,
        record_mode: false,
        tempo: 0.0,
        selected: -1,
    };
    try {
        var mc = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/MixerConsole");
        if (!mc) return state;
        // Normal channels (type 1)
        var list = mc.getChannelList(1);
        var trackCount = list ? list.numChannels : 0;
        var used = Math.min(trackCount, 16);
        for (var i = 0; i < used; i++) {
            var ch = list.getChannel(i);
            if (!ch) continue;
            var name = ch.title || ch.name || ("Track " + (i + 1));
            var vol = (ch.volume !== undefined) ? ch.volume : 1.0;
            var pan = (ch.pan !== undefined) ? ch.pan : 0.5;
            state.tracks.push({
                name: name,
                volume: this._volumeToNorm(vol),
                pan: Math.max(-1, Math.min(1, pan)),
                db: this._dbText(vol),
                muted: ch.mute === 1 || ch.mute === true,
                solo: ch.solo === 1 || ch.solo === true,
                armed: false,
                devices: [],
                slots: [],
            });
            state.meters.push([this._getPeak(ch, 0), this._getPeak(ch, 1)]);
        }
        // Returns (type 2 - sub-output busses)
        var retList = mc.getChannelList(2);
        if (retList) {
            for (var r = 0; r < Math.min(retList.numChannels, 2); r++) {
                var rch = retList.getChannel(r);
                if (!rch) continue;
                var rvol = (rch.volume !== undefined) ? rch.volume : 1.0;
                state.returns.push({
                    volume: this._volumeToNorm(rvol),
                    db: this._dbText(rvol),
                });
            }
        }
        // Master (type 3)
        var masterList = mc.getChannelList(3);
        if (masterList && masterList.numChannels > 0) {
            var mch = masterList.getChannel(0);
            if (mch) {
                var mvol = (mch.volume !== undefined) ? mch.volume : 1.0;
                state.master = {
                    volume: this._volumeToNorm(mvol),
                    db: this._dbText(mvol),
                };
                state.master_meter = [
                    this._getPeak(mch, 0),
                    this._getPeak(mch, 1),
                ];
            }
        }
        // Transport panel
        var tp = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/TransportPanel");
        if (tp) {
            var tempoParam = tp.findParameter("tempo");
            if (tempoParam) state.tempo = tempoParam.value || 0;
            var startParam = tp.findParameter("start");
            if (startParam) state.playing = startParam.value !== 0;
            var recordParam = tp.findParameter("record");
            if (recordParam) state.record_mode = recordParam.value !== 0;
            var clickParam = tp.findParameter("click");
            if (clickParam) state.metronome = clickParam.value !== 0;
        }
        // Selected track
        var mtl = Host.Objects.getObjectByUrl("://hostapp/DocumentManager/ActiveDocument/Environment/MainTrackList");
        if (mtl) {
            var selCount = mtl.numSelectedTracks ? mtl.numSelectedTracks : 0;
            if (selCount > 0) {
                var selTrack = mtl.getSelectedTrack ? mtl.getSelectedTrack(0) : null;
                if (selTrack) {
                    for (var s = 0; s < used; s++) {
                        var t = list.getChannel(s);
                        if (t === selTrack) {
                            state.selected = s;
                            break;
                        }
                    }
                }
            }
        }
    } catch (e) {
        try {
            Host.Console.writeLine("JarvisBridge buildState error: " + e);
        } catch (e2) {}
    }
    return state;
};

// JSON helpers
JarvisBridgeService.prototype._parseJson = function (text) {
    try {
        return JSON.parse(text);
    } catch (e) {
        return null;
    }
};

JarvisBridgeService.prototype._stringifyJson = function (obj) {
    try {
        return JSON.stringify(obj);
    } catch (e) {
        return null;
    }
};

// Volume conversion
JarvisBridgeService.prototype._volumeToNorm = function (vol) {
    if (vol <= 0) return 0;
    var db = 20 * Math.log10(vol);
    return Math.max(0, Math.min(1, (db + 60) / 60));
};

// DB text conversion
JarvisBridgeService.prototype._dbText = function (vol) {
    if (vol <= 0) return "-inf dB";
    return (20 * Math.log10(vol)).toFixed(1) + " dB";
};

// Main onIdle loop
JarvisBridgeService.prototype.onIdle = function (time) {
    if (!this._running) return;
    // Host.onIdle(time) reports milliseconds, not seconds.
    this._idleTicks = (this._idleTicks || 0) + 1;
    // Poll commands
    this.pollCommands(time);
    // Write state
    if (time - this._lastStateTime >= 150) {
        this._lastStateTime = time;
        var state = this._buildState();
        var stateText = this._stringifyJson(state);
        if (stateText && stateText !== this._lastStateText) {
            this._lastStateText = stateText;
            this._writeState(stateText);
        }
    }
};

// File write helper
JarvisBridgeService.prototype._writeState = function (text) {
    try {
        var file = Host.IO.createTextFile(Host.Url(this._statePath));
        if (!file) return false;
        file.writeLine(text);
        file.close();
        return true;
    } catch (e) {
        return false;
    }
};

// Factory function for service instance
function createJarvisBridgeService() {
    return new JarvisBridgeService();
}