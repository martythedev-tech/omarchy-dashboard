.pragma library

// True only for the one state Panel.qml's Update button ever fires on --
// "dirty", "diverged", "no-repo" and "unreachable" all need a person, not a
// click, because each would fail (or silently do the wrong thing) if
// `omarchy plugin update`'s fast-forward-only merge ran anyway.
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

// Hold is offered where it means something: an update is waiting that you do not want yet.
function canHold(item) {
    return canUpdate(item)
}

// True for anything a diff preview is meaningful for: 'behind' shows what
// the Update button would apply, 'diverged' shows what's making it refuse.
function canShowDiff(item) {
    return !!item && (item.updateState === 'behind' || item.updateState === 'diverged')
}

function badgeCount(items) {
    var n = 0
    for (var i = 0; i < (items || []).length; i++) if (canUpdate(items[i])) n++
    return n
}

// What the row's status line says. Kept here rather than inline in QML so
// tests/js/test_model.cjs can pin every wording without a running shell.
function stateLabel(item) {
    if (!item) return ''
    var why = item.reason ? ' (' + item.reason.split('\n')[0] + ')' : ''
    switch (item.updateState) {
        case 'behind': return item.behind + (item.behind === 1 ? ' commit behind' : ' commits behind')
        case 'dirty': return 'Local changes' + why + ' -- update manually'
        case 'diverged': return 'Local commits ahead' + why + ' -- update manually'
        // A git operation (rebase/merge/cherry-pick/bisect) left mid-flight --
        // the exact state a Dashboard-triggered rebuild.sh can leave a repo in
        // when it hits a conflict. Update/Diff must both refuse this the same
        // way they refuse 'dirty'/'diverged': see canUpdate/canShowDiff below.
        case 'in-progress': return 'Git operation in progress' + why + ' -- resolve manually'
        case 'not-installed': return 'Built, not installed' + why
        case 'no-repo': return 'No update source'
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
        case 'behind': return 'BEHIND ' + item.behind
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
function localTag(item) {
    if (!item || !(item.ahead > 0) || item.updateState === 'diverged') return ''
    return '+' + item.ahead + ' LOCAL'
}

function stripeKey(item) {
    if (!item) return 'ok'
    if (item.enabled === false || item.held) return 'off'
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
    if (e.action === 'rollback') what = (e.ok ? 'rolled back' : 'rollback failed') + versions
    else what = e.ok ? (versions ? 'updated' + versions : 'rebuilt, no version change') : 'update failed'
    return name + ' ' + what + ' · ' + relativeAge(e.ts, nowSeconds)
}

function historyMark(e) {
    if (!e) return ''
    if (!e.ok) return '✗'
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
    if (info.branch) rows.push(['Branch', info.branch])
    if (info.upstreamTs) rows.push(['Upstream', 'last commit ' + relativeAge(info.upstreamTs, nowSeconds)])
    if (info.path) rows.push(['Folder', home && info.path.indexOf(home + '/') === 0 ? '~' + info.path.slice(home.length) : info.path])
    return rows
}

// The source link as shown: host and path, no scheme.
function shortUrl(url) {
    return String(url || '').replace(/^https?:\/\//, '')
}

// The Update all summary, from [{id, name, ok}] in the order they ran.
function queueSummary(results) {
    results = results || []
    var ok = 0, failed = []
    for (var i = 0; i < results.length; i++) {
        if (results[i].ok) ok++
        else failed.push(results[i].name || results[i].id)
    }
    var parts = []
    if (ok) parts.push(ok + ' updated')
    if (failed.length) parts.push(failed.length + ' failed (' + failed.join(', ') + ')')
    return parts.length ? 'Update all: ' + parts.join(', ') + '.' : ''
}
