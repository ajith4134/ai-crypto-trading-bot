// WholeBrain — Phase-7d whole-brain dashboard / BrainPulse (design §Phase-E step 17). Renders every
// cognitive-OS region (brainstem/thalamus/workspace/metacortex/action/tail/learning) with its activity,
// authority, and health, plus the cross-cutting glance views: workspace broadcast, uncertainty (epistemic
// vs aleatoric, P(action_supported)), competence/OOD, the live-capital authority + producer-bus no-bypass,
// and the compute/attention budgets. Read-only: nothing here changes a live knob.
import React, { useEffect, useState } from 'react';
import { getSciBrainBrain } from '../../api';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN } from './ui';

const fmt = (v: any, d = 3) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d));
const card: React.CSSProperties = {
  background: '#10162a', border: '1px solid #243049', borderRadius: 6, padding: '8px 10px', marginBottom: 8,
};
const sectionTitle: React.CSSProperties = {
  fontSize: 9, color: COL_MUTE, letterSpacing: 0.4, marginBottom: 5, textTransform: 'uppercase',
};
const pill = (col: string): React.CSSProperties => ({
  padding: '1px 6px', borderRadius: 4, color: col, border: `1px solid ${col}`,
  background: '#0c1322', fontWeight: 700, fontSize: 9.5,
});
const authColor = (a: string) =>
  a === 'live' ? COL_LONG : a === 'veto' ? COL_WARN : a === 'bounded_canary' ? '#d9a23b'
    : a === 'advise' ? '#5a86c8' : COL_MUTE;

const Metric: React.FC<{ k: string; v: any }> = ({ k, v }) => (
  <span style={{ fontSize: 9, color: COL_MUTE, marginRight: 8 }}>
    {k.replace(/_/g, ' ')} <b style={{ color: '#cdd6e6' }}>{typeof v === 'number' ? fmt(v) : String(v ?? '—')}</b>
  </span>
);

const RegionRow: React.FC<{ rg: any }> = ({ rg }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0',
                borderBottom: '1px solid #18203400' }}>
    <span title={rg.active ? 'active' : 'idle'} style={{
      width: 9, height: 9, borderRadius: 5, flexShrink: 0,
      background: rg.active ? (rg.health ? COL_LONG : COL_SHORT) : '#2c3a52' }} />
    <span style={{ width: 230, color: '#cdd6e6', fontSize: 10.5, whiteSpace: 'nowrap', overflow: 'hidden',
      textOverflow: 'ellipsis' }} title={rg.label}>{rg.label}</span>
    <span style={pill(authColor(rg.authority))}>{rg.authority}</span>
    <span style={pill(rg.health ? COL_LONG : COL_SHORT)}>{rg.health ? 'healthy' : 'fault'}</span>
    <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
      {Object.entries(rg.metric || {}).map(([k, v]) => <Metric key={k} k={k} v={v} />)}
    </span>
  </div>
);

// Perception / Training Health (design §8) — the auto-diagnosed SSL training view. The point is to make
// a result INTERPRETABLE: a 0.49 AUC on ~20 test negatives is "underpowered", NOT "bad model". Surfaces
// the AUC vs baseline vs permutation with the 95% CI, the corpus/imbalance facts, and the typed issues.
const SEV_COL: Record<string, string> = { critical: COL_SHORT, warn: COL_WARN, info: COL_MUTE };
const HEALTH_COL: Record<string, string> = {
  trustworthy: COL_LONG, underpowered: COL_WARN, issues: COL_WARN, leakage: COL_SHORT, no_run: COL_MUTE,
};
const AucBar: React.FC<{ label: string; v: number; col: string; ci?: number[] }> = ({ label, v, col, ci }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9.5, margin: '1px 0' }}>
    <span style={{ width: 78, color: COL_MUTE }}>{label}</span>
    <span style={{ position: 'relative', width: 150, height: 9, background: '#1c2333', borderRadius: 3 }}>
      {/* 0.5 chance marker */}
      <span style={{ position: 'absolute', left: '50%', top: -1, bottom: -1, width: 1, background: '#5a6b86' }} />
      {/* CI band */}
      {ci && ci[0] != null && <span style={{ position: 'absolute', top: 3, height: 3, background: '#3a4a63',
        left: `${Math.max(0, ci[0]) * 100}%`, width: `${Math.min(1, ci[1]) * 100 - Math.max(0, ci[0]) * 100}%` }} />}
      {/* point estimate */}
      <span style={{ position: 'absolute', top: 0, height: '100%', width: 2, background: col,
        left: `calc(${Math.max(0, Math.min(1, v)) * 100}% - 1px)` }} />
    </span>
    <b style={{ width: 42, color: col }}>{(v ?? 0).toFixed(3)}</b>
    {ci && ci[0] != null && <span style={{ color: COL_MUTE, fontSize: 8.5 }}>CI [{ci[0]}, {ci[1]}]</span>}
  </div>
);

const TrainingHealth: React.FC<{ t: any }> = ({ t }) => {
  if (!t || !t.available) return (
    <div style={card}><div style={sectionTitle}>perception / training health</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>No SSL training run yet.</span></div>);
  const auc = t.auc || {}, cr = t.corpus || {}, issues = t.issues || [];
  return (
    <div style={{ ...card, borderColor: t.health === 'trustworthy' ? '#2c3a18'
      : t.health === 'leakage' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>perception / training health — shared-latent SSL · downstream-utility proof</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
        <span style={pill(HEALTH_COL[t.health] || COL_MUTE)}>{String(t.health).toUpperCase()}</span>
        <span style={pill(t.promoted ? COL_LONG : COL_MUTE)}>{t.promoted ? 'PROMOTED' : 'not promoted'}</span>
        <span style={{ fontSize: 9, color: COL_MUTE }}>{t.verdict}</span>
      </div>
      {/* AUC comparison with the chance marker + CI band */}
      <AucBar label="latent" v={auc.latent} col={auc.latent > auc.baseline ? COL_LONG : COL_WARN} ci={auc.latent_ci95} />
      <AucBar label="raw baseline" v={auc.baseline} col="#5a86c8" />
      <AucBar label="permutation" v={auc.permutation} col={Math.abs((auc.permutation ?? 0.5) - 0.5) > 0.15 ? COL_SHORT : COL_MUTE} />
      <div style={{ fontSize: 9, color: COL_MUTE, margin: '4px 0' }}>
        corpus n={cr.n} · test {cr.test} (<b style={{ color: (cr.n_test_neg ?? 0) < 30 ? COL_WARN : COL_MUTE }}>
          {cr.n_test_neg} minority</b>) · win-rate {cr.base_rate_win} · epochs {cr.epochs} · params {cr.n_params}</div>
      {/* the issues — the whole point */}
      <div style={sectionTitle}>diagnosed issues ({(t.n_issues || {}).critical || 0} crit · {(t.n_issues || {}).warn || 0} warn · {(t.n_issues || {}).info || 0} info)</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}>
            <b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>
          </span>
        </div>
      ))}
    </div>
  );
};

