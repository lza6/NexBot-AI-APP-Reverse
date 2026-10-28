# NexBot AI (com.nexbot.app) — APK 逆向与协议还原归档

> **一句话结论：这个 App 的 AI 上游已经不存在了。**
> 1.9.3 版的 Serverpod 后端 `nexbot.globeapp.dev` 与 Supabase 项目
> `zlymralqctgzydksekht.supabase.co` 已经整体注销（DNS 返回 NXDOMAIN），
> 当前 Google Play 上的 1.21.6 已改用 Firebase AI Logic 并强制 App Check。
> 因此本仓库**不是**一个可以接入的 2API 渠道，而是一份可复现的逆向分析归档 +
> 协议复刻实现。

---

## 这个仓库里有什么

| 目录 | 内容 |
|------|------|
| `docs/01-architecture.md` | 1.9.3 的完整技术架构（Flutter + Serverpod + Supabase + Firebase + RevenueCat） |
| `docs/02-protocol.md` | **核心产出**：从 AOT 快照还原出的完整协议规格（请求/响应模型、endpoint、传输格式、JWT 自签机制） |
| `docs/03-dead-backend-evidence.md` | 「后端已死」的证据链与逐步复现命令 |
| `docs/04-v190-migration.md` | 新版迁移到 Firebase AI Logic 的分析，以及为什么无法复用 |
| `docs/05-new-api-integration.md` | **对接指南**：为什么现在接不了、上游恢复时怎么接、以及可落地的替代方案 |
| `docs/06-methodology.md` | 可复用的方法论：Flutter AOT 逆向 + 后端存活判定流程 |
| `server/mock_serverpod.py` | 协议复刻服务端（零依赖），把协议规格变成可执行代码 |
| `demo/index.html` | 单文件 demo 页，真实可跑（流式输出、请求/响应回显） |
| `tests/smoke.py` | 29 项冒烟测试，覆盖正常/异常/额度/鉴权路径 |
| `tools/stress.js` | 压测脚本（并发、QPS、P50/P95/P99、错误分类） |
| `tools/verify_demo.js` | 用真实 Chromium 走一遍 demo 的用户路径并截图 |
| `evidence/` | 全部原始证据：DNS 记录、Remote Config 响应、App Check 401、反编译提取、压测报告、截图 |

---

## 快速开始

需要 Python 3.9+ 与 Node 18+（无第三方依赖）。

```bash
# 1. 起协议复刻服务端
python server/mock_serverpod.py
# 监听 http://127.0.0.1:8787

# 2. 另一个终端：起静态服务托管 demo
python -m http.server 8080 --directory .
# 打开 http://127.0.0.1:8080/demo/

# 3. 冒烟测试（会自己拉临时端口起一个实例）
python tests/smoke.py

# 4. 压测
node tools/stress.js --url http://127.0.0.1:8787 --concurrency 20 --requests 600
node tools/stress.js --url http://127.0.0.1:8787 --concurrency 20 --requests 300 --stream
```

实测结果（本机，`MOCK_LATENCY_MS=120`，即人为注入 120ms 延迟）：

| 模式 | 请求 | 成功率 | QPS | P50 | P95 | P99 |
|------|------|--------|-----|-----|-----|-----|
| JSON | 600 | 100% | 160.8 | 121.8 ms | 125.0 ms | 157.6 ms |
| SSE 流式 | 300 | 100% | 54.9 | 357.1 ms | 401.1 ms | 403.6 ms |

原始报告：`evidence/stress-json.json`、`evidence/stress-stream.json`。

> 这些数字衡量的是**本地复刻实现**，不是 NexBot 真实后端的性能。真实后端已无法测量。

---

## 协议速查（完整版见 `docs/02-protocol.md`）

```text
传输      : Serverpod（Dart 后端框架）
普通调用  : POST  {baseUrl}{endpoint}/{method}
            Content-Type: application/json
            Authorization: <auth key>
            body = arguments 的 JSON
流式调用  : WebSocket {ws|wss}://{host}/websocket
端点      : chatCompletion（该 App 唯一的业务端点）
```

```jsonc
// 请求：MyCompletionRequest
{
  "model": "…",          // string
  "system": "…",         // string，系统提示词
  "messages": [          // List<MyCompletionMessage>
    {
      "role": "user",    // assistant | user | system
      "content": [       // List<MessageContent>
        { "type": "text", "text": "…" },
        { "type": "image_url", "imageUrl": { "url": "…" } },
        { "type": "input_audio", "url": "…" }
      ]
    }
  ],
  "length": 300,         // int，回复字数上限
  "secret": "eyJhbGciOi…" // string，客户端自签 HS256 JWT
}

// 响应：MyCompletionResponse
{ "content": "…" }
```

`secret` 的生成逻辑（`app_core/utils/utils.crypto.dart::UtilsCrypto.generateSecret`）：

```text
JWT(payload: { app: <appId>, dev: <deviceId>, iat, exp })
   .sign(SecretKey(seed.substring(0, 30)), expiresIn: 30 minutes)
```

种子常量内嵌在 `app_core/config.dart`，已归档到 `docs/02-protocol.md` 第 6 节。
`server/mock_serverpod.py` 里有该算法的 Python 复刻（`sign_secret` / `verify_secret`），
冒烟测试覆盖了签名校验与篡改拒绝两条路径。

---

## 关于「能不能接进 new-api」

**现在不能，且不是改动量的问题，是没有对手方。** 详见 `docs/05-new-api-integration.md`。

可落地的替代路径有两条，文档里都给了具体做法：

1. 用你自己的 Gemini / OpenAI key 在 new-api 里开原生渠道，再用本项目还原出的
   prompt 结构（`Keep your response under N words` / `Respond in a tone of: X` /
   `Data: …`）复刻 NexBot 的交互手感；
2. 等上游恢复（`nexbot.globeapp.dev` 重新可解析）后，按 `docs/02-protocol.md` 实现
   adapter —— 服务端参考实现已经在 `server/` 里了。

---

## 免责声明

- 本仓库内容仅用于协议分析、安全研究与互操作性研究。
- APK 由第三方（ElaMods）修改并签名，`bin.mt.signature.KillerApplication` 为常见的签名校验绕过注入，与原始发行方无关。
- 仓库中出现的 Firebase 客户端 API key、Supabase anon key 均为**客户端内置的公开凭证**（任何装了该 App 的人都能拿到），且后端项目已被删除，不具备实际访问能力。
- 不含任何可绕过第三方访问控制（如 App Check / Play Integrity）的实现，也不分发原始 APK。

## 时间线

- 2026-10-29：1.9.3 逆向完成，确认后端注销；收集证据、构建协议复刻与验证脚本。
