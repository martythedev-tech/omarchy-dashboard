.pragma library

// True only for the one state Panel.qml's Update button ever fires on --
// "dirty", "diverged", "no-repo" and "unreachable" all need a person, not a
// click. A plugin with local edits or commits is still 'behind' when they merge
// cleanly onto the update (dashboard.py carries them over); 'dirty'/'diverged'
// now mean they clash with it.
//
// 'not-installed' is the other one: an app whose repo is current but whose installed
// package is older than what the repo builds. Its updateCmd (rebuild.sh --install)
// builds and installs whether or not upstream moved, so the same button applies.
//
// A held item is neither: holding it is saying "not this update", so it leaves the badge
// and Update all, and loses its own button until it is unheld.
function isUpdatableState(item) {
    return !!item && (item.updateState === 'behind' || item.updateState === 'not-installed')
}

function canUpdate(item) {
    return isUpdatableState(item) && !item.held
}

// Whether Update all takes this item. Under 'verified' a plugin goes only when its update
// is the commit the marketplace verified (unlisted, or unknown, stays); apps are not in
// the marketplace and always go. dashboard.py in_update_all is the same rule.
function inUpdateAll(item, scope) {
    if (!canUpdate(item)) return false
    if (scope !== 'verified' || item.kind === 'app') return true
    return !!item.market && item.market.incoming === 'verified'
}

// Hold is offered where it means something: an update is waiting that you do not want yet.
function canHold(item) {
    return canUpdate(item)
}

// True for anything a diff preview is meaningful for: 'behind' shows what
// the Update button would apply, 'diverged' shows what's making it refuse.
// Not for a package app: there is no checkout to diff, only a version.
function canShowDiff(item) {
    return !!item && item.source !== 'package' && (item.updateState === 'behind' || item.updateState === 'diverged')
}

// A package app (pacman or AUR) updates in a terminal, where sudo can ask -- never as a
// background job, so never in Update all's job either.
function inTerminal(item) {
    return !!item && item.source === 'package'
}

// The Update button's caption: a repo package is updated by updating the whole system.
function updateCaption(item) {
    if (!inTerminal(item)) return 'Update'
    return item.updateVia === 'system' ? 'Update system…' : 'Update…'
}

// Whether an item belongs in the bar badge (and its pulse): updatable, not held, and
// not known to leave its version unchanged -- a README or screenshot commit is still an
// update in the panel, just not one to flag from the bar. Mirrors alerts() in dashboard.py.
function alerts(item) {
    return canUpdate(item) && item.versionChange !== false
}

function badgeCount(items) {
    var n = 0
    for (var i = 0; i < (items || []).length; i++) if (alerts(items[i])) n++
    return n
}

// What a behind row adds to its subtitle. The pill already says how many commits
// ("BEHIND 1"), so this says only what they would do to the version -- short, because the
// subtitle shares its line with the pill, the switch and the Update button. How far behind
// is the fallback when the incoming version could not be read.
function behindLabel(item) {
    if (!item || item.updateState !== 'behind') return ''
    if (item.versionChange === false) return 'same version'
    if (item.upstreamVersion) return '→ v' + item.upstreamVersion
    return stateLabel(item)
}

// What the row's status line says. Kept here rather than inline in QML so
// tests/js/test_model.cjs can pin every wording without a running shell.
function stateLabel(item) {
    if (!item) return ''
    var why = item.reason ? ' (' + item.reason.split('\n')[0] + ')' : ''
    switch (item.updateState) {
        case 'behind':
            if (item.source === 'package') return 'Update available' + (item.upstreamVersion ? ': ' + item.upstreamVersion : '')
            return item.behind + (item.behind === 1 ? ' commit behind' : ' commits behind')
        case 'dirty': return 'Local changes' + why + ' -- update manually'
        case 'diverged': return 'Local commits ahead' + why + ' -- update manually'
        // A git operation (rebase/merge/cherry-pick/bisect) left mid-flight --
        // the exact state a Dashboard-triggered rebuild.sh can leave a repo in
        // when it hits a conflict. Update/Diff must both refuse this the same
        // way they refuse 'dirty'/'diverged': see canUpdate/canShowDiff below.
        case 'in-progress': return 'Git operation in progress' + why + ' -- resolve manually'
        case 'not-installed': return 'Built, not installed' + why
        case 'no-repo': return 'No update source' + why
        case 'unreachable': return 'Could not reach source' + (item.reason ? ': ' + item.reason.split('\n')[0] : '')
        case 'up-to-date': return 'Up to date'
        default: return ''
    }
}