// Hippocampus — episodic memory (design §3.5). The useful question: are RARE FAILURES preserved (top of
// the replay queue) or averaged into the wins? + is pattern separation actually decorrelating episodes?
const MEM_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, watch: COL_WARN, issues: COL_SHORT, cold: COL_MUTE,
};
const EpisodicMemory: React.FC<{ e: any }> = ({ e }) => {
  if (!e || !e.available) return (
    <div style={card}><div style={sectionTitle}>hippocampus / episodic memory</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{e?.note || 'Episodic memory cold.'}</span></div>);
  const st = e.store || {}, ps = e.pattern_separation || {}, pr = e.preservation || {}, issues = e.issues || [];
  const preserved = (pr.top_priority_loss_rate ?? 0) > (pr.base_loss_rate ?? 0) + 0.02;
  return (
    <div style={{ ...card, borderColor: e.health === 'healthy' ? '#2c3a18'
      : e.health === 'issues' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>hippocampus / episodic memory — rich episodes · pattern separation · replay priority</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
        <span style={pill(MEM_HEALTH_COL[e.health] || COL_MUTE)}>{String(e.health).toUpperCase()}</span>
        <Metric k="episodes" v={e.n_episodes} /><Metric k="win rate" v={st.win_rate} />
        <Metric k="span h" v={st.time_span_h} />
      </div>
      {/* the two diagnostics that matter */}
      <div style={{ fontSize: 9.5, margin: '2px 0' }}>
        <span style={{ color: COL_MUTE }}>rare-failure preservation: </span>
        <b style={{ color: preserved ? COL_LONG : COL_SHORT }}>
          top-priority loss {((pr.top_priority_loss_rate ?? 0) * 100).toFixed(0)}%</b>
        <span style={{ color: COL_MUTE }}> vs base {((pr.base_loss_rate ?? 0) * 100).toFixed(0)}% </span>
        {preserved ? <span style={pill(COL_LONG)}>preserved</span> : <span style={pill(COL_SHORT)}>averaged away</span>}
      </div>
      <div style={{ fontSize: 9.5, margin: '2px 0' }}>
        <span style={{ color: COL_MUTE }}>pattern separation: raw sim {ps.raw_similarity} → sparse {ps.code_similarity} </span>
        <b style={{ color: (ps.separation_gain ?? 0) > 0.02 ? COL_LONG : COL_WARN }}>
          (gain {ps.separation_gain})</b>
        <span style={{ color: COL_MUTE }}> · {ps.embed_dim}d / k-WTA {ps.kwta_active}</span>
      </div>
      {/* prioritized replay sampler + importance correction */}
      {e.replay && e.replay.available && (() => {
        const rp = e.replay, est = rp.estimates || {}, cov = rp.coverage || {};
        const recovered = est.bias_recovered;
        return (
          <div style={{ marginTop: 4, paddingTop: 4, borderTop: '1px solid #1c2742' }}>
            <div style={sectionTitle}>prioritized replay (α={rp.params?.alpha} β={rp.params?.beta}) · importance-corrected</div>
            <div style={{ fontSize: 9.5 }}>
              <span style={{ color: COL_MUTE }}>sampling: base loss {(est.base_loss_rate * 100).toFixed(0)}% → </span>
              <b style={{ color: COL_WARN }}>sampled {(est.sampled_loss_rate * 100).toFixed(0)}%</b>
              <span style={{ color: COL_MUTE }}> (over-samples failures) → IS-corrected </span>
              <b style={{ color: recovered ? COL_LONG : COL_SHORT }}>{(est.is_corrected_loss_rate * 100).toFixed(1)}%</b>
              {recovered ? <span style={pill(COL_LONG)}>unbiased</span> : <span style={pill(COL_SHORT)}>biased</span>}
            </div>
            <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 1 }}>
              coverage {(cov.ess_frac * 100).toFixed(0)}% ({cov.priority_ess?.toFixed?.(0)}/{cov.n_episodes} effective) ·
              IS-weight batch ESS {rp.is_weights?.batch_ess} · {(rp.n_issues || {}).warn || 0} warn
            </div>
          </div>
        );
      })()}

      {/* issues */}
      <div style={sectionTitle}>memory-health issues ({(e.n_issues || {}).critical || 0} crit · {(e.n_issues || {}).warn || 0} warn · {(e.n_issues || {}).info || 0} info)</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && i.recommendation !== '—' && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
      {/* top-priority episodes (the replay queue head) */}
      <div style={sectionTitle}>replay-queue head — highest-priority episodes</div>
      <div style={{ maxHeight: 120, overflowY: 'auto' }}>
        {(e.top_episodes || []).slice(0, 8).map((ep: any, k: number) => (
          <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1px 0', alignItems: 'center' }}>
            <span style={pill(ep.won ? COL_LONG : COL_SHORT)}>{ep.won ? 'win' : 'loss'}</span>
            <span style={{ width: 90, color: '#cdd6e6' }}>{ep.pair} {ep.direction}</span>
            <span style={{ width: 56, color: COL_WARN }}>p={ep.priority}</span>
            <span style={{ flex: 1, color: COL_MUTE, fontSize: 8.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              rpe {ep.signals?.rpe} · surprise {ep.signals?.surprise} · tail {ep.signals?.tail} · rarity {ep.signals?.rarity}
              {ep.failure_label ? ` · ${ep.failure_label}` : ''}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

// Neocortex — slow semantic consolidation with EWC (design §3.6). The useful question: does EWC actually
// reduce catastrophic forgetting vs the ablation, and did the protected-competence gate accept or reject?
const CONS_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, ewc_weak: COL_WARN, forgetting: COL_SHORT, issues: COL_WARN, no_run: COL_MUTE,
};
const ConsolidationHealth: React.FC<{ c: any }> = ({ c }) => {
  if (!c || !c.available) return (
    <div style={card}><div style={sectionTitle}>neocortex / slow consolidation (EWC)</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{c?.note || 'No consolidation run yet.'}</span></div>);
  const f = c.forgetting || {}, oc = c.old_competence || {}, issues = c.issues || [];
  const thrPct = (f.threshold ?? 0.2) * 100;
  const Bar = (label: string, v: number, col: string) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9.5, margin: '1px 0' }}>
      <span style={{ width: 92, color: COL_MUTE }}>{label}</span>
      <span style={{ position: 'relative', width: 150, height: 9, background: '#1c2333', borderRadius: 3 }}>
        <span style={{ position: 'absolute', left: `${thrPct}%`, top: -1, bottom: -1, width: 1, background: COL_SHORT }} title="threshold" />
        <span style={{ position: 'absolute', top: 0, height: '100%', borderRadius: 2, background: col,
          width: `${Math.max(0, Math.min(100, (v ?? 0) * 100))}%` }} />
      </span>
      <b style={{ width: 44, color: col }}>{((v ?? 0) * 100).toFixed(1)}%</b>
    </div>
  );
  return (
    <div style={{ ...card, borderColor: c.health === 'healthy' ? '#2c3a18' : c.health === 'forgetting' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>neocortex / slow consolidation — EWC · protected old-competence regression test</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(CONS_HEALTH_COL[c.health] || COL_MUTE)}>{String(c.health).toUpperCase()}</span>
        <span style={pill(c.accepted ? COL_LONG : COL_SHORT)}>{c.accepted ? 'ACCEPTED' : 'REJECTED (θ_old kept)'}</span>
        <span style={{ fontSize: 9, color: COL_MUTE }}>{c.verdict}</span>
      </div>
      <div style={{ fontSize: 8.5, color: COL_MUTE, marginBottom: 2 }}>catastrophic forgetting (red line = threshold {thrPct.toFixed(0)}%)</div>
      {Bar('with EWC', f.with_ewc, (f.with_ewc ?? 1) < (f.threshold ?? 0.2) ? COL_LONG : COL_SHORT)}
      {Bar('without EWC', f.without_ewc, COL_WARN)}
      <div style={{ fontSize: 9, color: COL_MUTE, margin: '3px 0' }}>
        old-task loss {oc.loss_before} → <b style={{ color: COL_LONG }}>{oc.loss_after_ewc} (EWC)</b> vs {oc.loss_after_no_ewc} (no-EWC)
        · EWC cuts forgetting by <b style={{ color: COL_LONG }}>{((f.reduction ?? 0) * 100).toFixed(1)}pp</b> · λ_ewc {c.lambda_ewc}</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span></span>
        </div>
      ))}
    </div>
  );
};

// Isolated sleep cycle (design §3.6/§9) — the off-hot-path maintenance jobs. Useful read: did every job
// run, and what did adversarial/homeostasis/consolidation-due flag?
const SLEEP_HEALTH_COL: Record<string, string> = {
  rested: COL_LONG, watch: COL_WARN, errors: COL_SHORT, never_slept: COL_MUTE,
};
const jobLine = (name: string, j: any) => {
  const okStatus = ['ran', 'fresh'].includes(j?.status);
  const col = j?.status === 'error' ? COL_SHORT : okStatus ? COL_LONG : COL_WARN;
  let detail = '';
  if (name === 'replay') detail = `cover ${((j.coverage ?? 0) * 100).toFixed(0)}% · ${j.is_unbiased ? 'unbiased' : 'biased'}`;
  else if (name === 'consolidation') detail = `${j.status} · forget ${((j.forget_ewc ?? 0) * 100).toFixed(0)}% · ${j.ewc_helps ? 'EWC✓' : 'EWC✗'}`;
  else if (name === 'calibration') detail = `Brier ${j.brier} · ${j.drift ? 'DRIFT' : 'ok'}`;
  else if (name === 'adversarial') detail = `jaccard ${j.top_priority_jaccard} · ${j.stable ? 'stable' : 'FRAGILE'}`;
  else if (name === 'homeostasis') detail = `entropy ${j.priority_entropy_norm} · ${j.balanced ? 'balanced' : 'COLLAPSING'}`;
  else if (name === 'pruning') detail = `${j.n_prune_modules} mods · ${j.n_redundant_episodes} redundant eps`;
  else detail = j?.status || '—';
  return (
    <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9, padding: '1px 0' }}>
      <span style={{ width: 8, height: 8, borderRadius: 4, background: col, flexShrink: 0 }} />
      <span style={{ width: 96, color: '#cdd6e6' }}>{name}</span>
      <span style={{ flex: 1, color: COL_MUTE }}>{detail}</span>
    </div>
  );
};
const SleepCycle: React.FC<{ s: any }> = ({ s }) => {
  if (!s || s.available === false) return (
    <div style={card}><div style={sectionTitle}>isolated sleep cycle</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{s?.note || 'No sleep cycle has run yet.'}</span></div>);
  const jobs = s.jobs || {}, issues = s.issues || [];
  return (
    <div style={{ ...card, borderColor: s.health === 'rested' ? '#2c3a18' : s.health === 'errors' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>isolated sleep cycle — off-hot-path maintenance (replay · consolidation · calibration · adversarial · homeostasis · pruning)</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(SLEEP_HEALTH_COL[s.health] || COL_MUTE)}>{String(s.health).toUpperCase()}</span>
        <span style={{ fontSize: 9, color: COL_MUTE }}>ran {s.duration_s}s · load {s.load_factor} · {Math.round((s.age_s ?? 0) / 60)}m ago</span>
      </div>
      {['replay', 'consolidation', 'calibration', 'adversarial', 'homeostasis', 'pruning'].map(nm => jobLine(nm, jobs[nm] || {}))}
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0' }}>
          <span style={pill(SEV_COL[i.severity] || COL_MUTE)}>{i.severity}</span>
          <span style={{ color: COL_MUTE }}><b style={{ color: '#cdd6e6' }}>{i.job}</b> — {i.msg}</span>
        </div>
      ))}
    </div>
  );
};

