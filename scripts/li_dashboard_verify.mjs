// Living-Intelligence dashboard render-verify (Phase-7c) — confirms the new LearningLab sections
// (Self-Improvement Unification + Rollback lineage) actually RENDER in a real browser with the live
// values the backend computed, and that no console error was introduced. Read-only; opens no trades.
//   PW_HOME=/tmp/pw node scripts/li_dashboard_verify.mjs
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
const PW_HOME = process.env.PW_HOME || '/tmp/pw';
const { chromium } = createRequire(PW_HOME + '/').call(null, 'playwright');

const BASE = 'http://localhost/';
const tok = execSync(`docker exec trading-bot-dashboard-1 python -c "import sys;sys.path.insert(0,'/app');from dashboard.api import _make_token;print(_make_token('li-verify'))"`).toString().trim();

const errors = [];
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1400 } });
await ctx.addInitScript(t => window.sessionStorage.setItem('token', t), tok);
const page = await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 160)); });
page.on('pageerror', e => errors.push('PAGEERR ' + String(e).slice(0, 160)));

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(2500);

// ground truth the backend computed
const gt = await page.evaluate(async (t) => {
  const r = await fetch('/scibrain/learning?limit=50', { headers: { Authorization: 'Bearer ' + t } });
  const j = await r.json();
  const si = j.self_improvement || {};
  return { available: si.available, n_producers: si.n_producers, n_open_bypasses: si.n_open_bypasses,
           n_changes: (si.recent_changes || []).length, has_rollback: !!j.rollback };
}, tok);
console.log('GROUND TRUTH (backend):', JSON.stringify(gt));

// open the Learning Lab tab
await page.getByTestId('view-lab').click().catch(() => {});
await page.waitForTimeout(4000); // lab mounts + fetches /scibrain/learning, then renders
const txt = (await page.locator('body').innerText()).replace(/\s+/g, ' ');

const checks = [
  ['self-improvement section renders', /self-improvement unification/i.test(txt)],
  ['no-bypass status shown', /no bypass/i.test(txt)],
  ['producer count rendered', new RegExp(`${gt.n_producers} producers under one kernel`, 'i').test(txt)],
  ['change-lineage section renders', /change lineage/i.test(txt)],
  ['rollback section renders', /promotion \/ rollback lineage/i.test(txt)],
  ['model-recorded mode chip rendered', /model recorded/i.test(txt)],
  ['bounded-recorded mode chip rendered', /bounded recorded/i.test(txt)],
];
let pass = 0;
for (const [name, ok] of checks) { console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}`); if (ok) pass++; }

await page.screenshot({ path: '/tmp/li_learninglab.png', fullPage: true }).catch(() => {});
console.log(`\nRESULT: ${pass}/${checks.length} render checks pass; console errors=${errors.length}`);
if (errors.length) console.log('ERRORS:', errors.slice(0, 5));
console.log(`OVERALL: ${pass === checks.length && errors.length === 0 ? 'ALL PASS' : 'REVIEW'}`);
await browser.close();
