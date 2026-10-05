const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const ctx = {Date: Date};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'Model.js'), 'utf8').replace('.pragma library', ''), ctx);

// canUpdate / canShowDiff / badgeCount
assert.equal(ctx.canUpdate({updateState: 'behind'}), true);
assert.equal(ctx.canUpdate({updateState: 'dirty'}), false);
assert.equal(ctx.canUpdate({updateState: 'diverged'}), false);
assert.equal(ctx.canUpdate({updateState: 'no-repo'}), false);
assert.equal(ctx.canUpdate({updateState: 'in-progress'}), false);
assert.equal(ctx.canUpdate(null), false);
assert.equal(ctx.canShowDiff({updateState: 'behind'}), true);
assert.equal(ctx.canShowDiff({updateState: 'diverged'}), true);
assert.equal(ctx.canShowDiff({updateState: 'dirty'}), false);
assert.equal(ctx.canShowDiff({updateState: 'in-progress'}), false);
assert.equal(ctx.canShowDiff({updateState: 'up-to-date'}), false);
assert.equal(ctx.canShowDiff(null), false);
assert.equal(ctx.badgeCount([{updateState: 'behind'}, {updateState: 'up-to-date'}, {updateState: 'behind'}]), 2);
assert.equal(ctx.badgeCount([]), 0);
assert.equal(ctx.badgeCount(undefined), 0);

// stateLabel
assert.equal(ctx.stateLabel({updateState: 'behind', behind: 1}), '1 commit behind');
assert.equal(ctx.stateLabel({updateState: 'behind', behind: 4}), '4 commits behind');
assert.equal(ctx.stateLabel({updateState: 'dirty'}), 'Local changes -- update manually');
assert.equal(ctx.stateLabel({updateState: 'dirty', reason: '2 file(s) changed locally'}), 'Local changes (2 file(s) changed locally) -- update manually');
assert.equal(ctx.stateLabel({updateState: 'diverged'}), 'Local commits ahead -- update manually');
assert.equal(ctx.stateLabel({updateState: 'diverged', reason: '1 local commit(s) not upstream'}), 'Local commits ahead (1 local commit(s) not upstream) -- update manually');
assert.equal(ctx.stateLabel({updateState: 'in-progress', reason: 'rebase onto cc65416'}), 'Git operation in progress (rebase onto cc65416) -- resolve manually');
assert.equal(ctx.stateLabel({updateState: 'in-progress'}), 'Git operation in progress -- resolve manually');
assert.equal(ctx.stateLabel({updateState: 'no-repo'}), 'No update source');
assert.equal(ctx.stateLabel({updateState: 'unreachable'}), 'Could not reach source');
assert.equal(ctx.stateLabel({updateState: 'unreachable', reason: 'no network\nmore detail'}), 'Could not reach source: no network');
assert.equal(ctx.stateLabel({updateState: 'up-to-date'}), 'Up to date');
assert.equal(ctx.stateLabel(null), '');

// healthFraction: the header ring's numerator/denominator
var h1 = ctx.healthFraction([{updateState: 'up-to-date'}, {updateState: 'behind'}, {updateState: 'no-repo'}]);
assert.equal(h1.done, 1);
assert.equal(h1.total, 2);
var h0 = ctx.healthFraction([]);
assert.equal(h0.done, 0);
assert.equal(h0.total, 0);
var hAllRepoless = ctx.healthFraction([{updateState: 'no-repo'}]);
assert.equal(hAllRepoless.total, 0);
// Disabled plugins do not drag the ring down — you turned them off on purpose.
var hOff = ctx.healthFraction([
    {updateState: 'up-to-date', enabled: true},
    {updateState: 'behind', enabled: false},
    {updateState: 'up-to-date', enabled: false},
    {kind: 'app', updateState: 'up-to-date'},
]);
assert.equal(hOff.done, 2);
assert.equal(hOff.total, 2);

// groupByKind: plugins vs apps, each alphabetical, independent of input order
var grouped = ctx.groupByKind([
    {kind: 'app', name: 'Howdy'},
    {kind: 'plugin', name: 'VPN'},
    {kind: 'app', name: 'Flea'},
    {kind: 'plugin', name: 'Mail Glance'},
]);
// .join, not deepEqual: arrays built inside the vm context are a different
// realm's Array, so deepStrictEqual's identity check on them fails even when
// the contents genuinely match -- comparing the joined primitive sidesteps it.
assert.equal(grouped.plugins.map(function (i) { return i.name }).join(','), 'Mail Glance,VPN');
assert.equal(grouped.apps.map(function (i) { return i.name }).join(','), 'Flea,Howdy');
assert.equal(ctx.groupByKind([]).plugins.length, 0);
assert.equal(ctx.groupByKind([]).apps.length, 0);
var split = ctx.groupByKind([
    {kind: 'plugin', name: 'VPN', enabled: true},
    {kind: 'plugin', name: 'CPU Pulse', enabled: false},
    {kind: 'plugin', name: 'Workspace Glance', enabled: false},
    {kind: 'app', name: 'Flea'},
]);
assert.equal(split.plugins.map(function (i) { return i.name }).join(','), 'VPN');
assert.equal(split.disabled.map(function (i) { return i.name }).join(','), 'CPU Pulse,Workspace Glance');
assert.equal(split.apps.map(function (i) { return i.name }).join(','), 'Flea');
assert.equal(ctx.groupByKind([]).disabled.length, 0);