// Abstention memory (design §3.5/§3.11) — reward correct abstention + preserve the non-trade events.
// Useful read: does the bot reject DISCRIMINATINGLY (rejected would-win rate < accepted win rate)?
const ABST_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, watch: COL_WARN, anti_skilled: COL_SHORT, cold: COL_MUTE,
};
const AbstentionMemory: React.FC<{ a: any }> = ({ a }) => {
  if (!a || a.available === false) return (
    <div style={card}><div style={sectionTitle}>abstention memory</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{a?.note || 'Abstention memory cold.'}</span></div>);
  const sk = a.skill || {}, cl = a.classes || {}, issues = a.issues || [];
  const disc = sk.discrimination ?? 0;
  return (
    <div style={{ ...card, borderColor: a.health === 'healthy' ? '#2c3a18' : a.health === 'anti_skilled' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>abstention memory — reward correct abstention · preserve rejected / near-miss / false-alarm events</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(ABST_HEALTH_COL[a.health] || COL_MUTE)}>{String(a.health).toUpperCase()}</span>
        <span style={{ fontSize: 10 }}>abstention reward <b style={{ color: (a.abstention_reward ?? 0) > 0 ? COL_LONG : COL_SHORT }}>{a.abstention_reward}</b></span>
        <span style={{ fontSize: 10 }}>discrimination <b style={{ color: disc > 0.03 ? COL_LONG : disc < 0 ? COL_SHORT : COL_WARN }}>{disc}</b></span>
      </div>
      <div style={{ fontSize: 9.5, color: COL_MUTE, marginBottom: 3 }}>
        rejected would-win <b style={{ color: '#cdd6e6' }}>{((sk.rejected_would_win_rate ?? 0) * 100).toFixed(0)}%</b>
        {' '}vs accepted win <b style={{ color: '#cdd6e6' }}>{((sk.accepted_win_rate ?? 0) * 100).toFixed(0)}%</b>
        {' '}{disc > 0.03 ? <span style={pill(COL_LONG)}>discriminating</span> : disc < 0 ? <span style={pill(COL_SHORT)}>anti-skilled</span> : <span style={pill(COL_WARN)}>indiscriminate</span>}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '1px 8px', fontSize: 9 }}>
        <span style={{ color: COL_LONG }}>correct abstention</span>
        <span style={{ color: COL_MUTE }}>{cl.correct_abstention?.n} ({((cl.correct_abstention?.rate ?? 0) * 100).toFixed(0)}%) · avoided {cl.correct_abstention?.avg_avoided_loss_pct}% loss</span>
        <span style={{ color: COL_SHORT }}>missed opportunity</span>
        <span style={{ color: COL_MUTE }}>{cl.missed_opportunity?.n} ({((cl.missed_opportunity?.rate ?? 0) * 100).toFixed(0)}%) · forgone {cl.missed_opportunity?.avg_forgone_gain_pct}% gain</span>
        <span style={{ color: COL_WARN }}>near miss</span>
        <span style={{ color: COL_MUTE }}>{cl.near_miss?.n}</span>
      </div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b><span style={{ color: COL_MUTE }}> — {i.detail}</span></span>
        </div>
      ))}
    </div>
  );
};

