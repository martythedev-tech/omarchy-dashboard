import QtQuick
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
    property var updateQueue: []
    property string diffOpenId: ""
    property string diffText: ""
    property bool addAppFormOpen: false
    property var addAppFields: ({name: "", id: "", repoDir: "", pkgName: "", branch: "", remote: "", updateCmd: "./rebuild.sh --install"})
    property real now: Date.now() / 1000

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

    function runCheck() {
        if (checkProc.running) return
        actionStatus = "Checking sources…"
        checkProc.running = true
    }

    function runAction(kind, id) {
        if (root.busyId !== "") return
        root.busyId = id
        root.lastActionKind = kind
        actionStatus = actionVerb(kind) + id + "…"
        actionProc.command = ["python3", helper, kind, id]
        actionProc.running = true
    }

    function runUpdateAll() {
        if (root.busyId !== "") return
        var ids = []
        for (var i = 0; i < root.items.length; i++) if (Model.canUpdate(root.items[i])) ids.push(root.items[i].id)
        if (ids.length === 0) return
        root.updateQueue = ids
        advanceUpdateQueue()
    }

    function advanceUpdateQueue() {
        if (root.updateQueue.length === 0) return
        var next = root.updateQueue[0]
        root.updateQueue = root.updateQueue.slice(1)
        runAction("update", next)
    }

    function toggleDiff(id) {
        if (root.diffOpenId === id) { root.diffOpenId = ""; return }
        root.diffOpenId = id
        root.diffText = "Loading…"
        diffProc.command = ["python3", root.helper, "diff", id]
        diffProc.running = true
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

    onOpenedChanged: if (opened) { statusFile.reload(); runCheck() }

    FileView {
        id: statusFile
        path: root.stateDir + "/status.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.status = JSON.parse(text()) } catch (e) {} }
    }

    Timer { interval: 1000; running: root.opened; repeat: true; onTriggered: root.now = Date.now() / 1000 }
    // A slow background refresh while closed too, so the badge count on the
    // bar chip itself stays roughly current without anyone opening the panel.
    Timer { interval: 900000; running: true; repeat: true; triggeredOnStart: true; onTriggered: if (!root.opened) checkProc.running = true }

    Process {
        id: checkProc
        command: ["python3", root.helper, "check"]
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
                    root.actionStatus = r.ok ? "Done." : (r.message || "Failed.")
                    if (r.ok && kind === "add-app") root.addAppFormOpen = false
                    if (r.ok && (kind === "remove" || kind === "remove-app") && root.diffOpenId === targetId) root.diffOpenId = ""
                } catch (e) {
                    root.actionStatus = "Action did not report a result."
                }
                root.busyId = ""
                root.lastActionKind = ""
                statusFile.reload()
                if (root.updateQueue.length > 0) Qt.callLater(root.advanceUpdateQueue)
            }
        }
        onExited: function(code) {
            if (code !== 0 && root.busyId !== "") {
                root.busyId = ""
                root.lastActionKind = ""
                if (!root.actionStatus) root.actionStatus = "Action failed."
                if (root.updateQueue.length > 0) Qt.callLater(root.advanceUpdateQueue)
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
                    root.diffText = r.ok ? (r.diff || "(no textual diff)") : ("Could not load diff: " + (r.message || ""))
                } catch (e) {
                    root.diffText = "Could not load diff."
                }
            }
        }
    }

    component Label: Text {
        color: "#91a5b0"; font.pixelSize: 11; textFormat: Text.PlainText
    }
    component Heading: Text {
        color: "#eff7fa"; font.pixelSize: 14; font.bold: true; textFormat: Text.PlainText
    }
    component SmallButton: Rectangle {
        id: btn
        property string text: ""
        property bool enabled: true
        property color accent: "#5aed95"
        signal clicked()
        implicitWidth: caption.implicitWidth + 20
        implicitHeight: 26
        radius: 7
        color: !btn.enabled ? "#1b262d" : area.containsMouse ? Qt.alpha(accent, 0.28) : Qt.alpha(accent, 0.16)
        border.color: !btn.enabled ? "#2a3b47" : accent
        opacity: btn.enabled ? 1 : 0.5
        Text { id: caption; anchors.centerIn: parent; text: btn.text; color: "#eff7fa"; font.pixelSize: 11; font.bold: true; textFormat: Text.PlainText }
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
        color: !tg.enabled ? "#28343d" : tg.checked ? "#3fae76" : "#2a3b47"
        opacity: tg.enabled ? 1 : 0.5
        Rectangle {
            width: 16; height: 16; radius: 8; color: "#eff7fa"
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
        color: "#111e28"
        border.color: input.activeFocus ? "#5a8fed" : "#263844"
        TextInput {
            id: input
            anchors.fill: parent
            anchors.margins: 8
            verticalAlignment: TextInput.AlignVCenter
            color: "#eff7fa"
            font.pixelSize: 11
            clip: true
        }
        Text {
            anchors.left: input.left; anchors.verticalCenter: parent.verticalCenter
            text: field.placeholder
            visible: input.text.length === 0 && !input.activeFocus
            color: "#5c7280"
            font.pixelSize: 11
        }
    }
    component Row_: Rectangle {
        id: row
        required property var modelData
        width: parent ? parent.width : 0
        readonly property bool busy: root.busyId === row.modelData.id
        readonly property bool diffShown: root.diffOpenId === row.modelData.id
        readonly property bool isSelf: row.modelData.id === root.moduleName
        property bool confirmingRemove: false
        height: content.implicitHeight + 16
        radius: 10
        color: rowMouse.containsMouse ? "#1d303b" : "#111e28"
        border.color: "#263844"

        MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
        Timer { id: confirmResetTimer; interval: 4000; onTriggered: row.confirmingRemove = false }
        onDiffShownChanged: if (!diffShown) confirmingRemove = false

        Column {
            id: content
            width: parent.width - 24
            x: 12; y: 8
            spacing: 6

            Row {
                width: parent.width
                spacing: 10
                Column {
                    width: 190
                    Heading { text: row.modelData.name; font.pixelSize: 12; elide: Text.ElideRight; width: 190 }
                    Label { text: (row.modelData.version ? "v" + row.modelData.version : "no version") + (row.modelData.kind === "app" ? "  ·  app" : "") + (row.isSelf ? "  ·  this widget" : ""); font.pixelSize: 10 }
                }
                Label {
                    width: parent.width - 190 - 10
                    wrapMode: Text.WordWrap
                    text: row.busy ? root.actionStatus : Model.stateLabel(row.modelData)
                    color: Model.canUpdate(row.modelData) ? "#5aed95" : (row.modelData.updateState === "dirty" || row.modelData.updateState === "diverged") ? "#f0ba82" : "#91a5b0"
                }
            }

            Row {
                anchors.right: parent.right
                spacing: 8
                Toggle {
                    visible: row.modelData.kind === "plugin" && row.modelData.canDisable !== false && !row.isSelf
                    checked: !!row.modelData.enabled
                    enabled: !row.busy && root.busyId === ""
                    anchors.verticalCenter: parent.verticalCenter
                    onToggled: root.runAction(checked ? "disable" : "enable", row.modelData.id)
                }
                SmallButton {
                    text: row.diffShown ? "Hide diff" : "Diff"
                    accent: "#5a8fed"
                    visible: Model.canShowDiff(row.modelData)
                    enabled: true
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: root.toggleDiff(row.modelData.id)
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
                    accent: "#e0654a"
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

            Rectangle {
                visible: row.diffShown
                width: parent.width
                height: visible ? Math.min(220, diffTextItem.implicitHeight + 16) : 0
                radius: 8
                color: "#0b141d"
                border.color: "#263844"
                clip: true
                Flickable {
                    anchors.fill: parent
                    anchors.margins: 8
                    contentWidth: width
                    contentHeight: diffTextItem.implicitHeight
                    clip: true
                    Text {
                        id: diffTextItem
                        width: parent.width
                        text: row.diffShown ? root.diffText : ""
                        color: "#c7d6dd"
                        font.family: "monospace"
                        font.pixelSize: 10
                        wrapMode: Text.NoWrap
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
            Text {
                text: ""
                color: root.barForeground
                font.family: Style.font.family
                font.pixelSize: Style.font.icon
                anchors.verticalCenter: parent.verticalCenter
            }
            Rectangle {
                visible: root.updatable > 0
                anchors.verticalCenter: parent.verticalCenter
                width: Math.max(14, badgeText.implicitWidth + 8)
                height: 14
                radius: 7
                color: "#e0654a"
                Text {
                    id: badgeText
                    anchors.centerIn: parent
                    text: String(root.updatable)
                    color: "#0b141d"
                    font.pixelSize: 9
                    font.bold: true
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
            Rectangle { anchors.fill: parent; anchors.margins: -10; radius: 14; color: "#0b141d" }

            Column {
                id: mainColumn
                width: parent.width
                spacing: 14

                Row {
                    width: parent.width
                    Column {
                        width: parent.width - 190
                        spacing: 3
                        Heading { text: "PLUGIN DASHBOARD"; font.pixelSize: 16; font.letterSpacing: 2 }
                        Label { text: "Everything that isn't Omarchy's own."; font.pixelSize: 10 }
                    }
                    Row {
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 8
                        SmallButton {
                            text: root.busyId !== "" ? "…" : "Update all (" + root.updatable + ")"
                            visible: root.updatable > 1
                            enabled: root.busyId === "" && !checkProc.running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.runUpdateAll()
                        }
                        SmallButton {
                            text: checkProc.running ? "…" : "Refresh"
                            enabled: !checkProc.running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.runCheck()
                        }
                    }
                }

                Column {
                    width: parent.width
                    visible: root.grouped.plugins.length > 0
                    spacing: 8
                    Label { text: "PLUGINS"; font.pixelSize: 10; font.letterSpacing: 1.5 }
                    Repeater {
                        model: root.grouped.plugins
                        Row_ {}
                    }
                }

                Column {
                    width: parent.width
                    visible: root.grouped.apps.length > 0 || root.addAppFormOpen
                    spacing: 8
                    Label { text: "APPS"; font.pixelSize: 10; font.letterSpacing: 1.5 }
                    Repeater {
                        model: root.grouped.apps
                        Row_ {}
                    }

                    SmallButton {
                        text: root.addAppFormOpen ? "Cancel" : "+ Add app"
                        accent: root.addAppFormOpen ? "#e0654a" : "#5aed95"
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
                    font.pixelSize: 11
                }

                Rectangle { width: parent.width; height: 1; color: "#25343f" }
                Label {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.pixelSize: 10
                    text: (root.busyId === "" && !checkProc.running ? root.actionStatus : "") ||
                          (checkProc.running ? "Checking sources…" :
                           "Last checked " + Model.relativeAge(root.status.ts, root.now) + "  ·  Esc closes")
                }
            }
        }
    }
}