// The header ring's numerator/denominator: every item with a real update
// source (kept simple, so an app/plugin with no git checkout at all doesn't
// drag down or inflate a number meant to answer "is anything worth looking
// at"), and how many of those are confirmed current as of the last check.
function healthFraction(items) {
    var total = 0, done = 0
    for (var i = 0; i < (items || []).length; i++) {
        if (items[i].updateState === 'no-repo') continue
        // A plugin you turned off is not "unhealthy" — it is out of the ring.
        if (items[i].kind !== 'app' && items[i].enabled === false) continue
        // Same for one you are holding back on purpose.
        if (items[i].held) continue
        total++
        if (items[i].updateState === 'up-to-date') done++
    }
    return { done: done, total: total }
}

// Where a row sorts within its section: what needs a person first (a stuck git
// operation, local changes or commits blocking an update), then what one click would
// update, then a source that could not be reached, then everything else. Held items
// sort with everything else -- holding is saying "not now".
function statusRank(item) {
    if (!item) return 9
    if (item.held) return 3
    switch (item.updateState) {
        case 'in-progress': case 'dirty': case 'diverged': return 0
        case 'behind': case 'not-installed': return 1
        case 'unreachable': return 2
        default: return 3
    }
}

// Plugins, disabled plugins and apps, each sorted by statusRank then name -- a stable
// split so the sections in Panel.qml never need their own sort call.
function groupByKind(items) {
    var plugins = [], disabled = [], apps = []
    for (var i = 0; i < (items || []).length; i++) {
        var it = items[i]
        if (it.kind === 'app') apps.push(it)
        else if (it.enabled === false) disabled.push(it)
        else plugins.push(it)
    }
    function byName(a, b) { return a.name < b.name ? -1 : a.name > b.name ? 1 : 0 }
    function byStatusThenName(a, b) { return (statusRank(a) - statusRank(b)) || byName(a, b) }
    plugins.sort(byStatusThenName)
    disabled.sort(byName)  // off is off; nothing in here is waiting on anyone
    apps.sort(byStatusThenName)
    return { plugins: plugins, disabled: disabled, apps: apps }
}

function pillLabel(item) {
    if (!item) return ''
    if (item.enabled === false) return 'OFF'
    if (item.held) return 'HELD'
    switch (item.updateState) {
        case 'behind': return item.source === 'package' ? 'UPDATE' : 'BEHIND ' + item.behind
        case 'up-to-date': return 'CURRENT'
        case 'diverged': return 'DIVERGED'
        case 'dirty': return 'DIRTY'
        case 'in-progress': return 'GIT'
        case 'not-installed': return 'NOT INSTALLED'
        case 'no-repo': return 'NO SOURCE'
        case 'unreachable': return 'OFFLINE'
        default: return ''
    }
}

// The "+N LOCAL" tag next to the state pill: commits this checkout carries that upstream
// does not. CURRENT/BEHIND alone said nothing about them, so a 92-commit fork looked the
// same as an untouched install. Not shown for 'diverged', whose pill already says it.
// "EDITED" is the same for uncommitted edits, which an update now carries over too; not
// shown for 'dirty', whose pill already says it.
function localTag(item) {
    if (!item) return ''
    var parts = []
    if (item.ahead > 0 && item.updateState !== 'diverged') parts.push('+' + item.ahead + ' LOCAL')
    if (item.edits > 0 && item.updateState !== 'dirty') parts.push('EDITED')
    return parts.join(' · ')
}

function stripeKey(item) {
    if (!item) return 'ok'
    if (item.enabled === false || item.held) return 'off'
    if (item.updateState === 'behind' && item.versionChange === false) return 'ok'
    if (item.updateState === 'behind' || item.updateState === 'not-installed') return 'behind'
    if (item.updateState === 'dirty' || item.updateState === 'diverged' || item.updateState === 'in-progress') return 'attention'
    return 'ok'
}

function statusFresh(ts, nowSeconds, maxAge) {
    if (!ts) return false
    var age = (nowSeconds || (Date.now() / 1000)) - ts
    return age >= 0 && age < (maxAge || 300)
}

