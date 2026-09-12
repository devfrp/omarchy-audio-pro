import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
    id: root
    moduleName: "devfrp.audio-patchbay"
    ipcTarget: moduleName
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    readonly property color foreground: bar ? bar.foreground : Color.foreground
    readonly property color dim: Qt.darker(foreground, 1.55)
    readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
    readonly property string helper: decodeURIComponent(Qt.resolvedUrl("patchbay.py").toString().replace(/^file:\/\//, ""))
    // Absolute, trusted interpreter path — never a bare "python3" resolved
    // through inherited PATH, which a shadow executable earlier in PATH
    // could hijack (github.com/omacom/omarchy-plugin-marketplace#6521).
    readonly property string python3: "/usr/bin/python3"
    // "-I" is Python's isolated mode: ignores PYTHONPATH/PYTHONSTARTUP/etc.
    // and drops the script directory + user site-packages from sys.path,
    // so nothing in the shell's inherited environment can influence
    // interpreter startup before patchbay.py's own _closed_env() takes
    // over for the tools it shells out to. Paired with clearEnvironment
    // below on both Process blocks.
    readonly property var pythonArgs: [python3, "-I"]
    // Only what the helper actually needs to reach the session (PipeWire
    // socket, D-Bus, config paths) — everything else inherited from this
    // shell process (PYTHONPATH, LD_PRELOAD, arbitrary PATH entries, ...)
    // is dropped via clearEnvironment on the Process itself.
    readonly property var closedEnv: ({
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "HOME": Quickshell.env("HOME"),
        "USER": Quickshell.env("USER"),
        "XDG_RUNTIME_DIR": Quickshell.env("XDG_RUNTIME_DIR"),
        "XDG_CONFIG_HOME": Quickshell.env("XDG_CONFIG_HOME"),
        "DBUS_SESSION_BUS_ADDRESS": Quickshell.env("DBUS_SESSION_BUS_ADDRESS"),
        "WAYLAND_DISPLAY": Quickshell.env("WAYLAND_DISPLAY")
    })

    readonly property var ratePresets: [
        { key: "auto", label: "Auto", rate: 0, buffer: 0 },
        { key: "music", label: "Music 44.1k", rate: 44100, buffer: 1024 },
        { key: "studio", label: "Studio 48k", rate: 48000, buffer: 256 },
        { key: "hires", label: "Hi-Res 96k", rate: 96000, buffer: 512 },
        { key: "ultra", label: "Ultra 192k", rate: 192000, buffer: 1024 },
        { key: "extreme", label: "Extreme 384k", rate: 384000, buffer: 2048 },
        { key: "max", label: "Max 768k", rate: 768000, buffer: 4096 }
    ]
    readonly property var bitPresets: [
        { key: "auto", label: "Auto", format: "" },
        { key: "16", label: "16-bit", format: "S16LE" },
        { key: "24", label: "24-bit", format: "S24LE" },
        { key: "32", label: "32-bit", format: "S32LE" }
    ]

    property var graph: ({ outputs: [], inputs: [], links: [] })
    property var info: ({ settings: {}, defaultSink: "", bitRule: "", hardware: [], capabilities: { rates: [], rateRanges: [], formats: [] } })

    function rateSupported(rate) {
        if (rate === 0) return true
        var caps = info.capabilities || {}
        var rates = caps.rates || []
        if (rates.indexOf(rate) >= 0) return true
        var ranges = caps.rateRanges || []
        for (var i = 0; i < ranges.length; i++)
            if (rate >= ranges[i][0] && rate <= ranges[i][1]) return true
        return false
    }

    function formatSupported(fmt) {
        if (!fmt) return true
        var formats = (info.capabilities || {}).formats || []
        return formats.indexOf(fmt) >= 0
    }

    readonly property var availableRatePresets: ratePresets.filter(function(p) { return root.rateSupported(p.rate) })
    readonly property var availableBitPresets: bitPresets.filter(function(p) { return root.formatSupported(p.format) })

    property string pendingKind: ""   // "rate" | "bitdepth" | ""
    property string pendingPreset: ""

    property string pendingOutputId: ""
    property string pendingOutputLabel: ""
    property string pendingInputId: ""
    property string pendingInputLabel: ""

    readonly property real rowHeight: Style.space(28)
    onGraphChanged: Qt.callLater(function() { if (cablesCanvas.available) cablesCanvas.requestPaint() })

    property string actionKind: ""
    property string message: ""
    property bool failed: false
    readonly property bool busy: action.running

    function settingValue(value) {
        var n = Number(value)
        return isFinite(n) ? n : 0
    }

    function currentRateKey() {
        var rate = settingValue(info.settings["clock.force-rate"])
        var buffer = settingValue(info.settings["clock.force-quantum"])
        for (var i = 0; i < ratePresets.length; i++) {
            var p = ratePresets[i]
            if (p.rate === rate && p.buffer === buffer) return p.key
        }
        return ""
    }

    function currentBitKey() {
        for (var i = 0; i < bitPresets.length; i++)
            if (bitPresets[i].format === (info.bitRule || "")) return bitPresets[i].key
        return ""
    }

    function refresh() { if (!status.running) status.running = true }

    function requestRate(preset) {
        if (busy || pendingKind) return
        pendingKind = "rate"; pendingPreset = preset
        message = ""; failed = false
    }

    function requestBitdepth(preset) {
        if (busy || pendingKind) return
        pendingKind = "bitdepth"; pendingPreset = preset
        message = ""; failed = false
    }

    function cancelPending() { pendingKind = ""; pendingPreset = "" }

    function confirmPending() {
        if (!pendingKind || busy) return
        var kind = pendingKind
        var preset = pendingPreset
        pendingKind = ""; pendingPreset = ""
        actionKind = kind
        action.command = pythonArgs.concat([helper, kind, preset])
        action.running = true
    }

    function selectOutput(port) {
        if (busy) return
        if (pendingInputId !== "") { doConnect(String(port.id), pendingInputId); return }
        pendingOutputId = (pendingOutputId === String(port.id)) ? "" : String(port.id)
        pendingOutputLabel = pendingOutputId ? port.label : ""
        cablesCanvas.requestPaint()
    }

    function selectInput(port) {
        if (busy) return
        if (pendingOutputId !== "") { doConnect(pendingOutputId, String(port.id)); return }
        pendingInputId = (pendingInputId === String(port.id)) ? "" : String(port.id)
        pendingInputLabel = pendingInputId ? port.label : ""
        cablesCanvas.requestPaint()
    }

    function clearRouteSelection() {
        pendingOutputId = ""; pendingOutputLabel = ""
        pendingInputId = ""; pendingInputLabel = ""
        cablesCanvas.requestPaint()
    }

    function doConnect(outId, inId) {
        actionKind = "connect"
        action.command = pythonArgs.concat([helper, "connect", outId, inId])
        action.running = true
        clearRouteSelection()
    }

    function linkTouchesOutput(id) {
        for (var i = 0; i < graph.links.length; i++)
            if (String(graph.links[i].outputPortId) === String(id)) return true
        return false
    }

    function linkTouchesInput(id) {
        for (var i = 0; i < graph.links.length; i++)
            if (String(graph.links[i].inputPortId) === String(id)) return true
        return false
    }

    function disconnectLink(linkId) {
        if (busy) return
        actionKind = "disconnect"
        action.command = pythonArgs.concat([helper, "disconnect", String(linkId)])
        action.running = true
    }

    onOpenedChanged: { if (opened) refresh(); else { pendingKind = ""; clearRouteSelection() } }

    Process {
        id: status
        command: root.pythonArgs.concat([root.helper, "json"])
        clearEnvironment: true
        environment: root.closedEnv
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    var snap = JSON.parse(text)
                    root.graph = snap.graph
                    root.info = snap.status
                } catch (e) { root.message = "Could not read audio status."; root.failed = true }
            }
        }
        stderr: StdioCollector { onStreamFinished: if (text.trim()) { root.message = text.trim(); root.failed = true } }
    }

    Process {
        id: action
        clearEnvironment: true
        environment: root.closedEnv
        stdout: StdioCollector { onStreamFinished: if (text.trim()) root.message = text.trim() }
        stderr: StdioCollector { onStreamFinished: if (text.trim()) { root.message = text.trim(); root.failed = true } }
        onExited: function(code) {
            root.failed = code !== 0
            if (code === 0 && root.message === "") {
                root.message = root.actionKind === "connect" ? "Connected."
                    : root.actionKind === "disconnect" ? "Disconnected."
                    : "Applied."
            }
            root.actionKind = ""
            root.refresh()
        }
    }

    Timer { interval: 5000; running: root.opened; repeat: true; onTriggered: root.refresh() }
    Component.onCompleted: refresh()

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "⇄"
        tooltipText: "Audio Patchbay"
        onPressed: function(b) { if (b === Qt.MiddleButton) root.refresh(); else root.toggle() }
    }

    KeyboardPanel {
        id: panel
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: content
        contentWidth: panel.fittedContentWidth(Style.space(420))
        contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(760))

        FocusScope {
            id: content
            anchors.fill: parent
            Keys.onEscapePressed: { if (root.pendingKind) root.cancelPending(); else root.close() }
            Flickable {
                anchors.fill: parent
                contentWidth: width
                contentHeight: column.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: column
                    width: parent.width
                    spacing: Style.space(14)

                    PanelHero {
                        width: parent.width
                        title: "Audio Patchbay"
                        meta: "Sample rate · bit depth · routage"
                        foreground: root.foreground
                        fontFamily: root.fontFamily
                        iconComponent: Component {
                            Text { textFormat: Text.PlainText; text: "⇄"; color: root.foreground; font.pixelSize: Style.font.display }
                        }
                        trailingControl: Component {
                            PanelActionButton { iconText: "󰑐"; tooltipText: "Refresh"; foreground: root.foreground; focusable: true; onClicked: root.refresh() }
                        }
                    }

                    PanelSeparator { width: parent.width; foreground: root.foreground }

                    // ---------------- Sample rate ----------------
                    Column {
                        width: parent.width; spacing: Style.space(6)
                        Label { text: "Sample rate (temporary)"; font.bold: true }
                        Label {
                            text: "Based on the default device: " + (root.info.defaultSink || "unknown")
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }
                        Flow {
                            width: parent.width; spacing: Style.space(4)
                            Repeater {
                                model: root.availableRatePresets
                                Button {
                                    required property var modelData
                                    text: modelData.label
                                    selected: root.currentRateKey() === modelData.key
                                    focusable: true
                                    enabled: !root.busy && !root.pendingKind
                                    foreground: root.foreground
                                    onClicked: root.requestRate(modelData.key)
                                }
                            }
                        }
                        Label {
                            visible: root.availableRatePresets.length <= 1
                            text: "No other rate supported by this device."
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }
                    }

                    PanelSeparator { width: parent.width; foreground: root.foreground }

                    // ---------------- Bit depth ----------------
                    Column {
                        width: parent.width; spacing: Style.space(6)
                        Label { text: "Bit depth (advanced — restarts audio)"; font.bold: true }
                        Label {
                            text: "Default output: " + (root.info.defaultSink || "unknown")
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }
                        Flow {
                            width: parent.width; spacing: Style.space(4)
                            Repeater {
                                model: root.availableBitPresets
                                Button {
                                    required property var modelData
                                    text: modelData.label
                                    selected: root.currentBitKey() === modelData.key
                                    focusable: true
                                    enabled: !root.busy && !root.pendingKind
                                    foreground: root.foreground
                                    onClicked: root.requestBitdepth(modelData.key)
                                }
                            }
                        }
                        Label {
                            visible: root.availableBitPresets.length <= 1
                            text: "No other bit depth supported by this device."
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }
                    }

                    // ---------------- Confirm card (rate / bitdepth) ----------------
                    Rectangle {
                        width: parent.width
                        visible: root.pendingKind !== ""
                        implicitHeight: confirmColumn.implicitHeight + Style.space(24)
                        color: Style.selectedFillFor(root.foreground, Color.accent)
                        radius: Style.cornerRadius
                        Column {
                            id: confirmColumn
                            x: Style.space(12); y: Style.space(12)
                            width: parent.width - Style.space(24); spacing: Style.space(10)
                            Label {
                                text: root.pendingKind === "rate"
                                    ? "Changing the sample rate affects all audio currently playing."
                                    : "Restarts the audio service (wireplumber). Brief sound interruption."
                                font.bold: true
                            }
                            Label { text: "Setting: " + root.pendingPreset; color: root.dim }
                            Row {
                                spacing: Style.space(8)
                                Button { text: root.busy ? "In progress…" : "Confirm"; selected: true; focusable: true; enabled: !root.busy; foreground: root.foreground; onClicked: root.confirmPending() }
                                Button { text: "Cancel"; focusable: true; enabled: !root.busy; foreground: root.foreground; onClicked: root.cancelPending() }
                            }
                        }
                    }

                    PanelSeparator { width: parent.width; foreground: root.foreground }

                    // ---------------- Virtual patchbay (visual graph) ----------------
                    Column {
                        width: parent.width; spacing: Style.space(6)
                        Label { text: "Virtual patchbay"; font.bold: true }
                        Label {
                            text: root.pendingOutputId !== "" ? "Click an input to connect « " + root.pendingOutputLabel + " »."
                                : root.pendingInputId !== "" ? "Click an output to connect « " + root.pendingInputLabel + " »."
                                : "Click an output jack then an input jack to create a cable."
                            color: root.dim; font.pixelSize: Style.font.bodySmall; wrapMode: Text.WordWrap
                        }

                        Row {
                            width: parent.width
                            Label { width: parent.width * 0.42; text: "SOURCES"; font.bold: true; font.pixelSize: Style.font.caption; horizontalAlignment: Text.AlignRight; color: root.dim }
                            Item { width: parent.width * 0.16; height: 1 }
                            Label { width: parent.width * 0.42; text: "DESTINATIONS"; font.bold: true; font.pixelSize: Style.font.caption; color: root.dim }
                        }

                        Item {
                            id: graphArea
                            width: parent.width
                            height: root.rowHeight * Math.max(root.graph.outputs.length, root.graph.inputs.length, 1)
                            visible: root.graph.outputs.length > 0 || root.graph.inputs.length > 0

                            readonly property real colWidth: width * 0.42

                            HoverHandler {
                                onPointChanged: {
                                    cablesCanvas.liveX = point.position.x
                                    cablesCanvas.liveY = point.position.y
                                    if (root.pendingOutputId !== "" || root.pendingInputId !== "") cablesCanvas.requestPaint()
                                }
                            }

                            Canvas {
                                id: cablesCanvas
                                anchors.fill: parent
                                antialiasing: true
                                property real liveX: 0
                                property real liveY: 0

                                function portIndex(list, id) {
                                    for (var i = 0; i < list.length; i++)
                                        if (String(list[i].id) === String(id)) return i
                                    return -1
                                }
                                function outPoint(i) { return { x: graphArea.colWidth, y: i * root.rowHeight + root.rowHeight / 2 } }
                                function inPoint(i) { return { x: width - graphArea.colWidth, y: i * root.rowHeight + root.rowHeight / 2 } }
                                function cable(ctx, p1, p2, color, width_) {
                                    ctx.strokeStyle = color
                                    ctx.lineWidth = width_
                                    ctx.beginPath()
                                    ctx.moveTo(p1.x, p1.y)
                                    var midX = (p1.x + p2.x) / 2
                                    ctx.bezierCurveTo(midX, p1.y, midX, p2.y, p2.x, p2.y)
                                    ctx.stroke()
                                }

                                onPaint: {
                                    var ctx = getContext("2d")
                                    ctx.reset()
                                    ctx.lineCap = "round"
                                    for (var i = 0; i < root.graph.links.length; i++) {
                                        var link = root.graph.links[i]
                                        var oi = portIndex(root.graph.outputs, link.outputPortId)
                                        var ii = portIndex(root.graph.inputs, link.inputPortId)
                                        if (oi >= 0 && ii >= 0) cable(ctx, outPoint(oi), inPoint(ii), Color.accent, 2)
                                    }
                                    if (root.pendingOutputId !== "") {
                                        var oi2 = portIndex(root.graph.outputs, root.pendingOutputId)
                                        if (oi2 >= 0) cable(ctx, outPoint(oi2), { x: liveX, y: liveY }, Qt.lighter(Color.accent, 1.4), 2)
                                    }
                                    if (root.pendingInputId !== "") {
                                        var ii2 = portIndex(root.graph.inputs, root.pendingInputId)
                                        if (ii2 >= 0) cable(ctx, { x: liveX, y: liveY }, inPoint(ii2), Qt.lighter(Color.accent, 1.4), 2)
                                    }
                                }
                            }

                            Item {
                                id: outCol
                                width: graphArea.colWidth
                                height: parent.height

                                Repeater {
                                    model: root.graph.outputs
                                    Item {
                                        id: outRow
                                        required property var modelData
                                        required property int index
                                        width: outCol.width
                                        height: root.rowHeight
                                        y: index * root.rowHeight

                                        readonly property bool isPending: root.pendingOutputId === String(modelData.id)
                                        readonly property bool isConnected: root.linkTouchesOutput(modelData.id)

                                        Text {
                                            textFormat: Text.PlainText
                                            text: outRow.modelData.label
                                            color: root.foreground
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.bodySmall
                                            font.bold: outRow.isPending
                                            elide: Text.ElideLeft
                                            horizontalAlignment: Text.AlignRight
                                            anchors.verticalCenter: parent.verticalCenter
                                            anchors.left: parent.left
                                            anchors.right: jack.left
                                            anchors.rightMargin: Style.space(6)
                                        }

                                        Rectangle {
                                            id: jack
                                            width: Style.space(10); height: width; radius: width / 2
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: (outRow.isPending || outRow.isConnected) ? Color.accent : "transparent"
                                            border.width: 1.5
                                            border.color: outRow.isPending ? Color.accent : root.foreground
                                        }

                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: root.selectOutput(outRow.modelData)
                                        }
                                    }
                                }
                            }

                            Item {
                                id: inCol
                                width: graphArea.colWidth
                                height: parent.height
                                anchors.right: parent.right

                                Repeater {
                                    model: root.graph.inputs
                                    Item {
                                        id: inRow
                                        required property var modelData
                                        required property int index
                                        width: inCol.width
                                        height: root.rowHeight
                                        y: index * root.rowHeight

                                        readonly property bool isPending: root.pendingInputId === String(modelData.id)
                                        readonly property bool isConnected: root.linkTouchesInput(modelData.id)

                                        Rectangle {
                                            id: inJack
                                            width: Style.space(10); height: width; radius: width / 2
                                            anchors.left: parent.left
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: (inRow.isPending || inRow.isConnected) ? Color.accent : "transparent"
                                            border.width: 1.5
                                            border.color: inRow.isPending ? Color.accent : root.foreground
                                        }

                                        Text {
                                            textFormat: Text.PlainText
                                            text: inRow.modelData.label
                                            color: root.foreground
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.bodySmall
                                            font.bold: inRow.isPending
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                            anchors.left: inJack.right
                                            anchors.leftMargin: Style.space(6)
                                            anchors.right: parent.right
                                        }

                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: root.selectInput(inRow.modelData)
                                        }
                                    }
                                }
                            }
                        }

                        Label {
                            visible: root.graph.outputs.length === 0 && root.graph.inputs.length === 0
                            text: "No active audio ports."
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }

                        Button {
                            visible: root.pendingOutputId !== "" || root.pendingInputId !== ""
                            text: "Clear selection"
                            focusable: true
                            foreground: root.foreground
                            onClicked: root.clearRouteSelection()
                        }
                    }

                    PanelSeparator { width: parent.width; foreground: root.foreground }

                    // ---------------- Active connections ----------------
                    Column {
                        width: parent.width; spacing: Style.space(6)
                        Label { text: "Active connections"; font.bold: true }
                        Label {
                            visible: root.graph.links.length === 0
                            text: "No patchbay connections yet."
                            color: root.dim; font.pixelSize: Style.font.bodySmall
                        }
                        Repeater {
                            model: root.graph.links
                            Row {
                                required property var modelData
                                width: parent.width
                                spacing: Style.space(8)
                                Label {
                                    width: parent.width - disconnectButton.width - Style.space(8)
                                    text: modelData.outputLabel + "  →  " + modelData.inputLabel
                                    font.pixelSize: Style.font.bodySmall
                                    elide: Text.ElideMiddle
                                }
                                Button {
                                    id: disconnectButton
                                    text: "×"
                                    focusable: true
                                    enabled: !root.busy
                                    foreground: root.foreground
                                    onClicked: root.disconnectLink(modelData.id)
                                }
                            }
                        }
                    }

                    Label { visible: root.message !== ""; text: root.message; color: root.failed ? Color.urgent : Color.accent; font.pixelSize: Style.font.bodySmall }
                }
            }
        }
    }

    component Label: Text {
        width: parent.width
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        wrapMode: Text.WordWrap
    }
}
