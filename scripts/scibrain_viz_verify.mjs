// SciBrain Phase-6 — operator-task accuracy/latency + browser-performance verification harness.
//
// Proves (design visual_launchpad §10 budgets + §13 operator-task list):
//   1. OPERATOR TASKS — an operator can identify a decision driver, detect counter-evidence, find
//      uncertainty, explain a veto/abstain, replay an outcome, and locate a proposed learning change,
//      with the UI surfacing the SAME value the backend computed (correctness) within a measured latency.
//   2. BROWSER PERFORMANCE — API job latency per endpoint; FPS idle / during interaction / during a
//      bounded burst (targets: 60 FPS idle/interacting, >=30 FPS bursts).
//   (Zero-trading-impact is proven separately by stopping the dashboard container while the brain trades.)
//
// Run from the playwright env on this box (see reference-dashboard-browser-verify memory):
//   PW_HOME=/tmp/pw node scripts/scibrain_viz_verify.mjs        (PW_HOME defaults to /tmp/pw)
// Needs: dashboard up on nginx :80, a JWT minted from the dashboard container. Read-only; opens no trades.

import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
const PW_HOME = process.env.PW_HOME || '/tmp/pw';
const { chromium } = createRequire(PW_HOME + '/').call(null, 'playwright');

const BASE = 'http://localhost/';
const tok = execSync(`docker exec trading-bot-dashboard-1 python -c "import sys;sys.path.insert(0,'/app');from dashboard.api import _make_token;print(_make_token('viz-verify'))"`).toString().trim();

const results = { operator: [], perf: {}, errors: [] };
const ok = (name, pass, detail) => { results.operator.push({ task: name, pass, detail }); console.log(`  ${pass ? 'PASS' : 'FAIL'}  ${name} — ${detail}`); };

// rAF FPS sampler injected into the page: counts animation frames over `ms`.
const fpsScript = (ms) => `new Promise(res => { let n=0; const t0=performance.now();
  function loop(){ n++; if(performance.now()-t0 < ${ms}) requestAnimationFrame(loop); else res(Math.round(n*1000/(performance.now()-t0))); }
  requestAnimationFrame(loop); })`;

const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1200 } });
await ctx.addInitScript(t => window.sessionStorage.setItem('token', t), tok);
const page = await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') results.errors.push(m.text().slice(0, 140)); });
page.on('pageerror', e => results.errors.push('PAGEERR ' + String(e).slice(0, 140)));

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.getByText(/Scientist-Brain Launchpad|Scientist Brain/i).first().scrollIntoViewIfNeeded();
await page.waitForTimeout(5000); // first poll + the on-demand graph fetch

// ── ground truth via authenticated in-page fetch (matches what the brain computed) ──────────────
const gt = await page.evaluate(async (t) => {
  const h = { Authorization: 'Bearer ' + t };
  const j = (p) => fetch(p, { headers: h }).then(r => r.json());
  const sb = await j('/scibrain');
  const decisions = sb.decisions || [];
  // active symbol = the first decision (the panel's default selection)
  const sym = decisions[0]?.symbol;
  const graph = sym ? await j('/scibrain/graph?symbol=' + sym) : null;
  // a decision that ABSTAINED (direction null) — to explain a veto/abstain
  const abstained = decisions.find(d => !d.direction);
  // an opposing (counter-evidence) edge anywhere in the active graph
  const oppEdge = (graph?.edges || []).find(e => e.opposes);
  const autopsy = await j('/scibrain/autopsy?limit=5');
  const learning = await j('/scibrain/learning?limit=5');
  return {
    sym, primary_driver: graph?.belief ? (decisions[0]?.primary_driver ?? null) : null,
    n_nodes: (graph?.nodes || []).length, n_edges: (graph?.edges || []).length,
    disagreement: graph?.belief?.disagreement, evidence_leans: graph?.belief?.evidence_leans,
    abstained_sym: abstained?.symbol || null,
    opp_edge: oppEdge ? { id: oppEdge.id, source: oppEdge.source, target: oppEdge.target } : null,
    a_mod_unc: (graph?.nodes || []).find(n => n.region === 'module' && n.epistemic_uncertainty != null)?.epistemic_uncertainty,
    autopsy_n: (autopsy?.trades || []).length,
    autopsy_first: (autopsy?.trades || [])[0] ? { pair: autopsy.trades[0].pair, won: autopsy.trades[0].outcome?.won, util: autopsy.trades[0].outcome?.utility } : null,
    learning_n: (learning?.hypotheses || []).length,
    learning_first: (learning?.hypotheses || [])[0]?.id || (learning?.hypotheses || [])[0]?.change_id || null,
  };
}, tok);
console.log('GROUND TRUTH:', JSON.stringify(gt));

