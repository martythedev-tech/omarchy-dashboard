import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Bar chip + popup for martythedev-tech.dashboard, following nixfred.cpu-pulse's
// shape: one file is both the bar-widget entry point and its own popup, using
// Panel as the base for open/close/settings rather than a separate BarWidget.qml.
Panel {
    id: root
    moduleName: "martythedev-tech.dashboard"
    ipcTarget: "martythedev-tech.dashboard"
    manageIpc: false
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    readonly property string stateDir: (Quickshell.env('XDG_STATE_HOME') || Quickshell.env('HOME') + '/.local/state') + '/omarchy/plugins/martythedev-tech.dashboard'
    readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl('dashboard.py')).replace(/^file:\/\//, ''))
    property var status: ({})
    readonly property var items: status.items || []
    readonly property var grouped: Model.groupByKind(items)
    readonly property int updatable: Model.badgeCount(items)
    property string actionStatus: ""
    property string busyId: ""
    property string lastActionKind: ""
    property real actionStartSec: 0
    property var updateQueue: []
    property int updateQueueTotal: 0
    property int updateQueueDone: 0
    // One expandable panel per row, used for either a pre-update diff preview
    // or the actual command output after an action -- never both at once, so
    // there's exactly one place to look for "did anything happen."
    property string detailOpenId: ""
    property string detailMode: ""
    property string detailText: ""
    property bool detailOk: true
    property bool addAppFormOpen: false
    property bool disabledOpen: false
    property var addAppFields: ({name: "", id: "", repoDir: "", pkgName: "", branch: "", remote: "", updateCmd: "./rebuild.sh --install"})
    property real now: Date.now() / 1000
    property real elapsedNow: Date.now() / 1000

    // Every color in this file reads through here rather than a fixed hex
    // palette, so the panel actually follows the user's chosen Omarchy theme
    // (colors.toml/shell.toml) instead of a hardcoded dark-slate look that
    // clashes with a light or differently-accented theme -- the same
    // approach nixfred.cpu-pulse (the plugin whose shape this file follows)
    // already uses throughout.
    readonly property color ink: Color.popups.text
    readonly property var health: Model.healthFraction(root.items)

    function actionVerb(kind) {
        switch (kind) {
            case "update": return "Updating "
            case "enable": return "Enabling "
            case "disable": return "Disabling "
            case "remove": return "Removing "
            case "remove-app": return "Removing "
            default: return "Working on "
        }
    }

    function runCheck(force) {
        if (checkProc.running) return
        checkProc.command = force ? ["python3", helper, "check", "--force"] : ["python3", helper, "check"]
        actionStatus = "Checking sources…"
        checkProc.running = true
    }

    function runAction(kind, id) {
        if (root.busyId !== "") return
        root.busyId = id
        root.lastActionKind = kind
        root.actionStartSec = Date.now() / 1000
        actionStatus = actionVerb(kind) + id + "…"
        if (kind === "update") {
            root.detailOpenId = id
            root.detailMode = "output"
            root.detailOk = true
            root.detailText = ""
        }
        actionProc.command = ["python3", helper, kind, id]
        actionProc.running = true
    }

    function runUpdateAll() {
        if (root.busyId !== "" || root.updateQueue.length > 0) return
        var ids = []
        for (var i = 0; i < root.items.length; i++) if (Model.canUpdate(root.items[i])) ids.push(root.items[i].id)
        if (ids.length === 0) return
        root.updateQueue = ids
        root.updateQueueTotal = ids.length
        root.updateQueueDone = 0
        advanceUpdateQueue()
    }

    function advanceUpdateQueue() {
        if (root.updateQueue.length === 0) { root.updateQueueTotal = 0; root.updateQueueDone = 0; return }
        var next = root.updateQueue[0]
        root.updateQueue = root.updateQueue.slice(1)
        root.updateQueueDone += 1
        runAction("update", next)
    }

    function showDiff(id) {
        if (root.detailOpenId === id && root.detailMode === "diff") { root.detailOpenId = ""; return }
        root.detailOpenId = id
        root.detailMode = "diff"
        root.detailText = "Loading…"
        diffProc.command = ["python3", root.helper, "diff", id]
        diffProc.running = true
    }

    function closeDetail() { root.detailOpenId = "" }

    function stripeColor(item) {
        var k = Model.stripeKey(item)
        if (k === "behind") return Color.accent
        if (k === "attention") return Color.urgent
        if (k === "off") return Util.alpha(root.ink, 0.28)
        return Util.alpha(root.ink, 0.12)
    }

    function pillAccent(item) {
        var k = Model.stripeKey(item)
        if (k === "behind") return Color.accent
        if (k === "attention") return Color.urgent
        return Util.alpha(root.ink, 0.45)
    }

    function submitAddApp() {
        if (root.busyId !== "") return
        var f = root.addAppFields
        if (!f.name || !f.id || !f.repoDir) { root.actionStatus = "Name, id, and repo dir are required."; return }
        root.busyId = "__add_app__"
        root.lastActionKind = "add-app"
        root.actionStatus = "Adding " + f.name + "…"
        actionProc.command = ["python3", root.helper, "add-app", JSON.stringify(f)]
        actionProc.running = true
    }

    onOpenedChanged: if (opened) {
        statusFile.reload()
        if (!Model.statusFresh(root.status.ts, Date.now() / 1000, 300)) root.runCheck()
    }

    FileView {
        id: statusFile
        path: root.stateDir + "/status.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.status = JSON.parse(text()) } catch (e) {} }
    }

    FileView {
        id: actionLog
        path: root.stateDir + "/action.log"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            if (root.lastActionKind === "update" && root.busyId !== "") {
                root.detailOpenId = root.busyId
                root.detailMode = "output"
                root.detailText = text()
            }
        }
    }

    Timer { interval: 1000; running: root.opened; repeat: true; onTriggered: root.now = Date.now() / 1000 }
    // Ticks the busy row's "…Ns" elapsed counter so a long update (Flea's
    // cargo build, say) visibly keeps moving instead of sitting on static text.
    Timer { interval: 500; running: root.busyId !== ""; repeat: true; onTriggered: root.elapsedNow = Date.now() / 1000 }

    Process {
        id: checkProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try { root.status = JSON.parse(text); root.actionStatus = "" } catch (e) { root.actionStatus = "Check failed to parse." }
            }
        }
        onExited: function(code) { if (code !== 0 && root.actionStatus.indexOf("Checking") === 0) root.actionStatus = "Check failed -- see journalctl --user -u dashboard-check.service" }
    }

    Process {
        id: actionProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var kind = root.lastActionKind
                var targetId = root.busyId
                try {
                    var r = JSON.parse(text)
                    root.actionStatus = r.ok ? "Done." : "Failed."
                    if (kind === "add-app") {
                        if (r.ok) root.addAppFormOpen = false
                    } else if (r.ok && (kind === "remove" || kind === "remove-app")) {
                        // The row is about to disappear from the list -- nothing left to show a panel on.
                        if (root.detailOpenId === targetId) root.detailOpenId = ""
                    } else {
                        // This is the actual answer to "did anything happen": the
                        // command's real output, shown until closed or overwritten
                        // by the next action -- not just a terse Done./Failed.
                        root.detailOpenId = targetId
                        root.detailMode = "output"
                        root.detailOk = !!r.ok
                        root.detailText = r.message || (r.ok ? "(no output)" : "(no error message)")
                    }
                } catch (e) {
                    root.actionStatus = "Action did not report a result."
                }
                root.busyId = ""
                root.lastActionKind = ""
                statusFile.reload()
                if (root.updateQueue.length > 0) Qt.callLater(root.advanceUpdateQueue)
                else { root.updateQueueTotal = 0; root.updateQueueDone = 0 }
            }
        }
        onExited: function(code) {
            if (code !== 0 && root.busyId !== "") {
                root.busyId = ""
                root.lastActionKind = ""
                if (!root.actionStatus) root.actionStatus = "Action failed."
                if (root.updateQueue.length > 0) Qt.callLater(root.advanceUpdateQueue)
                else { root.updateQueueTotal = 0; root.updateQueueDone = 0 }
            }
        }
    }

    Process {
        id: diffProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var r = JSON.parse(text)
                    root.detailOk = !!r.ok
                    root.detailText = r.ok ? (r.diff || "(no textual diff)") : ("Could not load diff: " + (r.message || ""))
                } catch (e) {
                    root.detailText = "Could not load diff."
                }
            }
        }
    }

    component Label: Text {
        color: Util.alpha(root.ink, 0.62); font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; textFormat: Text.PlainText
    }
    component Heading: Text {
        color: root.ink; font.family: Style.font.family; font.pixelSize: Style.font.title; font.bold: true; textFormat: Text.PlainText
    }
    component SmallButton: Rectangle {
        id: btn
        property string text: ""
        property bool enabled: true
        property color accent: Color.accent
        signal clicked()
        implicitWidth: caption.implicitWidth + 20
        implicitHeight: Style.spacing.controlHeight - 2
        radius: 7
        color: !btn.enabled ? Style.normalFill : area.containsMouse ? Util.alpha(accent, 0.28) : Util.alpha(accent, 0.16)
        border.color: !btn.enabled ? Style.normalBorderColor : accent
        opacity: btn.enabled ? 1 : 0.5
        Text {
            id: caption; anchors.centerIn: parent; text: btn.text; color: root.ink
            font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true; textFormat: Text.PlainText
        }
        MouseArea {
            id: area; anchors.fill: parent; hoverEnabled: true
            cursorShape: btn.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: if (btn.enabled) btn.clicked()
        }
    }
    component Toggle: Rectangle {
        id: tg
        property bool checked: false
        property bool enabled: true
        signal toggled()
        implicitWidth: 38; implicitHeight: 20
        radius: height / 2
        color: !tg.enabled ? Style.normalFill : tg.checked ? Color.accent : Util.alpha(root.ink, 0.16)
        opacity: tg.enabled ? 1 : 0.5
        Rectangle {
            width: 16; height: 16; radius: 8; color: Color.background
            anchors.verticalCenter: parent.verticalCenter
            x: tg.checked ? parent.width - width - 2 : 2
            Behavior on x { NumberAnimation { duration: 120 } }
        }
        MouseArea { anchors.fill: parent; cursorShape: tg.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: if (tg.enabled) tg.toggled() }
    }
    component FieldInput: Rectangle {
        id: field
        property alias text: input.text
        property string placeholder: ""
        width: parent ? parent.width : 200
        height: 30
        radius: 6
        color: Style.normalFill
        border.color: input.activeFocus ? Color.accent : Style.normalBorderColor
        TextInput {
            id: input
            anchors.fill: parent
            anchors.margins: 8
            verticalAlignment: TextInput.AlignVCenter
            color: root.ink
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            clip: true
        }
        Text {
            anchors.left: input.left; anchors.verticalCenter: parent.verticalCenter
            text: field.placeholder
            visible: input.text.length === 0 && !input.activeFocus
            color: Util.alpha(root.ink, 0.4)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
        }
    }
    // The header's update-health ring: an arc over Style.normalBorderColor's
    // track, filled with Color.accent for the done fraction. Small deliberate
    // centerpiece rather than a plain "8/9" line -- the one bit of genuinely
    // "at a glance" visual state this panel has, echoing cpu-pulse's own
    // radial CpuChip without trying to be that literal animated die.
    component HealthRing: Item {
        id: ring
        property int done: 0
        property int total: 0
        property color trackColor: Style.normalBorderColor
        property color fillColor: Color.accent
        // Compact mode (the bar chip's icon) drops the center label -- there's
        // no room to draw "8/9" legibly at bar-icon size -- and draws a
        // thinner stroke proportionate to the smaller ring.
        property bool showLabel: true
        property string labelText: total > 0 ? done + "/" + total : "--"
        property real strokeWidth: showLabel ? 3 : 2.2
        // 44px, not 36: a worst-case label like "10/10" is 5 characters, and
        // 36px left only ~25px of clear space inside a 3.5px stroke at that
        // size -- the label and the arc visibly collided (confirmed live,
        // 2026-09-16 screenshot). 44px plus the narrower stroke above leaves
        // ~35px clear, comfortable for a 9px label.
        implicitWidth: showLabel ? 44 : 36; implicitHeight: showLabel ? 44 : 36
        Canvas {
            id: canvas
            anchors.fill: parent
            property real frac: ring.total > 0 ? ring.done / ring.total : 0
            property color track: ring.trackColor
            property color fill: ring.fillColor
            property real stroke: ring.strokeWidth
            onFracChanged: requestPaint()
            onTrackChanged: requestPaint()
            onFillChanged: requestPaint()
            onStrokeChanged: requestPaint()
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var cx = width / 2, cy = height / 2, r = Math.min(width, height) / 2 - stroke
                var start = -Math.PI / 2
                ctx.lineWidth = stroke
                ctx.lineCap = "round"
                ctx.strokeStyle = track
                ctx.beginPath()
                ctx.arc(cx, cy, r, 0, Math.PI * 2)
                ctx.stroke()
                if (frac > 0) {
                    ctx.strokeStyle = fill
                    ctx.beginPath()
                    ctx.arc(cx, cy, r, start, start + Math.PI * 2 * Math.min(1, frac))
                    ctx.stroke()
                }
            }
        }
        Text {
            visible: ring.showLabel
            anchors.centerIn: parent
            text: ring.labelText
            color: root.ink
            font.family: Style.font.family
            font.pixelSize: 9
            font.bold: true
            textFormat: Text.PlainText
        }
    }
    component Row_: Rectangle {
        id: row
        required property var modelData
        width: parent ? parent.width : 0
        readonly property bool busy: root.busyId === row.modelData.id
        readonly property bool detailShown: root.detailOpenId === row.modelData.id
        readonly property bool isSelf: row.modelData.id === root.moduleName
        readonly property color accentNow: root.pillAccent(row.modelData)
        property bool confirmingRemove: false
        height: content.implicitHeight + 12
        radius: 10
        color: rowMouse.containsMouse ? Style.hoverFill : Style.normalFill
        border.color: Style.normalBorderColor
        Behavior on color { ColorAnimation { duration: 80 } }

        MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
        Timer { id: confirmResetTimer; interval: 4000; onTriggered: row.confirmingRemove = false }
        onDetailShownChanged: if (!detailShown) confirmingRemove = false

        Rectangle {
            width: 4
            height: parent.height
            radius: 2
            color: root.stripeColor(row.modelData)
        }

        Column {
            id: content
            width: parent.width - 28
            x: 16; y: 6
            spacing: 6

            Item {
                width: parent.width
                height: Math.max(nameCol.implicitHeight, actionsRow.implicitHeight, pillBox.implicitHeight)

                Column {
                    id: nameCol
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: localPill.visible ? localPill.left : pillBox.left
                    anchors.rightMargin: 8
                    Heading { text: row.modelData.name; font.pixelSize: Style.font.body; elide: Text.ElideRight; width: parent.width }
                    Label {
                        width: parent.width
                        elide: Text.ElideRight
                        text: row.busy
                              ? (Math.max(0, Math.round(root.elapsedNow - root.actionStartSec)) + "s")
                              : ((row.modelData.version ? "v" + row.modelData.version : "no version")
                                 + (row.modelData.kind === "app" ? " · app" : "")
                                 + (row.isSelf ? " · this widget" : "")
                                 + (row.modelData.updateState === "not-installed" && row.modelData.reason ? " · " + row.modelData.reason : ""))
                        font.pixelSize: Style.font.caption
                    }
                }
                // Muted on purpose: local commits are information (a fork, a held fix),
                // not a problem -- the state pill beside it carries any urgency.
                Rectangle {
                    id: localPill
                    readonly property string label: Model.localTag(row.modelData)
                    visible: label !== "" && !row.busy
                    anchors.right: pillBox.left
                    anchors.rightMargin: 6
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: localPillText.implicitWidth + 12
                    implicitHeight: 18
                    radius: 9
                    color: "transparent"
                    border.color: Util.alpha(root.ink, 0.3)
                    Text {
                        id: localPillText
                        anchors.centerIn: parent
                        text: localPill.label
                        color: Util.alpha(root.ink, 0.62)
                        font.family: Style.font.family
                        font.pixelSize: 9
                        font.bold: true
                        textFormat: Text.PlainText
                    }
                }
                Rectangle {
                    id: pillBox
                    anchors.right: actionsRow.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: pillText.implicitWidth + 12
                    implicitHeight: 18
                    radius: 9
                    color: Util.alpha(row.accentNow, 0.18)
                    border.color: row.accentNow
                    Text {
                        id: pillText
                        anchors.centerIn: parent
                        text: row.busy ? "WORKING" : Model.pillLabel(row.modelData)
                        color: row.accentNow
                        font.family: Style.font.family
                        font.pixelSize: 9
                        font.bold: true
                        textFormat: Text.PlainText
                    }
                }
                Row {
                    id: actionsRow
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6
                    Toggle {
                        visible: row.modelData.kind === "plugin" && row.modelData.canDisable !== false && !row.isSelf
                        checked: !!row.modelData.enabled
                        enabled: !row.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onToggled: root.runAction(checked ? "disable" : "enable", row.modelData.id)
                    }
                    SmallButton {
                        text: row.detailShown && root.detailMode === "diff" ? "Hide" : "Diff"
                        accent: Color.muted
                        visible: Model.canShowDiff(row.modelData)
                        enabled: true
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: root.showDiff(row.modelData.id)
                    }
                    SmallButton {
                        text: row.busy ? "…" : "Update"
                        visible: Model.canUpdate(row.modelData)
                        enabled: !row.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: root.runAction("update", row.modelData.id)
                    }
                    SmallButton {
                        text: row.confirmingRemove ? "Confirm?" : "Remove"
                        accent: Color.urgent
                        visible: !row.isSelf
                        enabled: !row.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: {
                            if (row.confirmingRemove) {
                                confirmResetTimer.stop()
                                row.confirmingRemove = false
                                root.runAction(row.modelData.kind === "app" ? "remove-app" : "remove", row.modelData.id)
                            } else {
                                row.confirmingRemove = true
                                confirmResetTimer.restart()
                            }
                        }
                    }
                }
            }

            Rectangle {
                visible: row.detailShown
                width: parent.width
                height: visible ? 220 : 0
                radius: 8
                color: Style.normalFill
                border.color: Style.normalBorderColor
                clip: true
                Column {
                    width: parent.width - 16
                    x: 8; y: 8
                    spacing: 4
                    Row {
                        width: parent.width
                        Label {
                            width: parent.width - 60
                            text: root.detailMode === "diff" ? "Diff vs upstream" : (root.detailOk ? "Output" : "Output — failed")
                            color: root.detailMode === "output" && !root.detailOk ? Color.urgent : Util.alpha(root.ink, 0.62)
                            font.pixelSize: Style.font.caption
                        }
                        SmallButton { text: "Close"; accent: Color.muted; onClicked: root.closeDetail() }
                    }
                    Flickable {
                        width: parent.width
                        height: 188
                        contentWidth: width
                        contentHeight: detailTextItem.implicitHeight
                        clip: true
                        Text {
                            id: detailTextItem
                            width: parent.width
                            text: !row.detailShown ? ""
                                  : (root.detailMode === "diff"
                                     ? Model.diffHtml(root.detailText, {
                                           add: String(Color.accent),
                                           del: String(Color.urgent),
                                           meta: String(Util.alpha(root.ink, 0.45)),
                                           text: String(root.ink)
                                       })
                                     : root.detailText)
                            color: Util.alpha(root.ink, 0.85)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                            wrapMode: Text.WrapAnywhere
                            textFormat: root.detailMode === "diff" ? Text.RichText : Text.PlainText
                        }
                    }
                }
            }
        }
    }

    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        labelVisible: false
        hasVisualContent: true
        fixedWidth: vertical ? -1 : chipRow.implicitWidth + 12
        tooltipText: "Plugin Dashboard" + (root.updatable > 0 ? " · " + root.updatable + " update" + (root.updatable === 1 ? "" : "s") + " available" : "")
        onPressed: function(b) { root.toggle() }

        Row {
            id: chipRow
            anchors.centerIn: parent
            spacing: 4
            // The bar's own version of the popup header's health ring, so the
            // chip itself answers "is anything worth a look" before it's even
            // opened, rather than a static icon that means the same thing
            // whether everything's current or half the list needs attention.
            HealthRing {
                id: barRing
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: Style.bar.iconCanvas
                implicitHeight: Style.bar.iconCanvas
                showLabel: root.updatable > 0
                labelText: String(root.updatable)
                trackColor: Util.alpha(root.barForeground, 0.35)
                fillColor: root.barForeground
                done: root.health.done
                total: root.health.total
                property real pulse: 1.0
                opacity: root.updatable > 0 ? pulse : 1.0
                SequentialAnimation {
                    running: root.updatable > 0
                    loops: Animation.Infinite
                    NumberAnimation { target: barRing; property: "pulse"; from: 1.0; to: 0.45; duration: 700; easing.type: Easing.InOutQuad }
                    NumberAnimation { target: barRing; property: "pulse"; from: 0.45; to: 1.0; duration: 700; easing.type: Easing.InOutQuad }
                }
            }
        }
    }

    KeyboardPanel {
        id: panel
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: body
        contentWidth: panel.fittedContentWidth(460)
        contentHeight: panel.fittedContentHeight(mainColumn.implicitHeight)

        Item {
            id: body
            anchors.fill: parent
            focus: true
            Keys.onEscapePressed: root.close()
            // No background rectangle here: KeyboardPanel's own `card` already
            // paints the themed surface (Color.popups.background, Style.cornerRadius,
            // a themed border) behind this content -- a second hardcoded one drawn
            // on top of it was redundant and, being hardcoded, the one thing in this
            // file that could never have followed the theme anyway.

            Column {
                id: mainColumn
                width: parent.width
                spacing: 14

                // An Item with the title Column explicitly anchored *between* the
                // ring and the button row (rather than a plain Row laying all three
                // out sequentially by nominal width) so the title's real width
                // always matches the space actually left over -- a plain Row here
                // let "PLUGIN DASHBOARD" overflow its guessed width and draw
                // straight through the Refresh button once the button row's own
                // width changed (adding the ring shrank the guess without ever
                // measuring the button row it was meant to leave room for).
                Item {
                    id: header
                    width: parent.width
                    height: Math.max(ring.implicitHeight, titleCol.implicitHeight, headerButtons.implicitHeight)

                    HealthRing {
                        id: ring
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        done: root.health.done
                        total: root.health.total
                    }
                    Row {
                        id: headerButtons
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 8
                        SmallButton {
                            text: root.updateQueueTotal > 0 ? ("Updating " + root.updateQueueDone + "/" + root.updateQueueTotal + "…") : "Update all (" + root.updatable + ")"
                            visible: root.updatable > 1 || root.updateQueueTotal > 0
                            enabled: root.updateQueueTotal === 0 && root.busyId === "" && !checkProc.running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.runUpdateAll()
                        }
                        SmallButton {
                            text: checkProc.running ? "…" : "Refresh"
                            enabled: !checkProc.running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.runCheck(true)
                        }
                    }
                    Column {
                        id: titleCol
                        anchors.left: ring.right
                        anchors.leftMargin: 10
                        anchors.right: headerButtons.left
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 3
                        Heading {
                            width: parent.width
                            text: "Plugins"; font.pixelSize: Style.font.heading
                            elide: Text.ElideRight
                        }
                        Label {
                            width: parent.width
                            text: root.health.total > 0 ? (root.health.done + " of " + root.health.total + " current") : "Third-party plugins and apps"
                            font.pixelSize: Style.font.caption
                            elide: Text.ElideRight
                        }
                    }
                }

                // Scrollable so a growing plugin/app list is capped instead of
                // pushing the APPS section (and the footer) off the bottom of
                // the screen -- KeyboardPanel clamps the popup's own height to
                // the screen, but content inside it isn't clipped on its own.
                Flickable {
                    id: listScroll
                    width: parent.width
                    height: Math.min(listColumn.implicitHeight, Style.space(420))
                    contentWidth: width
                    contentHeight: listColumn.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    interactive: contentHeight > height

                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                    Column {
                        id: listColumn
                        width: parent.width
                        spacing: 14

                        Column {
                            width: parent.width
                            visible: root.grouped.plugins.length > 0
                            spacing: 8
                            Item {
                                width: parent.width
                                height: pluginsLabel.implicitHeight
                                Label {
                                    id: pluginsLabel
                                    text: "PLUGINS"; font.pixelSize: Style.font.caption; font.letterSpacing: 1.5
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                // omarchyplugins.com is Omarchy's own community plugin directory
                                // (documented in its shell-plugins manual) -- this panel only ever
                                // manages what's already installed, so a link out is the entire
                                // "discovery" story rather than reimplementing a browse/search UI
                                // against a site with no API.
                                SmallButton {
                                    text: "Browse plugins ↗"
                                    accent: Color.muted
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    onClicked: Qt.openUrlExternally("https://omarchyplugins.com")
                                }
                            }
                            Repeater {
                                model: root.grouped.plugins
                                Row_ {}
                            }
                        }

                        Column {
                            width: parent.width
                            visible: root.grouped.disabled.length > 0
                            spacing: 8
                            Item {
                                width: parent.width
                                height: disabledLabel.implicitHeight + 4
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.disabledOpen = !root.disabledOpen
                                }
                                Label {
                                    id: disabledLabel
                                    text: "DISABLED  " + root.grouped.disabled.length
                                    font.pixelSize: Style.font.caption
                                    font.letterSpacing: 1.5
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Label {
                                    text: root.disabledOpen ? "hide" : "show"
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    font.pixelSize: Style.font.caption
                                }
                            }
                            Repeater {
                                model: root.disabledOpen ? root.grouped.disabled : []
                                Row_ {}
                            }
                        }

                        Column {
                            width: parent.width
                            visible: root.grouped.apps.length > 0 || root.addAppFormOpen
                            spacing: 8
                            Label { text: "APPS"; font.pixelSize: Style.font.caption; font.letterSpacing: 1.5 }
                            Repeater {
                                model: root.grouped.apps
                                Row_ {}
                            }

                            SmallButton {
                                text: root.addAppFormOpen ? "Cancel" : "+ Add app"
                                accent: root.addAppFormOpen ? Color.urgent : Color.accent
                                onClicked: root.addAppFormOpen = !root.addAppFormOpen
                            }

                            Column {
                                width: parent.width
                                visible: root.addAppFormOpen
                                spacing: 6
                                FieldInput { placeholder: "Display name"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {name: text}) }
                                FieldInput { placeholder: "id (lowercase, no spaces)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {id: text}) }
                                FieldInput { placeholder: "Repo dir, e.g. /home/you/Projects/myapp"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {repoDir: text}) }
                                FieldInput { placeholder: "Package name (defaults to id)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {pkgName: text}) }
                                Row {
                                    width: parent.width
                                    spacing: 6
                                    FieldInput { width: (parent.width - 6) / 2; placeholder: "Branch (default master)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {branch: text}) }
                                    FieldInput { width: (parent.width - 6) / 2; placeholder: "Remote (default origin)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {remote: text}) }
                                }
                                FieldInput { text: "./rebuild.sh --install"; placeholder: "Update command"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {updateCmd: text}) }
                                SmallButton { text: "Save"; enabled: root.busyId === ""; onClicked: root.submitAddApp() }
                            }
                        }

                        Label {
                            width: parent.width
                            visible: root.items.length === 0
                            text: checkProc.running ? "Checking…" : "No third-party plugins or tracked apps found."
                            font.pixelSize: Style.font.bodySmall
                        }
                    }
                }

                Rectangle { width: parent.width; height: 1; color: Style.normalBorderColor }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.pixelSize: Style.font.caption
                    text: (root.busyId === "" && !checkProc.running ? root.actionStatus : "") ||
                          (checkProc.running ? "Checking sources…" :
                           "Last checked " + Model.relativeAge(root.status.ts, root.now) + "  ·  Esc closes")
                }
            }
        }
    }
}