// pillLabel: short status for the row chip
assert.equal(ctx.pillLabel({updateState: 'behind', behind: 6}), 'BEHIND 6');
assert.equal(ctx.pillLabel({updateState: 'behind', behind: 1}), 'BEHIND 1');
assert.equal(ctx.pillLabel({updateState: 'up-to-date'}), 'CURRENT');
assert.equal(ctx.pillLabel({updateState: 'diverged'}), 'DIVERGED');
assert.equal(ctx.pillLabel({updateState: 'dirty'}), 'DIRTY');
assert.equal(ctx.pillLabel({updateState: 'in-progress'}), 'GIT');
assert.equal(ctx.pillLabel({updateState: 'no-repo'}), 'NO SOURCE');
assert.equal(ctx.pillLabel({updateState: 'unreachable'}), 'OFFLINE');
assert.equal(ctx.pillLabel({updateState: 'up-to-date', enabled: false}), 'OFF');
assert.equal(ctx.pillLabel(null), '');

// stripeKey: left-edge color role, not a hex
assert.equal(ctx.stripeKey({updateState: 'behind'}), 'behind');
assert.equal(ctx.stripeKey({updateState: 'dirty'}), 'attention');
assert.equal(ctx.stripeKey({updateState: 'diverged'}), 'attention');
assert.equal(ctx.stripeKey({updateState: 'in-progress'}), 'attention');
assert.equal(ctx.stripeKey({updateState: 'up-to-date'}), 'ok');
assert.equal(ctx.stripeKey({updateState: 'up-to-date', enabled: false}), 'off');
assert.equal(ctx.stripeKey(null), 'ok');

// statusFresh: skip a network check when the last one is still young
assert.equal(ctx.statusFresh(1000, 1100, 300), true);
assert.equal(ctx.statusFresh(1000, 1400, 300), false);
assert.equal(ctx.statusFresh(0, 1100, 300), false);
assert.equal(ctx.statusFresh(undefined, 1100, 300), false);

// diffHtml: color + / - / @@ lines; escape HTML in the payload
var html = ctx.diffHtml('diff --git a b\n@@ -1 +1 @@\n-old <tag>\n+new & ok\n context', {
    add: '#3dd68c', del: '#e05d44', meta: '#888888', text: '#cccccc'
});
assert.equal(html.indexOf('#3dd68c') >= 0, true);
assert.equal(html.indexOf('#e05d44') >= 0, true);
assert.equal(html.indexOf('#888888') >= 0, true);
assert.equal(html.indexOf('&lt;tag&gt;') >= 0, true);
assert.equal(html.indexOf('&amp; ok') >= 0, true);
assert.equal(html.indexOf('<tag>') >= 0, false);

// relativeAge
var now = 1000000;
assert.equal(ctx.relativeAge(0, now), 'never checked');
assert.equal(ctx.relativeAge(now - 30, now), 'just now');
assert.equal(ctx.relativeAge(now - 300, now), '5 min ago');
assert.equal(ctx.relativeAge(now - 3600, now), '1 hour ago');
assert.equal(ctx.relativeAge(now - 7200, now), '2 hours ago');
assert.equal(ctx.relativeAge(now - 86400, now), '1 day ago');
assert.equal(ctx.relativeAge(now - 172800, now), '2 days ago');


// not-installed: an app whose repo is current but whose package is older
assert.equal(ctx.canUpdate({updateState: 'not-installed'}), true);
assert.equal(ctx.canShowDiff({updateState: 'not-installed'}), false);
assert.equal(ctx.badgeCount([{updateState: 'not-installed'}, {updateState: 'behind'}]), 2);
assert.equal(ctx.pillLabel({kind: 'app', updateState: 'not-installed'}), 'NOT INSTALLED');
assert.equal(ctx.stripeKey({kind: 'app', updateState: 'not-installed'}), 'behind');
assert.equal(ctx.stateLabel({updateState: 'not-installed', reason: 'repo has 0.3.1-2, installed is 0.3.0-1'}),
             'Built, not installed (repo has 0.3.1-2, installed is 0.3.0-1)');

