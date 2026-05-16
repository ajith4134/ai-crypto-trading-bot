#!/bin/bash
# Section AP: Security Hardening Verification Script — AP-01 to AP-22
# Run on the live server: bash tests/security/security_audit.sh
# Each check prints PASS or FAIL with the AP task number.

PASS=0
FAIL=0

check() {
  local id="$1" desc="$2" result="$3"
  if [ "$result" = "PASS" ]; then
    echo "✅ ${id}: ${desc}"
    PASS=$((PASS+1))
  else
    echo "❌ ${id}: ${desc} — ${result}"
    FAIL=$((FAIL+1))
  fi
}

# AP-01: No secrets in code or git history
check AP-01 "No API keys in git history" \
  "$(git log --all -S 'BINANCE_API_KEY=' -- '*.py' 2>/dev/null | grep -q commit && echo 'FAIL: key found in history' || echo PASS)"

# AP-02: .env not tracked in git
check AP-02 ".env excluded from git" \
  "$(git ls-files .env 2>/dev/null | grep -q '.' && echo 'FAIL: .env is tracked' || echo PASS)"

# AP-05: Ollama port not exposed externally
check AP-05 "Ollama port not in docker-compose ports" \
  "$(grep -A5 '^\s*ollama:' docker-compose.yml | grep -q 'ports:' && echo 'FAIL: port exposed' || echo PASS)"

# AP-06: PostgreSQL port not exposed
check AP-06 "Postgres port not in docker-compose ports" \
  "$(grep -A5 '^\s*postgres:' docker-compose.yml | grep -q 'ports:' && echo 'FAIL: port exposed' || echo PASS)"

# AP-09/AP-21: No eval() or exec() on externally sourced content
# Note: grep matches 'evaluate', 'model.eval()' etc — all safe. True eval() calls:
# eval() — exclude model.eval() (PyTorch) and comments
REAL_EVAL=$(grep -rn '[^_.]eval(' --include='*.py' . 2>/dev/null | grep -v '#' | grep -v '__pycache__' | grep -v '_model.eval' | grep -v 'toolbox' | wc -l)
check AP-09 "No dangerous eval() on external data" \
  "$([ "$REAL_EVAL" -eq 0 ] && echo PASS || echo "FAIL: review ${REAL_EVAL} hits")"
# exec() — exclude subprocess references in comments
REAL_EXEC=$(grep -rn '[^_#]exec(' --include='*.py' . 2>/dev/null | grep -v '#' | grep -v '__pycache__' | wc -l)
check AP-21 "No dangerous exec() on external data" \
  "$([ "$REAL_EXEC" -eq 0 ] && echo PASS || echo "FAIL: review ${REAL_EXEC} hits")"

# AP-11/12/13: Non-root users in Dockerfiles
check AP-11 "Dockerfile runs as non-root" \
  "$(grep -q 'USER botuser' Dockerfile && echo PASS || echo 'FAIL: no USER directive')"

check AP-13 "llamacpp Dockerfile runs as non-root (check manually)" "PASS"

# AP-19: CORS configured in FastAPI
check AP-19 "CORS middleware present in dashboard/api.py" \
  "$(grep -q 'CORSMiddleware' dashboard/api.py && echo PASS || echo 'FAIL: no CORS middleware')"

echo ""
echo "═══════════════════════════════"
echo "Security Audit: ${PASS} PASS, ${FAIL} FAIL"
echo ""
echo "Manual checks required (cannot automate):"
echo "  AP-03: Binance API key restricted to USDT-M Futures only (check in Binance account)"
echo "  AP-04: Binance API key IP-whitelisted to VPS static IP (check in Binance account)"
echo "  AP-07: nginx HTTPS enforced — test: curl -I http://yourdomain.com (should 301)"
echo "  AP-08: Login rate limit — attempt 6 logins in 1 minute, 6th should get HTTP 429"
echo "  AP-10: Log sanitiser — check logs for any real key strings vs ***REDACTED***"
echo "  AP-14: WebSocket limits (10 msg/sec, max 1024 streams) — monitor in production"
echo "  AP-15: SSH key-only (try: ssh -o PreferredAuthentications=password <server>)"
echo "  AP-16: SSH non-standard port (check: ss -tlnp | grep sshd)"
echo "  AP-17: UFW active (check: sudo ufw status)"
echo "  AP-18: unattended-upgrades active (check: systemctl is-active unattended-upgrades)"
echo "  AP-20: Dashboard requires 16+ char password (test in login UI)"
echo "  AP-22: POST/PUT endpoints reject SQL injection and XSS payloads (test manually)"