// Prefrontal cortex — world model + planning (design §3.6/§3.9). The useful question: does open-loop
// multi-step IMAGINATION beat a constant baseline (normalized model error = 1−R²)? If not, planning
// authority is correctly WITHHELD (planning_weight≈0) — a safe, honest default, NOT a fault.
const WM_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, no_authority: COL_WARN, stale: COL_SHORT, cold: COL_MUTE,
};
const WorldModel: React.FC<{ w: any }> = ({ w }) => {
  if (!w || w.available === false) return (
    <div style={card}><div style={sectionTitle}>prefrontal cortex / world model (RSSM)</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{w?.note || 'World model has not trained yet.'}</span></div>);
  const cal = w.calibration || {}, pa = w.planning_authority || {}, unc = w.uncertainty || {}, issues = w.issues || [];
  const plan = w.planning || {}, tu = plan.twin_utility || {};
  const beats = !!cal.beats_baseline, granted = !!pa.granted;
  const perH = cal.per_horizon || [];
  const covGap = cal.coverage_gap_90 ?? 1;
  const liftB = plan.planning_lift_vs_baseline;
  const planHelps = !!pa.planning_helps;
  return (
    <div style={{ ...card, borderColor: w.health === 'healthy' ? '#2c3a18' : w.health === 'stale' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>prefrontal cortex / world model — {w.n_members}-member RSSM ensemble · epistemic uncertainty · multi-horizon calibration · planning-authority gate</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(WM_HEALTH_COL[w.health] || COL_MUTE)}>{String(w.health).toUpperCase()}</span>
        <span style={pill(granted ? COL_LONG : COL_MUTE)}>{granted ? `PLANNING AUTHORITY · ${pa.safe_planning_horizon}h` : 'authority withheld'}</span>
        <span style={{ fontSize: 10 }}>R² <b style={{ color: beats ? COL_LONG : COL_SHORT }}>{fmt(cal.r2)}</b></span>
        <span style={{ fontSize: 10 }}>planning weight <b style={{ color: (pa.planning_weight ?? 0) > 0 ? COL_LONG : COL_MUTE }}>{fmt(pa.planning_weight)}</b></span>
      </div>
      {/* epistemic vs aleatoric (§3.11) + reward-interval coverage (§8 multi-horizon calibration) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap', fontSize: 10 }}>
        <span>epistemic <b style={{ color: unc.epistemic_gt_threshold ? COL_WARN : COL_LONG }}>{fmt(unc.epistemic)}</b></span>
        <span style={{ color: COL_MUTE }}>aleatoric <b style={{ color: '#cdd6e6' }}>{fmt(unc.aleatoric)}</b></span>
        <span style={{ color: COL_MUTE }}>·</span>
        <span>90% interval coverage <b style={{ color: covGap <= 0.15 ? COL_LONG : COL_WARN }}>{fmt(cal.reward_coverage_90, 2)}</b>
          <span style={{ color: COL_MUTE }}> (nom 0.90, gap {fmt(covGap, 2)})</span></span>
      </div>
      <div style={{ fontSize: 8.5, color: COL_MUTE, marginBottom: 2 }}>
        ensemble of {w.n_members} · n={w.n_sequences} windows (T={w.T}) from {w.n_trades} trades · imagination horizon K={w.K_imagined}</div>
      {/* per-horizon: normalized model error bar (red line = baseline 1.0) + coverage + latent open-loop drift */}
      <div style={{ fontSize: 8.5, color: COL_MUTE, margin: '2px 0 1px' }}>per-horizon model error (1.0 = constant baseline; &lt;1 beats it) · 90% coverage · epistemic · latent drift</div>
      {perH.map((h: any) => (
        <div key={h.k} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9.5, margin: '1px 0' }}>
          <span style={{ width: 30, color: COL_MUTE }}>k={h.k}</span>
          <span style={{ position: 'relative', width: 130, height: 9, background: '#1c2333', borderRadius: 3 }}>
            <span style={{ position: 'absolute', left: '100%', top: -1, bottom: -1, width: 1, background: COL_SHORT }} title="baseline 1.0" />
            <span style={{ position: 'absolute', top: 0, height: '100%', borderRadius: 2,
              background: (h.normalized_model_error ?? 1) < 1 ? COL_LONG : COL_WARN,
              width: `${Math.max(0, Math.min(100, (h.normalized_model_error ?? 1) * 100))}%` }} />
          </span>
          <b style={{ width: 40, color: (h.normalized_model_error ?? 1) < 1 ? COL_LONG : COL_WARN }}>{fmt(h.normalized_model_error)}</b>
          <span style={{ color: COL_MUTE, fontSize: 8.5 }}>cov90 {fmt(h.reward_coverage?.nominal_90, 2)}</span>
          <span style={{ color: COL_MUTE, fontSize: 8.5 }}>epi {fmt(h.epistemic_norm, 2)}</span>
          <span style={{ color: COL_MUTE, fontSize: 8.5 }}>drift {fmt(h.latent_drift, 2)}</span>
        </div>
      ))}
      {/* bounded planning vs the deterministic digital twin (task 3) */}
      {plan.n_decisions ? (
        <div style={{ marginTop: 4, paddingTop: 4, borderTop: '1px solid #1c2742' }}>
          <div style={sectionTitle}>bounded planning vs deterministic digital twin ({plan.n_decisions} decisions · fallback {pa.fallback})</div>
          <div style={{ fontSize: 9.5, display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <span>planner <b style={{ color: planHelps ? COL_LONG : COL_WARN }}>{fmt(tu.planner, 3)}</b></span>
            <span style={{ color: COL_MUTE }}>realized <b style={{ color: '#cdd6e6' }}>{fmt(tu.realized, 3)}</b></span>
            <span style={{ color: COL_MUTE }}>baseline <b style={{ color: '#cdd6e6' }}>{fmt(tu.baseline_const_action, 3)}</b></span>
            <span style={{ color: COL_MUTE }}>oracle <b style={{ color: '#7f9bc8' }}>{fmt(tu.oracle, 3)}</b></span>
            <span>lift vs baseline <b style={{ color: planHelps ? COL_LONG : COL_MUTE }}>{(liftB ?? 0) >= 0 ? '+' : ''}{fmt(liftB, 3)}</b></span>
            {planHelps ? <span style={pill(COL_LONG)}>meaningful lift</span> : <span style={pill(COL_MUTE)}>no lift → baseline</span>}
          </div>
          <div style={{ fontSize: 8.5, color: COL_MUTE, marginTop: 1 }}>
            oracle-action agreement {fmt(plan.oracle_action_agreement, 2)} · planner picks long {plan.planner_action_mix?.long}/short {plan.planner_action_mix?.short}/abstain {plan.planner_action_mix?.abstain}
            {(plan.planner_action_mix && Math.max(...Object.values(plan.planner_action_mix as Record<string, number>)) >= plan.n_decisions)
              ? <span style={{ color: COL_WARN }}> · COLLAPSED to a constant action (no state-dependent planning)</span> : null}
          </div>
        </div>
      ) : null}
      <div style={{ fontSize: 9, color: COL_MUTE, margin: '3px 0' }}>{w.verdict}</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
    </div>
  );
};

// Basal ganglia — hierarchical controllers (design §3.7/§8-417). The useful question: does reframing the
// day/minute bandits as a DAY→HOUR→MINUTE hierarchy with shared belief actually COORDINATE (beat the flat
// baseline) — or are levels decorative? SHADOW: the live PPO agents are untouched.
const HC_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, no_coordination: COL_WARN, stale: COL_SHORT, cold: COL_MUTE,
};
const Controllers: React.FC<{ h: any }> = ({ h }) => {
  if (!h || h.available === false) return (
    <div style={card}><div style={sectionTitle}>basal ganglia / hierarchical controllers</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{h?.note || 'Hierarchical controllers have not run yet.'}</span></div>);
  const co = h.coordination || {}, tu = h.twin_utility || {}, abl = h.ablation || {}, issues = h.issues || [];
  const coordinates = !!co.coordinates;
  const gain = co.coordination_gain_vs_flat;
  // ablation ladder (utility ascends as levels/belief add value) — render a small bar per stage
  const stages: [string, number][] = [
    ['flat indep', abl.flat_independent], ['no belief', abl.no_belief], ['day only', abl.day_only],
    ['day+hour', abl.day_hour], ['full', abl.full_hierarchy],
  ];
  const vals = stages.map(s => s[1]).filter(v => typeof v === 'number');
  const lo = Math.min(...vals, tu.realized ?? 0), hi = Math.max(...vals, tu.oracle ?? 0, 1e-6);
  const span = (hi - lo) || 1;
  return (
    <div style={{ ...card, borderColor: h.health === 'healthy' ? '#2c3a18' : h.health === 'stale' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>basal ganglia / hierarchical controllers — day→hour→minute · shared belief · real trajectories · coordination gain + ablation</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(HC_HEALTH_COL[h.health] || COL_MUTE)}>{String(h.health).toUpperCase()}</span>
        <span style={pill(COL_MUTE)}>SHADOW · live agents untouched</span>
        <span style={{ fontSize: 10 }}>coordination gain <b style={{ color: coordinates ? COL_LONG : COL_SHORT }}>{(gain ?? 0) >= 0 ? '+' : ''}{fmt(gain, 4)}</b></span>
        {coordinates ? <span style={pill(COL_LONG)}>coordinates</span> : <span style={pill(COL_MUTE)}>no lift vs flat</span>}
      </div>
      {/* ablation ladder: flat → +belief → day → +hour → +minute, with realized + oracle reference lines */}
      <div style={{ fontSize: 8.5, color: COL_MUTE, margin: '2px 0 1px' }}>ablation — twin utility per stage (realized {fmt(tu.realized, 3)} · oracle {fmt(tu.oracle, 3)})</div>
      {stages.map(([lab, v]) => (
        <div key={lab} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9.5, margin: '1px 0' }}>
          <span style={{ width: 64, color: COL_MUTE }}>{lab}</span>
          <span style={{ position: 'relative', width: 150, height: 9, background: '#1c2333', borderRadius: 3 }}>
            <span style={{ position: 'absolute', top: 0, height: '100%', borderRadius: 2, background: lab === 'full' ? COL_LONG : '#5a86c8',
              left: 0, width: `${Math.max(2, Math.min(100, (((v ?? lo) - lo) / span) * 100))}%` }} />
          </span>
          <b style={{ width: 52, color: '#cdd6e6' }}>{fmt(v, 4)}</b>
        </div>
      ))}
      <div style={{ fontSize: 9, color: COL_MUTE, margin: '3px 0' }}>
        hour veto <b style={{ color: co.hour_decorative ? COL_WARN : COL_LONG }}>{fmt(co.hour_marginal_veto_rate, 2)}</b>
        {co.hour_decorative ? ' (decorative)' : ''} · minute veto <b style={{ color: co.minute_decorative ? COL_WARN : COL_LONG }}>{fmt(co.minute_marginal_veto_rate, 2)}</b>
        {co.minute_decorative ? ' (decorative)' : ''} · strategy-vs-hindsight conflict {fmt(co.strategy_vs_hindsight_conflict, 2)}</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 3 }}>{h.verdict}</div>
    </div>
  );
};

