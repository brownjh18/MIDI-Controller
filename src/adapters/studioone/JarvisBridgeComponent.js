// JarvisBridgeComponent.js
// Binds the RemoteAllBank mixer channels to the surface's global
// display/selection params, exactly like the FaderPort components bind
// their text cells and select buttons.
//
// The device surface (JarvisBridgeMappings.surface.xml) maps:
//   textCell[i][0]  -> param "display"+i+"Line0"   (String mapping, Global)
//   selectMirror[i] -> param "selectValue"+i       (Value mapping, Global)
//   selectRequest[i]-> param "selectValue"+i       (Toggle mapping, Global)
//
// A surface mapping alone never makes the host push a parameter. The
// host only feeds those global device params when the COMPONENT creates
// same-named alias params and connects each alias to a channel property
// (kLabel / kSelect) through the channel's bank element. Without that
// binding Studio One knows those params do not exist and pushes nothing,
// which is why names and selection never appeared before.
//
// The focus (selected) channel is found the same way PreSonus' own
// FocusChannelComponent does it:
//     let focusBankElement = mixerMapping.find("FocusBankElement");
//     this.focusChannelElement = focusBankElement.getElement(0);
// The nested Inserts/Sends banks live on that STRIP element, not on the
// ScrollBank itself. Looking them up on the bank returned null, so the
// slot-name aliases were never bound and the host never streamed the
// selected channel's insert/send names. That was the popover bug.
//
// This component is deliberately defensive: any failure in one slot must
// not take down the whole surface, so every host call is guarded and a
// slot that cannot be bound is skipped silently.

include_file("resource://com.presonus.musicdevices/sdk/controlsurfacecomponent.js");

var kJarvisStripCount = 15;
var kJarvisFocusSlotCount = 8;
var kJarvisParamCount = 16;

class JarvisBridgeComponent extends PreSonus.ControlSurfaceComponent {
    onInit(hostComponent) {
        super.onInit(hostComponent);
        this.debugLog = false;
        this.channels = [];
        this.channelBankElement = null;
        this.focusChannelElement = null;
        this.focusInsertsBankElement = null;
        this.focusSendsBankElement = null;
        this.genericBankElement = null;
        this.insertNameAliases = [];
        this.sendNameAliases = [];
        this.paramTitleAliases = [];
        this.paramValueAliases = [];
        try {
            this.model = hostComponent.model;
            let root = this.model.root;
            let mixerMapping = root.find("MixerMapping");
            if (mixerMapping)
                this.channelBankElement = mixerMapping.find("ChannelBankElement");
            // Focus channel = element 0 of the FollowBank scroll bank. The
            // Inserts/Sends banks are children of that strip, exactly like
            // PreSonus' FocusChannelComponent / FocusChannelPanComponent.
            if (mixerMapping) {
                try {
                    let focusBankElement = mixerMapping.find("FocusBankElement");
                    if (focusBankElement)
                        this.focusChannelElement = focusBankElement.getElement(0);
                } catch (ignored) {
                    this.focusChannelElement = null;
                }
            }
            try {
                this.genericBankElement = root.getGenericMapping().getElement(0);
            } catch (ignored) {
                this.genericBankElement = null;
            }
            let paramList = hostComponent.paramList;
            for (let i = 0; i < kJarvisStripCount; i++) {
                this.channels.push({
                    labelAlias: paramList.addAlias("display" + i + "Line0"),
                    selectAlias: paramList.addAlias("selectValue" + i),
                });
            }
            if (this.focusChannelElement) {
                try {
                    this.focusInsertsBankElement = this.focusChannelElement.find("InsertsBankElement");
                    this.focusSendsBankElement = this.focusChannelElement.find("SendsBankElement");
                } catch (ignored) {
                    this.focusInsertsBankElement = null;
                    this.focusSendsBankElement = null;
                }
                for (let i = 0; i < kJarvisFocusSlotCount; i++) {
                    this.insertNameAliases.push(paramList.addAlias("insertName" + i));
                    this.sendNameAliases.push(paramList.addAlias("sendName" + i));
                }
            }
            for (let i = 0; i < kJarvisParamCount; i++) {
                this.paramTitleAliases.push(paramList.addAlias("paramTitle" + i));
                this.paramValueAliases.push(paramList.addAlias("paramValue" + i));
            }
            // App-tapped insert/send requests: the surface mapping uses
            // <Invoke onReceive="onReqInsert($i)"/> which calls the component
            // method directly on the MIDI event (FaderPort onFaderTouched /
            // onEncoderPushed pattern). No params needed.
            this.bindAllChannels();
            this.bindFocusSlots();
            this.bindGenericParams();
        } catch (error) {
            // Never take the surface down; the faders/transport path lives
            // in the device and keeps working without names or selection.
            this.channelBankElement = null;
        }
    }

    bindAllChannels() {
        for (let i = 0; i < kJarvisStripCount; i++)
            this.bindChannel(i);
    }

    bindChannel(index) {
        let channel = this.channels[index];
        if (!channel) return;
        try {
            if (!this.channelBankElement) {
                channel.labelAlias.setOriginal(null);
                channel.selectAlias.setOriginal(null);
                return;
            }
            let element = this.channelBankElement.getElement(index);
            if (!element) {
                channel.labelAlias.setOriginal(null);
                channel.selectAlias.setOriginal(null);
                return;
            }
            element.connectAliasParam(channel.labelAlias, PreSonus.ParamID.kLabel);
            element.connectAliasParam(channel.selectAlias, PreSonus.ParamID.kSelect);
        } catch (error) {
            // A slot beyond the DAW's channel count (or a transient
            // bank rebuild) is not fatal; leave it unbound.
            try { channel.labelAlias.setOriginal(null); } catch (ignored) {}
            try { channel.selectAlias.setOriginal(null); } catch (ignored) {}
        }
    }

