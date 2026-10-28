// 用 CDP 验证 demo 页真实可交互：填输入、点发送、等流式结束、截图并回读 DOM
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const CHROME = process.env.CHROME_EXE;
const URL = process.argv[2] || 'http://127.0.0.1:8080/demo/';
const OUT = process.argv[3] || 'demo_shot.png';
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdpdemo-'));

const chrome = spawn(
  CHROME,
  ['--headless=new', '--remote-debugging-port=9334', '--no-first-run', '--disable-gpu',
   '--window-size=1280,900', `--user-data-dir=${userDataDir}`,
   '--enable-unsafe-swiftshader', 'about:blank'],
  { stdio: 'ignore' },
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  let wsUrl = null;
  for (let i = 0; i < 40; i++) {
    try {
      const j = await (await fetch('http://127.0.0.1:9334/json/version')).json();
      if (j.webSocketDebuggerUrl) { wsUrl = j.webSocketDebuggerUrl; break; }
    } catch (_) {}
    await sleep(300);
  }
  if (!wsUrl) throw new Error('cdp not ready');

  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}, sessionId) =>
    new Promise((res, rej) => {
      const mid = ++id;
      pending.set(mid, { res, rej });
      ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
    });

  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    }
  });

  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const s = (m, p) => send(m, p, sessionId);

  await s('Page.enable');
  await s('Runtime.enable');
  await s('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });

  await s('Page.navigate', { url: URL });
  await sleep(2500);

  const steps = [];
  const evalJs = async (expr) => {
    const r = await s('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result?.value;
  };

  steps.push(['initial status', await evalJs(`document.getElementById('status').textContent`)]);
  steps.push(['deviceId', await evalJs(`document.getElementById('kDevice').textContent`)]);
  steps.push(['model', await evalJs(`document.getElementById('kModel').textContent`)]);

  // 类型化输入 + 点击发送（真实用户路径）
  await evalJs(`(() => {
    const t = document.getElementById('input');
    t.value = 'Hello from the browser smoke test, please reply.';
    t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('send').click();
    return true;
  })()`);
  await sleep(4000);

  steps.push(['bubble count', await evalJs(`document.querySelectorAll('.msg').length`)]);
  steps.push(['bot reply', await evalJs(`(document.querySelector('.msg.bot .bubble')||{}).textContent`)]);
  steps.push(['latency', await evalJs(`document.getElementById('kLatency').textContent`)]);
  steps.push(['res dump', await evalJs(`document.getElementById('resDump').textContent.slice(0,200)`)]);

  const shot = await s('Page.captureScreenshot', { format: 'png' });
  if (shot?.data) fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));

  for (const [k, v] of steps) console.log(`${k}: ${String(v).slice(0, 200)}`);

  try { chrome.kill(); } catch (_) {}
  process.exit(0);
})().catch((e) => {
  console.error('ERR', e.message);
  try { chrome.kill(); } catch (_) {}
  process.exit(1);
});
