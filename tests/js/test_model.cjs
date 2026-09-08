const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const ctx = {Date: Date};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'Model.js'), 'utf8').replace('.pragma library', ''), ctx);

// canUpdate / badgeCount
assert.equal(ctx.canUpdate({updateState: 'behind'}), true);
assert.equal(ctx.canUpdate({updateState: 'dirty'}), false);
assert.equal(ctx.canUpdate({updateState: 'no-repo'}), false);
assert.equal(ctx.canUpdate(null), false);
assert.equal(ctx.badgeCount([{updateState: 'behind'}, {updateState: 'up-to-date'}, {updateState: 'behind'}]), 2);
assert.equal(ctx.badgeCount([]), 0);
assert.equal(ctx.badgeCount(undefined), 0);

// stateLabel
assert.equal(ctx.stateLabel({updateState: 'behind', behind: 1}), '1 commit behind');
assert.equal(ctx.stateLabel({updateState: 'behind', behind: 4}), '4 commits behind');
assert.equal(ctx.stateLabel({updateState: 'dirty'}), 'Local changes -- update manually');
assert.equal(ctx.stateLabel({updateState: 'no-repo'}), 'No update source');
assert.equal(ctx.stateLabel({updateState: 'unreachable'}), 'Could not reach source');
assert.equal(ctx.stateLabel({updateState: 'up-to-date'}), 'Up to date');
assert.equal(ctx.stateLabel(null), '');

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
