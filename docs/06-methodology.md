# 06 · 方法论：Flutter AOT 逆向 + 后端存活判定

这份流程提炼自 NexBot 的分析过程，适用于「拿到一个 Flutter APK，判断它还能不能被打通」
这类任务。按顺序执行，可以在两小时内得出结论，避免在死项目上白干。

## 0. 顺序很重要

先判断**后端还活着吗**，再做协议还原。反过来做的话，可能花一天反编译出一个
早就没有对手方的协议。

```
存活判定 → 架构识别 → 协议还原 → 复刻验证 → 结论
  30min      20min       60min      30min
```

## 1. 第一步：存活判定

### 1.1 提 URL

```bash
python - <<'PY'
import zipfile, re
z = zipfile.ZipFile('app.apk')
d = z.read('lib/arm64-v8a/libapp.so')
seen = []
for m in re.finditer(rb'https?://[A-Za-z0-9\.\-_/:%\?=&]{4,140}', d):
    u = m.group().decode('latin-1')
    if u not in seen: seen.append(u)
print("\n".join(seen))
PY
```

注意 Flutter 的字符串在 AOT 快照里是明文，通常能直接抓到后端域名。

### 1.2 逐个查 DNS（用 DoH，绕开本地缓存与污染）

```bash
curl "https://dns.google/resolve?name=<host>&type=A"
# Status=3 → NXDOMAIN，域名不存在
# Status=0 → 有 A 记录
```

**关键判据：查父域。** 如果 `example.dev` 本身 NXDOMAIN，那所有子域都不用再查了。
NexBot 就是这种情况（`globeapp.dev` 整个注销）。

### 1.3 不要被「部分存活」骗了

一个 App 常同时依赖多个后端：业务后端、Supabase/Firebase、CDN、监控。
NexBot 的 Firebase 项目仍然活着（Remote Config 能拉到配置），但业务后端已死。
**只看 Firebase 会得出「还活着」的错误结论。**

判据：问「哪个域名承载真正的业务请求」。
方法：在 `libapp.so` 里看哪个域名出现在业务逻辑附近（例如 `chatCompletion` 调用点周围），
而不是只出现在 SDK 初始化里。

## 2. 第二步：架构识别

### 2.1 看 Dart 包结构（最快的指纹）

```bash
rg -o "package:[a-z_0-9]+/" libapp_strings.txt | sort | uniq -c | sort -rn | head -40
```

自研包名（如 `package:app_core/`、`package:nexbot_client/`）直接暴露架构分层，
而 `package:serverpod_client/` 一出现就说明后端是 Serverpod。

### 2.2 常见后端的识别特征

| 特征包 | 后端类型 | 后续怎么打 |
|--------|----------|-----------|
| `serverpod_client` / `serverpod_serialization` | 自建 Serverpod | 反编译 `*_client/src/protocol/` 拿模型 |
| `supabase_flutter`（gotrue/postgrest/realtime） | Supabase | 提 anon key，直接打 REST |
| `firebase_ai` / `firebasevertexai` | Firebase AI Logic | 需 App Check，基本封死 |
| `dio` + 无上述 | 自研 HTTP | 找 baseUrl 常量 + 抓包 |

### 2.3 确认它是不是「白标」

如果看到 `package:core_client/`（配置下发）+ 多个姊妹 App 共用同一 `app_core`，
基本可以判定是白标 SaaS：一套后端服务多个前端品牌。这类后端通常零鉴权或弱鉴权，
是最容易打穿的类型 —— 前提是它还活着。

## 3. 第三步：协议还原

### 3.1 优先用 blutter 反编译，不要靠 strings 猜

`strings` 出来的东西噪声极大（本地化文案、SQLite 关键字、vim 帮助），
而且 Dart AOT 里字段名不一定以独立字符串存在。blutter 给出的是
真实的类定义、字段顺序、方法体：

```bash
# Windows 前置
python scripts/init_env_win.py          # 下载 capstone + icu

# 必须在 VS Native Tools 环境里跑
cmd /c 'call "C:\Program Files\Microsoft Visual Studio\...\vcvars64.bat" && python blutter.py <lib/arm64-v8a> <outdir>'
```