function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function diffHtml(text, colors) {
    colors = colors || {}
    var add = colors.add || '#3dd68c'
    var del = colors.del || '#e05d44'
    var meta = colors.meta || '#888888'
    var ink = colors.text || '#cccccc'
    var lines = String(text || '').split('\n')
    var out = []
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i]
        var color = ink
        if (line.indexOf('@@') === 0) color = meta
        else if (line.charAt(0) === '+' && line.indexOf('+++') !== 0) color = add
        else if (line.charAt(0) === '-' && line.indexOf('---') !== 0) color = del
        else if (line.indexOf('diff ') === 0 || line.indexOf('index ') === 0) color = meta
        out.push('<span style="color:' + color + '">' + escapeHtml(line) + '</span>')
    }
    return out.join('<br/>')
}

function relativeAge(ts, nowSeconds) {
    if (!ts) return 'never checked'
    var secs = Math.max(0, (nowSeconds || (Date.now() / 1000)) - ts)
    if (secs < 90) return 'just now'
    var mins = Math.round(secs / 60)
    if (mins < 60) return mins + ' min ago'
    var hours = Math.round(mins / 60)
    if (hours < 24) return hours + (hours === 1 ? ' hour ago' : ' hours ago')
    var days = Math.round(hours / 24)
    return days + (days === 1 ? ' day ago' : ' days ago')
}

// The "what's new" list: upstream's commits an update would bring, then (for a plugin
// that has them) the local commits upstream lacks. Rich text for the detail panel.
function commitsHtml(r, colors, nowSeconds) {
    colors = colors || {}
    var ink = colors.text || '#cccccc'
    var meta = colors.meta || '#888888'
    var head = colors.head || ink
    var incoming = (r && r.incoming) || [], local = (r && r.local) || []
    function section(title, list) {
        var out = ['<span style="color:' + head + '"><b>' + escapeHtml(title) + '</b></span>']
        for (var i = 0; i < list.length; i++) {
            var c = list[i]
            out.push('<span style="color:' + meta + '">' + escapeHtml(c.hash) + '</span>  '
                     + '<span style="color:' + ink + '">' + escapeHtml(c.subject) + '</span>'
                     + '<span style="color:' + meta + '">  · ' + escapeHtml(c.author) + ', '
                     + relativeAge(c.ts, nowSeconds) + '</span>')
        }
        return out.join('<br/>')
    }
    var parts = []
    if (incoming.length) parts.push(section('Upstream · ' + incoming.length + (incoming.length === 1 ? ' commit' : ' commits'), incoming))
    if (local.length) parts.push(section('Only here · ' + local.length + (local.length === 1 ? ' commit' : ' commits'), local))
    return parts.length ? parts.join('<br/><br/>') : '<span style="color:' + meta + '">(no commits to list)</span>'
}

// One line of the Recent section.
function historyLine(e, nowSeconds) {
    if (!e) return ''
    var name = e.name || e.id
    var versions = e.fromVersion && e.toVersion && e.fromVersion !== e.toVersion ? ' ' + e.fromVersion + ' → ' + e.toVersion : ''
    var what
    if (e.action === 'remove') what = e.ok ? 'removed' : 'removed, with problems'
    else if (e.action === 'install') what = e.ok ? 'installed' + (e.toVersion ? ' ' + e.toVersion : '') : 'install failed'
    else if (e.action === 'rollback') what = (e.ok ? 'rolled back' : 'rollback failed') + versions
    // Same version: an app's update really did rebuild and reinstall it; a plugin's only
    // pulled commits that left the version alone (docs, tooling).
    else if (!e.ok) what = 'update failed'
    else if (versions) what = 'updated' + versions
    // A package app's update is the system's: one that left it alone did not rebuild anything.
    else if (e.source === 'package') what = 'not changed by the update'
    else what = e.kind === 'app' ? 'rebuilt, no version change' : 'updated, same version'
    return name + ' ' + what + ' · ' + relativeAge(e.ts, nowSeconds)
}

function historyMark(e) {
    if (!e) return ''
    if (!e.ok) return '✗'
    if (e.action === 'remove') return '−'
    if (e.action === 'install') return '+'
    return e.action === 'rollback' ? '↩' : '✓'
}

