# 02 · 协议规格（从 AOT 快照还原）

> 置信度标注：**[确认]** = 反编译中直接读到；**[推断]** = 依据框架公开实现与上下文补齐，未能在运行时验证（后端已注销）。

## 1. Endpoint 清单

`package:nexbot_client/src/protocol/client.dart` **[确认]**

```dart
class Client extends ServerpodClientShared {
  late final EndpointChatCompletion chatCompletion;   // 唯一的业务端点
}

class EndpointChatCompletion extends EndpointRef {}
```

序列化管理器 `Protocol extends SerializationManager` 注册了 4 个模型：
`MyCompletionMessage`、`MessageContent`、`MessageContentImageUrl`、`MyCompletionRequest`、`MyCompletionResponse`。

`package:core_client` 另有 `EndpointConfig`（配置/链接/构建号下发），服务端地址硬编码为
`https://core-server.globeapp.dev/` **[确认]**。

## 2. 传输格式

依据 `serverpod_client/src/serverpod_client_shared.dart` 与 `serverpod_client_io.dart` 的反编译 **[确认 + 推断]**：

| 项 | 值 |
|----|----|
| 普通调用 | `POST {baseUrl}{endpoint}/{method}` |
| 流式调用 | `WebSocket {ws\|wss}://{host}/websocket`，`EndpointRef::stream` + `sendStreamMessage` |
| Content-Type | `application/json`（`ContentType::ContentType` 构造后赋值给 `_HttpHeaders.contentType`） |
| Body 编码 | `Utf8Encoder::convert`，`contentLength` 显式设置 |
| 鉴权头 | `authorization`（`_HttpHeaders::add`） |
| 响应 | 直接返回结果对象的 JSON；错误为 `{error, stackTrace, code, type}` 且 HTTP 码非 200 |

反编译中可见的证据（`asm/serverpod_client/src/`）：

```
postUrl()                       → dart:_http _HttpClient::postUrl        （HTTP 方法确定）
ContentType::ContentType        → 请求体类型
Utf8Encoder::convert            → body 编码
"authorization"                 → 唯一的自定义头
"ws" / "wss" / "ping" / "pong"  → WebSocket 通道与心跳
"Opened Serverpod Stream!!!"    → App 侧日志，出现在 openStreamingConnection 之后
```

调用顺序（`messages_screen.controller.dart` 反编译）**[确认]**：

```
ServerpodClientShared::openStreamingConnection()
  → EndpointRef::stream(...)          // 返回 Stream<SerializableModel>
  → StreamIterator 逐块消费
```

> **方法名**：`chatCompletion` 端点下被调用方法的具体名称未能从快照中恢复 ——
> blutter 输出的 `EndpointChatCompletion` 类体为空（方法大概率被内联，字符串常量未进对象池）。
> 复刻实现暂用 `chat`（Serverpod 默认命名习惯）。这是本仓库唯一的未决字段，
> 需要真实后端存活时抓包确认。

## 3. 数据模型

### 3.1 `MyCompletionRequest` **[确认]**

| 字段 | 类型 | 说明 |
|------|------|------|
| `model` | String | 模型标识 |
| `system` | String | 系统提示词 |
| `messages` | `List<MyCompletionMessage>` | 对话历史 |
| `length` | int | 回复字数上限（客户端另有 `Keep your response under N words` 提示拼接） |
| `secret` | String | 客户端自签 HS256 JWT，见第 6 节 |

```jsonc
{
  "model": "gpt-4o",
  "system": "You are a helpful assistant.",
  "messages": [
    { "role": "user", "content": [ { "type": "text", "text": "hello" } ] }
  ],
  "length": 300,
  "secret": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...."
}
```

### 3.2 `MyCompletionMessage` **[确认]**

| 字段 | 类型 |
|------|------|
| `role` | String，枚举 `assistant` / `user` / `system` |
| `content` | `List<MessageContent>`（内部实现字段名 `contentList`） |

反编译中确认的三元组（对象池直接给出）：

```
Obj!MessageRole : assistant  (off_8 = 0)
Obj!MessageRole : user       (off_8 = 1)
Obj!MessageRole : system     (off_8 = 2)
```

即序列化时按字符串写 `role`，按序号 0/1/2 参与枚举比较。

### 3.3 `MessageContent` **[确认]**

| 字段 | 类型 | 适用 type |
|------|------|-----------|
| `type` | String | `text` / `image_url` / `input_audio` |
| `text` | String? | `text` |
| `imageUrl` | `MessageContentImageUrl?` | `image_url` |
| `url` | String? | `input_audio` |

### 3.4 `MessageContentImageUrl` **[确认]**

| 字段 | 类型 |
|------|------|
| `url` | String |