产物：

```
outdir/
├── asm/<package>/<path>.dart   ← 反编译出的类与方法（含字符串常量）
├── pp.txt                      ← object pool 全量转储（字段名、枚举值在这）
└── objs.txt
```

### 3.2 读 asm 的三个诀窍

1. **找字段名**：看 `toJson()` 里的数组构造，每个 `r16 = "<key>"` 就是序列化 key：

   ```
   // 0x878e18: r16 = "model"
   // 0x878e18: add x16, PP, #0xc, lsl #12 ; [pp+0xcec0] "model"
   ```

2. **找调用链**：`bl #0xADDR ; [package:xxx] Class::method` 就是跨文件调用，
   顺着它可以从 UI 一路走到传输层。

3. **找枚举**：`pp.txt` 里 `Obj!MessageRole@...: { off_8: int(0x1), off_10: "user" }`
   直接给出枚举值与序号的对应关系。

### 3.3 传输层是框架公开行为，可以被推断

`serverpod_client` 是开源框架，反编译里能确认到的关键点（NexBot 实测）：

```
postUrl()              → HTTP 方法
ContentType::ContentType → application/json
Utf8Encoder::convert   → UTF-8 body
"authorization"        → 唯一的自定义头
"ws" / "wss" / "ping"  → WebSocket 通道
```

剩下的 URI 拼接细节可以按框架源码补齐，但要在文档里标注为「推断」。

### 3.4 找不到的字段要显式列进「未决项」

NexBot 的 `chatCompletion` 方法名就没能恢复（类体为空 + 字符串未入池）。
正确做法是写进文档的未决项表格，说明「怎么补齐」，而不是含糊带过或用猜测填坑。

## 4. 第四步：复刻验证

反编译出来的协议，**用可执行代码验证一遍**，否则只是纸上推断。

最小闭环：

1. 写一个 mock 服务端，按还原的结构返回数据；
2. 写冒烟测试，覆盖正常路径 + 参数校验 + 鉴权失败 + 限流；
3. 写 demo 页，用真实浏览器点一遍；
4. 写压测脚本，跑一遍拿到真实数字。

这四步能证明「你对协议的理解是自洽且可运行的」。它的价值不在于能连真上游
（已经连不上了），而在于：
- 上游恢复时，服务端参考实现已经存在；
- 下次遇到同框架的 App，这套代码能直接抄。

NexBot 的实测结果（`evidence/stress-*.json`）：

| 模式 | 请求 | 成功率 | QPS | P95 |
|------|------|--------|-----|-----|
| JSON | 600 | 100% | 160.8 | 125.0 ms |
| SSE | 300 | 100% | 54.9 | 401.1 ms |

## 5. 踩过的坑

| 坑 | 现象 | 解法 |
|----|------|------|
| 只查子域不查父域 | 子域 NXDOMAIN，误以为「临时故障」 | 查父域，父域不存在就是彻底注销 |
| 被 Firebase 存活误导 | Remote Config 能拉，以为后端还活着 | 区分「遥测/配置」与「业务请求」 |
| 用 strings 找字段名 | 全是本地化文案噪声，字段名找不到 | 上 blutter，读 `toJson()` |
| 断言「后端死了所以没得逆」 | 直接放弃 | 协议还原仍有价值（复用方法论 + 上游恢复时直接接） |
| Python `threading.Lock` 嵌套 | mock server 请求全部挂起超时 | 嵌套取锁用 `RLock` |
| `ThreadingHTTPServer` 默认 backlog=5 | 20 并发压测 ~2% 连接被 reset | `request_queue_size = 512` |

## 6. 结论模板

做完后按这三档给结论，不要含糊：

- **可打通**：后端存活 + 鉴权可绕/零鉴权 → 做 2API，真实验证；
- **协议可还原但不可用**：后端已死 / 强制 App Check → 归档协议 + 复刻实现，明确写「不可对接」；
- **不可还原**：native 层全加密 + 反调试 → 说明阻塞点与所需条件。

NexBot 属于第二档。
