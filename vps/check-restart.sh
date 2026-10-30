#!/bin/bash
# Verify the bridge after a PM2 restart and measure the recovery window.
set -e
APP=/var/www/www-root/data/www/ds.sfi-choir.ru
export PM2_HOME=/var/www/www-root/data/.pm2
export PATH=/usr/lib/ispnodejs/bin:$PATH

pm2 restart ds.sfi-choir.ru --no-color >/dev/null 2>&1

for i in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 https://ds.sfi-choir.ru/v1/healthz || echo none)
    echo "t+${i}: ${code}"
    if [ "$code" = "200" ]; then
        break
    fi
    sleep 0.5
done

echo "--- proxy processes ---"
pgrep -af 'bin/deepseek-cursor-proxy' || echo 'none'
echo "--- bridge log tail ---"
pm2 logs ds.sfi-choir.ru --lines 12 --nostream --no-color 2>&1 | tail -n 14