// localTag
assert.equal(ctx.localTag({updateState: 'up-to-date', ahead: 92}), '+92 LOCAL');
assert.equal(ctx.localTag({updateState: 'behind', ahead: 1}), '+1 LOCAL');
assert.equal(ctx.localTag({updateState: 'diverged', ahead: 1}), '');
assert.equal(ctx.localTag({updateState: 'up-to-date', ahead: 0}), '');
assert.equal(ctx.localTag({updateState: 'up-to-date'}), '');
assert.equal(ctx.localTag(null), '');
// holds: out of the badge, ring and Update all; pill and stripe go quiet
assert.equal(ctx.canUpdate({updateState: 'behind', held: true}), false);
assert.equal(ctx.canHold({updateState: 'behind'}), true);
assert.equal(ctx.canHold({updateState: 'behind', held: true}), false);
assert.equal(ctx.canHold({updateState: 'up-to-date'}), false);
assert.equal(ctx.badgeCount([{updateState: 'behind', held: true}, {updateState: 'behind'}]), 1);
assert.equal(ctx.pillLabel({kind: 'plugin', enabled: true, updateState: 'behind', behind: 2, held: true}), 'HELD');
assert.equal(ctx.pillLabel({kind: 'plugin', enabled: false, held: true}), 'OFF');
assert.equal(ctx.stripeKey({kind: 'plugin', updateState: 'behind', held: true}), 'off');
var hh = ctx.healthFraction([{updateState: 'behind', held: true}, {updateState: 'up-to-date'}]);
assert.equal(hh.done, 1); assert.equal(hh.total, 1);

// commitsHtml
var ch = ctx.commitsHtml({incoming: [{hash: 'abc1234', subject: 'Fix <thing>', author: 'nix', ts: 1000}],
                          local: [{hash: 'def5678', subject: 'Local fix', author: 'me', ts: 1000}]}, {}, 1000 + 7200);
assert.ok(ch.indexOf('Upstream · 1 commit') >= 0);
assert.ok(ch.indexOf('Only here · 1 commit') >= 0);
assert.ok(ch.indexOf('Fix &lt;thing&gt;') >= 0);
assert.ok(ch.indexOf('2 hours ago') >= 0);
assert.ok(ctx.commitsHtml({incoming: [], local: []}).indexOf('no commits') >= 0);
assert.ok(ctx.commitsHtml(null).indexOf('no commits') >= 0);

// historyLine / historyMark
var now = 100000;
assert.equal(ctx.historyLine({name: 'Omastorm', action: 'update', ok: true, fromVersion: '0.1.15', toVersion: '0.1.16', ts: now - 30}, now),
             'Omastorm updated 0.1.15 → 0.1.16 · just now');
assert.equal(ctx.historyLine({id: 'flea', kind: 'app', action: 'update', ok: true, fromVersion: '1', toVersion: '1', ts: now}, now),
             'flea rebuilt, no version change · just now');
assert.equal(ctx.historyLine({name: 'Omastorm', kind: 'plugin', action: 'update', ok: true, fromVersion: '0.1.16', toVersion: '0.1.16', ts: now}, now),
             'Omastorm updated, same version · just now');
assert.equal(ctx.historyLine({name: 'X', action: 'update', ok: false, ts: now}, now), 'X update failed · just now');
assert.equal(ctx.historyLine({name: 'X', action: 'rollback', ok: true, fromVersion: '2', toVersion: '1', ts: now}, now),
             'X rolled back 2 → 1 · just now');
assert.equal(ctx.historyMark({ok: true, action: 'update'}), '✓');
assert.equal(ctx.historyMark({ok: true, action: 'rollback'}), '↩');
assert.equal(ctx.historyMark({ok: false, action: 'update'}), '✗');

// statusRank / groupByKind order: needs-a-person, then updatable, then unreachable, then the rest
var g = ctx.groupByKind([
    {kind: 'plugin', name: 'A', updateState: 'up-to-date'},
    {kind: 'plugin', name: 'B', updateState: 'behind', behind: 1},
    {kind: 'plugin', name: 'C', updateState: 'dirty'},
    {kind: 'plugin', name: 'D', updateState: 'unreachable'},
    {kind: 'plugin', name: 'E', updateState: 'behind', held: true},
    {kind: 'plugin', name: 'F', updateState: 'diverged'},
    {kind: 'app', name: 'Alpha', updateState: 'up-to-date'},
    {kind: 'app', name: 'Zeta', updateState: 'not-installed'},
]);
assert.deepEqual(JSON.parse(JSON.stringify(g.plugins.map(function (x) { return x.name }))), ['C', 'F', 'B', 'D', 'A', 'E']);
assert.deepEqual(JSON.parse(JSON.stringify(g.apps.map(function (x) { return x.name }))), ['Zeta', 'Alpha']);