// The expanded row's facts, as [label, value] pairs; empty values are left out.
function infoRows(item, nowSeconds, home) {
    if (!item) return []
    var info = item.info || {}
    var rows = []
    var status = stateLabel(item)
    if (item.held) status += ' · held' + (item.holdReason && item.holdReason !== 'held' ? ' (' + item.holdReason + ')' : '')
    if (item.ahead > 0) status += ' · ' + item.ahead + (item.ahead === 1 ? ' local commit' : ' local commits')
    rows.push(['Status', status])
    if (item.source === 'package') {
        rows.push(['From', info.repo === 'AUR' ? 'the AUR' : 'the ' + info.repo + ' repo'])
        rows.push(['Updates', item.updateVia === 'system' ? 'with the system (omarchy update), in a terminal' : 'yay -S, in a terminal'])
    }
    if (item.shortcut) rows.push(['Shortcut', item.shortcut])
    var market = marketLine(item)
    if (market) rows.push(['Checked', market])
    if (info.branch) rows.push(['Branch', info.branch])
    if (info.upstreamTs) rows.push(['Upstream', 'last commit ' + relativeAge(info.upstreamTs, nowSeconds)])
    if (info.path) rows.push(['Folder', home && info.path.indexOf(home + '/') === 0 ? '~' + info.path.slice(home.length) : info.path])
    return rows
}

// Keyboard shortcuts. A plugin can have one when the shell can open something of it:
// a bar widget's popup, a panel or an overlay (dashboard.py SHORTCUT_KINDS).
function canShortcut(item) {
    if (!item || item.kind === 'app' || item.enabled === false) return false
    var kinds = item.kinds || []
    return kinds.indexOf('bar-widget') >= 0 || kinds.indexOf('panel') >= 0 || kinds.indexOf('overlay') >= 0
}

// A key press, as Qt reports it, turned into a combination: {combo} when complete,
// {held} while only modifiers are down, {error} for a key a shortcut cannot use. Qt's
// key and modifier values are spelled out so this needs no Qt object (and can be tested).
function comboFromKey(key, modifiers) {
    var mods = []
    if (modifiers & 0x10000000) mods.push('SUPER')
    if (modifiers & 0x04000000) mods.push('CTRL')
    if (modifiers & 0x08000000) mods.push('ALT')
    if (modifiers & 0x02000000) mods.push('SHIFT')
    var name = ''
    if (key >= 0x41 && key <= 0x5a) name = String.fromCharCode(key)
    else if (key >= 0x30 && key <= 0x39) name = String.fromCharCode(key)
    else if (key >= 0x01000030 && key <= 0x0100003b) name = 'F' + (key - 0x01000030 + 1)
    else if ((key >= 0x01000020 && key <= 0x01000023) || key === 0x01000053 || key === 0x01000054)
        return { held: mods.join(' + ') }
    if (!name) return { error: 'Use a letter, a digit or F1-F12.' }
    if (mods.indexOf('SUPER') < 0 && mods.indexOf('CTRL') < 0 && mods.indexOf('ALT') < 0)
        return { error: 'Hold Super, Ctrl or Alt with it.' }
    return { combo: mods.concat([name]).join(' + ') }
}

// The marketplace badge: about the update when one is waiting (that is the decision in
// front of you), otherwise about the code installed. '' for a plugin it does not list.
function marketPill(item) {
    var m = item && item.market
    if (!m) return ''
    if (item.updateState === 'behind' && m.incoming)
        return m.incoming === 'verified' ? 'UPDATE VERIFIED' : 'UPDATE UNVERIFIED'
    return m.installed === 'verified' ? 'VERIFIED' : 'UNVERIFIED'
}

// The expanded row's Marketplace line: what was checked and how your copy relates to it.
function marketLine(item) {
    if (!item || item.kind === 'app' || item.market === undefined) return ''
    var m = item.market
    if (m === null) return 'Not listed in the marketplace'
    var line
    if (m.installed === 'verified') {
        line = 'Verified: this is the commit its security check passed (' + m.checked + ')'
        if (item.edits > 0) line += ', but your uncommitted edits are not covered'
    } else if (!m.checked) {
        line = 'Unverified: its security check has not passed any commit'
    } else if (m.sinceChecked > 0) {
        line = 'Unverified: ' + m.sinceChecked + (m.sinceChecked === 1 ? ' commit' : ' commits')
             + ' past the one its security check passed (' + m.checked + ')'
    } else {
        line = 'Unverified: not the commit its security check passed (' + m.checked + ')'
    }
    if (item.updateState === 'behind' && m.incoming)
        line += m.incoming === 'verified' ? '. The update is the verified commit.' : '. The update is not verified either.'
    return line
}

