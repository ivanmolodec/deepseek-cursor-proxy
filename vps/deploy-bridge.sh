#!/bin/bash
# Install the bridge into the site directory, restart it, and report recovery.
set -e

APP=/var/www/www-root/data/www/ds.sfi-choir.ru
export PM2_HOME=/var/www/www-root/data/.pm2
export PATH=/usr/lib/ispnodejs/bin:$PATH

sed -i 's/\r$//' /tmp/bridge-server.js
cp /tmp/bridge-server.js "$APP/server.js"
chmod 644 "$APP/server.js"
rm -f /tmp/bridge-server.js

echo "--- installed marker ---"
grep -c 'cannot listen on' "$APP/server.js" || true

pm2 restart ds.sfi-choir.ru --no-color >/dev/null 2>&1

for i in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 https://ds.sfi-choir.ru/v1/healthz || echo err)
    echo "t+$((i))0ms: ${code}"
    if [ "$code" = "200" ]; then
        break
    fi
    sleep 0.25
done

echo "--- public endpoint ---"
curl -s --max-time 10 https://ds.sfi-choir.ru/v1/models | cut -c1-100
echo
echo "--- proxy pid ---"
pgrep -f 'bin/deepseek-cursor-proxy' | tr '\n' ' '
echo
