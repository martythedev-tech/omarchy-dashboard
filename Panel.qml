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
    property real now: Date.now() / 1000

    function runCheck() {
        if (checkProc.running) return
        actionStatus = "Checking sources…"
        checkProc.running = true
    }
    function runAction(kind, id) {
        if (root.busyId !== "") return
        root.busyId = id
        actionStatus = (kind === "update" ? "Updating " : kind === "enable" ? "Enabling " : "Disabling ") + id + "…"
        actionProc.command = ["python3", helper, kind, id]
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
                try {
                    var r = JSON.parse(text)
                    root.actionStatus = r.ok ? "Done." : (r.message || "Failed.")
                } catch (e) {
                    root.actionStatus = "Action did not report a result."
                }
                root.busyId = ""
                statusFile.reload()
            }
        }
        onExited: function(code) { if (code !== 0 && root.busyId !== "") { root.busyId = ""; if (!root.actionStatus) root.actionStatus = "Action failed." } }
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
    component Row_: Rectangle {
        id: row
        required property var modelData
        width: parent ? parent.width : 0
        height: 46
        radius: 10
        color: rowMouse.containsMouse ? "#1d303b" : "#111e28"
        border.color: "#263844"
        readonly property bool busy: root.busyId === row.modelData.id

        MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }

        Row {
            anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
            spacing: 10
            Column {
                width: 220
                Heading { text: row.modelData.name; font.pixelSize: 12; elide: Text.ElideRight; width: 220 }
                Label { text: (row.modelData.version ? "v" + row.modelData.version : "no version") + (row.modelData.kind === "app" ? "  ·  app" : ""); font.pixelSize: 10 }
            }
            Label {
                width: 190; wrapMode: Text.WordWrap
                text: row.busy ? root.actionStatus : Model.stateLabel(row.modelData)
                color: Model.canUpdate(row.modelData) ? "#5aed95" : row.modelData.updateState === "dirty" ? "#f0ba82" : "#91a5b0"
            }
        }

        Row {
            anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
            spacing: 10
            Toggle {
                visible: row.modelData.kind === "plugin" && row.modelData.canDisable !== false
                checked: !!row.modelData.enabled
                enabled: !row.busy && root.busyId === ""
                anchors.verticalCenter: parent.verticalCenter
                onToggled: root.runAction(checked ? "disable" : "enable", row.modelData.id)
            }
            SmallButton {
                text: row.busy ? "…" : "Update"
                visible: Model.canUpdate(row.modelData)
                enabled: !row.busy && root.busyId === ""
                anchors.verticalCenter: parent.verticalCenter
                onClicked: root.runAction("update", row.modelData.id)
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
                        width: parent.width - 90
                        spacing: 3
                        Heading { text: "PLUGIN DASHBOARD"; font.pixelSize: 16; font.letterSpacing: 2 }
                        Label { text: "Everything that isn't Omarchy's own."; font.pixelSize: 10 }
                    }
                    SmallButton {
                        text: checkProc.running ? "…" : "Refresh"
                        enabled: !checkProc.running
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: root.runCheck()
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
                    visible: root.grouped.apps.length > 0
                    spacing: 8
                    Label { text: "APPS"; font.pixelSize: 10; font.letterSpacing: 1.5 }
                    Repeater {
                        model: root.grouped.apps
                        Row_ {}
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
