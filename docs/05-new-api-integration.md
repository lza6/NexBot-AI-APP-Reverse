# 05 · new-api 对接指南

## 0. 先给结论

| 问题 | 答案 |
|------|------|
| 现在能把 NexBot 接成 new-api 渠道吗？ | **不能。** 上游 `nexbot.globeapp.dev` 已 DNS 注销，没有任何可以发请求的目标。 |
| 花时间写 adapter 有意义吗？ | 现在没有。adapter 写完也只能打到空气。 |
| 那这份文档还有用吗？ | 有，三件事：① 上游若恢复，按第 2 节直接照做；② 第 3 节给出**立刻可落地**的替代方案；③ 第 4 节给出用本地复刻做联调/压测的做法。 |

请先看 `03-dead-backend-evidence.md` 和 `04-v190-migration.md`，确认上述前提再往下读。

---

## 1. 为什么不是「改改就能用」

NexBot 的协议有三处与 OpenAI 不同，任何 new-api adapter 都必须处理：

1. **响应不是 `choices` 结构**，而是单字段 `{"content": "..."}`；
2. **鉴权不是 Bearer key**，而是 `secret` 字段里一个客户端自签 HS256 JWT
   （密钥内置在 APK 里，见 `02-protocol.md` 第 6 节）；
3. **流式走 WebSocket**，不是 HTTP SSE。

这三点都已经被还原并写进 `server/mock_serverpod.py`，可以直接复用作为参照实现。

---

## 2. 上游恢复后的对接步骤（照做即可）

### 2.1 前置验证（先做，1 分钟）

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://nexbot.globeapp.dev/
# 必须不是 000。若还是 000，直接停止，回到第 0 节。
```

### 2.2 确认方法名（唯一未决字段）

`02-protocol.md` 第 2 节标注了 `chatCompletion` 端点下方法名未能从快照恢复。
恢复后先抓一次真实请求确认真实路径（例如是 `/chatCompletion/chat` 还是
`/chatCompletion/generate`），再写死到 adapter 里。

### 2.3 new-api 侧的实现位置

按 new-api 现有约定，新增渠道需要动这些地方：

```
relay/channel/nexbot/adaptor.go     ← ConvertRequest / ConvertResponse / DoRequest
relay/channel/nexbot/constant.go    ← 渠道类型常量
relay/channel/nexbot/dto.go         ← MyCompletionRequest / MyCompletionResponse
constant/channel.go                 ← 注册渠道类型与名称
relay/relay_adaptor.go              ← 在 adaptor 工厂里挂上
web/src/.../channel-edit           ← 前端加渠道表单（可选）
```

### 2.4 adapter 骨架（Go）

```go
package nexbot

import (
    "bytes"
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "crypto/hmac"
    "crypto/sha256"
    "encoding/base64"
    "time"
)

// MyCompletionRequest 对应 docs/02-protocol.md 3.1
type MyCompletionRequest struct {
    Model    string                `json:"model"`
    System   string                `json:"system,omitempty"`
    Messages []MyCompletionMessage `json:"messages"`
    Length   int                   `json:"length,omitempty"`
    Secret   string                `json:"secret"`
}

type MyCompletionMessage struct {
    Role    string           `json:"role"`
    Content []MessageContent `json:"content"`
}

type MessageContent struct {
    Type     string                  `json:"type"`               // text | image_url | input_audio
    Text     string                  `json:"text,omitempty"`
    ImageURL *MessageContentImageURL `json:"imageUrl,omitempty"`
    URL      string                  `json:"url,omitempty"`
}

type MessageContentImageURL struct {
    URL string `json:"url"`
}

type MyCompletionResponse struct {
    Content string `json:"content"`
}

// ConvertOpenAIRequest 把 OpenAI 请求转成 NexBot 请求。
// 注意：OpenAI 的 system 角色要提升成顶层 system 字段。
func ConvertOpenAIRequest(openaiReq map[string]any, secret string) (*MyCompletionRequest, error) {
    // ... 按 docs/02-protocol.md 第 7 节的映射表逐项转换
    // max_tokens -> length（语义是字数上限，不是 token 上限，注意别当成 token 用）
}

