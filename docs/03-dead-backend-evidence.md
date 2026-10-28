# 03 · 「后端已死」的证据链

所有结论都可以用下面的命令复现。执行时间：2026-10-29。

## 1. DNS：三个域名全部 NXDOMAIN

```bash
# DoH 查询，绕过本地 DNS 缓存
curl "https://dns.google/resolve?name=globeapp.dev&type=A"
curl "https://dns.google/resolve?name=nexbot.globeapp.dev&type=A"
curl "https://dns.google/resolve?name=zlymralqctgzydksekht.supabase.co&type=A"
```

实测（`Status: 3` = NXDOMAIN）：

```text
zlymralqctgzydksekht.supabase.co   Status=3 A=-
globeapp.dev                       Status=3 A=-
core-server.globeapp.dev           Status=3 A=-
nexbot.ai                          Status=0 A=188.114.96.1,188.114.97.1   ← Cloudflare，存活
app.nexbot.ai                      Status=0 A=188.114.97.1,188.114.96.1   ← Cloudflare，存活
```

原始记录：`evidence/dns-records.txt`。

**关键点：`globeapp.dev` 这个父域本身已经 NXDOMAIN**，说明不是某个子域挂掉，
而是整个域名未续费/被删除。`nexbot.globeapp.dev` 作为其子域不可能存活。

## 2. TLS：连接层直接失败

```bash
curl -sS -m 20 https://nexbot.globeapp.dev/
curl -sS -m 20 https://core-server.globeapp.dev/
curl -sS -m 20 https://zlymralqctgzydksekht.supabase.co/auth/v1/health
```

三者均返回 `curl: (6) Could not resolve host` 或 `(35) SSL connect error`。
即使走代理（代理对 `google.com` 返回 200）也拿不到隧道：

```text
> CONNECT zlymralqctgzydksekht.supabase.co:443 HTTP/1.1
< HTTP/1.1 200 Connection established
* schannel: failed to receive handshake, SSL/TLS connection failed
```

原因是 DNS 层面就不存在，代理无法建立到目标的上游连接。

## 3. 干扰项：Firebase 仍然活着

这一点最容易误判，必须单独说明。

```bash
curl -X POST \
  "https://firebaseremoteconfig.googleapis.com/v1/projects/nexbot-ai/namespaces/firebase:fetch?key=<client key>" \
  -H "Content-Type: application/json" \
  -d '{"app_instance_id":"<uuid>","app_id":"1:1038619189670:android:c7f448e5298b5668ff2f67"}'
```

返回 200 + 完整配置（`templateVersion: 46`，见 `evidence/remote-config.json`）。

含义：

- Firebase 项目 `nexbot-ai` 由运营方继续维护（还在发新版配置）；
- 但**它只承载分析/崩溃/推送/灰度开关**，不承载推理；
- 真正提供对话能力的 Serverpod 后端已经不存在。

## 4. 结论

| 组件 | 状态 | 能否承载推理 |
|------|------|-------------|
| `nexbot.globeapp.dev`（Serverpod） | NXDOMAIN，域名注销 | 否 |
| `core-server.globeapp.dev`（Serverpod） | NXDOMAIN | 否 |
| Supabase 项目 | 已删除 | 否 |
| Firebase 项目 | 存活，仅配置与遥测 | 否 |
| `nexbot.ai` / `app.nexbot.ai` | 存活，已是新版 Web（Firebase AI Logic） | 见 04 |

因此：**1.9.3 这个版本的 App 打开后无法完成任何一次对话请求**。
协议还原仍然有效，但没有可以发起请求的对手方。

## 5. 版本落差

Google Play 上 `com.nexbot.app` 当前版本为 **1.21.6**（页面显示更新于 2026-10-25），
本仓库分析的是 1.9.3。也就是说：交付物本身已经落后于线上版本，
且线上版本换了整套后端架构（见 `04-v190-migration.md`）。

## 6. 复现脚本一览

```bash
# 全部 DNS 状态
python - <<'PY'
import json, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:10808'}))
for h in ['zlymralqctgzydksekht.supabase.co','globeapp.dev','nexbot.globeapp.dev',
          'core-server.globeapp.dev','nexbot.ai','app.nexbot.ai']:
    d = json.loads(op.open(f'https://dns.google/resolve?name={h}&type=A', timeout=25).read())
    print(f'{h:<38} Status={d.get("Status")}')
PY
```

（`ProxyHandler` 部分按你的网络环境删改；无代理时直接 `urllib.request.urlopen` 即可。）
