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
assert(!(d.sl > 0) && d.slSrc !== '技术参考' && !(d.tp > 0) && d.tpSrc !== '技术参考', 'technical ref is not execution');
assert(d.ref.has && d.ref.sl === 42 && d.ref.tp === 70 && d.ref.differs, 'technical stays in the reference area');

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
const auditMissing = histAuditOf({symbol:'TQQQ'});
assert(auditMissing.hide && auditMissing.state === '未校验', 'TQQQ without audit stays unchecked');
const nvdaAudit = histAuditOf({symbol:'NVDA'});
assert(nvdaAudit.ordinary && !nvdaAudit.hide, 'NVDA is not behind the ETF corporate-action gate');
const rr = entryRiskReturn(50, 45, [60, 68]);
assert(Math.abs(rr.risk - 10) < 1e-9 && rr.rets.length === 2 && Math.abs(rr.rets[0].ret - 20) < 1e-9, 'risk and return use the candidate entry, not the quote');
const auditBad = histAuditOf({ symbol:'SOXL', hist_audit: { status: 'pending', hide_technical: true } });
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
assert(visibleAddons({extra_tranches:[45.11, 4.97], proposed_tranches:[{price:4.97}]}).length === 0, 'generated add-ons are not shown');
assert(visibleAddons({confirmed_addons:[40]}).length === 1, 'only an explicit confirmed add-on is visible');
const bufs = atrBuffers([52, 50], 45, 2);
assert(bufs.length === 2 && deepestBuffer(bufs).entry === 50 && deepestBuffer(bufs).multiple === 2.5, 'deepest entry has the smaller ATR buffer');
assert(Number.isNaN(officialClosePx({price:48, price_label:'盘中', close:47})), 'intraday quote is not an official close');
assert(officialClosePx({price:48, close:47, price_label:'收盘'}) === 47, 'confirmed close is used');
const touched = {price:40, close:50, price_label:'盘中'};
assert(Number.isNaN(officialClosePx(touched)), 'a live print below the stop does not become a close');

