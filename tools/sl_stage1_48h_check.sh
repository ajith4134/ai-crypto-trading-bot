#!/usr/bin/env bash
# cont. 69x one-shot: 48h check of the stage-1 SL loosening (floor removed → true 60% trail).
# Writes a report, then removes its own crontab line. Created 2026-06-03.
set -uo pipefail
OUT=/opt/trading-bot/logs/sl_stage1_check_48h.log
SINCE="2026-06-03 16:10:00"   # deploy moment (UTC)
{
  echo "===== SL stage-1 48h check @ $(date -u '+%Y-%m-%d %H:%M:%S') UTC ====="
  echo "(change deployed $SINCE UTC: 6-8% band now trails true 60% of peak, no +6% floor)"
  echo
  echo "--- trailing_sl net by peak/capital bucket SINCE the change ---"
  docker exec trading-bot-postgres-1 psql -U botuser -d trading_bot -c "
    SELECT CASE WHEN capital_usdt>0 THEN
      width_bucket(100.0*peak_pnl_usdt/capital_usdt, ARRAY[0,6,8,16,30]) ELSE 0 END AS bucket,
      COUNT(*) n, ROUND(SUM(net_pnl_usdt)::numeric,2) net, ROUND(AVG(net_pnl_usdt)::numeric,2) avg
    FROM trades WHERE status='closed' AND exit_reason='trailing_sl'
      AND exit_time > TIMESTAMP '$SINCE' AND capital_usdt>0
    GROUP BY 1 ORDER BY 1;"
  echo "buckets: 1=<6%  2=6-8%(CHANGED)  3=8-16%  4=16-30%  5=>30%"
  echo
  echo "--- BASELINE before the change (48h pre): 6-8% was +\$972 / 230 trades (avg +\$4.22) ---"
  echo
  echo "--- toggle + counter state ---"
  echo -n "risk:ladder_stage1_floor = "; docker exec trading-bot-redis-1 redis-cli GET risk:ladder_stage1_floor
  echo "  (nil/0 = new 60% trail ACTIVE ; 1 = reverted to +6% pin)"
  echo -n "trail:capital_ladder_applied_count = "; docker exec trading-bot-redis-1 redis-cli GET trail:capital_ladder_applied_count
  echo
  echo ">>> VERDICT: if bucket-2 (6-8%) net is NEGATIVE or clearly worse than the +\$972 baseline,"
  echo ">>> REVERT with:  docker exec trading-bot-redis-1 redis-cli SET risk:ladder_stage1_floor 1"
  echo "============================================================================"
} >> "$OUT" 2>&1
# self-remove this one-shot cron line
( crontab -l 2>/dev/null | grep -v 'sl_stage1_48h_check.sh' ) | crontab -
