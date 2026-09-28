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

console.log('Model.js: canUpdate/badgeCount, stateLabel wording, groupByKind sort/split, and relativeAge buckets all pass.');
