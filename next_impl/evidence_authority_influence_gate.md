# Evidence, Authority, and Influence Gate

Status: **RULE REPLACEMENT + IMPLEMENTATION PLAN — owner requested 2026-06-09**.

## Objective

Replace the fixed “50 shadow cycles before live” Rule 14 with a faster, evidence-based rule that:

1. Does not delay low-risk observability and additive instrumentation.
2. Scales evidence requirements with the blast radius of a change.
3. Makes every candidate component's influence visible on each applicable opened trade.
4. Distinguishes actual causal influence from suppressed, advisory, and counterfactual evidence.
5. Preserves rollback, kill switches, and explicit approval for capital-affecting authority.

## Replacement Rule 14

### Rule 14 — Evidence, Authority, and Influence Gate

Every new or changed model, strategy, formula, gate, SL/TP/leverage rule, risk controller, or
autonomous recommendation must declare an authority level:

```text
observe -> advise -> bounded_canary -> live -> veto
```

There is no fixed cycle count. Promotion evidence scales with risk and must include:

- deterministic and adversarial tests;
- exact version/config lineage;
- applicable-decision coverage and effective sample size;
- incremental utility/risk evidence versus the current champion;
- calibration, failure telemetry, and support/OOD checks where relevant;
- a kill switch and tested rollback;
- explicit owner approval before first capital-affecting promotion.

Every applicable decision/opened trade records an influence manifest containing:

```text
source, version, authority, status, raw signal, applied gain/effect,
suppression/veto reason, uncertainty, and evidence IDs
```

Statuses are:

```text
applied | gate_applied | suppressed | abstained | advised |
counterfactual_only | unavailable
```

Only `applied` and `gate_applied` may be described as causes of the opened action. Advisory,
suppressed, abstained, and counterfactual components remain visible but must never be presented as
having caused the trade.

### Risk-tier evidence

| Tier | Example | Initial authority and promotion requirement |
|---|---|---|
| 0 | logging, dashboard, provenance, replay capture | deploy immediately after tests; no trading effect |
| 1 | bounded calibration/residual, ranking tie-breaker | observe/advise; promote when evaluation proves incremental value and rollback |
| 2 | direction, entry gate, sizing, SL/TP, exit policy | observe/advise or bounded canary; owner approval plus strong out-of-sample/risk evidence |
| 3 | leverage, kill-switch logic, autonomous live mutation, risk-limit relaxation | no autonomous promotion; explicit owner approval and strongest safety/rollback evidence |

## Active-code audit

| Path | Current meaning | Required treatment |
|---|---|---|
| SciBrain module/router/fusion bank | actually influences current SciBrain opens | persist full decision snapshot and normalized influence manifest |
| SciBrain IC tracker | learned router evidence; already affects gains when enabled | show resulting router gain/effect in each opened trade |
| SciBrain audit/remediation | post-open advisory unless independently consumed | persist audit and recommendation as `advised`, never falsely mark applied |
| legacy coherence versus historical score | one selected, one comparison-only | persist selected component as applied and alternative as observe-only |
| TFT multi-timeframe candidate | selected only when its toggle is enabled | record selected/candidate authority and values |
| OI/LS soft bonuses | currently active when toggles are on | include within legacy influence manifest when legacy path opens |
| EV override | evaluates rejected signals; cannot influence an existing opened trade | keep in rejected/counterfactual ledger; stamp only if it actually originates a future open |
| shadow ablation models | research evaluator, never production predictor | remain counterfactual-only; do not mislabel as trade cause |

## Implementation slices

1. Update canonical rule stores and active Scientist-Brain goal documents.
2. Add `Decision.influence_manifest()` and persist the full SciBrain decision snapshot at open.
3. Add a normalized legacy-engine influence manifest inside `signals_at_entry`.
4. Persist SciBrain audit remediation/recommendation onto `signals_at_entry`.
5. Normalize influence data in `/trades/open`.
6. Add expandable influence evidence to the Open Trades dashboard.
7. Verify manifests, dry-run opening, dashboard build, and runtime deployment without opening a
   test trade.
