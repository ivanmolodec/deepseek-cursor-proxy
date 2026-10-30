// Throwaway SSE emitter: pretends to be the proxy on 127.0.0.1:9000 and sends
// five events one second apart, so we can see whether nginx forwards them
// incrementally or buffers the whole answer.
const http = require('http');

const server = http.createServer((req, res) => {
  if (req.url !== '/v1/sse') {
    res.writeHead(404).end('no');
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
  let n = 0;
  const timer = setInterval(() => {
    n += 1;
    res.write(`data: {"tick":${n}}\n\n`);
    if (n >= 5) {
      clearInterval(timer);
      res.end();
    }
  }, 1000);
});

server.listen(9000, '127.0.0.1', () => console.log('fake sse listening on 9000'));