// infoRows / shortUrl
var rows = ctx.infoRows({updateState: 'up-to-date', ahead: 92, held: true, holdReason: 'rolled back from v2',
                         info: {branch: 'ews-support', upstreamTs: 1000, path: '/home/ali/.config/omarchy/plugins/omamail', webUrl: 'x'}},
                        1000 + 3 * 86400, '/home/ali');
assert.deepEqual(JSON.parse(JSON.stringify(rows)), [
    ['Status', 'Up to date · held (rolled back from v2) · 92 local commits'],
    ['Branch', 'ews-support'],
    ['Upstream', 'last commit 3 days ago'],
    ['Folder', '~/.config/omarchy/plugins/omamail']]);
assert.deepEqual(JSON.parse(JSON.stringify(ctx.infoRows({updateState: 'no-repo', info: {path: '/opt/x'}}, 0, '/home/ali'))),
                 [['Status', 'No update source'], ['Folder', '/opt/x']]);
assert.equal(ctx.infoRows(null).length, 0);
assert.equal(ctx.shortUrl('https://aur.archlinux.org/packages/flea'), 'aur.archlinux.org/packages/flea');

// queueSummary
assert.equal(ctx.queueSummary([{id: 'a', name: 'A', ok: true}, {id: 'b', name: 'B', ok: false}, {id: 'c', name: 'C', ok: true}]),
             'Update all: 2 updated, 1 failed (B).');
assert.equal(ctx.queueSummary([{id: 'a', name: 'A', ok: true}]), 'Update all: 1 updated.');
assert.equal(ctx.queueSummary([]), '');

// version-change awareness: same-version commits stay updatable but leave the badge
assert.equal(ctx.alerts({updateState: 'behind', versionChange: false}), false);
assert.equal(ctx.alerts({updateState: 'behind', versionChange: true}), true);
assert.equal(ctx.alerts({updateState: 'behind'}), true, 'unknown still alerts');
assert.equal(ctx.alerts({updateState: 'behind', versionChange: true, held: true}), false);
assert.equal(ctx.canUpdate({updateState: 'behind', versionChange: false}), true, 'still updatable from the panel');
assert.equal(ctx.badgeCount([{updateState: 'behind', versionChange: false}, {updateState: 'behind', versionChange: true}]), 1);
assert.equal(ctx.behindLabel({updateState: 'behind', behind: 1, versionChange: false}), 'same version');
assert.equal(ctx.behindLabel({updateState: 'behind', behind: 3, versionChange: true, upstreamVersion: '1.2.0'}), '→ v1.2.0');
assert.equal(ctx.behindLabel({updateState: 'behind', behind: 2}), '2 commits behind');
assert.equal(ctx.behindLabel({updateState: 'up-to-date'}), '');
assert.equal(ctx.stripeKey({kind: 'plugin', updateState: 'behind', versionChange: false}), 'ok');
assert.equal(ctx.stripeKey({kind: 'plugin', updateState: 'behind', versionChange: true}), 'behind');


// jobs: which actions run detached, and what a job.json means for one panel instance
assert.equal(ctx.isJobKind('update'), true);
assert.equal(ctx.isJobKind('rollback'), true);
assert.equal(ctx.isJobKind('remove'), true);
assert.equal(ctx.isJobKind('remove-app'), false);
assert.equal(ctx.isJobKind('hold'), false);
assert.equal(ctx.jobView(null, '', '', 100).busy, false);
assert.equal(ctx.jobView({}, '', '', 100).apply, false);
var running = ctx.jobView({jobId: 'j1', kind: 'update', state: 'running', ids: ['a', 'b', 'c'], current: 'b',
                           results: [{id: 'a', ok: true}], itemTs: 90}, '', '', 100);
assert.equal(running.busy, true);
assert.equal(running.id, 'b');
assert.equal(running.total, 3);
assert.equal(running.done, 1);
assert.equal(running.startSec, 90);
assert.equal(ctx.jobView({jobId: 'j1', state: 'starting', ids: ['a'], startTs: 95}, '', '', 100).id, 'a');
var done = {jobId: 'j1', kind: 'update', state: 'done', ids: ['a'], results: [{id: 'a', ok: true}], finishedTs: 50};
assert.equal(ctx.jobView(done, 'j1', '', 100).apply, true, 'one this instance saw going');
assert.equal(ctx.jobView(done, 'j1', 'j1', 100).apply, false, 'once only');
assert.equal(ctx.jobView(done, '', '', 100).apply, false, 'an old one found on load');
assert.equal(ctx.jobView(done, '', '', 60).apply, true, 'one that only just finished (rebuilt at the end)');
assert.equal(ctx.jobView(done, 'j1', '', 100).busy, false);

console.log('Model.js: canUpdate/badgeCount, stateLabel wording, groupByKind sort/split, relativeAge buckets, not-installed, localTag, holds, commits, history, status sort, row info and Update all summary, version-change badge, jobs all pass.');
