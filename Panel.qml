import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import qs.Commons as Commons

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
    // The bar badge, pulse, tooltip and IPC count: updates that change a version.
    readonly property int updatable: Model.badgeCount(items)
    // Everything Update all would run, including commits that leave the version alone.
    readonly property int updatableAll: {
        var n = 0
        for (var i = 0; i < items.length; i++) if (Model.canUpdate(items[i])) n++
        return n
    }
    property string actionStatus: ""
    property string busyId: ""
    property string lastActionKind: ""
    property real actionStartSec: 0
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
    property bool recentOpen: false
    // The row whose facts (status, branch, source, folder) are expanded; one at a time.
    property string infoOpenId: ""
    readonly property string home: Quickshell.env("HOME") || ""
    // Update all's per-item results, for the summary once the queue is done.
    property var queueResults: []
    // Updates, rollbacks and removals run as a job outside this widget (see Model.isJobKind):
    // the job this instance last saw going, and the last one whose ending it showed.
    property string jobSeenId: ""
    // Remove's confirmation: what dashboard.py remove-plan says it would do, and whether
    // the plugin's data goes to the trash with it.
    property var removePlan: null
    property bool removePurge: false
    readonly property var leftovers: status.leftovers || []
    property bool leftoversOpen: false
    // The theme's named colours (colors.toml), for the gauge's mood.
    property var palette: ({})
    // When the panel was last opened or closed: a failed action from before then has been
    // seen, so it stops turning the gauge red. Kept in a file so a shell restart and the
    // bars on other monitors agree.
    property real failureSeenTs: 0
    readonly property string gaugeToneName: Model.gaugeTone(items, history, Date.now() / 1000, failureSeenTs)
    readonly property color gaugeTone: Model.toneColor(gaugeToneName, palette,
        {urgent: String(Commons.Color.urgent), accent: String(Commons.Color.accent), muted: String(Commons.Color.muted)})
    readonly property bool working: busyId !== "" || checkProc.running
    property string jobAppliedId: ""
    property var addAppNotes: []
    // The Changes panel's two views of the same update: the commit list (default, readable
    // at any size) and the raw diff.
    property string diffTab: "commits"
    property var detailCommits: ({incoming: [], local: []})
    readonly property var history: status.history || []
    property var addAppFields: ({name: "", id: "", repoDir: "", pkgName: "", branch: "", remote: "", updateCmd: "./rebuild.sh --install", pullFirst: false})
    // What is being added: a source repo built here ("repo") or an installed pacman/AUR
    // package ("package"), and whether the notes under the form are problems or just facts.
    property string addAppSource: "repo"
    property bool addAppNotesOk: false
    property real now: Date.now() / 1000
    property real elapsedNow: Date.now() / 1000

    // Which list the panel shows: what is installed, or Discover -- the official plugin
    // catalog (dashboard.py catalog), read only once Discover is first opened, since it is
    // a few MB.
    property string view: "installed"
    property bool catalogWanted: false
    property var catalog: ({})
    readonly property var catalogPlugins: catalog.plugins || []
    property var catalogIndex: []
    property string catalogError: ""
    property string discoverQuery: ""
    property bool discoverInstallableOnly: true
    property string discoverSort: "popular"
    property var discoverResults: []
    // The Discover row expanded, and the one whose Install is waiting for its confirmation.
    property string discoverOpenId: ""
    property string installConfirmId: ""
    readonly property var installedKeys: Model.installedKeys(items)

    // Every color in this file reads through here rather than a fixed hex
    // palette, so the panel actually follows the user's chosen Omarchy theme
    // (colors.toml/shell.toml) instead of a hardcoded dark-slate look that
    // clashes with a light or differently-accented theme -- the same
    // approach nixfred.cpu-pulse (the plugin whose shape this file follows)
    // already uses throughout.
    readonly property color ink: Commons.Color.popups.text
    readonly property var health: Model.healthFraction(root.items)

    function actionVerb(kind) {
        switch (kind) {
            case "update": return "Updating "
            case "enable": return "Enabling "
            case "disable": return "Disabling "
            case "remove": return "Removing "
            case "remove-app": return "Removing "
            case "rollback": return "Rolling back "
            case "hold": return "Holding "
            case "unhold": return "Unholding "
            case "cleanup": return "Cleaning up "
            case "install": return "Installing "
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
        if (kind === "update" || kind === "install") {
            root.detailOpenId = id
            root.detailMode = "output"
            root.detailOk = true
            root.detailText = ""
        }
        if (Model.isJobKind(kind)) { startJob(kind, [id]); return }
        actionProc.command = ["python3", helper, kind, id]
        actionProc.running = true
    }

    function runUpdateAll() {
        if (root.busyId !== "" || root.updateQueueTotal > 0) return
        var ids = [], terminal = []
        for (var i = 0; i < root.items.length; i++) {
            if (!Model.canUpdate(root.items[i])) continue
            if (Model.inTerminal(root.items[i])) terminal.push(root.items[i].id)
            else ids.push(root.items[i].id)
        }
        // Package apps get one terminal between them; the rest run as the usual job.
        if (terminal.length > 0) root.updateInTerminal(terminal)
        if (ids.length === 0) return
        root.updateQueueTotal = ids.length
        root.updateQueueDone = 1
        root.queueResults = []
        root.busyId = ids[0]
        root.lastActionKind = "update"
        root.actionStartSec = Date.now() / 1000
        root.actionStatus = actionVerb("update") + ids[0] + "…"
        startJob("update", ids)
    }

    // Package apps (pacman, AUR): their update needs sudo, so it opens in a terminal
    // (dashboard.py terminal-update), which records it and rechecks when done.
    function updateInTerminal(ids) {
        Quickshell.execDetached(["python3", root.helper, "open-terminal-update"].concat(ids))
        root.actionStatus = "Updating " + ids.map(root.itemName).join(", ") + " in a terminal…"
    }

    function startJob(kind, ids, purge) {
        startProc.command = ["python3", helper, "start", kind].concat(ids).concat(purge ? ["--purge"] : [])
        startProc.running = true
    }

    // Remove asks first, in the row's detail panel, with the plan in front of you: what
    // goes, which services stop, and the data it would leave or trash.
    function showRemovePlan(id) {
        if (root.detailOpenId === id && root.detailMode === "remove") { root.detailOpenId = ""; return }
        root.removePlan = null
        root.removePurge = false
        root.detailOpenId = id
        root.detailMode = "remove"
        root.detailOk = true
        root.detailText = "Looking at what it left around…"
        planProc.command = ["python3", root.helper, "remove-plan", id]
        planProc.running = true
    }

    function confirmRemove(id) {
        if (root.busyId !== "" || !root.removePlan) return
        var purge = root.removePurge && (root.removePlan.data || []).length > 0
        root.busyId = id
        root.lastActionKind = "remove"
        root.actionStartSec = Date.now() / 1000
        root.actionStatus = actionVerb("remove") + root.itemName(id) + "…"
        startJob("remove", [id], purge)
    }

    function cleanupLeftovers(keys) {
        if (root.busyId !== "" || keys.length === 0) return
        root.busyId = keys[0]
        root.lastActionKind = "cleanup"
        root.actionStartSec = Date.now() / 1000
        root.actionStatus = actionVerb("cleanup") + (keys.length > 1 ? keys.length + " items…" : "…")
        startJob("cleanup", keys)
    }

    // job.json changed (or this instance was just created and read it): mirror a running
    // job as busy, and show how one ended -- the same way a direct action's result shows.
    function syncJob(job) {
        var v = Model.jobView(job, root.jobSeenId, root.jobAppliedId, Date.now() / 1000)
        if (v.busy) {
            var fresh = root.jobSeenId !== job.jobId || root.busyId !== v.id
            root.jobSeenId = job.jobId
            root.busyId = v.id
            root.lastActionKind = v.kind
            root.actionStartSec = v.startSec
            var queued = v.total > 1 && v.kind === "update"
            root.updateQueueTotal = queued ? v.total : 0
            root.updateQueueDone = queued ? Math.min(v.done + 1, v.total) : 0
            root.actionStatus = actionVerb(v.kind) + (v.kind === "cleanup" ? (v.total > 1 ? (v.done + 1) + "/" + v.total + "…" : "…")
                                                                           : root.itemName(v.id) + "…")
            if (fresh && (v.kind === "update" || v.kind === "install")) {
                root.detailOpenId = v.id
                root.detailMode = "output"
                root.detailOk = true
                root.detailText = ""
                actionLog.reload()
            }
            return
        }
        if (!v.apply) return
        root.jobAppliedId = job.jobId
        root.busyId = ""
        root.lastActionKind = ""
        var results = v.results
        if (v.total > 1 && v.kind === "cleanup") {
            root.updateQueueTotal = 0
            root.updateQueueDone = 0
            root.actionStatus = Model.queueSummary(results.map(function(r) { return {id: r.id, name: r.id, ok: !!r.ok} }), "Clean up", "cleared")
        } else if (v.kind === "cleanup") {
            root.actionStatus = results.length && results[0].ok ? "Cleaned up." : (results.length ? results[0].message : "")
        } else if (v.total > 1) {
            root.queueResults = results.map(function(r) { return {id: r.id, name: root.itemName(r.id), ok: !!r.ok, message: r.message || ""} })
            root.finishUpdateQueue()
        } else {
            root.updateQueueTotal = 0
            root.updateQueueDone = 0
            if (results.length > 0) root.applyActionResult(v.kind, results[0].id, results[0])
        }
        statusFile.reload()
    }

    // A finished action's result, shown on its row: the command's own output, or nothing
    // where the row itself already says it.
    function applyActionResult(kind, targetId, r) {
        root.actionStatus = r.ok ? "Done." : "Failed."
        // A new plugin is shown where it now lives, with what installing it said; a failed
        // install, on its Discover row.
        if (kind === "install") root.showView(r.ok ? "installed" : "discover")
        if (kind === "add-app") {
            if (!r.ok) { root.addAppNotes = [r.message || "Could not add it."]; root.addAppNotesOk = false }
            if (r.ok) root.addAppFormOpen = false
        } else if (r.ok && (kind === "remove" || kind === "remove-app")) {
            // The row is about to disappear from the list -- nothing left to show a panel on;
            // the footer carries what happened.
            if (root.detailOpenId === targetId) root.detailOpenId = ""
            root.actionStatus = (r.message || "Removed.").split("\n").slice(-1)[0]
        } else if (r.ok && (kind === "hold" || kind === "unhold")) {
            // The pill says it; an output panel reading "Holding x" adds nothing.
            if (root.detailOpenId === targetId) root.detailOpenId = ""
            root.actionStatus = r.message
        } else {
            // This is the actual answer to "did anything happen": the
            // command's real output, shown until closed or overwritten
            // by the next action -- not just a terse Done./Failed.
            root.detailOpenId = targetId
            root.detailMode = "output"
            root.detailOk = !!r.ok
            root.detailText = r.message || (r.ok ? "(no output)" : "(no error message)")
        }
    }

    function itemName(id) {
        for (var i = 0; i < root.items.length; i++) if (root.items[i].id === id) return root.items[i].name
        for (var j = 0; j < root.catalogPlugins.length; j++) if (root.catalogPlugins[j].id === id) return root.catalogPlugins[j].name
        return id
    }

    function showView(which) {
        root.view = which
        if (which !== "discover") return
        root.catalogWanted = true
        if (!Model.statusFresh(root.catalog.ts, Date.now() / 1000, 6 * 3600)) root.runCatalog(false)
    }

    function runCatalog(force) {
        if (catalogProc.running) return
        root.catalogError = ""
        catalogProc.command = ["python3", root.helper, "catalog"].concat(force ? ["--force"] : [])
        catalogProc.running = true
    }

    function refilterDiscover() {
        root.discoverResults = Model.discoverList(root.catalogPlugins, root.catalogIndex, root.discoverQuery,
                                                  {installableOnly: root.discoverInstallableOnly, sort: root.discoverSort})
    }

    // Install runs as a job like an update: adding a plugin makes the shell rebuild every
    // plugin widget, this one included.
    function runInstall(id) {
        if (root.busyId !== "") return
        root.installConfirmId = ""
        root.busyId = id
        root.lastActionKind = "install"
        root.actionStartSec = Date.now() / 1000
        root.actionStatus = actionVerb("install") + root.itemName(id) + "…"
        root.detailOpenId = id
        root.detailMode = "output"
        root.detailOk = true
        root.detailText = ""
        startJob("install", [id])
    }

    // After the last item of an Update all: one line saying how it went, and the first
    // failure's output left open -- not whichever item happened to run last.
    function finishUpdateQueue() {
        var results = root.queueResults
        root.updateQueueTotal = 0
        root.updateQueueDone = 0
        root.queueResults = []
        if (results.length === 0) return
        root.actionStatus = Model.queueSummary(results)
        for (var i = 0; i < results.length; i++) {
            if (!results[i].ok) {
                root.detailOpenId = results[i].id
                root.detailMode = "output"
                root.detailOk = false
                root.detailText = results[i].message || "(no error message)"
                return
            }
        }
    }

    function showDiff(id) {
        if (root.detailOpenId === id && root.detailMode === "diff") { root.detailOpenId = ""; return }
        root.detailOpenId = id
        root.detailMode = "diff"
        root.diffTab = "commits"
        root.detailCommits = ({incoming: [], local: []})
        root.detailText = "Loading…"
        diffProc.command = ["python3", root.helper, "diff", id]
        diffProc.running = true
    }

    function closeDetail() { root.detailOpenId = "" }

    function stripeColor(item) {
        var k = Model.stripeKey(item)
        if (k === "behind") return Commons.Color.accent
        if (k === "attention") return Commons.Color.urgent
        if (k === "off") return Util.alpha(root.ink, 0.28)
        return Util.alpha(root.ink, 0.12)
    }

    function pillAccent(item) {
        var k = Model.stripeKey(item)
        if (k === "behind") return root.paletteColor("yellow", Commons.Color.accent)
        if (k === "attention") return Commons.Color.urgent
        if (k === "off") return Util.alpha(root.ink, 0.45)
        if (item && (item.updateState === "no-repo" || item.updateState === "unreachable")) return Util.alpha(root.ink, 0.45)
        return root.paletteColor("green", Util.alpha(root.ink, 0.45))
    }

    function submitAddApp() {
        if (root.busyId !== "") return
        var f = root.addAppFields
        if (root.addAppSource === "package") {
            f = {source: "package", pkgName: pkgLookupField.text.trim(), name: f.name, id: f.id || pkgLookupField.text.trim()}
            if (!f.pkgName) { root.actionStatus = "A package name is required."; return }
        } else if (!f.name || !f.id || !f.repoDir) { root.actionStatus = "Name, id, and repo dir are required."; return }
        root.busyId = "__add_app__"
        root.lastActionKind = "add-app"
        root.actionStatus = "Adding " + f.name + "…"
        actionProc.command = ["python3", root.helper, "add-app", JSON.stringify(f)]
        actionProc.running = true
    }

    onOpenedChanged: {
        root.markFailuresSeen()
        if (opened) {
            if (root.view === "discover") root.showView("discover")
            statusFile.reload()
            if (!Model.statusFresh(root.status.ts, Date.now() / 1000, 300)) root.runCheck()
        }
    }

    function markFailuresSeen(): void {
        root.failureSeenTs = Date.now() / 1000
        seenFile.setText(JSON.stringify({failureSeenTs: root.failureSeenTs}) + "\n")
    }

    // manageIpc is false so this handler can own the target (one handler per target; the
    // shell answers with the first live one, so with a bar per monitor the first bar's
    // copy opens). `omarchy-shell martythedev-tech.dashboard toggle` from a keybinding,
    // `check` to force a check, `count` for scripts. The update notification's Open
    // button calls `open`.
    ShellIpc {
        target: root.ipcTarget
        function open(): void { root.open() }
        function close(): void { root.close() }
        function show(): void { root.open() }
        function hide(): void { root.close() }
        function toggle(): void { root.toggle() }
        function discover(): void { root.showView("discover"); root.open() }
        function check(): string { root.runCheck(true); return "ok" }
        function count(): string { return String(root.updatable) }
        function update(id: string): string {
            for (var i = 0; i < root.items.length; i++)
                if (root.items[i].id === id && Model.inTerminal(root.items[i])) { root.updateInTerminal([id]); return "terminal" }
            if (root.busyId !== "") return "busy"
            root.runAction("update", id)
            return "started"
        }
    }

    FileView {
        id: statusFile
        path: root.stateDir + "/status.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.status = JSON.parse(text()) } catch (e) {} }
    }

    // The theme's named colors (red, green, ...), which Commons.Color does not expose: the
    // tiles and badges take their colors from here so they follow the theme.
    property var themePalette: ({})
    FileView {
        path: Commons.Color.currentThemePath + "/colors.toml"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            var pal = {}
            var lines = text().split("\n")
            for (var i = 0; i < lines.length; i++) {
                var m = lines[i].match(/^\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"/)
                if (m) pal[m[1]] = m[2]
            }
            root.themePalette = pal
        }
    }
    function paletteColor(name, fallback) {
        return root.themePalette[name] ? root.themePalette[name] : fallback
    }
    function tileColor(name) {
        var keys = ["blue", "magenta", "green", "cyan", "yellow", "orange", "red"]
        var have = keys.filter(function(k) { return !!root.themePalette[k] })
        return have.length ? root.themePalette[have[Model.tileIndex(name, have.length)]] : Commons.Color.accent
    }

    FileView {
        id: catalogFile
        path: root.catalogWanted ? root.stateDir + "/catalog.json" : ""
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            try {
                var c = JSON.parse(text())
                root.catalog = c
                root.catalogIndex = Model.discoverIndex(c.plugins || [])
                if (c.error && !catalogProc.running) root.catalogError = c.error
                root.refilterDiscover()
            } catch (e) {}
        }
    }

    Process {
        id: catalogProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var r = null
                try { r = JSON.parse(text) } catch (e) {}
                root.catalogError = r && r.ok ? "" : ((r && r.message) || "Could not load the plugin catalog.")
                catalogFile.reload()
            }
        }
    }

    // Typing filters after a pause, not on every key: the catalog has thousands of entries.
    Timer { id: discoverDebounce; interval: 180; onTriggered: root.refilterDiscover() }

    FileView {
        id: seenFile
        path: root.stateDir + "/seen.json"
        watchChanges: true
        atomicWrites: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.failureSeenTs = Number(JSON.parse(text()).failureSeenTs) || 0 } catch (e) {} }
    }

    FileView {
        id: actionLog
        path: root.stateDir + "/action.log"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            if ((root.lastActionKind === "update" || root.lastActionKind === "install") && root.busyId !== "") {
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
                    root.applyActionResult(kind, targetId, JSON.parse(text))
                } catch (e) {
                    root.actionStatus = "Action did not report a result."
                }
                root.busyId = ""
                root.lastActionKind = ""
                statusFile.reload()
            }
        }
        onExited: function(code) {
            if (code !== 0 && root.busyId !== "") {
                root.busyId = ""
                root.lastActionKind = ""
                if (!root.actionStatus) root.actionStatus = "Action failed."
            }
        }
    }

    // Hands a job over and returns at once; job.json reports from then on.
    Process {
        id: startProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var r = null
                try { r = JSON.parse(text) } catch (e) {}
                if (r && r.ok) {
                    root.jobSeenId = r.jobId
                    jobFile.reload()
                    return
                }
                root.busyId = ""
                root.lastActionKind = ""
                root.updateQueueTotal = 0
                root.updateQueueDone = 0
                root.actionStatus = (r && r.message) || "Could not start it."
            }
        }
    }

    FileView {
        id: jobFile
        path: root.stateDir + "/job.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: { try { root.syncJob(JSON.parse(text())) } catch (e) {} }
    }

    // Fill the Add app form from a repo: inspect-repo for a typed path, pick-folder for the
    // desktop's folder chooser (its own process; see cmd_pick_folder for why).
    function applyRepoGuess(r) {
        root.addAppNotes = r.notes || []
        root.addAppNotesOk = !!r.ok
        var f = r.fields || {}
        if (f.source === "package") {
            nameField.text = f.name || ""
            idField.text = f.id || ""
            return
        }
        if (f.repoDir !== undefined) repoField.text = f.repoDir
        if (!r.ok) return
        nameField.text = f.name || ""
        idField.text = f.id || ""
        pkgField.text = f.pkgName || ""
        branchField.text = f.branch || ""
        remoteField.text = f.remote || ""
        cmdField.text = f.updateCmd || ""
        root.addAppFields = Object.assign({}, root.addAppFields, {pullFirst: !!f.pullFirst})
    }

    Process {
        id: repoProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var r = JSON.parse(text)
                    if (r.cancelled) return
                    root.applyRepoGuess(r)
                } catch (e) {
                    root.addAppNotes = ["Could not read that repo."]
                    root.addAppNotesOk = false
                }
            }
        }
    }

    Process {
        id: planProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var r = JSON.parse(text)
                    if (!r.ok) { root.detailText = r.message || "Cannot remove it."; return }
                    if (root.detailOpenId === r.id) root.removePlan = r
                } catch (e) { root.detailText = "Could not read what it would remove." }
            }
        }
    }

    FileView {
        id: paletteFile
        path: root.home + "/.local/state/omarchy/current/theme/colors.toml"
        printErrors: false
        onLoaded: root.palette = Model.parsePalette(text())
        onLoadFailed: root.palette = ({})
    }
    // A theme switch repoints current/theme, which a file watch would not follow.
    Connections {
        target: Commons.Color
        function onAccentChanged() { paletteFile.reload() }
        function onBackgroundChanged() { paletteFile.reload() }
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
                    root.detailCommits = r.ok ? {incoming: r.incoming || [], local: r.local || []} : {incoming: [], local: []}
                    if (!r.ok) root.diffTab = "diff"
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
        property color accent: Commons.Color.accent
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
        color: !tg.enabled ? Style.normalFill : tg.checked ? Commons.Color.accent : Util.alpha(root.ink, 0.16)
        opacity: tg.enabled ? 1 : 0.5
        Rectangle {
            width: 16; height: 16; radius: 8; color: Commons.Color.background
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
        border.color: input.activeFocus ? Commons.Color.accent : Style.normalBorderColor
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
    // The Dashboard's mark: a puzzle piece, the usual sign for plugins. It wears the theme's
    // colour for the mood (Model.gaugeTone) and moves with it:
    //   calm       green, seated in its slot
    //   waiting    yellow, lifted out of its slot (tilted), the count beside it
    //   attention  red, with one shake when it turns red
    //   working    rocks on its base while a check or an action runs
    // and drops back into its slot with a bounce when updates are done -- a small pop if
    // it was already seated.
    component Puzzle: Item {
        id: pz
        property color tone: Commons.Color.accent
        property color track: Style.normalBorderColor
        property string mood: "idle"
        property bool working: false
        property bool compact: false
        implicitWidth: compact ? 22 : 48
        implicitHeight: implicitWidth

        property real lift: mood === "waiting" ? 1 : 0
        property real rock: 0
        property real pop: 1
        readonly property bool lifted: mood === "waiting"
        onLiftedChanged: {
            if (lifted) { dropIn.stop(); liftUp.restart() }
            else { liftUp.stop(); dropIn.restart() }
        }
        onMoodChanged: if (mood === "attention" && !working) shake.restart()
        onWorkingChanged: {
            if (working) return
            settle.restart()
            if (mood === "calm" && !lifted) popIn.restart()
        }
        NumberAnimation { id: liftUp; target: pz; property: "lift"; to: 1; duration: 380; easing.type: Easing.OutBack }
        NumberAnimation { id: dropIn; target: pz; property: "lift"; to: 0; duration: 700; easing.type: Easing.OutBounce }
        SequentialAnimation {
            running: pz.working
            loops: Animation.Infinite
            NumberAnimation { target: pz; property: "rock"; to: 10; duration: 260; easing.type: Easing.InOutSine }
            NumberAnimation { target: pz; property: "rock"; to: -10; duration: 520; easing.type: Easing.InOutSine }
            NumberAnimation { target: pz; property: "rock"; to: 0; duration: 260; easing.type: Easing.InOutSine }
        }
        NumberAnimation { id: settle; target: pz; property: "rock"; to: 0; duration: 200; easing.type: Easing.OutQuad }
        SequentialAnimation {
            id: shake
            NumberAnimation { target: pz; property: "rock"; to: 14; duration: 60 }
            NumberAnimation { target: pz; property: "rock"; to: -14; duration: 110 }
            NumberAnimation { target: pz; property: "rock"; to: 9; duration: 100 }
            NumberAnimation { target: pz; property: "rock"; to: -6; duration: 90 }
            NumberAnimation { target: pz; property: "rock"; to: 0; duration: 80 }
        }
        SequentialAnimation {
            id: popIn
            NumberAnimation { target: pz; property: "pop"; to: 1.18; duration: 90; easing.type: Easing.OutQuad }
            NumberAnimation { target: pz; property: "pop"; to: 1; duration: 320; easing.type: Easing.OutBack }
        }

        // The piece's outline in a 1x1 box: a tab on top and on the right, a blank (an
        // inward notch) on the left, a flat bottom to sit on.
        function trace(ctx, ox, oy, k) {
            function X(v) { return ox + v * k }
            function Y(v) { return oy + v * k }
            var x0 = 0.12, x1 = 0.76, y0 = 0.3, y1 = 0.94
            var kx = (x0 + x1) / 2, ky = (y0 + y1) / 2
            var half = 0.065, reach = 0.075, r = Math.sqrt(half * half + reach * reach) * k
            var a = Math.atan2(reach, half)
            ctx.beginPath()
            ctx.moveTo(X(x0), Y(y0))
            ctx.lineTo(X(kx - half), Y(y0))
            ctx.arc(X(kx), Y(y0 - reach), r, Math.PI - a, a, false)
            ctx.lineTo(X(x1), Y(y0))
            ctx.lineTo(X(x1), Y(ky - half))
            ctx.arc(X(x1 + reach), Y(ky), r, -Math.PI + (Math.PI / 2 - a), Math.PI - (Math.PI / 2 - a), false)
            ctx.lineTo(X(x1), Y(y1))
            ctx.lineTo(X(x0), Y(y1))
            ctx.lineTo(X(x0), Y(ky + half))
            ctx.arc(X(x0 + reach), Y(ky), r, Math.PI - (Math.PI / 2 - a), -Math.PI + (Math.PI / 2 - a), true)
            ctx.closePath()
        }

        // Where it sits: a faint outline that shows once the piece lifts out of it.
        Canvas {
            id: slot
            anchors.fill: parent
            opacity: Math.min(1, pz.lift) * 0.9
            visible: opacity > 0.01
            readonly property color track: pz.track
            onTrackChanged: requestPaint()
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.lineWidth = pz.compact ? 1 : 1.5
                ctx.strokeStyle = track
                pz.trace(ctx, 0, 0, Math.min(width, height))
                ctx.stroke()
            }
        }

        Canvas {
            id: piece
            // Bigger than the icon by the glow's reach, so the glow fades out instead of
            // being cut off square at the edges.
            readonly property real pad: pz.compact ? 3 : 5
            anchors.fill: parent
            anchors.margins: -pad
            readonly property color tone: pz.tone
            onToneChanged: requestPaint()
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            transform: [
                Rotation {
                    origin.x: piece.width / 2; origin.y: piece.height - piece.pad
                    angle: pz.rock - pz.lift * 10
                },
                Scale {
                    origin.x: piece.width / 2; origin.y: piece.height / 2
                    xScale: pz.pop; yScale: pz.pop
                },
                Translate { y: -pz.lift * pz.height * 0.2 }
            ]
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.save()
                ctx.shadowColor = tone
                ctx.shadowBlur = pz.compact ? 3 : 6
                ctx.fillStyle = tone
                pz.trace(ctx, pad, pad, Math.min(width, height) - 2 * pad)
                ctx.fill()
                ctx.restore()
            }
        }
    }
    // A plugin's icon: a rounded square in a theme color with its letter, and a dot on
    // the corner when it wants something (an update, or attention).
    component Tile: Rectangle {
        id: tile
        property string name: ""
        property color dot: "transparent"
        property bool dim: false
        width: 32; height: 32
        radius: 8
        color: Util.alpha(root.tileColor(tile.name), tile.dim ? 0.35 : 0.9)
        Text {
            anchors.centerIn: parent
            text: Model.tileLetter(tile.name)
            color: Commons.Color.background
            font.family: Style.font.family
            font.pixelSize: 15
            font.bold: true
        }
        Rectangle {
            visible: tile.dot.a > 0
            x: -3; y: -3
            width: 10; height: 10; radius: 5
            color: tile.dot
            border.width: 2
            border.color: Commons.Color.popups.background
        }
    }
    component Row_: Rectangle {
        id: row
        required property var modelData
        width: parent ? parent.width : 0
        readonly property bool busy: root.busyId === row.modelData.id
        readonly property bool detailShown: root.detailOpenId === row.modelData.id
        readonly property bool infoShown: root.infoOpenId === row.modelData.id
        readonly property var info: row.modelData.info || ({})
        readonly property bool isSelf: row.modelData.id === root.moduleName
        readonly property color accentNow: root.pillAccent(row.modelData)
        // Which destructive button is waiting for its second click ("remove"/"rollback"), if any.
        property string confirming: ""
        height: content.implicitHeight + 20
        radius: 8
        color: rowMouse.containsMouse ? Style.hoverFill : "transparent"
        Behavior on color { ColorAnimation { duration: 80 } }

        MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
        Timer { id: confirmResetTimer; interval: 4000; onTriggered: row.confirming = "" }
        onDetailShownChanged: if (!detailShown) confirming = ""
        function confirmThen(what, action) {
            if (row.confirming === what) {
                confirmResetTimer.stop()
                row.confirming = ""
                action()
            } else {
                row.confirming = what
                confirmResetTimer.restart()
            }
        }

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left; anchors.right: parent.right
            anchors.leftMargin: 8; anchors.rightMargin: 8
            height: 1
            color: Style.normalBorderColor
        }

        Tile {
            x: 8; y: 12
            name: row.modelData.name
            dim: Model.stripeKey(row.modelData) === "off"
            dot: Model.stripeKey(row.modelData) === "behind" || Model.stripeKey(row.modelData) === "attention"
                 ? root.pillAccent(row.modelData) : "transparent"
        }

        Column {
            id: content
            width: parent.width - 60
            x: 52; y: 10
            spacing: 6

            Item {
                width: parent.width
                height: Math.max(nameCol.implicitHeight, actionsRow.implicitHeight, pillBox.implicitHeight)

                Column {
                    id: nameCol
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: actionsRow.left
                    anchors.rightMargin: 10
                    Row {
                        width: parent.width
                        spacing: 6
                        Heading { id: nameText; text: row.modelData.name; font.pixelSize: Style.font.body; elide: Text.ElideRight; width: Math.min(implicitWidth, parent.width - (pillBox.visible ? pillBox.width + 6 : 0) - (localPill.visible ? localPill.width + 6 : 0) - 22) }
                        // Only when there is something to say: CURRENT on every row was a column
                        // of green that drowned out the one row that needed you.
                        Rectangle {
                            id: pillBox
                            visible: row.busy || (pillText.text !== "" && pillText.text !== "CURRENT")
                            anchors.verticalCenter: nameText.verticalCenter
                            implicitWidth: pillText.implicitWidth + 12
                            implicitHeight: 18
                            radius: 9
                            color: Util.alpha(row.accentNow, 0.16)
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
                        // Muted on purpose: local commits are information (a fork, a held fix),
                        // not a problem -- the state pill beside it carries any urgency.
                        Rectangle {
                            id: localPill
                            readonly property string label: Model.localTag(row.modelData)
                            visible: label !== "" && !row.busy
                            anchors.verticalCenter: nameText.verticalCenter
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
                        // The first thing to give way when the line is full: name and pills stay whole.
                        Text {
                            id: versionText
                            width: Math.max(0, Math.min(implicitWidth, parent.width - nameText.width - (pillBox.visible ? pillBox.width + 6 : 0) - (localPill.visible ? localPill.width + 6 : 0) - 28))
                            visible: width > 24
                            elide: Text.ElideRight
                            text: (row.modelData.version ? "v" + row.modelData.version : "")
                                  + (row.modelData.kind === "app" ? (row.modelData.version ? " · " : "") + (row.modelData.source === "package" ? "package" : "app") : "")
                            anchors.verticalCenter: nameText.verticalCenter
                            color: Util.alpha(root.ink, 0.45)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                        }
                        Text {
                            text: row.infoShown ? "▾" : "▸"
                            anchors.verticalCenter: nameText.verticalCenter
                            color: Util.alpha(root.ink, 0.45)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                        }
                    }
                    Label {
                        width: parent.width
                        visible: text !== ""
                        elide: Text.ElideRight
                        text: row.modelData.description || ""
                        color: Util.alpha(root.ink, 0.62)
                        font.pixelSize: Style.font.caption
                    }
                    Label {
                        readonly property string extra: row.busy
                              ? (Math.max(0, Math.round(root.elapsedNow - root.actionStartSec)) + "s")
                              : [row.modelData.held ? "held" + (row.modelData.holdReason && row.modelData.holdReason !== "held" ? ": " + row.modelData.holdReason : "") : "",
                                 !row.modelData.held && row.modelData.updateState === "not-installed" && row.modelData.reason ? row.modelData.reason : "",
                                 !row.modelData.held && row.modelData.updateState === "behind" ? Model.behindLabel(row.modelData) : ""]
                                .filter(function(t) { return t !== "" }).join(" · ")
                        width: parent.width
                        visible: extra !== ""
                        elide: Text.ElideRight
                        text: extra
                        color: row.busy || row.modelData.updateState === "behind" ? root.pillAccent(row.modelData) : Util.alpha(root.ink, 0.45)
                        font.pixelSize: Style.font.caption
                    }
                }
                // Over the name and subtitle only; the pills and buttons keep their own clicks.
                MouseArea {
                    anchors.fill: nameCol
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.infoOpenId = row.infoShown ? "" : row.modelData.id
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
                    // At most one button here (Update, or Unhold): with every action inline, a
                    // row that was both behind and recently updated grew Changes, Update, Roll
                    // back and Remove and ran them over its own name and pill. The rest live in
                    // the expanded row.
                    SmallButton {
                        text: row.busy ? "…" : Model.updateCaption(row.modelData)
                        visible: Model.canUpdate(row.modelData)
                        enabled: !row.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: Model.inTerminal(row.modelData) ? root.updateInTerminal([row.modelData.id]) : root.runAction("update", row.modelData.id)
                    }
                    SmallButton {
                        text: "Unhold"
                        accent: Commons.Color.muted
                        visible: !!row.modelData.held
                        enabled: !row.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: root.runAction("unhold", row.modelData.id)
                    }
                }
            }

            Column {
                visible: row.infoShown
                width: parent.width
                spacing: 4
                Repeater {
                    model: row.infoShown ? Model.infoRows(row.modelData, root.now, root.home) : []
                    Row {
                        required property var modelData
                        width: parent ? parent.width : 0
                        spacing: 8
                        Label { text: modelData[0]; width: 64; font.pixelSize: Style.font.caption; color: Util.alpha(root.ink, 0.45) }
                        Label { text: modelData[1]; width: parent.width - 72; wrapMode: Text.Wrap; font.pixelSize: Style.font.caption; color: Util.alpha(root.ink, 0.8) }
                    }
                }
                // What the row itself no longer has room for, in one Flow so it wraps
                // instead of running past the edge however many apply: review first,
                // then the links, then the destructive ones last.
                Flow {
                    width: parent.width
                    spacing: 6
                    topPadding: 2
                    SmallButton {
                        text: row.detailShown && root.detailMode === "diff" ? "Hide changes" : "Changes"
                        accent: Commons.Color.muted
                        visible: Model.canShowDiff(row.modelData)
                        onClicked: root.showDiff(row.modelData.id)
                    }
                    SmallButton {
                        text: "Hold"
                        accent: Commons.Color.muted
                        visible: Model.canHold(row.modelData)
                        enabled: root.busyId === ""
                        onClicked: root.runAction("hold", row.modelData.id)
                    }
                    SmallButton {
                        text: Model.shortUrl(row.info.webUrl).split("/").slice(0, 3).join("/") + " ↗"
                        visible: !!row.info.webUrl
                        accent: Commons.Color.muted
                        onClicked: Qt.openUrlExternally(row.info.webUrl)
                    }
                    SmallButton {
                        text: "Open folder"
                        visible: !!row.info.path
                        accent: Commons.Color.muted
                        onClicked: Quickshell.execDetached(["setsid", "uwsm-app", "--", "nautilus", "--new-window", row.info.path])
                    }
                    SmallButton {
                        text: "Terminal"
                        visible: !!row.info.path
                        accent: Commons.Color.muted
                        onClicked: Quickshell.execDetached(["setsid", "uwsm-app", "--", "xdg-terminal-exec", "--dir=" + row.info.path])
                    }
                    // Only for a plugin the Dashboard updated in the last week and that has
                    // not moved since (rollback_candidate decides); confirm like Remove,
                    // since it resets the checkout.
                    SmallButton {
                        text: row.confirming === "rollback" ? "Confirm roll back?" : "Roll back to " + (row.modelData.rollback && row.modelData.rollback.toVersion ? "v" + row.modelData.rollback.toVersion : "previous")
                        accent: Commons.Color.urgent
                        visible: !!row.modelData.rollback && !row.modelData.held
                        enabled: !row.busy && root.busyId === ""
                        onClicked: row.confirmThen("rollback", function() { root.runAction("rollback", row.modelData.id) })
                    }
                    SmallButton {
                        text: row.confirming === "remove" ? "Confirm remove?" : (row.modelData.kind === "app" ? "Stop tracking" : "Remove…")
                        accent: Commons.Color.urgent
                        visible: !row.isSelf
                        enabled: !row.busy && root.busyId === ""
                        onClicked: row.modelData.kind === "app"
                                   ? row.confirmThen("remove", function() { root.runAction("remove-app", row.modelData.id) })
                                   : root.showRemovePlan(row.modelData.id)
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
                    Item {
                        width: parent.width
                        height: detailButtons.implicitHeight
                        Label {
                            anchors.left: parent.left
                            anchors.right: detailButtons.left
                            anchors.verticalCenter: parent.verticalCenter
                            elide: Text.ElideRight
                            text: root.detailMode === "remove" ? "Remove it?"
                                  : root.detailMode === "diff" ? "Changes vs upstream" : (root.detailOk ? "Output" : "Output — failed")
                            color: (root.detailMode === "output" && !root.detailOk) || root.detailMode === "remove" ? Commons.Color.urgent : Util.alpha(root.ink, 0.62)
                            font.pixelSize: Style.font.caption
                        }
                        Row {
                            id: detailButtons
                            anchors.right: parent.right
                            spacing: 6
                            SmallButton {
                                text: "Commits"
                                visible: root.detailMode === "diff"
                                accent: root.diffTab === "commits" ? Commons.Color.accent : Commons.Color.muted
                                onClicked: root.diffTab = "commits"
                            }
                            SmallButton {
                                text: "Diff"
                                visible: root.detailMode === "diff"
                                accent: root.diffTab === "diff" ? Commons.Color.accent : Commons.Color.muted
                                onClicked: root.diffTab = "diff"
                            }
                            // Decide after seeing what is coming: holding keeps this update out
                            // of the badge, Update all and notifications until unheld.
                            SmallButton {
                                text: "Hold"
                                visible: root.detailMode === "diff" && Model.canHold(row.modelData)
                                accent: Commons.Color.muted
                                enabled: root.busyId === ""
                                onClicked: root.runAction("hold", row.modelData.id)
                            }
                            SmallButton {
                                text: (root.removePurge ? "☑" : "☐") + " Trash its data"
                                visible: root.detailMode === "remove" && !!root.removePlan && (root.removePlan.data || []).length > 0
                                accent: root.removePurge ? Commons.Color.urgent : Commons.Color.muted
                                onClicked: root.removePurge = !root.removePurge
                            }
                            SmallButton {
                                text: "Remove"
                                visible: root.detailMode === "remove"
                                accent: Commons.Color.urgent
                                enabled: !!root.removePlan && root.busyId === ""
                                onClicked: root.confirmRemove(row.modelData.id)
                            }
                            SmallButton { text: root.detailMode === "remove" ? "Cancel" : "Close"; accent: Commons.Color.muted; onClicked: root.closeDetail() }
                        }
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
                                  : (root.detailMode === "diff" && root.diffTab === "commits"
                                     ? Model.commitsHtml(root.detailCommits, {
                                           text: String(root.ink),
                                           meta: String(Util.alpha(root.ink, 0.5)),
                                           head: String(Commons.Color.accent)
                                       }, root.now)
                                     : root.detailMode === "diff"
                                     ? Model.diffHtml(root.detailText, {
                                           add: String(Commons.Color.accent),
                                           del: String(Commons.Color.urgent),
                                           meta: String(Util.alpha(root.ink, 0.45)),
                                           text: String(root.ink)
                                       })
                                     : root.detailMode === "remove" && root.removePlan
                                     ? Model.removePlanLines(root.removePlan, root.removePurge, root.home).map(function(l) { return l.indexOf("   ") === 0 ? l : "· " + l }).join("\n")
                                     : root.detailText)
                            color: Util.alpha(root.ink, 0.85)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                            wrapMode: root.detailMode === "diff" && root.diffTab === "commits" ? Text.Wrap : Text.WrapAnywhere
                            textFormat: root.detailMode === "diff" ? Text.RichText : Text.PlainText
                        }
                    }
                }
            }
        }
    }

    // A catalog entry in Discover: name, who and how liked, what it does, and Install when
    // `omarchy plugin add` can install it alone (the repo link otherwise). Install asks
    // first, in the row, with what installing a plugin means.
    component DiscoverRow: Rectangle {
        id: drow
        required property var modelData
        width: ListView.view ? ListView.view.width : 0
        readonly property bool isInstalled: Model.discoverInstalled(drow.modelData, root.installedKeys)
        readonly property bool open: root.discoverOpenId === drow.modelData.id
        readonly property bool busy: root.busyId === drow.modelData.id
        readonly property bool confirming: root.installConfirmId === drow.modelData.id
        readonly property bool outputShown: root.detailOpenId === drow.modelData.id && root.detailMode === "output"
        readonly property color pillColor: drow.isInstalled || drow.modelData.verification === "verified" && drow.modelData.installable
                                           ? root.paletteColor("green", Commons.Color.accent) : Util.alpha(root.ink, 0.45)
        height: dcontent.implicitHeight + 20
        radius: 8
        color: dMouse.containsMouse ? Style.hoverFill : "transparent"

        MouseArea { id: dMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left; anchors.right: parent.right
            anchors.leftMargin: 8; anchors.rightMargin: 8
            height: 1
            color: Style.normalBorderColor
        }

        Tile {
            x: 8; y: 12
            name: drow.modelData.name
        }

        Column {
            id: dcontent
            width: parent.width - 60
            x: 52; y: 10
            spacing: 6

            Item {
                width: parent.width
                height: Math.max(dNameCol.implicitHeight, dActions.implicitHeight, dPill.implicitHeight)

                Column {
                    id: dNameCol
                    anchors.left: parent.left
                    anchors.right: dPill.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    Row {
                        width: parent.width
                        spacing: 6
                        Heading { id: dName; text: drow.modelData.name; font.pixelSize: Style.font.body; elide: Text.ElideRight; width: Math.min(implicitWidth, parent.width - 16) }
                        Text {
                            text: drow.open ? "▾" : "▸"
                            anchors.verticalCenter: dName.verticalCenter
                            color: Util.alpha(root.ink, 0.45)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                        }
                    }
                    Label {
                        width: parent.width
                        elide: Text.ElideRight
                        text: drow.busy ? (Math.max(0, Math.round(root.elapsedNow - root.actionStartSec)) + "s") : Model.discoverMeta(drow.modelData)
                        font.pixelSize: Style.font.caption
                    }
                }
                MouseArea {
                    anchors.fill: dNameCol
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.discoverOpenId = drow.open ? "" : drow.modelData.id
                }
                Rectangle {
                    id: dPill
                    anchors.right: dActions.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: dPillText.implicitWidth + 12
                    implicitHeight: 18
                    radius: 9
                    color: Util.alpha(drow.pillColor, 0.16)
                    Text {
                        id: dPillText
                        anchors.centerIn: parent
                        text: drow.busy ? "INSTALLING" : Model.discoverPill(drow.modelData, drow.isInstalled)
                        color: drow.pillColor
                        font.family: Style.font.family
                        font.pixelSize: 9
                        font.bold: true
                        textFormat: Text.PlainText
                    }
                }
                Row {
                    id: dActions
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6
                    SmallButton {
                        text: drow.busy ? "…" : "Install…"
                        visible: drow.modelData.installable && !drow.isInstalled
                        enabled: !drow.busy && root.busyId === ""
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: { root.installConfirmId = drow.confirming ? "" : drow.modelData.id; root.discoverOpenId = drow.modelData.id }
                    }
                }
            }

            Label {
                width: parent.width
                visible: text !== ""
                text: drow.modelData.description
                wrapMode: Text.WordWrap
                maximumLineCount: drow.open ? 12 : 2
                elide: Text.ElideRight
                font.pixelSize: Style.font.caption
                color: Util.alpha(root.ink, 0.75)
            }

            Column {
                visible: drow.open
                width: parent.width
                spacing: 4
                Repeater {
                    model: drow.open ? Model.discoverInfoRows(drow.modelData, root.now) : []
                    Row {
                        required property var modelData
                        width: parent ? parent.width : 0
                        spacing: 8
                        Label { text: modelData[0]; width: 64; font.pixelSize: Style.font.caption; color: Util.alpha(root.ink, 0.45) }
                        Label { text: modelData[1]; width: parent.width - 72; wrapMode: Text.Wrap; font.pixelSize: Style.font.caption; color: Util.alpha(root.ink, 0.8) }
                    }
                }
                // The catalog's screenshot, fetched only once the row is opened.
                Rectangle {
                    visible: !!drow.modelData.preview
                    width: parent.width
                    height: visible ? Math.round(width / (drow.modelData.previewAspect || 1.778)) : 0
                    radius: 6
                    color: Style.normalFill
                    border.color: Style.normalBorderColor
                    clip: true
                    Image {
                        id: previewImage
                        anchors.fill: parent
                        anchors.margins: 1
                        source: drow.open && drow.modelData.preview ? drow.modelData.preview : ""
                        asynchronous: true
                        cache: true
                        fillMode: Image.PreserveAspectFit
                        sourceSize.width: Math.ceil(width * 2)
                    }
                    Label {
                        anchors.centerIn: parent
                        visible: previewImage.status !== Image.Ready
                        text: previewImage.status === Image.Error ? "No preview" : "Loading preview…"
                        font.pixelSize: Style.font.caption
                    }
                }
                Flow {
                    width: parent.width
                    spacing: 6
                    topPadding: 2
                    SmallButton {
                        text: Model.shortUrl(drow.modelData.repo) + " ↗"
                        visible: !!drow.modelData.repo
                        accent: Commons.Color.muted
                        onClicked: Qt.openUrlExternally(drow.modelData.repo)
                    }
                    SmallButton {
                        text: "Show in list"
                        visible: drow.isInstalled
                        accent: Commons.Color.muted
                        onClicked: { root.infoOpenId = drow.modelData.id; root.showView("installed") }
                    }
                }
            }

            // The same warning `omarchy plugin add` gives before it clones anything.
            Rectangle {
                visible: drow.confirming
                width: parent.width
                height: visible ? confirmCol.implicitHeight + 16 : 0
                radius: 8
                color: Util.alpha(Commons.Color.urgent, 0.08)
                border.color: Util.alpha(Commons.Color.urgent, 0.5)
                Column {
                    id: confirmCol
                    width: parent.width - 16
                    x: 8; y: 8
                    spacing: 6
                    Label {
                        width: parent.width
                        wrapMode: Text.WordWrap
                        color: Util.alpha(root.ink, 0.85)
                        text: "Plugins run as unsandboxed code inside your shell; install only ones you trust. "
                              + "This installs what " + Model.shortUrl(drow.modelData.repo) + " has now"
                              + (drow.modelData.verification === "verified" ? ", which the catalog has checked." : ", which the catalog has not checked.")
                              + " It is turned on in the bar where it asks to go."
                    }
                    Row {
                        spacing: 6
                        SmallButton {
                            text: "Install"
                            enabled: root.busyId === ""
                            onClicked: root.runInstall(drow.modelData.id)
                        }
                        SmallButton { text: "Cancel"; accent: Commons.Color.muted; onClicked: root.installConfirmId = "" }
                    }
                }
            }

            Rectangle {
                visible: drow.outputShown || (drow.busy && root.detailText !== "")
                width: parent.width
                height: visible ? Math.min(outText.implicitHeight, 160) + 36 : 0
                radius: 8
                color: Style.normalFill
                border.color: Style.normalBorderColor
                clip: true
                Item {
                    id: outHead
                    x: 8; y: 6
                    width: parent.width - 16
                    height: outClose.implicitHeight
                    Label {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.detailOk ? "Output" : "Output — failed"
                        color: root.detailOk ? Util.alpha(root.ink, 0.62) : Commons.Color.urgent
                        font.pixelSize: Style.font.caption
                    }
                    SmallButton { id: outClose; anchors.right: parent.right; text: "Close"; accent: Commons.Color.muted; onClicked: root.closeDetail() }
                }
                Flickable {
                    x: 8; y: outHead.y + outHead.height + 4
                    width: parent.width - 16
                    height: parent.height - y - 6
                    contentWidth: width
                    contentHeight: outText.implicitHeight
                    clip: true
                    Text {
                        id: outText
                        width: parent.width
                        text: root.detailText
                        color: Util.alpha(root.ink, 0.85)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WrapAnywhere
                        textFormat: Text.PlainText
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
            // The piece on the bar answers "is anything worth a look" before the panel is
            // opened; the count beside it says how many updates are waiting.
            Puzzle {
                id: barIcon
                anchors.verticalCenter: parent.verticalCenter
                compact: true
                implicitWidth: Style.bar.iconCanvas + 2
                implicitHeight: Style.bar.iconCanvas + 2
                tone: root.gaugeTone
                track: Util.alpha(root.barForeground, 0.4)
                mood: root.gaugeToneName
                working: root.working
            }
            Text {
                id: barCount
                visible: root.updatable > 0
                anchors.verticalCenter: parent.verticalCenter
                text: String(root.updatable)
                color: root.gaugeTone
                font.family: Style.font.family
                font.pixelSize: 12
                font.bold: true
                textFormat: Text.PlainText
                SequentialAnimation on opacity {
                    running: barCount.visible
                    loops: Animation.Infinite
                    NumberAnimation { from: 1.0; to: 0.5; duration: 900; easing.type: Easing.InOutQuad }
                    NumberAnimation { from: 0.5; to: 1.0; duration: 900; easing.type: Easing.InOutQuad }
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
            // paints the themed surface (Commons.Color.popups.background, Style.cornerRadius,
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

                    Puzzle {
                        id: ring
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        tone: root.gaugeTone
                        mood: root.gaugeToneName
                        working: root.working
                    }
                    Row {
                        id: headerButtons
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 8
                        SmallButton {
                            text: root.updateQueueTotal > 0 ? ("Updating " + root.updateQueueDone + "/" + root.updateQueueTotal + "…") : "Update all (" + root.updatableAll + ")"
                            visible: root.view === "installed" && (root.updatableAll > 1 || root.updateQueueTotal > 0)
                            enabled: root.updateQueueTotal === 0 && root.busyId === "" && !checkProc.running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.runUpdateAll()
                        }
                        SmallButton {
                            readonly property bool running: root.view === "discover" ? catalogProc.running : checkProc.running
                            text: running ? "…" : "Refresh"
                            enabled: !running
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.view === "discover" ? root.runCatalog(true) : root.runCheck(true)
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
                            text: root.view === "discover" ? "Discover" : "Plugins"; font.pixelSize: Style.font.heading
                            elide: Text.ElideRight
                        }
                        Label {
                            width: parent.width
                            text: root.view === "discover"
                                  ? (root.catalogPlugins.length > 0 ? root.catalogPlugins.length + " in the catalog · " + Model.relativeAge(root.catalog.ts, root.now)
                                                                    : "The official plugin catalog")
                                  : root.items.length > 0 ? Model.installedSummary(root.items) : "Third-party plugins and apps"
                            font.pixelSize: Style.font.caption
                            elide: Text.ElideRight
                        }
                    }
                }

                // Scrollable so a growing plugin/app list is capped instead of
                // pushing the APPS section (and the footer) off the bottom of
                // the screen -- KeyboardPanel clamps the popup's own height to
                // the screen, but content inside it isn't clipped on its own.
                Row {
                    spacing: 6
                    SmallButton {
                        text: "Installed"
                        accent: root.view === "installed" ? Commons.Color.accent : Commons.Color.muted
                        onClicked: root.showView("installed")
                    }
                    SmallButton {
                        text: "Discover"
                        accent: root.view === "discover" ? Commons.Color.accent : Commons.Color.muted
                        onClicked: root.showView("discover")
                    }
                }

                // Discover: the catalog, searched and sorted here; a ListView, since it has
                // thousands of entries and only a screenful are ever drawn.
                Column {
                    width: parent.width
                    visible: root.view === "discover"
                    spacing: 8
                    Item {
                        width: parent.width
                        height: searchField.height
                        FieldInput {
                            id: searchField
                            anchors.left: parent.left
                            anchors.right: discoverFilters.left
                            anchors.rightMargin: 6
                            placeholder: "Search plugins"
                            onTextChanged: { root.discoverQuery = text; discoverDebounce.restart() }
                        }
                        Row {
                            id: discoverFilters
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 6
                            SmallButton {
                                text: (root.discoverInstallableOnly ? "☑" : "☐") + " Installable"
                                accent: Commons.Color.muted
                                onClicked: { root.discoverInstallableOnly = !root.discoverInstallableOnly; root.refilterDiscover() }
                            }
                            SmallButton {
                                text: root.discoverSort === "new" ? "Newest" : "Popular"
                                accent: Commons.Color.muted
                                onClicked: { root.discoverSort = root.discoverSort === "new" ? "popular" : "new"; root.refilterDiscover() }
                            }
                        }
                    }
                    Label {
                        width: parent.width
                        wrapMode: Text.WordWrap
                        font.pixelSize: Style.font.caption
                        color: root.catalogError ? Commons.Color.urgent : Util.alpha(root.ink, 0.62)
                        text: root.catalogError
                              || (root.catalogPlugins.length === 0 ? (catalogProc.running ? "Loading the plugin catalog…" : "")
                                  : root.discoverResults.length + (root.discoverResults.length === 1 ? " plugin" : " plugins")
                                    + (root.discoverQuery ? " match" + (root.discoverResults.length === 1 ? "es" : "") : ""))
                    }
                    ListView {
                        id: discoverList
                        width: parent.width
                        height: Math.min(contentHeight, Style.space(420))
                        clip: true
                        spacing: 2
                        boundsBehavior: Flickable.StopAtBounds
                        model: root.view === "discover" ? root.discoverResults : []
                        delegate: DiscoverRow {}
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    }
                }

                Flickable {
                    id: listScroll
                    visible: root.view === "installed"
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
                            spacing: 2
                            Item {
                                width: parent.width
                                height: pluginsLabel.implicitHeight
                                Label {
                                    id: pluginsLabel
                                    text: "PLUGINS"; font.pixelSize: Style.font.caption; font.letterSpacing: 1.5
                                    anchors.verticalCenter: parent.verticalCenter
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
                            spacing: 2
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

                        // What removed plugins left behind (dashboard.py leftovers): omarchy's
                        // backup folders, services still pointing at a plugin that is gone, and
                        // data a removal kept. Clean up sends folders to the trash.
                        Column {
                            width: parent.width
                            visible: root.leftovers.length > 0
                            spacing: 6
                            Item {
                                width: parent.width
                                height: leftoversLabel.implicitHeight + 4
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.leftoversOpen = !root.leftoversOpen
                                }
                                Label {
                                    id: leftoversLabel
                                    text: "LEFTOVERS  " + root.leftovers.length + "  ·  " +
                                          Model.formatSize(root.leftovers.reduce(function(t, l) { return t + (l.size || 0) }, 0))
                                    font.pixelSize: Style.font.caption
                                    font.letterSpacing: 1.5
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Label {
                                    text: root.leftoversOpen ? "hide" : "show"
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    font.pixelSize: Style.font.caption
                                }
                            }
                            Repeater {
                                model: root.leftoversOpen ? root.leftovers : []
                                Item {
                                    required property var modelData
                                    width: parent ? parent.width : 0
                                    height: Math.max(leftoverText.implicitHeight, leftoverButton.implicitHeight)
                                    Label {
                                        id: leftoverText
                                        anchors.left: parent.left
                                        anchors.right: leftoverButton.left
                                        anchors.rightMargin: 8
                                        anchors.verticalCenter: parent.verticalCenter
                                        elide: Text.ElideMiddle
                                        text: modelData.label + (modelData.size ? "  ·  " + Model.formatSize(modelData.size) : "")
                                        font.pixelSize: Style.font.caption
                                    }
                                    SmallButton {
                                        id: leftoverButton
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: root.busyId === modelData.key ? "…" : "Clean up"
                                        accent: Commons.Color.urgent
                                        enabled: root.busyId === ""
                                        onClicked: root.cleanupLeftovers([modelData.key])
                                    }
                                }
                            }
                            SmallButton {
                                visible: root.leftoversOpen && root.leftovers.length > 1
                                text: "Clean up all (" + root.leftovers.length + ")"
                                accent: Commons.Color.urgent
                                enabled: root.busyId === ""
                                onClicked: root.cleanupLeftovers(root.leftovers.map(function(l) { return l.key }))
                            }
                        }

                        Column {
                            width: parent.width
                            visible: root.grouped.apps.length > 0 || root.addAppFormOpen
                            spacing: 2
                            Label { text: "APPS"; font.pixelSize: Style.font.caption; font.letterSpacing: 1.5 }
                            Repeater {
                                model: root.grouped.apps
                                Row_ {}
                            }

                            SmallButton {
                                text: root.addAppFormOpen ? "Cancel" : "+ Add app"
                                accent: root.addAppFormOpen ? Commons.Color.urgent : Commons.Color.accent
                                onClicked: { root.addAppFormOpen = !root.addAppFormOpen; root.addAppNotes = [] }
                            }

                            Column {
                                width: parent.width
                                visible: root.addAppFormOpen
                                spacing: 6
                                Row {
                                    spacing: 6
                                    SmallButton {
                                        text: "Source repo"
                                        accent: root.addAppSource === "repo" ? Commons.Color.accent : Commons.Color.muted
                                        onClicked: { root.addAppSource = "repo"; root.addAppNotes = [] }
                                    }
                                    SmallButton {
                                        text: "Installed package"
                                        accent: root.addAppSource === "package" ? Commons.Color.accent : Commons.Color.muted
                                        onClicked: { root.addAppSource = "package"; root.addAppNotes = [] }
                                    }
                                }
                                // A package (brave-bin): Fill in reads where it comes from and how it
                                // updates -- with the system for a repo package, yay for an AUR one.
                                Item {
                                    width: parent.width
                                    height: pkgLookupField.height
                                    visible: root.addAppSource === "package"
                                    FieldInput {
                                        id: pkgLookupField
                                        anchors.left: parent.left
                                        anchors.right: pkgLookupButton.left
                                        anchors.rightMargin: 6
                                        placeholder: "Installed package, e.g. brave-bin"
                                    }
                                    SmallButton {
                                        id: pkgLookupButton
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: "Fill in"
                                        accent: Commons.Color.muted
                                        enabled: !repoProc.running && pkgLookupField.text.length > 0
                                        onClicked: { repoProc.command = ["python3", root.helper, "inspect-package", pkgLookupField.text]; repoProc.running = true }
                                    }
                                }
                                // The repo comes first: Choose… or Fill in reads it and guesses the rest
                                // (name, id, package from .SRCINFO, upstream branch and remote, rebuild.sh).
                                Item {
                                    width: parent.width
                                    height: repoField.height
                                    visible: root.addAppSource === "repo"
                                    FieldInput {
                                        id: repoField
                                        anchors.left: parent.left
                                        anchors.right: repoButtons.left
                                        anchors.rightMargin: 6
                                        placeholder: "Repo dir, e.g. /home/you/Projects/myapp"
                                        onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {repoDir: text})
                                    }
                                    Row {
                                        id: repoButtons
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        spacing: 6
                                        SmallButton {
                                            text: "Choose…"
                                            accent: Commons.Color.muted
                                            enabled: !repoProc.running
                                            onClicked: { repoProc.command = ["python3", root.helper, "pick-folder"]; repoProc.running = true }
                                        }
                                        SmallButton {
                                            text: "Fill in"
                                            accent: Commons.Color.muted
                                            enabled: !repoProc.running && repoField.text.length > 0
                                            onClicked: { repoProc.command = ["python3", root.helper, "inspect-repo", repoField.text]; repoProc.running = true }
                                        }
                                    }
                                }
                                FieldInput { id: nameField; placeholder: "Display name"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {name: text}) }
                                FieldInput { id: idField; placeholder: "id (lowercase, no spaces)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {id: text}) }
                                FieldInput { id: pkgField; visible: root.addAppSource === "repo"; placeholder: "Package name (defaults to id)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {pkgName: text}) }
                                Row {
                                    width: parent.width
                                    spacing: 6
                                    visible: root.addAppSource === "repo"
                                    FieldInput { id: branchField; width: (parent.width - 6) / 2; placeholder: "Upstream branch (default master)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {branch: text}) }
                                    FieldInput { id: remoteField; width: (parent.width - 6) / 2; placeholder: "Remote (default origin)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {remote: text}) }
                                }
                                FieldInput { id: cmdField; visible: root.addAppSource === "repo"; text: "./rebuild.sh --install"; placeholder: "Update command (builds and installs it)"; onTextChanged: root.addAppFields = Object.assign({}, root.addAppFields, {updateCmd: text}) }
                                // Off for a rebuild.sh, which merges upstream itself; on for a command
                                // that only builds (make install), or it would rebuild the old code.
                                SmallButton {
                                    visible: root.addAppSource === "repo"
                                    text: (root.addAppFields.pullFirst ? "☑" : "☐") + " Pull upstream first"
                                    accent: Commons.Color.muted
                                    onClicked: root.addAppFields = Object.assign({}, root.addAppFields, {pullFirst: !root.addAppFields.pullFirst})
                                }
                                Repeater {
                                    model: root.addAppNotes
                                    Label {
                                        required property var modelData
                                        width: parent ? parent.width : 0
                                        wrapMode: Text.WordWrap
                                        text: "· " + modelData
                                        font.pixelSize: Style.font.caption
                                        color: root.addAppNotesOk ? Util.alpha(root.ink, 0.62) : Commons.Color.urgent
                                    }
                                }
                                SmallButton { text: "Save"; enabled: root.busyId === ""; onClicked: root.submitAddApp() }
                            }
                        }

                        Column {
                            width: parent.width
                            visible: root.history.length > 0
                            spacing: 6
                            Item {
                                width: parent.width
                                height: recentLabel.implicitHeight + 4
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.recentOpen = !root.recentOpen
                                }
                                Label {
                                    id: recentLabel
                                    text: "RECENT  " + root.history.length
                                    font.pixelSize: Style.font.caption
                                    font.letterSpacing: 1.5
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Label {
                                    text: root.recentOpen ? "hide" : "show"
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    font.pixelSize: Style.font.caption
                                }
                            }
                            Repeater {
                                model: root.recentOpen ? root.history : []
                                Row {
                                    required property var modelData
                                    width: parent ? parent.width : 0
                                    spacing: 8
                                    Text {
                                        text: Model.historyMark(modelData)
                                        color: modelData.ok ? (modelData.action === "rollback" ? Commons.Color.urgent : Commons.Color.accent) : Commons.Color.urgent
                                        font.family: Style.font.family
                                        font.pixelSize: Style.font.bodySmall
                                        textFormat: Text.PlainText
                                    }
                                    Label {
                                        width: parent.width - 20
                                        elide: Text.ElideRight
                                        text: Model.historyLine(modelData, root.now) + (!modelData.ok && modelData.message ? " — " + modelData.message : "")
                                        font.pixelSize: Style.font.caption
                                    }
                                }
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
                    id: footerStatus
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.pixelSize: Style.font.caption
                    text: (root.busyId === "" && !checkProc.running ? root.actionStatus : "") ||
                          (checkProc.running ? "Checking sources…" :
                           "Last checked " + Model.relativeAge(root.status.ts, root.now))
                }
            }
        }
    }
}
