#!/bin/bash
# Prove whether nginx forwards server-sent events incrementally.
# Temporarily replaces the proxy with a fake SSE emitter on the same port,
# measures arrival times of each event, then lets the bridge restore the proxy.
export PM2_HOME=/var/www/www-root/data/.pm2
NODE=/usr/lib/ispnodejs/bin/node

echo "=== stopping the real proxy ==="
pkill -f 'bin/deepseek-cursor-proxy' || true
sleep 2

echo "=== starting fake sse emitter ==="
$NODE /tmp/fake-sse.js >/tmp/fake-sse.log 2>&1 &
fake=$!
sleep 2

echo "=== event arrival times (each event is 1s apart upstream) ==="
curl -sN --max-time 20 https://ds.sfi-choir.ru/v1/sse | while IFS= read -r line; do
    case "$line" in
        data:*) echo "$(date +%H:%M:%S.%3N) $line" ;;
    esac
done

echo "=== stopping fake emitter ==="
kill "$fake" 2>/dev/null || true
sleep 1

echo "=== waiting for the bridge to bring the real proxy back ==="
for i in $(seq 1 45); do
    sleep 1
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 https://ds.sfi-choir.ru/v1/healthz || echo err)
    if [ "$code" = "200" ]; then
        echo "proxy restored after ~${i}s"
        break
    fi
done
pgrep -f 'bin/deepseek-cursor-proxy' | tr '\n' ' '
echo