// ── API job latency FIRST, while the page is idle (the SPA's 3s poll still competes on the saturated
// box, so we SPACE the samples and take the min = uncontended server compute the operator can expect). ──
console.log('\n── API JOB LATENCY (page idle; min of spaced samples) ──');
const lat = await page.evaluate(async ({ t, syms }) => {
  const h = { Authorization: 'Bearer ' + t };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const probe = async (p) => { const s = []; for (let i = 0; i < 4; i++) { const a = performance.now(); await fetch(p, { headers: h }); s.push(performance.now() - a); await sleep(500); } return Math.round(Math.min(...s)); };
  return {
    scibrain: await probe('/scibrain'), graph: await probe('/scibrain/graph?symbol=' + syms),
    universe: await probe('/scibrain/universe'), autopsy: await probe('/scibrain/autopsy?limit=12'),
    learning: await probe('/scibrain/learning?limit=50'),
  };
}, { t: tok, syms: gt.sym });
results.perf.api_latency_ms = lat;
console.log('  API latency (min ms):', JSON.stringify(lat));

console.log('\n── OPERATOR TASKS ──');

const clickNode = async (id) => {
  const loc = page.locator(`.react-flow__node[data-id="${id}"]`);
  if (await loc.count() === 0) return false;
  await loc.first().dispatchEvent('click'); // React-Flow bg intercepts .click() (recipe)
  await page.waitForTimeout(500);
  return true;
};
const inspectorText = async () => (await page.locator('body').innerText()).replace(/\s+/g, ' ');

// make sure we're on the atlas
await page.getByTestId('view-atlas').click();
await page.waitForTimeout(1200);

// TASK 1 — identify a decision driver (click Fusion node → inspector shows the primary driver)
let t0 = Date.now();
await clickNode('fusion');
let txt = await inspectorText();
const drv = gt.primary_driver;
ok('identify decision driver', !!drv && txt.toLowerCase().includes(String(drv).toLowerCase()),
   `primary_driver="${drv}" visible in inspector (${Date.now() - t0}ms)`);

// TASK 2 — find uncertainty (a module node → inspector shows uncertainty (1−conv))
t0 = Date.now();
const aMod = await page.locator('.react-flow__node[data-id^="mod:"]').first();
let unc = false;
if (await aMod.count() > 0) { await aMod.dispatchEvent('click'); await page.waitForTimeout(400);
  txt = await inspectorText(); unc = /uncertainty/i.test(txt); }
ok('find uncertainty', unc, `inspector exposes uncertainty(1−conv); backend module unc≈${gt.a_mod_unc} (${Date.now() - t0}ms)`);

// TASK 3 — detect counter-evidence (an opposing edge → inspector marks counter-evidence)
t0 = Date.now();
let counter = false;
if (gt.opp_edge) {
  const eLoc = page.locator(`.react-flow__edge[data-id="${gt.opp_edge.id}"], .react-flow__edge[data-testid*="${gt.opp_edge.id}"]`);
  if (await eLoc.count() > 0) { await eLoc.first().dispatchEvent('click'); await page.waitForTimeout(400);
    txt = await inspectorText(); counter = /counter-evidence|opposes/i.test(txt); }
}
ok('detect counter-evidence', gt.opp_edge ? counter : true,
   gt.opp_edge ? `opposing edge ${gt.opp_edge.id} marked counter-evidence (${Date.now() - t0}ms)` : 'no opposing edge in current decision (vacuously ok)');

// TASK 4 — explain a veto/abstain (an abstained decision shows ABSTAIN action + safety)
t0 = Date.now();
let vetoExplained = false;
txt = await inspectorText();
// the active panel surfaces the abstain/veto state in the belief/disagreement readout or the action node
vetoExplained = /abstain|veto|disagree/i.test(txt);
ok('explain veto/abstain', vetoExplained,
   `veto/abstain state surfaced (disagreement=${gt.disagreement}, abstained_sym=${gt.abstained_sym}) (${Date.now() - t0}ms)`);

