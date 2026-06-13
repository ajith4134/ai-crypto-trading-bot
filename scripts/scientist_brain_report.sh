#!/usr/bin/env bash
# Scientist-Brain Launchpad — login progress report.
# Wired as a Claude Code SessionStart hook so a build report prints on every
# login/session start (owner mandate, 2026-06-08). Reads the task ledger in
# scientist_brain_PROGRESS.md and computes % complete + what's left.
# Pure read-only; never fails the session (always exit 0).

set -o pipefail
TRACKER="/opt/trading-bot/next_impl/scientist_brain_PROGRESS.md"

if [ ! -f "$TRACKER" ]; then
  echo "🧠 Scientist-Brain Launchpad: progress tracker not found at $TRACKER"
  exit 0
fi

echo "🧠 ════════════════ SCIENTIST-BRAIN LAUNCHPAD — BUILD REPORT ════════════════"
echo "    (auto-generated on login · source: $TRACKER)"
echo

awk '
  /^## PHASE/      { p=$0; sub(/^## /,"",p); if(!(p in seen)){seen[p]=1; order[++n]=p} cur=p; next }
  /^- \[[xX]\]/    { if(cur){pd[cur]++; pt[cur]++} td++; tt++; next }
  /^- \[ \]/       { if(cur){pt[cur]++} tt++;
                     if(opens<6){opens++; nexttask[opens]=$0} next }
  END {
    op = (tt? td*100/tt : 0)
    printf "    OVERALL: %d%%  (%d of %d tasks complete)\n\n", op, td, tt
    printf "    ── Phase breakdown ──\n"
    for(i=1;i<=n;i++){ q=order[i]; pct=(pt[q]? pd[q]*100/pt[q] : 0)
      bar=""; full=int(pct/10); for(b=0;b<10;b++){ bar = bar (b<full?"█":"░") }
      printf "      %s %3d%%  %s  (%d/%d)\n", bar, pct, q, pd[q], pt[q] }
    if(opens>0){
      printf "\n    ── Next up (open tasks) ──\n"
      for(i=1;i<=opens;i++){ t=nexttask[i]; sub(/^- \[ \] */,"",t); printf "      • %s\n", t }
    } else {
      printf "\n    ✅ All tracked tasks complete.\n"
    }
  }
' "$TRACKER"

echo
echo "🧠 ═══════════════════════════════════════════════════════════════════════════"
exit 0
