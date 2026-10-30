#!/bin/bash
# Final acceptance check for the public DeepSeek endpoint.
set -e
B=https://ds.sfi-choir.ru

echo "=== TLS ==="
echo | openssl s_client -connect ds.sfi-choir.ru:443 -servername ds.sfi-choir.ru 2>/dev/null \
    | openssl x509 -noout -subject -issuer -dates

echo "=== endpoints ==="
printf 'root        : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 "$B/"
printf 'v1/healthz  : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 "$B/v1/healthz"
printf 'v1/models   : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 "$B/v1/models"
printf 'chat no key : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 -X POST "$B/v1/chat/completions" \
    -H 'Content-Type: application/json' -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"x"}]}'

echo "=== state ==="
export PM2_HOME=/var/www/www-root/data/.pm2
export PATH=/usr/lib/ispnodejs/bin:$PATH
pm2 list --no-color 2>&1 | grep -E 'courses|ds\.sfi' || true
echo "proxy pid: $(pgrep -f 'bin/deepseek-cursor-proxy' | tr '\n' ' ')"
echo "cache db : $(ls -la /var/www/www-root/data/deepseek-cursor-proxy/reasoning_content.sqlite3 2>/dev/null || echo 'not created yet (appears on first real request)')"