// Offline RL challengers (design §3.7/§8-416) — CQL/IQL over abstain/enter/manage/exit with a SUPPORT-AWARE
// baseline fallback. Useful read: does it beat the baseline WITHOUT chasing out-of-support value, and which
// options are unsupported by the current logging? SHADOW.
const ORL_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, no_lift: COL_WARN, stale: COL_SHORT, cold: COL_MUTE,
};
const OfflineRL: React.FC<{ o: any }> = ({ o }) => {
  if (!o || o.available === false) return (
    <div style={card}><div style={sectionTitle}>offline RL challengers (CQL / IQL)</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{o?.note || 'Offline-RL challengers have not run yet.'}</span></div>);
  const tu = o.twin_utility || {}, saf = o.support_aware_fallback || {}, os = o.option_support || {};
  const ch = o.challenger || {}, issues = o.issues || [], unsup = o.unsupported_options || [];
  const beats = !!(ch.cql_beats_baseline || ch.iql_beats_baseline);
  const consHelps = (tu.cql ?? -9) > (tu.naive_no_fallback ?? 9);
  return (
    <div style={{ ...card, borderColor: o.health === 'healthy' ? '#2c3a18' : o.health === 'stale' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>offline RL challengers — CQL · IQL · abstain/enter/manage/exit · support-aware fallback (shadow)</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(ORL_HEALTH_COL[o.health] || COL_MUTE)}>{String(o.health).toUpperCase()}</span>
        <span style={pill(COL_MUTE)}>SHADOW</span>
        {beats ? <span style={pill(COL_LONG)}>beats baseline</span> : <span style={pill(COL_MUTE)}>no lift vs baseline</span>}
        <span style={{ fontSize: 10 }}>best <b style={{ color: '#cdd6e6' }}>{ch.best}</b></span>
      </div>
      {/* utilities: CQL / IQL / baseline / naive(no-fallback) / realized / oracle */}
      <div style={{ fontSize: 9.5, display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 3 }}>
        <span>CQL <b style={{ color: beats ? COL_LONG : COL_WARN }}>{fmt(tu.cql, 3)}</b></span>
        <span>IQL <b style={{ color: beats ? COL_LONG : COL_WARN }}>{fmt(tu.iql, 3)}</b></span>
        <span style={{ color: COL_MUTE }}>baseline <b style={{ color: '#cdd6e6' }}>{fmt(tu.baseline_global, 3)}</b></span>
        <span style={{ color: COL_MUTE }}>naive(no-fb) <b style={{ color: consHelps ? COL_SHORT : '#cdd6e6' }}>{fmt(tu.naive_no_fallback, 3)}</b></span>
        <span style={{ color: COL_MUTE }}>realized <b style={{ color: '#cdd6e6' }}>{fmt(tu.behavior_realized, 3)}</b></span>
        <span style={{ color: COL_MUTE }}>oracle <b style={{ color: '#7f9bc8' }}>{fmt(tu.oracle, 3)}</b></span>
      </div>
      <div style={{ fontSize: 9, color: COL_MUTE, marginBottom: 2 }}>
        support-aware fallback: CQL defers on <b style={{ color: COL_LONG }}>{fmt(saf.cql_fallback_rate, 2)}</b> of states
        (sparse {fmt(saf.sparse_state_rate, 2)}) · {consHelps ? <span style={{ color: COL_LONG }}>conservatism beats naive OOD-chaser</span> : 'conservatism neutral'}</div>
      {/* option support — manage/exit unsupported is the honest data-limit finding */}
      <div style={{ fontSize: 9, color: COL_MUTE }}>option support:
        {Object.entries(os).map(([k, v]: any) => (
          <span key={k} style={{ marginLeft: 6, color: v === 0 ? COL_WARN : '#cdd6e6' }}>{k} <b>{v}</b></span>))}
        {unsup.length ? <span style={{ color: COL_WARN }}> · unsupported: {unsup.join(', ')}</span> : null}</div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 3 }}>{o.verdict}</div>
    </div>
  );
};

