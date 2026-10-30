'use strict';

// Bridge between ISPmanager's nginx and deepseek-cursor-proxy.
//
// ISPmanager owns the nginx vhost for ds.sfi-choir.ru and proxies every request
// to a unix socket (SOCKET env var), expecting our app to listen there. The
// DeepSeek proxy speaks TCP only, so this file plays the role of "the app":
// it listens on that socket and forwards to 127.0.0.1:9000.
//
// It also keeps the Python proxy alive. PM2 restarts this file when the panel
// or the machine restarts, and the proxy is started from here, so the pair
// comes back together without root, systemd or pm2 startup.
//
// Streaming matters: Cursor reads chat completions as server-sent events. The
// response carries X-Accel-Buffering: no so nginx hands chunks on immediately
// instead of buffering them until the answer is complete.

const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');

const TARGET_HOST = '127.0.0.1';
const TARGET_PORT = 9000;
const PROXY_HOME = '/var/www/www-root/data/deepseek-cursor-proxy';
const PROXY_BIN = `${PROXY_HOME}/.venv/bin/deepseek-cursor-proxy`;
const PID_FILE = '/var/www/www-root/data/deepseek-cursor-proxy/proxy.pid';
const SOCKET_PATH = process.env.SOCKET || '/var/www/www-root/data/nodejs/0.sock';

// Headers that belong to a single hop and must not be forwarded.
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'proxy-connection',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

/** Resolve true when the proxy answers its health endpoint. */
function proxyHealthy(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: TARGET_HOST, port: TARGET_PORT, path: '/healthz', timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', () => resolve(false));
  });
}

/** Start the Python proxy unless something already answers on its port. */
async function ensureProxy() {
  if (await proxyHealthy()) {
    return true;
  }

  // A stale pid from a previous run can hold the port; clear it before spawning.
  try {
    const stalePid = Number.parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (Number.isInteger(stalePid) && stalePid > 0) {
      process.kill(stalePid, 'SIGTERM');
      console.log(`bridge: stopped stale proxy pid ${stalePid}`);
    }
  } catch (err) {
    if (err.code !== 'ENOENT' && err.code !== 'ESRCH') {
      console.log(`bridge: could not clear stale pid: ${err.message}`);
    }
  }

  const child = spawn(PROXY_BIN, [], {
    cwd: PROXY_HOME,
    detached: false,
    stdio: ['ignore', 'inherit', 'inherit'],
    env: process.env,
  });

  child.on('error', (err) => console.error(`bridge: proxy spawn failed: ${err.message}`));
  child.on('exit', (code, signal) =>
    console.log(`bridge: proxy exited code=${code} signal=${signal}`),
  );

  if (child.pid) {
    fs.writeFileSync(PID_FILE, `${child.pid}\n`);
    console.log(`bridge: started proxy pid ${child.pid}`);
  }

  // Give it a few seconds; a cold start binds the port quickly.
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (await proxyHealthy(500)) {
      return true;
    }
  }
  console.error('bridge: proxy did not become healthy in time');
  return false;
}

/** Copy headers, dropping the hop-by-hop ones. */
function filterHeaders(headers) {
  const result = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) {
      result[name] = value;
    }
  }
  return result;
}

/** Forward one incoming request to the local proxy and stream the answer back. */
function forward(req, res) {
  const options = {
    host: TARGET_HOST,
    port: TARGET_PORT,
    method: req.method,
    path: req.url,
    headers: filterHeaders(req.headers),
  };

  const upstream = http.request(options, (upstreamRes) => {
    const headers = filterHeaders(upstreamRes.headers);
    // Without this nginx buffers the event stream and Cursor shows nothing
    // until the whole completion has arrived.
    headers['X-Accel-Buffering'] = 'no';
    res.writeHead(upstreamRes.statusCode || 502, headers);
    upstreamRes.pipe(res);
    upstreamRes.on('error', () => res.destroy());
  });

  upstream.on('error', (err) => {
    console.error(`bridge: upstream error: ${err.message}`);
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    }
    res.end(JSON.stringify({ error: { message: `proxy unreachable: ${err.message}` } }));
  });

  // A client that goes away must not leave the upstream request hanging.
  req.on('aborted', () => upstream.destroy());
  req.pipe(upstream);
}

async function main() {
  if (fs.existsSync(SOCKET_PATH)) {
    fs.unlinkSync(SOCKET_PATH);
  }

  const server = http.createServer((req, res) => {
    if (req.url === '/' || req.url === '/healthz') {
      // Cheap self-check that does not touch the model.
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: true, bridge: 'deepseek-cursor-proxy' }));
      return;
    }
    forward(req, res);
  });

  // Long thinking answers can take minutes; the reply is only over when the
  // proxy closes it.
  server.requestTimeout = 0;
  server.headersTimeout = 60000;
  server.timeout = 0;

  server.listen(SOCKET_PATH, () => {
    fs.chmodSync(SOCKET_PATH, 0o660);
    console.log(`bridge: listening on ${SOCKET_PATH} -> ${TARGET_HOST}:${TARGET_PORT}`);
  });

  server.on('error', (err) => {
    console.error(`bridge: cannot listen on ${SOCKET_PATH}: ${err.message}`);
    process.exit(1);
  });

  // The proxy is started after the socket is bound. Holding the socket first
  // means nginx never sees it missing: while the proxy warms up a request gets
  // an honest 502 instead of a dropped connection.
  ensureProxy();

  // Self-healing: if the proxy dies, the next tick brings it back.
  setInterval(() => {
    proxyHealthy().then((ok) => {
      if (!ok) {
        console.log('bridge: proxy unhealthy, restarting it');
        ensureProxy();
      }
    });
  }, 30000).unref();

  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      console.log(`bridge: ${signal} received, shutting down`);
      server.close();
      process.exit(0);
    });
  }
}

main().catch((err) => {
  console.error(`bridge: fatal: ${err.stack || err.message}`);
  process.exit(1);
});