// BuildSecret 复刻 APK 里的 UtilsCrypto.generateSecret()
// key = 内置种子常量的前 30 个字符（见 docs/02-protocol.md 第 6 节）
func BuildSecret(appID, deviceID, seed string) string {
    key := seed
    if len(key) > 30 {
        key = key[:30]
    }
    header := b64(`{"alg":"HS256","typ":"JWT"}`)
    now := time.Now().Unix()
    body := b64(fmt.Sprintf(`{"app":%q,"dev":%q,"iat":%d,"exp":%d}`,
        appID, deviceID, now, now+30*60))
    signingInput := header + "." + body
    mac := hmac.New(sha256.New, []byte(key))
    mac.Write([]byte(signingInput))
    return signingInput + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func b64(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }

// ConvertResponse 把 {"content": "..."} 包装回 OpenAI 结构
func ConvertResponse(content, model string) map[string]any {
    return map[string]any{
        "object": "chat.completion",
        "created": time.Now().Unix(),
        "model":  model,
        "choices": []map[string]any{{
            "index":         0,
            "message":       map[string]any{"role": "assistant", "content": content},
            "finish_reason": "stop",
        }},
    }
}
```

### 2.5 流式的额外工作

流式必须走 WebSocket（`{ws|wss}://{host}/websocket`，`EndpointRef::stream`）。
new-api 现有渠道适配器基本都基于 HTTP SSE，这里需要单独实现一个 WebSocket 客户端：
建立连接 → 发送 `sendStreamMessage` → 逐块把 `MyCompletionResponse.content` 转成
OpenAI `chat.completion.chunk` 往外吐。这块工作量比普通渠道大，实现前先确认
方法名与分片封装（见 2.2）。

### 2.6 验收标准

```bash
curl http://<new-api>/v1/chat/completions -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"model":"nexbot-gemini","messages":[{"role":"user","content":"hi"}]}'
# 期望：返回 OpenAI 结构，content 非空
```

---

## 3. 立刻可落地的替代方案（推荐）

既然拿不到 NexBot 的上游，可以直接复刻它的**交互形态**，用你自己的 key 跑：

### 3.1 用 new-api 原生 Gemini/OpenAI 渠道

1. 在 new-api 里新建一个 Gemini 渠道，填你自己的 Google API key；
2. 模型名填 `gemini-3.5-flash-lite` 之类（只是展示名，实际请求由渠道决定）；
3. 完成 —— 这一步不需要任何代码改动。

### 3.2 复刻 NexBot 的提示词形态

NexBot 的「语气 / 字数 / 素材」三个控件最终都拼进 `system` 字段
（`02-protocol.md` 第 5 节），模板是：

```text
Keep your response under {length} words.
Respond in a tone of: {tone}.
Data: {smart_form_data}
```

在 new-api 里可以用「模型预设 / 系统提示词」或重定向规则把这段拼进去，
就能得到接近原版的输出风格。tone 的默认值是 `auto`。

### 3.3 想连 UI 一起复刻

`demo/index.html` 是现成的单页实现，把它的请求地址从
`http://127.0.0.1:8787` 改成你的 new-api 地址、把 body 换成标准 OpenAI 格式即可。

---

## 4. 用本地复刻做联调与压测

`server/mock_serverpod.py` 同时提供 Serverpod 形态和 OpenAI 兼容形态，
所以可以把它当「假上游」来验证 new-api 侧的 adapter 逻辑（不产生任何外部费用）：

```bash
# 假上游
python server/mock_serverpod.py           # :8787

# new-api 里把渠道 base_url 指向 http://127.0.0.1:8787
# 走 OpenAI 兼容路径：/v1/chat/completions

# 压测这个联合链路
node tools/stress.js --url http://127.0.0.1:8787 --concurrency 20 --requests 600
```

这样可以在上游不存在的情况下，把 adapter 的字段映射、错误处理、超时/重试/限流
逻辑全部验证完 —— 等真上游恢复时，只需要改 base_url 和确认方法名。

---

## 5. 检查清单

上游恢复后动手前，逐条确认：

- [ ] `curl https://nexbot.globeapp.dev/` 不是 000
- [ ] 抓到一次真实请求，确认 `/{endpoint}/{method}` 里的方法名
- [ ] 确认 `authorization` 头里放的是什么（空？固定值？还是同样用 `secret`）
- [ ] 确认流式是 WebSocket 且拿到第一个分片的真实结构
- [ ] 确认 `length` 语义（字数 vs token）
- [ ] 确认额度策略（`credit_daily_free` 是否服务端强制）
- [ ] 在 mock 上先把 adapter 全绿，再指向真上游