    // The bank's onConnect="onConnectChannel($ch)" strips call this
    // whenever a channel slot (dis)connects. Rebinding everything is
    // idempotent and cheap (15 slots).
    onConnectChannel(channelIndex) {
        this.bindChannel(channelIndex);
    }

    // Focus channel (follows selection in the host): bind its insert and
    // send slot names to the surface's global slot-name params. The
    // popover in the app shows inserts/sends of the selected channel, so
    // only the focus channel needs its slots streamed.
    bindFocusSlots() {
        for (let i = 0; i < kJarvisFocusSlotCount; i++) {
            this.bindFocusSlot(
                this.focusInsertsBankElement,
                this.insertNameAliases[i],
                PreSonus.ParamID.kInsertName,
                i);
            this.bindFocusSlot(
                this.focusSendsBankElement,
                this.sendNameAliases[i],
                PreSonus.ParamID.kSendPort,
                i);
        }
    }

    bindFocusSlot(bankElement, alias, paramID, index) {
        if (!alias) return;
        try {
            if (!bankElement) {
                alias.setOriginal(null);
                return;
            }
            let slot = bankElement.getElement(index);
            if (!slot) {
                alias.setOriginal(null);
                return;
            }
            slot.connectAliasParam(alias, paramID);
        } catch (error) {
            try { alias.setOriginal(null); } catch (ignored) {}
        }
    }

    // The focus strip's onConnect="onConnectFocusChannel" calls this when
    // the followed (selected) channel changes or reconnects; the alias
    // bindings persist, but re-binding refreshes the pushes.
    onConnectFocusChannel() {
        this.bindFocusSlots();
    }

    // Focus channel insert/send slot names: called when the focus channel
    // (selected channel) changes or reconnects. Re-binding ensures fresh
    // pushes for the selected channel's slot names.
    onConnectFocusChannelInsert(index) {
        if (this.focusInsertsBankElement) {
            try {
                let slot = this.focusInsertsBankElement.getElement(index);
                if (slot) {
                    slot.connectAliasParam(this.insertNameAliases[index], PreSonus.ParamID.kInsertName);
                }
            } catch (error) {
                try { this.insertNameAliases[index].setOriginal(null); } catch (ignored) {}
            }
        }
    }

    onConnectFocusChannelSend(index) {
        if (this.focusSendsBankElement) {
            try {
                let slot = this.focusSendsBankElement.getElement(index);
                if (slot) {
                    slot.connectAliasParam(this.sendNameAliases[index], PreSonus.ParamID.kSendPort);
                }
            } catch (error) {
                try { this.sendNameAliases[index].setOriginal(null); } catch (ignored) {}
            }
        }
    }

    // ------------------------------------------------------------------
    // Focused-device parameters (generic mapping page 0).
    // ------------------------------------------------------------------

    // Bind one generic fader slot to its title/value aliases. Safe to call
    // repeatedly: connectAliasParam is idempotent.
    bindGenericSlot(index) {
        if (!this.genericBankElement || !this.paramTitleAliases[index])
            return;
        let fader = null;
        try {
            fader = this.genericBankElement.find("fader[" + index + "]");
        } catch (ignored) {
            fader = null;
        }
        if (!fader)
            return;
        // Force a fresh push: a different device can have a parameter with
        // the same title/value at this slot, and the handlers suppress
        // identical strings. Clearing first makes the host re-publish.
        try { this.paramTitleAliases[index].setOriginal(null); } catch (ignored) {}
        try { this.paramValueAliases[index].setOriginal(null); } catch (ignored) {}
        try {
            fader.connectAliasParam(this.paramTitleAliases[index], PreSonus.ParamID.kTitle);
        } catch (ignored) {}
        try {
            fader.connectAliasParam(this.paramValueAliases[index], PreSonus.ParamID.kValue);
        } catch (ignored) {}
    }

    bindGenericParams() {
        for (let i = 0; i < kJarvisParamCount; i++)
            this.bindGenericSlot(i);
    }

    // The generic mapping's strips call this when Studio One fills a plugin
    // parameter slot (i.e. a device just got focus).
    onConnectPlugControl(index) {
        this.bindGenericSlot(index);
    }

    onConnectPlugMapping() {
        this.bindGenericParams();
    }

    // The surface mapping's <Invoke onReceive="onReqInsert($i)"/> calls this
    // directly on the MIDI event, receiving (index, value) exactly like the
    // FaderPort's onFaderTouched(channelIndex, value). value is nonzero on
    // press; act on press only.
    onReqInsert(index, value) {
        if (value === 0 || value === false)
            return;
        this.openDeviceEditor(this.focusInsertsBankElement, index);
        // Alias bindings made before the fill are stale: Studio One swaps the
        // generic strip's kTitle/kValue params when the plugin gets ControlLink
        // focus, so re-bind now to hook the live params and push their
        // current values to the device.
        this.bindGenericParams();
    }

    onReqSend(index, value) {
        if (value === 0 || value === false)
            return;
        this.openDeviceEditor(this.focusSendsBankElement, index);
        this.bindGenericParams();
    }

    openDeviceEditor(bankElement, index) {
        if (!bankElement)
            return;
        try {
            let element = bankElement.getElement(index);
            if (!element || !element.isConnected())
                return;
            PreSonus.HostUtils.openEditorAndFocus(this, element);
        } catch (error) {
            // Host build without the GUI bridge: the names still stream,
            // only the parameter detail panel stays empty.
        }
    }
}

function createJarvisBridgeComponentInstance() {
    return new JarvisBridgeComponent();
}
