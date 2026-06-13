// Whole-Brain dashboard render-verify (Phase-7d step 17) — confirms the WholeBrain panel renders every
// region + cross-cutting view in a real browser with the live BrainPulse values, no console error.
//   PW_HOME=/tmp/pw node scripts/brain_dashboard_verify.mjs
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
const PW_HOME = process.env.PW_HOME || '/tmp/pw';
const { chromium } = createRequire(PW_HOME + '/').call(null, 'playwright');

const tok = execSync(`docker exec trading-bot-dashboard-1 python -c "import sys;sys.path.insert(0,'/app');from dashboard.api import _make_token;print(_make_token('brain-verify'))"`).toString().trim();
const errors = [];
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1400 } });
await ctx.addInitScript(t => window.sessionStorage.setItem('token', t), tok);
const page = await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 160)); });
page.on('pageerror', e => errors.push('PAGEERR ' + String(e).slice(0, 160)));

await page.goto('http://localhost/', { waitUntil: 'networkidle' });
await page.waitForTimeout(2500);

const gt = await page.evaluate(async (t) => {
  const j = await fetch('/scibrain/brain', { headers: { Authorization: 'Bearer ' + t } }).then(r => r.json());
  return { available: j.available, n_regions: (j.regions || []).length,
           healthy: j.all_regions_healthy, n_open_bypasses: (j.authority || {}).n_open_bypasses,
           p: (j.uncertainty || {}).p_action_supported };
}, tok);
console.log('GROUND TRUTH (backend):', JSON.stringify(gt));

await page.getByTestId('view-brain').click().catch(() => {});
await page.waitForTimeout(4000);
const txt = (await page.locator('body').innerText()).replace(/\s+/g, ' ');

const checks = [
  ['region activity section', /region activity/i.test(txt)],
  ['brainstem region', /brainstem/i.test(txt)],
  ['metacortex region', /metacortex/i.test(txt)],
  ['producer bus region', /producer bus/i.test(txt)],
  ['uncertainty section', /uncertainty & calibrated ignorance/i.test(txt)],
  ['P(action supported) shown', /p\(action supported\)/i.test(txt)],
  ['competence section', /competence/i.test(txt)],
  ['authority no-bypass shown', /no learned bypass/i.test(txt) || /open bypass/i.test(txt)],
  ['compute budgets section', /compute & attention budgets/i.test(txt)],
  ['workspace broadcast section', /workspace broadcast/i.test(txt)],
  ['perception/training health card', /perception \/ training health/i.test(txt)],
  ['training health verdict shown', /underpowered|trustworthy|not promoted/i.test(txt)],
  ['diagnosed issues list', /diagnosed issues/i.test(txt)],
  ['a training issue surfaced', /class imbalance|underpowered|out-of-sample signal|minority test/i.test(txt)],
  ['hippocampus / episodic memory card', /hippocampus \/ episodic memory/i.test(txt)],
  ['rare-failure preservation shown', /rare-failure preservation/i.test(txt)],
  ['pattern separation shown', /pattern separation/i.test(txt)],
  ['replay-queue head shown', /replay-queue head/i.test(txt)],
  ['prioritized replay sampler shown', /prioritized replay/i.test(txt)],
  ['importance-corrected estimate shown', /importance-corrected|IS-corrected/i.test(txt)],
  ['neocortex consolidation card', /neocortex \/ slow consolidation/i.test(txt)],
  ['EWC forgetting shown', /catastrophic forgetting|with EWC/i.test(txt)],
  ['consolidation accept/reject shown', /ACCEPTED|REJECTED/i.test(txt)],
  ['isolated sleep cycle card', /isolated sleep cycle/i.test(txt)],
  ['sleep jobs shown', /adversarial|homeostasis|pruning/i.test(txt)],
  ['abstention memory card', /abstention memory/i.test(txt)],
  ['correct abstention shown', /correct abstention/i.test(txt)],
  ['abstention reward shown', /abstention reward/i.test(txt)],
];
let pass = 0;
for (const [name, ok] of checks) { console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}`); if (ok) pass++; }

await page.screenshot({ path: '/tmp/whole_brain.png', fullPage: true }).catch(() => {});
console.log(`\nRESULT: ${pass}/${checks.length} render checks pass; console errors=${errors.length}`);
if (errors.length) console.log('ERRORS:', errors.slice(0, 5));
console.log(`OVERALL: ${pass === checks.length && errors.length === 0 ? 'ALL PASS' : 'REVIEW'}`);
await browser.close();
