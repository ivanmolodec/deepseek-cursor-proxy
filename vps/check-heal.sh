#!/bin/bash
# 1) confirm the bridge binds before starting the proxy, 2) confirm nginx does
# not buffer (X-Accel-Buffering), 3) kill the proxy and watch it come back.
export PM2_HOME=/var/www/www-root/data/.pm2

echo "=== 1. log order (listen must come before started proxy) ==="
grep -E 'bridge: (listening|started proxy)' /var/www/www-root/data/.pm2/logs/ds.sfi-choir.ru-out.log | tail -n 4

echo "=== 2. response headers through nginx ==="
curl -si --max-time 10 https://ds.sfi-choir.ru/v1/healthz | tr -d '\r' | grep -iE '^HTTP/|^x-accel|^server|^content-type'

echo "=== 3. self-healing: kill the proxy ==="
old=$(pgrep -f 'bin/deepseek-cursor-proxy')
echo "old pid: $old"
kill "$old"
sleep 2
curl -s -o /dev/null -w 'immediately after kill: %{http_code}\n' --max-time 5 https://ds.sfi-choir.ru/v1/healthz || echo 'after kill: connection failed'

echo "waiting for the 30s health tick..."
for i in $(seq 1 45); do
    sleep 1
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 https://ds.sfi-choir.ru/v1/healthz || echo err)
    if [ "$code" = "200" ]; then
        echo "recovered after ~${i}s"
        break
    fi
done
new=$(pgrep -f 'bin/deepseek-cursor-proxy')
echo "new pid: $new"
