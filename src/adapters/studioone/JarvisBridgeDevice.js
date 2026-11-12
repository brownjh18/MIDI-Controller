// JarvisBridgeDevice.js
// Control surface device for the Studio One <-> MIDI Bridge.
//
// Studio One only transmits a parameter value when that value changes, so a
// freshly connected surface is completely silent until the performer touches a
// control. Every built-in JS surface (FaderPort, Tranzport, ATOM) solves this
// by calling hostDevice.invalidateAll() once the MIDI output connects, which
// makes Studio One push the current value of every mapped parameter. This
// device does the same, and also honours a MIDI request so the bridge can ask
// for a fresh snapshot after it restarts on its own.
//
// This device has no Host.IO / Host.Objects access in the ControlSurfaceDevice
// sandbox, so the bridge handshake rides entirely over loopMIDI.
//
//   host -> bridge   (via mappings in JarvisBridgeMappings.surface.xml)
//     display$chLine0  String mapping -> channelName[i] textdisplay control;
//                       the ChannelNameHandler registered for that control
//                       re-broadcasts the text as MIDI ch8 CC 85 (target
//                       index), CC 86 (byte length) and one CC 88 per
//                       7-bit ASCII byte. The bridge accumulates the bytes
//                       and adopts the name.
//     selectValue$ch   Value mapping -> selectMirror[i] transmit button;
//                       Studio One itself emits CC 103+i (data2 0/127).
//                       No handler required, exactly like the mute/solo LEDs.
//     insertName$i     String mapping -> insertName[i] textdisplay control;
//                       insertName/sendName String mappings of the focus
//                       channel's slot names are streamed as CC 75/76/77
//                       (inserts) and CC 78/79/80 (sends) with one byte per
//                       CC. Handler-based (SlotNameHandler).
//
//   bridge -> host   (mapped controls, no JS involved)
//     CC 60+index on ch8 -> selectRequest[i] trigger control ->
//     <Toggle control="selectRequest[$ch]" param="selectValue$ch"
//            options="momentary"/>: the host selects that channel.
//
// The fader / pan / mute / solo / record-arm echo is unchanged: the wire
// contract matches the iOS app and the Ableton adapter (ch0-7 -> strips
// 0-7, ch9-15 -> strips 8-14, ch8 -> master).

include_file("resource://com.presonus.musicdevices/sdk/midiprotocol.js");
include_file("resource://com.presonus.musicdevices/sdk/controlsurfacedevice.js");

// Not bound to any control, so they never change a mixer parameter.
var kJarvisRefreshCC = 119;  // bridge -> device: re-send every mapped value
var kJarvisGoodbyeCC = 118;  // device -> bridge: surface is exiting

// ch8 = the "main section" channel; unused CCs carry the name/selection
// stream. Keep in sync with src/core/studioone.py and JarvisBridgeControls.
var kJarvisWireChannel = 0xB8;      // MIDI channel 8
var kJarvisNameTargetCC = 85;
var kJarvisNameLenCC = 86;
var kJarvisNameCharCC = 88;
// Focus-channel insert/send slot names ride these CCs on the same channel.
var kJarvisInsertSlotCC = 75;
var kJarvisInsertLenCC = 76;
var kJarvisInsertByteCC = 77;
var kJarvisSendSlotCC = 78;
var kJarvisSendLenCC = 79;
var kJarvisSendByteCC = 80;
var kJarvisSelectMirrorBaseCC = 103;  // reference only: plain Value mapping
// Focused-device parameter mirror. While a plugin is focused in Studio One
// the component binds the generic mapping's fader[i] strips to
// paramTitle$i / paramValue$i; these handlers stream each string as an
// index/len/byte burst on ch8, exactly like the slot-name stream.
var kJarvisParamTitleCC = 95;
var kJarvisParamTitleLenCC = 96;
var kJarvisParamTitleByteCC = 97;
var kJarvisParamValueCC = 98;
var kJarvisParamValueLenCC = 99;
var kJarvisParamValueByteCC = 100;
var kJarvisParamCount = 16;
var kJarvisMaxNameBytes = 40;
var kJarvisChannelCount = 15;