### 3.5 `MyCompletionResponse` **[确认]**

| 字段 | 类型 |
|------|------|
| `content` | String |

注意：响应只有一个 `content` 字符串，**没有** OpenAI 风格的 `choices` / `usage` 结构。
流式场景下每一块都是新的 `MyCompletionResponse`（增量文本）**[推断]**。

## 4. 配置下发

`core_client` 的 `EndpointConfig` 返回三个模型：

| 模型 | 字段（反编译可见） |
|------|-------------------|
| `ConfigBuild` | 构建号 / 强制更新相关 |
| `ConfigLinks` / `ConfigLinksStore` | 隐私、条款、支持等外链 |
| `ConfigEmails` | 联系邮箱 |

此外 Firebase Remote Config 另有一层运行时开关（实测拉取值）：

```json
{
  "credit_daily_free": "2",
  "gemini_llm_model": "gemini-3.5-flash-lite",
  "maintenance_mode": "false",
  "max_input_words": "3000",
  "min_input_words": "50",
  "min_build_number": "0",
  "paywall_show_on_app_open": "true"
}
```

两层配置同时存在：Serverpod 的 `config` 端点（随业务后端一起死掉），
与 Remote Config（仍存活）。后者是 1.9.3 之后仍能影响旧客户端的唯一通道。

## 5. 客户端拼装的提示词模板

反编译对象池中紧邻 `chatCompletion` 调用点的字符串 **[确认]**：

```
"Keep your response under "   + <length> + " words.\n"
"Respond in a tone of: "      + <tone>   + ".\n"
"Data: "                      + <smart form 数据>
"auto"                        ← tone 默认值
```

`features/smart_form` 的 tone / length / data 三个表单值最终拼进 `system` 字段，
而不是作为独立参数 —— 所以**服务端拿到的 `system` 里已经包含这些约束**。

这意味着：若要复刻 NexBot 的交互手感，只需要复刻这三条模板即可，不需要复刻后端。

## 6. `secret`：客户端自签 JWT

`package:app_core/utils/utils.crypto.dart` **[确认]**：

```dart
static String generateSecret() {
  final Map<String, String> payload = {
    'app': config.<appId>,
    'dev': <deviceId>,
  };
  final jwt = JWT(payload);
  final key = secretConfig.<...>.<...>.substring(0, 30);   // 取前 30 字符
  return jwt.sign(SecretKey(key), expiresIn: <N>.minutes);  // 实测时长常量 30
}
```

要点：

1. **密钥不是服务端下发，而是内置在客户端**（`app_core/config.dart` 的常量），
   取该常量的前 30 个字符作为 HS256 密钥。这是典型的「白标 App 共享一套后端」做法。
2. payload 只有 `app` 与 `dev` 两个字段（`dart_jsonwebtoken` 自动补 `iat` / `exp`）。
3. 内嵌种子常量（86 字符，归档原文）：

```
BIxNxvzZzWLzRtf3_IYKmBIkhanz1pQtdgTHG6E4D7-cgi2Kl4tAqvFR1ZV0CyoxX8MFNY5JGVu8e5n81J0TPbY
```

推导出的实际签名密钥（前 30 字符）：`BIxNxvzZzWLzRtf3_IYKmBIkhanz1p`

`server/mock_serverpod.py` 用 Python 标准库完整复刻了这两个函数
（`sign_secret` / `verify_secret`），冒烟测试覆盖：
正常签名通过、非法 token 拒绝、篡改签名拒绝。

## 7. 与 OpenAI 协议的映射

| OpenAI | NexBot |
|--------|--------|
| `messages[].role` | 同名，取值集合一致 |
| `messages[].content`（string） | `[{type:"text", text:"…"}]` |
| `messages[].content[].type="image_url"` | `{type:"image_url", imageUrl:{url}}` |
| `system` 角色消息 | 提升为顶层 `system` 字段 |
| `max_tokens` | `length`（语义上是「字数上限」，不是 token 上限） |
| `stream: true` | 走 WebSocket 通道 |
| 无对应 | `secret`（客户端自签 JWT） |

`server/mock_serverpod.py` 的 `/v1/chat/completions` 就是按这张表做的双向转换，
方便 new-api 之类的网关直接指向。

## 8. 未决项

| 项 | 原因 | 如何补齐 |
|----|------|----------|
| `chatCompletion` 下方法名 | 反编译类体为空，字符串常量未入池 | 后端存活时抓包；或拿到新版 APK 再反编译对比 |
| 流式分片的确切封装 | WebSocket 消息体未在快照中还原 | 同上 |
| `AppCheck` 无关，但新版需要 | 见 `04-v190-migration.md` | — |
