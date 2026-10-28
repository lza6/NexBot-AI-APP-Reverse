#!/usr/bin/env node
/**
 * stress.js — 对本地协议复刻服务端做并发压测
 *
 * 用法:
 *   node tools/stress.js [--url http://127.0.0.1:8787] [--concurrency 20]
 *                        [--requests 400] [--stream] [--out report.json]
 *
 * 说明:
 *   - 每个 worker 使用独立 deviceId, 避免 mock 服务端的每日额度把统计打歪;
 *   - 指标为 HTTP 层真实往返 (客户端耗时), 不包含任何真实上游;
 *   - 结论仅代表本地实现, 不代表 NexBot 真实后端性能。
 */

const fs = require('fs');

function parseArgs(argv) {
  const out = { url: 'http://127.0.0.1:8787', concurrency: 20, requests: 400, stream: false, out: '' };
  for (let i = 2; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--url') out.url = argv[++i];
    else if (a === '--concurrency') out.concurrency = parseInt(argv[++i], 10);
    else if (a === '--requests') out.requests = parseInt(argv[++i], 10);
    else if (a === '--stream') out.stream = true;
    else if (a === '--out') out.out = argv[++i];
  }
  return out;
}

const cfg = parseArgs(process.argv);

const PROMPTS = [
  'Write a short product description for a note taking app.',
  'Summarize the following idea in one sentence: offline first sync.',
  'Explain the difference between latency and throughput.',
  'Give me three blog title ideas about AI writing assistants.',
  'Rewrite this sentence to be more concise: we are in the process of doing.',
  'List four risks of shipping a mobile app without crash reporting.',
  'What is a reasonable retry policy for idempotent HTTP calls?',
  'Describe a smoke test for a chat completion endpoint.',
];

const pct = (sorted, p) => {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx];
};

const summarize = (values) => {
  const s = [...values].sort((a, b) => a - b);
  return {
    count: s.length,
    min: s.length ? s[0] : 0,
    p50: pct(s, 50),
    p90: pct(s, 90),
    p95: pct(s, 95),
    p99: pct(s, 99),
    max: s.length ? s[s.length - 1] : 0,
    mean: s.length ? Math.round(s.reduce((a, b) => a + b, 0) / s.length) : 0,
  };
};

async function oneRequest(deviceId, idx) {
  const prompt = PROMPTS[idx % PROMPTS.length];
  const body = {
    model: 'gemini-3.5-flash-lite',
    user: deviceId,
    stream: cfg.stream,
    messages: [{ role: 'user', content: [{ type: 'text', text: prompt }] }],
  };  const t0 = process.hrtime.bigint();
  let status = 0;
  let bytes = 0;
  let errKind = null;
  try {
    const res = await fetch(`${cfg.url}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    status = res.status;
    if (cfg.stream && res.body) {
      const reader = res.body.getReader();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value ? value.length : 0;
      }
    } else {
      const text = await res.text();
      bytes = text.length;
      if (status !== 200) errKind = 'http_' + status;
    }
  } catch (e) {
    errKind = e.name === 'AbortError' ? 'timeout' : 'network';
  }
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  const ok = status === 200 && !errKind;
  return { ok, status, ms, bytes, errKind };
}

(async () => {
  console.log(`target      : ${cfg.url}`);
  console.log(`concurrency : ${cfg.concurrency}`);
  console.log(`requests    : ${cfg.requests}`);
  console.log(`stream      : ${cfg.stream}`);

  const health = await fetch(`${cfg.url}/health`).then((r) => r.json()).catch(() => null);
  if (!health) {
    console.error('服务端不可达，先启动 server/mock_serverpod.py');
    process.exit(1);
  }

  const before = health.stats;
  const startedAt = Date.now();

  const results = [];
  let cursor = 0;
  const latencies = [];
  const errors = {};
  // 每次压测轮次用独立前缀，避免复用上一轮的 deviceId 撞上每日额度
  const runId = Date.now().toString(36);

  await Promise.all(
    Array.from({ length: cfg.concurrency }, async (_unused, workerId) => {
      while (true) {
        const idx = cursor;
        cursor += 1;
        if (idx >= cfg.requests) break;
        // 每个请求使用独立 deviceId：模拟大量独立设备，避免触发 mock 的
        // 每日免费额度（2 条/设备/天）把吞吐数字打歪。
        const deviceId = `stress-${runId}-${workerId}-${idx}`;
        const r = await oneRequest(deviceId, idx);
        results.push(r);
        if (r.ok && Number.isFinite(r.ms)) latencies.push(r.ms);
        if (!r.ok) {
          const key = r.errKind || `status_${r.status}`;
          errors[key] = (errors[key] || 0) + 1;
        }
      }
    }),
  );

  const wallMs = Date.now() - startedAt;
  const total = results.length;
  const ok = results.filter((r) => r.ok).length;
  const after = await fetch(`${cfg.url}/stats`).then((r) => r.json()).catch(() => null);

  const report = {
    generatedAt: new Date().toISOString(),
    target: cfg.url,
    mode: cfg.stream ? 'stream' : 'json',
    concurrency: cfg.concurrency,
    plannedRequests: cfg.requests,
    executedRequests: total,
    succeeded: ok,
    failed: total - ok,
    successRate: total ? +(ok / total).toFixed(4) : 0,
    wallMs,
    qps: +(total / (wallMs / 1000)).toFixed(2),
    latencyMs: summarize(latencies),
    errors,
    serverStats: { before, after },
    note: 'local protocol replica only — not a benchmark of the real NexBot backend',
  };

  console.log('\n=== result ===');
  console.log(`success ${ok}/${total} (${(report.successRate * 100).toFixed(2)}%)`);
  console.log(`wall ${wallMs} ms  qps ${report.qps}`);
  console.log(
    `latency ms  p50=${report.latencyMs.p50.toFixed(1)} p90=${report.latencyMs.p90.toFixed(1)} ` +
      `p95=${report.latencyMs.p95.toFixed(1)} p99=${report.latencyMs.p99.toFixed(1)} max=${report.latencyMs.max.toFixed(1)}`,
  );
  if (Object.keys(errors).length) console.log('errors', errors);

  if (cfg.out) {
    fs.writeFileSync(cfg.out, JSON.stringify(report, null, 2));
    console.log('report written to', cfg.out);
  }

  process.exit(report.failed === 0 ? 0 : 2);
})();