// Risk-sensitive policy (design §3.7/§3.10/§8-416) — distributional CVaR + costs + CBF safety projection.
// Useful read: does the CVaR objective lower TAIL risk (CVaR_0.1 / worst / drawdown) vs the mean policy, and
// at what mean cost? SHADOW.
const RP_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, no_tail_gain: COL_WARN, stale: COL_SHORT, cold: COL_MUTE,
};
const RiskPolicy: React.FC<{ p: any }> = ({ p }) => {
  if (!p || p.available === false) return (
    <div style={card}><div style={sectionTitle}>amygdala / risk-sensitive policy (CVaR)</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{p?.note || 'Risk-sensitive policy has not run yet.'}</span></div>);
  const ev = p.eval || {}, td = p.tradeoff || {}, issues = p.issues || [];
  const helps = !!td.risk_sensitive_helps;
  const cols: [string, string][] = [
    ['mean', 'mean'], ['CVaR.10', 'cvar_10'], ['worst', 'worst'], ['maxDD', 'max_drawdown'], ['turn', 'turnover'],
  ];
  const rowsP: [string, any][] = [
    ['mean policy', ev.mean_policy], ['CVaR policy', ev.cvar_policy], ['CVaR+safety', ev.cvar_safety_policy],
    ['baseline', ev.baseline], ['realized', ev.realized],
  ];
  return (
    <div style={{ ...card, borderColor: p.health === 'healthy' ? '#2c3a18' : p.health === 'stale' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>amygdala / risk-sensitive policy — distributional · CVaR objective · realistic costs · CBF safety projection (shadow)</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(RP_HEALTH_COL[p.health] || COL_MUTE)}>{String(p.health).toUpperCase()}</span>
        <span style={pill(COL_MUTE)}>SHADOW</span>
        {helps ? <span style={pill(COL_LONG)}>lowers tail risk</span> : <span style={pill(COL_MUTE)}>no tail gain</span>}
        <span style={{ fontSize: 10 }}>tail gain <b style={{ color: helps ? COL_LONG : COL_WARN }}>{(td.tail_gain_cvar10_vs_mean ?? 0) >= 0 ? '+' : ''}{fmt(td.tail_gain_cvar10_vs_mean, 3)}</b></span>
        <span style={{ fontSize: 10, color: COL_MUTE }}>mean cost <b style={{ color: '#cdd6e6' }}>{fmt(td.mean_cost_vs_mean, 3)}</b></span>
        <span style={{ fontSize: 10, color: COL_MUTE }}>DD↓ <b style={{ color: COL_LONG }}>{fmt(td.drawdown_reduction_safety, 2)}</b></span>
      </div>
      {/* policy × tail-metric table */}
      <div style={{ display: 'grid', gridTemplateColumns: `92px repeat(${cols.length}, 1fr)`, gap: '1px 6px', fontSize: 9 }}>
        <span style={{ color: COL_MUTE }}></span>
        {cols.map(([lab]) => <span key={lab} style={{ color: COL_MUTE, fontWeight: 700 }}>{lab}</span>)}
        {rowsP.map(([name, ev1]) => (
          <React.Fragment key={name}>
            <span style={{ color: name.startsWith('CVaR') ? COL_LONG : '#cdd6e6' }}>{name}</span>
            {cols.map(([lab, key]) => (
              <span key={lab} style={{ color: key === 'max_drawdown' ? ((ev1?.[key] ?? 9) < 2 ? COL_LONG : COL_WARN)
                : key === 'cvar_10' ? ((ev1?.[key] ?? -9) > -0.2 ? COL_LONG : COL_WARN) : '#cdd6e6' }}>
                {fmt(ev1?.[key], key === 'turnover' ? 2 : 3)}</span>
            ))}
          </React.Fragment>
        ))}
      </div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 3 }}>{p.verdict}</div>
    </div>
  );
};