// Track name stream: the host pushes display$chLine0 as a text string
// whenever the channel is named/renamed; re-broadcast its bytes on ch8.
// Handlers are registered by control name in onInit, mirroring how the
// FaderPort registers its textCell handlers.
class ChannelNameHandler extends PreSonus.ControlHandler {
    constructor(name, index) {
        super();
        this.name = name;
        this.index = index;
        this._lastText = null;
    }
    sendValue(value, flags) {
        var text = String(value == null ? "" : value);
        if (text === this._lastText) return;
        this._lastText = text;
        var maxBytes = Math.min(text.length, kJarvisMaxNameBytes);
        this.sendMidi(kJarvisWireChannel, kJarvisNameTargetCC, this.index & 15);
        this.sendMidi(kJarvisWireChannel, kJarvisNameLenCC, Math.min(maxBytes, 127));
        for (var i = 0; i < maxBytes; i++) {
            var code = text.charCodeAt(i) & 0x7F;
            if (code < 32) code = 32;
            this.sendMidi(kJarvisWireChannel, kJarvisNameCharCC, code);
        }
    }
}

// Focus-channel insert/send slot names: same 7-bit byte burst format as
// the channel names but on dedicated CCs so the two streams never mix.
class SlotNameHandler extends PreSonus.ControlHandler {
    constructor(name, index, targetCC, lenCC, byteCC) {
        super();
        this.name = name;
        this.index = index;
        this.targetCC = targetCC;
        this.lenCC = lenCC;
        this.byteCC = byteCC;
        this._lastText = null;
    }
    sendValue(value, flags) {
        var text = String(value == null ? "" : value);
        if (text === this._lastText) return;
        this._lastText = text;
        var maxBytes = Math.min(text.length, kJarvisMaxNameBytes);
        this.sendMidi(kJarvisWireChannel, this.targetCC, this.index & 15);
        this.sendMidi(kJarvisWireChannel, this.lenCC, Math.min(maxBytes, 127));
        for (var i = 0; i < maxBytes; i++) {
            var code = text.charCodeAt(i) & 0x7F;
            if (code < 32) code = 32;
            this.sendMidi(kJarvisWireChannel, this.byteCC, code);
        }
    }
}

class JarvisBridgeDevice extends PreSonus.ControlSurfaceDevice {
    onInit(hostDevice) {
        super.onInit(hostDevice);
        this.debugLog = false;
        // One name handler per channelName control. Studio One routes the
        // String mapping of display$chLine0 into handler.sendValue(text).
        for (var i = 0; i < kJarvisChannelCount; i++) {
            this.addHandler(new ChannelNameHandler("channelName[" + i + "]", i));
        }
        // Focus-channel insert/send slot names (16 slot-name controls).
        for (var slot = 0; slot < 8; slot++) {
            this.addHandler(new SlotNameHandler(
                "insertName[" + slot + "]", slot,
                kJarvisInsertSlotCC, kJarvisInsertLenCC, kJarvisInsertByteCC));
            this.addHandler(new SlotNameHandler(
                "sendName[" + slot + "]", slot,
                kJarvisSendSlotCC, kJarvisSendLenCC, kJarvisSendByteCC));
        }
        // Focused-device parameter names + values (generic mapping page 0).
        for (var param = 0; param < kJarvisParamCount; param++) {
            this.addHandler(new SlotNameHandler(
                "paramTitle[" + param + "]", param,
                kJarvisParamTitleCC, kJarvisParamTitleLenCC, kJarvisParamTitleByteCC));
            this.addHandler(new SlotNameHandler(
                "paramValue[" + param + "]", param,
                kJarvisParamValueCC, kJarvisParamValueLenCC, kJarvisParamValueByteCC));
        }
    }
    createHandler(name, attributes) {
        // No <Handler class=...> controls are declared; everything else
        // rides plain Value/Toggle/String mappings. Fail closed.
        return false;
    }
    onMidiOutConnected(state) {
        super.onMidiOutConnected(state);
        if (state) {
            // Ask Studio One for a complete snapshot (faders, pans, mutes,
            // solos, record arms, transport, names, selection).
            this.hostDevice.invalidateAll();
        }
    }
    onMidiEvent(status, data1, data2) {
        // The bridge restarts independently of Studio One. When it does, it
        // sends this request so the host re-sends everything; otherwise only
        // whatever loopMIDI happened to replay would reach the bridge.
        if ((status & 0xF0) === 0xB0 && data1 === kJarvisRefreshCC && data2 > 0) {
            this.hostDevice.invalidateAll();
            return;
        }
        return super.onMidiEvent(status, data1, data2);
    }
    onExit() {
        // Tell the bridge the surface is gone so it stops mirroring a dead
        // host and the app can show its waiting screen again.
        try {
            this.sendMidi(0xB0, kJarvisGoodbyeCC, 0);
        } catch (error) {
        }
        super.onExit();
    }
}

function createJarvisBridgeDeviceInstance() {
    return new JarvisBridgeDevice();
}