const brk = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 0, fixedTp: [], refSl: 490, refTp: 596.92, techOk: true });
assert(!(brk.sl > 0) && brk.ref.sl === 490 && brk.ref.tp === 596.92, 'BRK.B reference is not an execution price');
const brkCard = sltpCardModel(brk, { held: false, entries: [480, 460], px: 500 });
assert(brkCard.pending && brkCard.risks.length === 0 && brkCard.exec.quotePct === false, 'unconfirmed ref is not a candidate-risk outcome');
assert(!/再跌|再涨/.test(brkCard.line), 'pending execution line has no quote distance');
const adbe = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 0, fixedTp: [], refSl: 178.25, refTp: 253.68, techOk: true });
const adbeIn = inputDraft(adbe);
assert(adbeIn.status === '未设定' && adbeIn.slValue === '' && adbeIn.tpValue === '' && adbeIn.slPlaceholder === '' && adbeIn.tpPlaceholder === '', 'unconfirmed reference leaves the input blank');
assert(adbeIn.refHint && adbeIn.refHint.sl === 178.25 && adbeIn.refHint.tp === 253.68 && adbeIn.refHint.confirmed === false, 'ADBE numbers stay a reference hint');
assert(!/已定\s*[>＞]\s*技术参考/.test(adbeIn.headline) && /未设定/.test(adbeIn.headline), 'input headline is confirmed-only');
const mine = resolvePriceLayers({ loc: { sl: 170, tp: 240 }, strat: {}, fixedSl: 0, fixedTp: [], refSl: 178.25, refTp: 253.68, techOk: true });
const mineIn = inputDraft(mine);
assert(mineIn.slValue === 170 && mineIn.tpValue === 240, 'an existing confirmed local price still prefills');
const clearBegun = beginClearOverride(mine);
assert(clearBegun.started && clearBegun.review.writesNow === false, 'clear starts a review and does not write yet');
const clearCancel = applyClearOverride(clearBegun.review, 'cancel');
assert(!clearCancel.applied && clearCancel.loc.sl === 170 && clearCancel.history === null, 'canceled clear keeps the local price');
const clearNone = applyClearOverride(clearBegun.review, 'confirm');
assert(clearNone.applied && !clearNone.loc.sl && clearNone.effective.sl == null && clearNone.effective.src === '' && clearNone.history.after.src === '未设定', 'clear with no agreed baseline stays unset');
assert(clearNone.effective.sl !== 178.25 && clearNone.history.after.sl !== 178.25, 'clear does not adopt the technical reference');
const agreedMine = resolvePriceLayers({ loc: { sl: 170, tp: 240 }, strat: {}, fixedSl: 160, fixedTp: [250], refSl: 178.25, refTp: 253.68, techOk: true });
const agreedDraft = inputDraft(agreedMine);
assert(agreedDraft.slValue === 170 && agreedDraft.agreedSl === 160, 'local confirmed value prefills and agreed stays visible');
const baseSnap = beginClearOverride(agreedMine).review.baseline;
const clearAgreed = applyClearOverride(beginClearOverride(agreedMine).review, 'confirm');
assert(clearAgreed.loc && !clearAgreed.loc.sl && clearAgreed.effective.sl === 160 && clearAgreed.effective.src === '已定', 'clear restores agreed and does not copy it into a new local override');
assert(baseSnap.sl === 160, 'clear does not rewrite the agreed baseline object');
assert(!beginClearOverride(resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 45, fixedTp: [60, 68], refSl: 42, refTp: 70, techOk: true })).started, 'agreed without a local override is not cleared');
const staged = stageInputReview(adbe, { sl: 175, tp: 250 });
assert(staged.writesNow === false && staged.kind === 'sltp', 'enter-review does not activate the price');
const stagedCancel = applyReview(staged, 'cancel');
assert(!stagedCancel.applied && stagedCancel.history === null, 'canceled input review writes nothing');
const stagedOk = applyReview(staged, 'confirm');
assert(stagedOk.applied && stagedOk.loc.sl === 175 && stagedOk.loc.tp === 250 && stagedOk.history.before.sl == null && stagedOk.history.after.sl === 175, 'final confirm activates and records history');
const afterConfirm = resolvePriceLayers({ loc: stagedOk.loc, strat: {}, fixedSl: 0, fixedTp: [], refSl: 178.25, refTp: 253.68, techOk: true });
assert(afterConfirm.sl === 175 && afterConfirm.slSrc === '我的' && afterConfirm.ref.sl === 178.25, 'confirmed local price is execution and the reference remains separate');
const koru = resolvePriceLayers({ loc: {}, strat: {}, fixedSl: 6, fixedTp: [8.5, 10.5], refSl: 5, refTp: 12, techOk: true });
const emptyKoru = sltpCardModel(koru, { held: false, entries: [7, 6.5], px: 7.2 });
assert(!emptyKoru.pending && emptyKoru.exec.lines.every(l => l.pct == null) && emptyKoru.risks.length === 2 && emptyKoru.risks[0].basis === 'entry', 'empty position keeps candidate distances and drops quote percentages');
assert(!/再跌|再涨|分母/.test(emptyKoru.line), 'empty execution line has no quote-distance prose');
const heldKoru = sltpCardModel(koru, { held: true, cost: 6.8, entries: [7, 6.5], px: 7.2 });
assert(heldKoru.risks.length === 1 && heldKoru.risks[0].basis === 'cost' && heldKoru.exec.lines.every(l => l.pct == null), 'held risk is versus cost');
const legacySrc = provenanceText({ price: 490, close: 490, date: '2026-11-14' }, { updated_at_beijing: '2026-11-16 08:50' });
assert(legacySrc.indexOf('来源：Yahoo Finance（经 yfinance；旧版数据未附逐条元数据）') === 0, 'legacy cloud close uses the yfinance pipeline label');
assert(legacySrc.indexOf('2026-11-14') > 0 && legacySrc.indexOf('2026-11-16 08:50 北京时间') > 0 && legacySrc.indexOf('腾讯') < 0, 'legacy cutoff and Beijing time come from the file');
const liveSrc = provenanceText({ price: 491, close: 490, date: '2026-11-14', _liveSrc: '腾讯' }, { updated_at_beijing: '2026-11-16 08:50' });
assert(liveSrc.indexOf('来源：腾讯（浏览器即时参考，不是云端收盘）') === 0 && liveSrc.indexOf('Yahoo') < 0 && liveSrc.indexOf('2026-11-14') < 0 && liveSrc.indexOf('2026-11-16') < 0, 'a browser quote is not relabeled as the old close');
const freshSrc = provenanceText({ price: 490, date: '2026-11-14', quote_source: 'Yahoo Finance', quote_via: 'yfinance', hist_audit: { cutoff: '2026-11-14' } }, { updated_at_beijing: '2026-11-16 08:50' });
assert(freshSrc.indexOf('来源：Yahoo Finance（经 yfinance）') === 0 && freshSrc.indexOf('旧版数据未附') < 0, 'new rows keep their own source');
const missingSrc = provenanceText({}, {});
assert(missingSrc.indexOf('来源：旧数据未附来源，待下次更新写入') === 0, 'a row with no price does not invent a source');
assert(!/我的\s*[>＞]\s*已定\s*[>＞]\s*技术参考/.test(html), 'the old technical-fallback slogan is gone');
assert(!/留空则用雷达里的技术参考/.test(html), 'inputs no longer say a blank field adopts the technical price');

if (failed) {
  console.error('\n' + failed + ' failed');
  process.exit(1);
}
console.log('\nAll JS layer tests passed');