// Meta-learning (design §3.7/§5.4-7/§5.5 stage-6/§8-419) — real regime×VPIN cohort tasks + protected competence.
// Upgrades ml/maml.py (fixes synthetic-zero-state + target leak + per-pair). Useful read: does few-shot adapt
// beat the simpler baselines (esp. a single pooled model), and does the protected init avoid forgetting? SHADOW.
const ML_HEALTH_COL: Record<string, string> = {
  healthy: COL_LONG, no_edge: COL_WARN, integrity_fail: COL_SHORT, stale: COL_SHORT, cold: COL_MUTE,
};
const MetaLearning: React.FC<{ m: any }> = ({ m }) => {
  if (!m || m.available === false) return (
    <div style={card}><div style={sectionTitle}>cerebellum / meta-learning (regime adaptation)</div>
      <span style={{ fontSize: 10, color: COL_MUTE }}>{m?.note || 'Meta-learning has not run yet.'}</span></div>);
  const fs = m.few_shot || {}, pc = m.protected_competence || {}, integ = m.integrity || {}, issues = m.issues || [];
  const helps = !!fs.adaptation_helps, beatsPool = !!fs.beats_pooled;
  const integOk = !!integ.state_real && !integ.target_leakage;
  const cols: [string, string][] = [['noadapt', 'mean_noadapt'], ['maml', 'mean_maml'], ['scratch', 'mean_scratch'], ['pooled', 'mean_pooled']];
  return (
    <div style={{ ...card, borderColor: m.health === 'healthy' ? '#2c3a18' : m.health === 'integrity_fail' || m.health === 'stale' ? '#5a1d1d' : '#3a3318' }}>
      <div style={sectionTitle}>cerebellum / meta-learning — real regime×VPIN cohort tasks · few-shot Reptile · protected competence (shadow)</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, flexWrap: 'wrap' }}>
        <span style={pill(ML_HEALTH_COL[m.health] || COL_MUTE)}>{String(m.health).toUpperCase()}</span>
        <span style={pill(COL_MUTE)}>SHADOW</span>
        <span style={pill(integOk ? COL_LONG : COL_SHORT)}>{integOk ? 'no zero-state · no leak' : 'integrity fail'}</span>
        {helps ? <span style={pill(COL_LONG)}>earns its keep</span> : <span style={pill(COL_MUTE)}>no edge vs pooled</span>}
        <span style={{ fontSize: 10 }}>adapt gain <b style={{ color: (fs.adaptation_gain ?? 0) > 0 ? COL_LONG : COL_WARN }}>{(fs.adaptation_gain ?? 0) >= 0 ? '+' : ''}{fmt(fs.adaptation_gain, 3)}</b></span>
        <span style={{ fontSize: 10, color: COL_MUTE }}>pool gain <b style={{ color: beatsPool ? COL_LONG : COL_WARN }}>{fmt(fs.pool_gain, 3)}</b></span>
        <span style={{ fontSize: 10, color: COL_MUTE }}>{m.n_cohorts} cohorts</span>
      </div>
      {/* few-shot query MSE per strategy */}
      <div style={{ display: 'grid', gridTemplateColumns: `120px repeat(${cols.length}, 1fr)`, gap: '1px 6px', fontSize: 9, marginBottom: 4 }}>
        <span style={{ color: COL_MUTE, fontWeight: 700 }}>query MSE</span>
        {cols.map(([lab, key]) => (
          <span key={lab} style={{ color: key === 'mean_maml' ? COL_LONG : key === 'mean_pooled' ? '#7f9bc8' : COL_MUTE, fontWeight: 700 }}>{lab} {fmt(fs[key], 3)}</span>
        ))}
      </div>
      {/* protected competence */}
      <div style={{ fontSize: 9, color: COL_MUTE, marginBottom: 4 }}>
        protected competence: <b style={{ color: pc.competence_protected ? COL_LONG : COL_WARN }}>{pc.competence_protected ? 'preserved' : 'not shown'}</b>
        <span> (maml forgetting {fmt(pc.maml_forgetting, 2)}; sequential fine-tune Δ <b style={{ color: (pc.finetune_forgetting ?? 0) > 0 ? COL_SHORT : COL_LONG }}>{(pc.finetune_forgetting ?? 0) >= 0 ? '+' : ''}{fmt(pc.finetune_forgetting, 2)}</b> on '{pc.protected_cohort}'{pc.forgetting_demonstrated ? ' — FORGETS' : ' — positive transfer'})</span>
      </div>
      {issues.map((i: any, k: number) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 9, padding: '1.5px 0', alignItems: 'flex-start' }}>
          <span style={{ ...pill(SEV_COL[i.severity] || COL_MUTE), flexShrink: 0 }}>{i.severity}</span>
          <span style={{ flex: 1 }}><b style={{ color: '#cdd6e6' }}>{i.title}</b>
            <span style={{ color: COL_MUTE }}> — {i.detail}</span>
            {i.recommendation && <span style={{ color: '#7f9bc8' }}> ▸ {i.recommendation}</span>}</span>
        </div>
      ))}
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 3 }}>{m.verdict}</div>
    </div>
  );
};

