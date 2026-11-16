#!/usr/bin/env node
/* Fake-fixture tests for resolvePriceLayers / refApplyPatch extracted from index.html. No network. */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/\/\* === PRICE_LAYERS_BEGIN === \*\/([\s\S]*?)\/\* === PRICE_LAYERS_END === \*\//);
if (!m) {
  console.error('PRICE_LAYERS block not found in index.html');
  process.exit(1);
}
const fns = {};
const prelude = `
function resolvePriceLayers(input){ return fns.resolvePriceLayers(input); }
function refApplyPatch(loc, ref){ return fns.refApplyPatch(loc, ref); }
`;
// eval the extracted block into fns
const boxed = m[1] + `
fns.resolvePriceLayers = resolvePriceLayers;
fns.refApplyPatch = refApplyPatch;
`;
eval(boxed);

let failed = 0;
function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); failed++; }
  else console.log('ok  ', msg);
}
function almost(a, b, msg) {
  assert(Math.abs(a - b) < 1e-9, msg + ` (got ${a}, expected ${b})`);
}

// manual override preserved when reference changes
const loc = { sl: 44, tp: 80 };
const base = {
  loc, strat: {}, fixedSl: 45, fixedTp: [60, 68],
  refSl: 42, refTp: 70, costTech: null, techOk: true
};
const a = resolvePriceLayers(base);
const b = resolvePriceLayers(Object.assign({}, base, { refSl: 30, refTp: 90 }));
assert(a.sl === 44 && a.slSrc === '我的' && a.tp === 80 && a.tpSrc === '我的', 'manual used as execution');
assert(b.sl === 44 && b.tp === 80, 'manual execution unchanged when ref changes');
assert(a.ref.sl === 42 && a.ref.tp === 70, 'ref shown with override');
assert(b.ref.sl === 30 && b.ref.tp === 90, 'latest ref updates independently');
assert(a.ref.differs && b.ref.differs, 'ref differs from manual execution');

// fallback: agreed, then technical
const c = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 45, fixedTp: [60, 68], refSl: 42, refTp: 70, techOk: true });
assert(c.sl === 45 && c.slSrc === '已定' && c.tps.length === 2 && c.tps[0] === 60 && c.tpSrc === '已定', 'fallback to agreed');
assert(c.ref.sl === 42 && c.ref.tp === 70, 'tech ref still shown under agreed');
const d = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 0, fixedTp: [], refSl: 42, refTp: 70, techOk: true });
assert(d.sl === 42 && d.slSrc === '技术参考' && d.tp === 70 && d.tpSrc === '技术参考', 'fallback to technical');
assert(d.ref.has && !d.ref.differs, 'when execution is technical, ref matches');

// no automatic reference: never invent
const e = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 45, fixedTp: [60], refSl: null, refTp: null, techOk: true });
assert(e.sl === 45 && e.slSrc === '已定', 'agreed still works without ref');
assert(e.ref.missing && !e.ref.has && !(e.ref.sl > 0) && !(e.ref.tp > 0), 'no ref → 暂无参考, nothing invented');

// old data.json shape (only fixed_* + ref_*, no exec_*)
const f = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 38, fixedTp: [60, 75], refSl: 32, refTp: 80, techOk: true });
assert(f.sl === 38 && f.fixed && f.ref.sl === 32 && f.ref.tp === 80, 'old fields still resolve both layers');

// confirm applies, cancel does not
const before = { sl: 44, tp: 80 };
const cancelled = Object.assign({}, before);
const applied = refApplyPatch(before, { sl: 42, tp: 70 });
assert(cancelled.sl === 44 && cancelled.tp === 80, 'cancel leaves override unchanged');
assert(applied.sl === 42 && applied.tp === 70, 'confirm copies ref into 我的 override');
assert(before.sl === 44 && before.tp === 80, 'refApplyPatch does not mutate original loc');

// cost-based ref is used as automatic reference even with agreed
const g = resolvePriceLayers({
  loc: {}, strat: {}, fixedSl: 45, fixedTp: [60, 68],
  refSl: 42, refTp: 70, costTech: { sl: 41.5, tp: 72 }, techOk: true
});
assert(g.sl === 45 && g.slSrc === '已定', 'execution stays agreed');
assert(g.ref.sl === 41.5 && g.ref.tp === 72 && /按成本/.test(g.ref.src), 'cost-based ref shown');

const legacy = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 45, fixedTp: [60, 68], refSl: 42, refTp: 70 });
assert(legacy.sl === 45 && legacy.slSrc === '已定', 'legacy keeps agreed execution');
assert(!(legacy.ref.sl > 0) && !(legacy.ref.tp > 0), 'legacy without techOk hides technical ref');
const auditMissing = histAuditOf({});
assert(auditMissing.hide && auditMissing.status === 'unknown', 'missing audit is unverified');
const auditBad = histAuditOf({ hist_audit: { status: 'pending', hide_technical: true } });
assert(auditBad.hide, 'pending audit hides adopt path');
assert(addonConflict(4.97, 6) && addonConflict(6, 6), 'add-on <= stop is conflict, equality included');
assert(!addonConflict(45.11, 45), 'TQQQ 45.11 above 45 is not a conflict');
const reviewed = { sym: 'KORU', before: { sl: 6, tp: 8.5 }, proposed: { sl: 5, tp: 9 } };
const reviewCancelled = applyReview(reviewed, 'cancel');
assert(!reviewCancelled.applied && reviewCancelled.loc.sl === 6 && reviewCancelled.history === null, 'cancel keeps execution');
const confirmed = applyReview(reviewed, 'confirm');
assert(confirmed.applied && confirmed.loc.sl === 5 && confirmed.history.before.sl === 6 && confirmed.history.after.sl === 5, 'confirm writes history without touching before');
assert(reviewed.before.sl === 6, 'review object before price unchanged');
const emptyPos = positionMode({hasQty:false, levHeldCount:0});
assert(emptyPos.candidateOnly && emptyPos.reviewNotBuy && !emptyPos.showHoldingStats && emptyPos.wholePosition, 'no holding: candidate ladder is review only');
const heldPos = positionMode({hasQty:true, levHeldCount:1});
assert(heldPos.showHoldingStats && heldPos.historicalLadder && !heldPos.blockNewExecutable, 'one holding shows stats and does not block its own stop');
const multiPos = positionMode({hasQty:true, levHeldCount:2});
assert(multiPos.blockNewExecutable && multiPos.wholePosition, 'two leveraged holdings block new executable advice');

if (failed) {
  console.error('\n' + failed + ' failed');
  process.exit(1);
}
console.log('\nAll JS layer tests passed');