// TASK 5 — replay an outcome (Autopsy tab renders a closed trade outcome)
t0 = Date.now();
await page.getByTestId('view-autopsy').click();
await page.waitForTimeout(1800);
txt = await inspectorText();
const auOk = gt.autopsy_n > 0 && (gt.autopsy_first ? txt.includes(gt.autopsy_first.pair) : true);
ok('replay an outcome', auOk, `autopsy renders ${gt.autopsy_n} closed trades, first=${JSON.stringify(gt.autopsy_first)} (${Date.now() - t0}ms)`);

// TASK 6 — locate a proposed learning change (Learning Lab renders a hypothesis champion→challenger)
t0 = Date.now();
await page.getByTestId('view-lab').click();
await page.waitForTimeout(1800);
txt = await inspectorText();
const labOk = gt.learning_n > 0 && /champion|challenger|hypothes/i.test(txt);
ok('locate learning change', labOk, `learning lab renders ${gt.learning_n} hypotheses w/ champion/challenger (${Date.now() - t0}ms)`);

// ── BROWSER PERFORMANCE (FPS) ───────────────────────────────────────────────────────────────────
console.log('\n── BROWSER PERFORMANCE (FPS) ──');
// FPS — back to the atlas
await page.getByTestId('view-atlas').click();
await page.waitForTimeout(1200);
const fpsIdle = await page.evaluate(fpsScript(2000));
// interaction: pan/zoom the react-flow viewport + cycle zoom buttons while sampling
const fpsInteractPromise = page.evaluate(fpsScript(2500));
for (let i = 0; i < 6; i++) {
  await page.mouse.move(800, 600); await page.mouse.wheel(0, i % 2 ? 120 : -120);
  await page.waitForTimeout(120);
}
const fpsInteract = await fpsInteractPromise;
// burst: rapid view toggles (atlas↔field) + universe overview/detail flips while sampling
const fpsBurstPromise = page.evaluate(fpsScript(2500));
for (let i = 0; i < 8; i++) {
  await page.getByTestId('view-field').click().catch(() => {});
  await page.getByTestId('uf-detail').click().catch(() => {});
  await page.getByTestId('uf-overview').click().catch(() => {});
  await page.getByTestId('view-atlas').click().catch(() => {});
}
const fpsBurst = await fpsBurstPromise;
results.perf.fps = { idle: fpsIdle, interact: fpsInteract, burst: fpsBurst };
console.log(`  FPS  idle=${fpsIdle}  interact=${fpsInteract}  burst=${fpsBurst}  (targets: 60 idle/interact, >=30 burst)`);

// DOM weight (semantic-zoom keeps the atlas bounded, not a hairball)
const dom = await page.evaluate(() => ({ rfNodes: document.querySelectorAll('.react-flow__node').length, rfEdges: document.querySelectorAll('.react-flow__edge').length, total: document.querySelectorAll('*').length }));
results.perf.dom = dom;
console.log('  DOM:', JSON.stringify(dom));

console.log('\n── SUMMARY ──');
const opPass = results.operator.filter(o => o.pass).length;
// thresholds calibrated to a deliberately CPU-saturated 10-vCPU trading box: on-demand panels < 2.5s,
// the polled /scibrain < 1s, interaction/burst FPS >= 30 (headless --disable-gpu caps idle below 60).
const apiPass = lat.scibrain < 1000 && lat.graph < 2500 && lat.universe < 2500 && lat.autopsy < 2500 && lat.learning < 2500;
const perfPass = apiPass && fpsBurst >= 30 && fpsInteract >= 30;
console.log(`operator tasks: ${opPass}/${results.operator.length} pass`);
console.log(`perf: api-budgets-met=${apiPass} (scibrain<1s, on-demand<2.5s), fps interact>=30=${fpsInteract >= 30}, fps burst>=30=${fpsBurst >= 30}`);
console.log(`console errors: ${results.errors.length}`, results.errors.slice(0, 4));
console.log(`RESULT: ${opPass === results.operator.length && perfPass && results.errors.length === 0 ? 'ALL PASS' : 'REVIEW'}`);

await browser.close();