const WholeBrain: React.FC = () => {
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    getSciBrainBrain().then(x => { setD(x); if (!x.available) setErr(x.error || 'unavailable'); })
      .catch(e => setErr(String(e?.message || e)));
  }, []);
  if (err) return <div style={{ color: COL_WARN, fontSize: 12, padding: 12 }}>Whole-brain unavailable: {err}</div>;
  if (!d) return <div style={{ color: COL_MUTE, fontSize: 12, padding: 12 }}>Loading whole-brain pulse…</div>;

  const u = d.uncertainty || {}, a = d.authority || {}, c = d.compute || {}, comp = d.competence || {};
  const bcast = d.broadcast || {};
  const noBypass = (a.n_open_bypasses ?? 1) === 0;

  return (
    <div style={{ maxHeight: 640, overflowY: 'auto', paddingRight: 4 }}>
      {/* health banner */}
      <div style={{ ...card, borderColor: d.all_regions_healthy ? '#2c3a18' : '#5a1d1d',
                    background: d.all_regions_healthy ? '#141a0f' : '#1a0f0f' }}>
        <span style={pill(d.all_regions_healthy ? COL_LONG : COL_SHORT)}>
          {d.all_regions_healthy ? '✓ ALL REGIONS HEALTHY' : '✗ REGION FAULT'}</span>
        <span style={{ fontSize: 10, color: COL_MUTE, marginLeft: 8 }}>belief symbol {d.symbol || '—'}</span>
        <span style={{ ...pill(noBypass ? COL_LONG : COL_SHORT), marginLeft: 8 }}>
          {noBypass ? 'no learned bypass' : `${a.n_open_bypasses} OPEN BYPASS`}</span>
      </div>

      {/* region activity */}
      <div style={card}>
        <div style={sectionTitle}>region activity — every cognitive-OS region · authority · health · key metric</div>
        {(d.regions || []).map((rg: any) => <RegionRow key={rg.region} rg={rg} />)}
      </div>

      {/* perception / training health — auto-diagnosed issues */}
      <TrainingHealth t={d.training_health} />

      {/* hippocampus / episodic memory — rare-failure preservation + pattern separation */}
      <EpisodicMemory e={d.episodic_memory} />

      {/* neocortex / slow consolidation — EWC forgetting + protected competence */}
      <ConsolidationHealth c={d.consolidation} />

      {/* isolated sleep cycle — off-hot-path maintenance jobs */}
      <SleepCycle s={d.sleep} />

      {/* abstention memory — reward correct abstention + preserved non-trade events */}
      <AbstentionMemory a={d.abstention} />

      {/* prefrontal cortex / world model — RSSM on rich sequences + honest imagination calibration + planning gate */}
      <WorldModel w={d.world_model} />

      {/* basal ganglia / hierarchical controllers — day→hour→minute coordination gain + ablation (shadow) */}
      <Controllers h={d.controllers} />

      {/* basal ganglia / offline RL challengers — CQL/IQL + support-aware fallback (shadow) */}
      <OfflineRL o={d.offline_rl} />

      {/* amygdala / risk-sensitive policy — distributional CVaR + costs + CBF safety projection (shadow) */}
      <RiskPolicy p={d.risk_policy} />

      {/* cerebellum / meta-learning — real regime/cohort tasks + protected competence (shadow) */}
      <MetaLearning m={d.meta_learning} />

      {/* cross-cutting glance views */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <div style={{ ...card, flex: 1, minWidth: 260 }}>
          <div style={sectionTitle}>uncertainty & calibrated ignorance</div>
          <Metric k="epistemic" v={u.epistemic} /><Metric k="aleatoric" v={u.aleatoric} /><br />
          <Metric k="disagreement" v={u.disagreement} /><Metric k="changepoint" v={u.changepoint} /><br />
          <span style={{ fontSize: 10 }}>P(action supported) <b style={{
            color: (u.p_action_supported ?? 0) >= 0.15 ? COL_LONG : COL_WARN }}>
            {fmt(u.p_action_supported)}</b>{u.abstention_recommended ?
              <span style={{ ...pill(COL_WARN), marginLeft: 6 }}>ABSTAIN</span> : null}</span>
        </div>
        <div style={{ ...card, flex: 1, minWidth: 260 }}>
          <div style={sectionTitle}>competence</div>
          <Metric k="mean calibration" v={comp.mean_calibration} /><Metric k="OOD" v={comp.n_ood} /><br />
          <Metric k="mean epistemic" v={comp.mean_epistemic} /><Metric k="mean aleatoric" v={comp.mean_aleatoric} /><br />
          <span style={{ fontSize: 9, color: COL_MUTE }}>top: <b style={{ color: COL_LONG }}>
            {(comp.top || []).join(', ') || '—'}</b></span><br />
          <span style={{ fontSize: 9, color: COL_MUTE }}>abstain-worthy (OOD): <b style={{ color: COL_SHORT }}>
            {(comp.abstain_worthy || []).join(', ') || 'none'}</b></span>
        </div>
        <div style={{ ...card, flex: 1, minWidth: 260 }}>
          <div style={sectionTitle}>authority & no-bypass</div>
          <span style={{ fontSize: 9.5, color: COL_MUTE }}>live capital path: <b style={{ color: COL_LONG }}>
            {(a.live_capital_components || []).join(', ') || '—'}</b></span><br />
          <Metric k="invariants ok" v={a.all_invariants_ok ? 'yes' : 'NO'} />
          <Metric k="open bypasses" v={a.n_open_bypasses} /><br />
          <span style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 }}>
            {Object.entries(a.by_effective_mode || {}).map(([m, n]) => (
              <span key={m} style={pill(COL_MUTE)}>{m.replace(/_/g, ' ')} {n as number}</span>))}</span>
        </div>
        <div style={{ ...card, flex: 1, minWidth: 260 }}>
          <div style={sectionTitle}>compute & attention budgets</div>
          <Metric k="load factor" v={c.load_factor} /><Metric k="compute fraction" v={c.compute_fraction} /><br />
          {Object.entries(c.budgets || {}).filter(([k]) => k !== 'compute_fraction').map(([k, v]) => (
            <Metric key={k} k={k} v={v} />))}
        </div>
      </div>

      {/* broadcast */}
      <div style={card}>
        <div style={sectionTitle}>workspace broadcast — the salient few that reach the shared belief</div>
        <span style={{ fontSize: 9.5, color: COL_MUTE }}>workspace: <b style={{ color: '#cdd6e6' }}>
          {(bcast.workspace || []).join(' · ') || '—'}</b></span><br />
        <span style={{ fontSize: 9.5, color: COL_MUTE }}>thalamus-admitted: <b style={{ color: '#5a86c8' }}>
          {(bcast.thalamus_evidence || []).join(' · ') || '—'}</b></span>
      </div>
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 4, lineHeight: 1.5 }}>{d.note}</div>
    </div>
  );
};
export default WholeBrain;