// The source link as shown: host and path, no scheme.
function shortUrl(url) {
    return String(url || '').replace(/^https?:\/\//, '')
}

// The Update all summary, from [{id, name, ok}] in the order they ran.
function queueSummary(results, title, verb) {
    results = results || []
    var ok = 0, failed = []
    for (var i = 0; i < results.length; i++) {
        if (results[i].ok) ok++
        else failed.push(results[i].name || results[i].id)
    }
    var parts = []
    if (ok) parts.push(ok + ' ' + (verb || 'updated'))
    if (failed.length) parts.push(failed.length + ' failed (' + failed.join(', ') + ')')
    return parts.length ? (title || 'Update all') + ': ' + parts.join(', ') + '.' : ''
}

// The actions that run as a job (dashboard.py JOB_KINDS): each changes a plugin's
// directory, and the shell answers that by rebuilding this widget mid-action.
function isJobKind(kind) {
    return kind === 'update' || kind === 'rollback' || kind === 'remove' || kind === 'cleanup' || kind === 'install'
}

// What job.json means for this panel instance. busy: show it going (whichever instance
// started it -- this one may have been rebuilt since). apply: show how it ended, once,
// and only for a job this instance saw going or one that has only just finished, never
// an old one found on load.
var JOB_FRESH_SECONDS = 15
function jobView(job, seenJobId, appliedJobId, nowSeconds) {
    if (!job || !job.jobId) return {busy: false, apply: false}
    var ids = job.ids || [], results = job.results || []
    if (job.state === 'starting' || job.state === 'running') {
        return {busy: true, apply: false, id: job.current || ids[0] || '', kind: job.kind || 'update',
                total: ids.length, done: results.length, startSec: job.itemTs || job.startTs || nowSeconds}
    }
    var apply = job.state === 'done' && job.jobId !== appliedJobId &&
                (job.jobId === seenJobId || nowSeconds - (job.finishedTs || 0) < JOB_FRESH_SECONDS)
    return {busy: false, apply: apply, kind: job.kind || 'update', total: ids.length, results: results}
}

function formatSize(bytes) {
    var b = Number(bytes) || 0
    if (b < 1024) return b + ' B'
    if (b < 1024 * 1024) return Math.round(b / 1024) + ' KB'
    if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(b < 10 * 1024 * 1024 ? 1 : 0) + ' MB'
    return (b / 1024 / 1024 / 1024).toFixed(1) + ' GB'
}

function shortPath(path, home) {
    path = String(path || '')
    return home && path.indexOf(home + '/') === 0 ? '~' + path.slice(home.length) : path
}

// What Remove will do, as lines for the confirmation panel (dashboard.py remove-plan).
function removePlanLines(plan, purge, home) {
    if (!plan) return []
    var lines = []
    var folder = plan.folder || {}
    var where = shortPath(folder.path, home)
    if (folder.kind === 'link') lines.push('Unlink ' + where + ' (the folder it points to stays).')
    else if (folder.kind === 'git') lines.push('Delete ' + where + ' (its source stays upstream).')
    else lines.push('Move ' + where + ' to a backup (it is not a git checkout).')
    var units = plan.units || []
    if (units.length) lines.push('Stop and remove ' + units.join(', ') + '.')
    var data = plan.data || []
    if (data.length) {
        var total = 0
        for (var i = 0; i < data.length; i++) total += data[i].size || 0
        lines.push((purge ? 'Move its data to the trash' : 'Keep its data (Leftovers will list it)') +
                   ' — ' + formatSize(total) + ':')
        for (var j = 0; j < data.length; j++) lines.push('   ' + shortPath(data[j].path, home) + '  ' + formatSize(data[j].size))
    } else {
        lines.push('No data folders of its own found.')
    }
    var backups = plan.backups || []
    if (backups.length) lines.push(backups.length + ' old backup' + (backups.length === 1 ? '' : 's') + ' of it stay' +
                                   (backups.length === 1 ? 's' : '') + ' in Leftovers.')
    return lines
}

// The theme's named colours from colors.toml (`red = "#f7768e"`); {} when unreadable.
function parsePalette(raw) {
    var out = {}, lines = String(raw || '').split('\n')
    for (var i = 0; i < lines.length; i++) {
        var m = /^\s*([A-Za-z0-9_]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/.exec(lines[i])
        if (m) out[m[1].toLowerCase()] = m[2]
    }
    return out
}

// The gauge's mood. attention: something needs a person (local changes, a git operation
// half done, diverged, or a recent action that failed and the panel has not been opened
// since); waiting: updates in the badge; calm: all current; idle: nothing checked yet.
var ATTENTION_STATES = ['dirty', 'diverged', 'in-progress']
var FAILURE_FRESH_SECONDS = 24 * 3600
function gaugeTone(items, history, nowSeconds, seenTs) {
    items = items || []
    if (!items.length) return 'idle'
    for (var i = 0; i < items.length; i++) {
        if (items[i] && !items[i].held && ATTENTION_STATES.indexOf(items[i].updateState) !== -1) return 'attention'
    }
    var last = (history || [])[0]
    if (last && !last.ok && (last.ts || 0) > (seenTs || 0) && nowSeconds - (last.ts || 0) < FAILURE_FRESH_SECONDS) return 'attention'
    return badgeCount(items) > 0 ? 'waiting' : 'calm'
}

// Theme colour for a tone, falling back to what the shell always has.
function toneColor(tone, palette, fallback) {
    palette = palette || {}
    fallback = fallback || {}
    if (tone === 'attention') return palette.red || fallback.urgent
    if (tone === 'waiting') return palette.yellow || palette.orange || fallback.accent
    if (tone === 'calm') return palette.green || fallback.accent
    return fallback.muted
}

// Discover (dashboard.py catalog). What counts as already installed: a plugin here under
// the catalog's id, or one whose source is the catalog entry's repo (a fork renamed).
function normRepo(url) {
    return String(url || '').toLowerCase().replace(/\.git$/, '').replace(/\/+$/, '')
}

function installedKeys(items) {
    var ids = {}, urls = {}
    for (var i = 0; i < (items || []).length; i++) {
        var it = items[i]
        if (!it || it.kind === 'app') continue
        ids[it.id] = true
        var url = it.info && it.info.webUrl
        if (url) urls[normRepo(url)] = true
    }
    return {ids: ids, urls: urls}
}

function discoverInstalled(entry, keys) {
    if (!entry || !keys) return false
    return !!(keys.ids[entry.id] || (entry.repo && keys.urls[normRepo(entry.repo)]))
}

// One lowercase string per entry to search in, built once per catalog load rather than on
// every keystroke.
function discoverIndex(plugins) {
    var out = []
    for (var i = 0; i < (plugins || []).length; i++) {
        var p = plugins[i]
        out.push([p.name, p.id, p.description, p.author, p.category, p.kind, (p.tags || []).join(' ')].join(' ').toLowerCase())
    }
    return out
}

// The entries every word of the query appears in, popular (stars) or newest (listed) first.
// installableOnly drops what `omarchy plugin add` cannot install on its own.
function discoverList(plugins, index, query, opts) {
    plugins = plugins || []
    opts = opts || {}
    var words = String(query || '').toLowerCase().split(/\s+/).filter(function(w) { return w.length > 0 })
    var out = []
    for (var i = 0; i < plugins.length; i++) {
        var p = plugins[i]
        if (opts.installableOnly && !p.installable) continue
        var hay = index && index[i] !== undefined ? index[i] : discoverIndex([p])[0]
        var all = true
        for (var w = 0; w < words.length; w++) if (hay.indexOf(words[w]) === -1) { all = false; break }
        if (all) out.push(p)
    }
    function byName(a, b) { var x = a.name.toLowerCase(), y = b.name.toLowerCase(); return x < y ? -1 : x > y ? 1 : 0 }
    if (opts.sort === 'new') out.sort(function(a, b) { return a.added < b.added ? 1 : a.added > b.added ? -1 : byName(a, b) })
    else out.sort(function(a, b) { return (b.stars || 0) - (a.stars || 0) || byName(a, b) })
    return out
}

function discoverPill(entry, installed) {
    if (!entry) return ''
    if (installed) return 'INSTALLED'
    if (!entry.installable) return 'MANUAL SETUP'
    if (entry.verification === 'verified') return 'VERIFIED'
    if (entry.verification === 'update-unverified') return 'UPDATE UNVERIFIED'
    return 'UNVERIFIED'
}

// The line under the name: how liked, who, what -- stars first, so a long author name is
// what gets cut off, not them.
function discoverMeta(entry) {
    if (!entry) return ''
    var parts = []
    if (entry.stars) parts.push('★ ' + entry.stars)
    if (entry.author) parts.push(entry.author)
    if (entry.category) parts.push(entry.category)
    return parts.join(' · ')
}

function isoSeconds(text) {
    var t = Date.parse(text || '')
    return isNaN(t) ? 0 : t / 1000
}

// The expanded Discover row's facts, as [label, value] pairs.
function discoverInfoRows(entry, nowSeconds) {
    if (!entry) return []
    var rows = []
    if (entry.version) rows.push(['Version', entry.version])
    if (entry.kind) rows.push(['Kind', entry.kind])
    var updated = isoSeconds(entry.updated)
    if (updated) rows.push(['Updated', relativeAge(updated, nowSeconds)])
    if (entry.added) rows.push(['Listed', entry.added.slice(0, 10)])
    var check = entry.verification === 'verified' ? 'the catalog checked its current code'
              : entry.verification === 'update-unverified' ? 'changed since the catalog last checked it'
              : 'not checked by the catalog'
    rows.push(['Checked', check])
    if (!entry.installable && entry.note) rows.push(['Install', entry.note])
    return rows
}

// The row's icon tile: a letter, since plugin manifests carry no icon. "Omarchy Doctor"
// and "Omarchy Multiroom Speakers" would both be "O", so a leading "Omarchy" is skipped.
function tileLetter(name) {
    var words = String(name || '').trim().split(/\s+/)
    if (words.length > 1 && /^omarchy$/i.test(words[0])) words.shift()
    var ch = (words[0] || '?').charAt(0)
    return ch ? ch.toUpperCase() : '?'
}

// A stable pick from n colors for a name, so a plugin keeps its tile color across checks.
function tileIndex(name, n) {
    var s = String(name || ''), h = 0
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
    return n > 0 ? Math.abs(h) % n : 0
}

// The header's second line on Installed: counts, then what needs doing.
function installedSummary(items) {
    var plugins = 0, apps = 0, waiting = 0
    for (var i = 0; i < (items || []).length; i++) {
        var it = items[i]
        if (it.kind === 'app') apps++
        else plugins++
        if (stripeKey(it) === 'behind') waiting++
    }
    // Updates first: with Update all beside it the line is short, and the counts can elide.
    var parts = [waiting ? waiting + (waiting === 1 ? ' update waiting' : ' updates waiting') : 'All current',
                 plugins + (plugins === 1 ? ' plugin' : ' plugins')]
    if (apps) parts.push(apps + (apps === 1 ? ' app' : ' apps'))
    return parts.join(' · ')
}

// The bar layout editor. Sections in bar order; the two outer-center ones only when they
// hold something (Omarchy's default bar leaves them empty), and never the tray's drawer,
// which the tray itself draws.
function barSections(layout) {
    layout = layout || {}
    var out = ['left']
    if ((layout.centerBegin || []).length) out.push('centerBegin')
    out.push('center')
    if ((layout.centerEnd || []).length) out.push('centerEnd')
    out.push('right')
    return out
}

function barSectionLabel(section) {
    return { left: 'LEFT', centerBegin: 'CENTER START', center: 'CENTER', centerEnd: 'CENTER END', right: 'RIGHT' }[section] || section.toUpperCase()
}

// The placement `omarchy-shell shell moveBarWidget` takes for one step: up or down within
// the section, or into the neighbouring section -- at its end going left, at its start
// going right, so a widget walks across the bar in order. null when there is no step.
// The index is where it lands once taken out of its old place (the shell removes first).
function barMove(layout, sections, section, index, dir) {
    var list = (layout || {})[section] || []
    if (index < 0 || index >= list.length) return null
    var place = { fromSection: section, fromIndex: index }
    if (dir === 'up' || dir === 'down') {
        var to = dir === 'up' ? index - 1 : index + 1
        if (to < 0 || to >= list.length) return null
        place.section = section
        place.index = to
        return place
    }
    var si = sections.indexOf(section)
    var ti = dir === 'left' ? si - 1 : si + 1
    if (si < 0 || ti < 0 || ti >= sections.length) return null
    place.section = sections[ti]
    place.index = dir === 'left' ? ((layout || {})[sections[ti]] || []).length : 0
    return place
}
