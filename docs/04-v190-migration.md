# 04 · 新版迁移：Firebase AI Logic + 强制 App Check

线上最新版（1.21.6）不再使用自建 Serverpod 后端，改为客户端直连 Google
Vertex AI（Firebase AI Logic）。这解释了为什么 1.9.3 的协议即使还原完整也无法复活。

## 1. 证据来源

对 `https://app.nexbot.ai/`（Flutter Web 版，与新版 App 同一套代码）的 `main.dart.js`
做静态分析 + 无头 Chromium 实跑。原始抓包：`evidence/web-traffic.json`。

## 2. 关键常量（客户端公开配置）

```text
projectId          : nexbot-ai
web apiKey         : AIzaSyDPNccVqACW0i0BKjcoxMpzlTqHImurJYw
web appId          : 1:1038619189670:web:013672411ddc9e2cff2f67
android apiKey     : AIzaSyApZaRnMribFBQ569DfdkR-8jIYMeG7EKA
android appId      : 1:1038619189670:android:c7f448e5298b5668ff2f67
gcm sender id      : 1038619189670
default web client : 1038619189670-2lfc3m1rnrtvngusqbof20fpfgsvav78.apps.googleusercontent.com
```

## 3. 请求形态

`main.dart.js` 中可直接读到 SDK 拼出的 URL：

```js
A.bGs("https","firebasevertexai.googleapis.com","v1beta/projects/"+o.d, s)
...
n.h(0,"x-goog-api-key", p.a)
n.h(0,"x-goog-api-client","vertexai-dart/0.2.3")
```

即：

```http
POST https://firebasevertexai.googleapis.com/v1beta/projects/nexbot-ai/models/<model>:generateContent
x-goog-api-key: <Firebase web api key>
Content-Type: application/json
```

模型名不写死在代码里，由 Remote Config 下发：

```js
s = remoteConfig.getString("gemini_llm_model")   // 实测值 "gemini-3.5-flash-lite"
model = s.length !== 0 ? s : "gemini-2.5-flash-lite"
```

## 4. App Check 是硬门槛（实测）

不带 App Check token 直接打该端点：

```http
POST https://firebasevertexai.googleapis.com/v1beta/projects/nexbot-ai/models/gemini-2.5-flash-lite:generateContent
x-goog-api-key: <key>
{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}
```

响应：

```json
{"error":{"code":401,"message":"Firebase App Check token is invalid.","status":"UNAUTHENTICATED"}}
```

原始响应：`evidence/appcheck-401.json`。

对照实验：同一把 key 打 `generativelanguage.googleapis.com` 是被服务级拦截的：

```json
{"error":{"code":403,
 "message":"Requests to this API generativelanguage.googleapis.com method ... are blocked.",
 "details":[{"reason":"API_KEY_SERVICE_BLOCKED", ...}]}}
```

说明该项目**只启用了 `firebasevertexai` 这一个 API**，并且在该 API 上强制 App Check。
换句话说，客户端公开的 API key 单独没有任何用处。

## 5. 为什么这条路不做

要在没有官方客户端的情况下调用该端点，唯一途径是伪造 App Check 断言
（Web 端 reCAPTCHA v3 / Enterprise，Android 端 Play Integrity）。
这属于绕过第三方应用的访问控制机制，并以对方项目的 Vertex AI 计费额度承担成本。
技术上也许有办法，但本仓库不提供、也不建议实现 —— 这不是「互操作性研究」，
而是盗用他人付费资源。

因此新版链路在**合规层面**就已经关闭，与技术水平无关。

## 6. 对 1.9.3 用户的直接影响

- 打开 App → 业务请求打到 NXDOMAIN → 界面提示
  `Please check your internet connection.` / `Send Message Failed`
  （这两条文案在反编译对象池中就紧邻 `chatCompletion` 调用点）；
- Remote Config 里 `min_build_number = 0`、`maintenance_mode = false`，所以运营方
  没有用远程配置把旧客户端明确挡住 —— 旧客户端只是「静默失效」；
- 想要重新可用，只能升级到新版，而新版受 App Check 保护。

## 7. 结论

| 版本 | 后端 | 可复用性 |
|------|------|----------|
| 1.9.3（本仓库分析对象） | 自建 Serverpod + Supabase | 协议已完整还原，但**后端已注销，不可用** |
| 1.21.6（线上） | Firebase AI Logic | 协议公开可读，但**强制 App Check，不可复用** |
