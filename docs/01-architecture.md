# 01 · 1.9.3 技术架构

分析对象：`NexBot AI_1.9.3_ Mod by ElaMods.apk`

| 项 | 值 |
|----|----|
| 包名 | `com.nexbot.app` |
| versionName / versionCode | `1.9.3` / `98` |
| minSdk / targetSdk | 21 / 35 |
| SHA-256 | `e82b157ac737f9d49c4614585e632c69cd13cac3d64e53b72c1d8aee021877b0` |
| 体积 | 57,725,772 字节 |
| 引擎 | Flutter，Dart **3.7.2** AOT 快照，仅 `arm64-v8a` |
| Application 类 | `bin.mt.signature.KillerApplication`（Mod 注入的签名校验绕过，非原厂） |

## 1. 整体分层

```
┌──────────────────────────────────────────────────────────────┐
│ Flutter UI (package:app)                                     │
│   features/messages · conversations · documents · generate   │
│   features/smart_form (tone / length / data 设定)             │
├──────────────────────────────────────────────────────────────┤
│ 共享层 (package:app_core)                                     │
│   config  · hive  · supabase  · firebase  · purchases        │
│   services (main/config) · utils.crypto (JWT 自签)            │
├──────────────────────────────────────────────────────────────┤
│ 协议层                                                        │
│   package:nexbot_client  ← Serverpod 生成，业务端点           │
│   package:core_client    ← Serverpod 生成，配置/链接下发      │
│   package:supabase_flutter（历史遗留）                        │
├──────────────────────────────────────────────────────────────┤
│ 传输层                                                        │
│   serverpod_client（HTTP POST / WebSocket）                   │
│   gotrue + postgrest + realtime_client（Supabase）            │
└──────────────────────────────────────────────────────────────┘
```

## 2. 四套后端并存

1.9.3 里同时存在四条互不相关的外部依赖，这是判断「后端是否已死」时最容易踩坑的地方 ——
其中三条已注销，一条（Firebase）仍存活，因此**只看其中一个会得出错误结论**。

| # | 用途 | 地址 | 状态（2026-10-29） |
|---|------|------|--------------------|
| 1 | 业务后端（聊天/配置） | `https://nexbot.globeapp.dev/` | **NXDOMAIN**（父域 `globeapp.dev` 亦 NXDOMAIN） |
| 2 | 共享核心服务（配置下发） | `https://core-server.globeapp.dev/` | **NXDOMAIN** |
| 3 | 素材/助手资源 + 用户态 | `https://zlymralqctgzydksekht.supabase.co` | **NXDOMAIN**（项目已删除） |
| 4 | 分析 / 崩溃 / 远程配置 / 推送 | `nexbot-ai` Firebase 项目 | **存活** |

第 4 项存活具有误导性：Firebase Remote Config 仍能正常返回配置（说明运营方还在），
但真正承载推理的业务后端（第 1、2 项）已经不存在。详见 `03-dead-backend-evidence.md`。

## 3. 依赖的第三方 SDK

| SDK | 用途 | 备注 |
|-----|------|------|
| `serverpod_client` / `serverpod_flutter` / `serverpod_serialization` | 主业务 RPC | 版本族为 Serverpod 2.x（见 `02-protocol.md` 第 2 节） |
| `supabase_flutter`（gotrue / postgrest / realtime_client） | 用户、素材、部分实时能力 | 项目已删除 |
| `firebase_core / analytics / crashlytics / messaging / performance / remote_config` | 分析、崩溃、推送、灰度 | 仍存活 |
| `purchases_flutter`（RevenueCat） | 订阅与权益校验 | 客户端 |
| `drift` + `sqlite3` | 本地会话/消息库（`ConversationsContent`、`PromptsContent` 等表） | 纯本地 |
| `hive` | 轻量 KV（`device.hive` 存设备标识） | 纯本地 |
| `dart_jsonwebtoken` | 客户端自签 JWT（`secret` 字段） | 见 `02-protocol.md` 第 6 节 |
| `flutter_quill` / `markdown_widget` / `highlight` | 文档编辑与渲染 | 纯本地 |
| `mlkit` OCR（`libmlkit_google_ocr_pipeline.so`） | 图片转文字 | 纯本地 |
| `speech_to_text` / `flutter_tts` | 语音输入/朗读 | 系统能力 |

## 4. 用户态与额度

- 设备身份：`package:app_core/hive/models/device.hive.dart`，首次启动生成 deviceId 并持久化。
- 每日免费额度：Remote Config `credit_daily_free = 2`（实测拉取，见 `evidence/remote-config.json`）。
- 付费权益：RevenueCat entitlements；`app_core/supabase/model/entitlement_response.model.dart` 负责把权益映射成可用模型列表。
- 模型清单：不走后端接口，模型图标与名称从 Supabase Storage 的 `assets/ai-models/*.jpg`
  静态路径直接拼（`openai.jpg` / `claude.jpg` / `gemini.jpg` / `llama.jpg`）。

## 5. 关键文件索引（还原后的 Dart 包结构）

```
package:app/features/messages/messages_screen.controller.dart   ← 聊天主流程（sendMessage / submit）
package:app/features/generate/generate_screen.controller.dart   ← 生成类入口
package:app/features/smart_form/*                               ← tone / length / data 表单
package:app_core/utils/utils.crypto.dart                        ← UtilsCrypto.generateSecret()
package:app_core/config.dart                                    ← CoreConfig + 内嵌 secret 种子
package:app_core/config/secrets.model.dart                      ← SecretsConfig / ConfigRedirect
package:nexbot_client/src/protocol/client.dart                  ← Client + EndpointChatCompletion
package:nexbot_client/src/protocol/chat_completion_*.dart       ← 四个协议模型
package:core_client/src/protocol/config*.dart                   ← 配置下发模型
```

这些路径来自 blutter 对 `libapp.so` 的 AOT 反编译，文件名与类名均为原始未混淆名称。
