.pragma library

// True only for the one state Panel.qml's Update button ever fires on --
// "dirty", "diverged", "no-repo" and "unreachable" all need a person, not a
// click, because each would fail (or silently do the wrong thing) if
// `omarchy plugin update`'s fast-forward-only merge ran anyway.
//
// 'not-installed' is the other one: an app whose repo is current but whose installed
// package is older than what the repo builds. Its updateCmd (rebuild.sh --install)
// builds and installs whether or not upstream moved, so the same button applies.
function canUpdate(item) {
    return !!item && (item.updateState === 'behind' || item.updateState === 'not-installed')
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
        total++
        if (items[i].updateState === 'up-to-date') done++
    }
    return { done: done, total: total }
}

// Plugins first (alphabetical), then apps (alphabetical) -- a stable split
// so the two sections in Panel.qml never need their own sort call.
function groupByKind(items) {
    var plugins = [], disabled = [], apps = []
    for (var i = 0; i < (items || []).length; i++) {
        var it = items[i]
        if (it.kind === 'app') apps.push(it)
        else if (it.enabled === false) disabled.push(it)
        else plugins.push(it)
    }
    function byName(a, b) { return a.name < b.name ? -1 : a.name > b.name ? 1 : 0 }
    plugins.sort(byName)
    disabled.sort(byName)
    apps.sort(byName)
    return { plugins: plugins, disabled: disabled, apps: apps }
}

function pillLabel(item) {
    if (!item) return ''
    if (item.enabled === false) return 'OFF'
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
    if (item.enabled === false) return 'off'
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
