/**
 * TodoWatch - Local HTTP Server
 * Serves the HTML widget and provides close-control endpoints.
 *
 * Endpoints:
 *   GET  /         -> HTML page (with app-mode CSS/JS injected)
 *   GET  /cmd      -> { cmd: "close" | "none" }  (polled by HTML)
 *   POST /close    -> signal the widget to close
 *   POST /stop     -> server self-exit
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 18964;
const HTML_FILE = path.join(__dirname, '\u6BCF\u65E5\u6E05\u5355.html'); // 每日清单.html

let closeRequested = false;

// Injected when served over HTTP: remove 78vh cap so card fills the window,
// and position the app window at bottom-right of screen.
const INJECT = [
  '<style>#card{max-height:calc(100vh - 40px) !important;}</style>',
  '<script>',
  'try{window.resizeTo(360,680);',
  'window.moveTo(screen.availWidth-380,screen.availHeight-700);}catch(e){}',
  '<\/script>',
].join('');

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');

  // HTML polls this to check for remote close command
  if (req.url === '/cmd') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ cmd: closeRequested ? 'close' : 'none' }));
    return;
  }

  // Close bat calls this to request graceful close
  if (req.url === '/close' && (req.method === 'POST' || req.method === 'GET')) {
    closeRequested = true;
    res.end('ok');
    return;
  }

  // HTML calls this on quitApp() to kill the server
  if (req.url === '/stop' && req.method === 'POST') {
    res.end('ok');
    setTimeout(() => process.exit(0), 200);
    return;
  }

  // Serve HTML with injected app-mode overrides
  try {
    let html = fs.readFileSync(HTML_FILE, 'utf-8');
    html = html.replace('</body>', INJECT + '\n</body>');
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(html);
  } catch (e) {
    res.statusCode = 500;
    res.end('HTML file not found');
  }
});

server.listen(PORT);
