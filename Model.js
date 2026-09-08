.pragma library

// True only for the one state Panel.qml's Update button ever fires on --
// "dirty", "no-repo" and "unreachable" all need a person, not a click.
function canUpdate(item) {
    return !!item && item.updateState === 'behind'
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
    switch (item.updateState) {
        case 'behind': return item.behind + (item.behind === 1 ? ' commit behind' : ' commits behind')
        case 'dirty': return 'Local changes -- update manually'
        case 'no-repo': return 'No update source'
        case 'unreachable': return 'Could not reach source'
        case 'up-to-date': return 'Up to date'
        default: return ''
    }
}

// Plugins first (alphabetical), then apps (alphabetical) -- a stable split
// so the two sections in Panel.qml never need their own sort call.
function groupByKind(items) {
    var plugins = [], apps = []
    for (var i = 0; i < (items || []).length; i++) {
        var it = items[i]
        if (it.kind === 'app') apps.push(it)
        else plugins.push(it)
    }
    function byName(a, b) { return a.name < b.name ? -1 : a.name > b.name ? 1 : 0 }
    plugins.sort(byName)
    apps.sort(byName)
    return { plugins: plugins, apps: apps }
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
