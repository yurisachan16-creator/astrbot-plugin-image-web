# astrbot_plugin_gpt_image_web

AstrBot 网页授权绘图插件。插件只处理 QQ 群命令、排队和发图，不读取
ChatGPT、AI Studio token、cookie 或浏览器 profile；所有网页登录态能力都放在
本机 `gpt_image_web_broker.py` 后面。

> 这是本机研究能力，不承诺 ChatGPT 或 AI Studio 网页端协议稳定性。官方稳定
> 路线仍是各厂商 API；本插件用于没有 API key、但能在本机登录网页的场景。

## 15 分钟 Quickstart

1. 生成一个本机 broker token：

   ```bash
   python3 - <<'PY'
   import secrets
   print(secrets.token_urlsafe(32))
   PY
   ```

2. 在 Mac mini 上启动专用浏览器 profile：

   ```bash
   python3 scripts/astrbot/gpt_image_web_broker.py launch-browser --provider gpt_browser
   python3 scripts/astrbot/gpt_image_web_broker.py launch-browser --provider ai_studio_browser
   ```

   分别在打开的 Chrome 里登录 ChatGPT 和 AI Studio。不要关闭这些专用 Chrome
   窗口；broker 默认通过本机 CDP 连接常驻窗口，避免反复验证码和登录态丢失。

   如果你之前已经用旧命令打开了这个专用 profile，`launch-browser` 可能会提示
   `browser_cdp_unreachable`。这是因为 Chrome 不能给已运行的窗口动态补上
   remote debugging 端口。处理方式是：只关闭这一个旧的专用 Chrome 窗口，
   重新运行 `launch-browser`，之后保持它打开。

3. 启动 broker：

   ```bash
   IMAGE_WEB_PROVIDER_MODE=auto \
   IMAGE_WEB_BROWSER_CONNECTION_MODE=cdp \
   IMAGE_WEB_BROKER_TOKEN="粘贴上一步 token" \
   python3 scripts/astrbot/gpt_image_web_broker.py serve
   ```

4. 健康检查：

   ```bash
   curl -H "Authorization: Bearer 粘贴上一步 token" \
     http://127.0.0.1:18791/health
   ```

5. 在 AstrBot WebUI 配置：

   ```json
   {
     "enabled_groups": ["123456789"],
     "broker_url": "http://127.0.0.1:18791",
     "broker_token": "粘贴上一步 token",
     "admin_user_ids": ["维护者 QQ 号"],
     "command_providers": {
       "gptimg": "gpt_browser",
       "gptedit": "gpt_browser",
       "banana": "ai_studio_browser",
       "bananaedit": "ai_studio_browser"
     },
     "timeout_seconds": 300,
     "queue_max_size_per_group": 3,
     "output_dir": ""
   }
   ```

6. 可选：深度检查 browser 后端：

   ```bash
   IMAGE_WEB_BROKER_TOKEN="粘贴上一步 token" \
   python3 scripts/astrbot/gpt_image_web_broker.py doctor-browser --provider gpt_browser
   IMAGE_WEB_BROKER_TOKEN="粘贴上一步 token" \
   python3 scripts/astrbot/gpt_image_web_broker.py doctor-browser --provider ai_studio_browser
   ```

7. 在 QQ 群发送：

   ```text
   /gptimg a cat in watercolor
   /banana 三月七相机贴纸
   ```

## 命令

- `/gptimg <prompt>`：文字生图。
- `/gptedit <prompt>`：回复一张 JPEG/PNG/WebP 图片后改图。
- `/banana <prompt>`：走 AI Studio / nanobanana2 provider 文字生图。
- `/bananaedit <prompt>`：走 AI Studio provider 改图；若网页端不可用会返回 broker 错误。
- `/imgstatus`：维护者专用状态命令，只返回脱敏 provider health 摘要。

命令解析兼容带 `/` 和不带 `/` 的写法；roleplay 和 recent-images 插件会避让这些
命令文本，避免三月七把生图命令当普通聊天回复。

`/gptedit` 一次只接受一张静态图片。图片超过 8 MiB、格式不支持、URL 过期
或下载失败时，插件会直接提示原因。

## Broker API

所有请求都必须带：

```text
Authorization: Bearer <broker_token>
```

### `GET /health`

返回 broker 和后端状态：

```json
{
  "ok": true,
  "active_backend": "gpt_browser",
  "active_provider": "gpt_browser",
  "checks": [{"provider": "ai_studio_browser", "available": false, "code": "ai_studio_login_required"}],
  "request_id": "req_xxx"
}
```

### `POST /generate`

```bash
curl -X POST http://127.0.0.1:18791/generate \
  -H "Authorization: Bearer $GPT_IMAGE_WEB_BROKER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a cat in watercolor","provider":"gpt_browser"}'
```

### `POST /edit`

```json
{
  "prompt": "make it watercolor",
  "provider": "gpt_browser",
  "mime_type": "image/png",
  "image_base64": "..."
}
```

成功响应：

```json
{
  "ok": true,
  "image_base64": "...",
  "mime_type": "image/png",
  "backend": "gpt_browser",
  "request_id": "req_xxx"
}
```

失败响应：

```json
{
  "ok": false,
  "code": "browser_login_required",
  "problem": "browser backend is not ready",
  "cause": "dedicated profile is not logged in",
  "fix": "open the dedicated profile and log in to ChatGPT",
  "docs_url": "https://github.com/yurisachan16/openclaw/tree/main/deploy/astrbot/plugins/astrbot_plugin_gpt_image_web",
  "request_id": "req_xxx",
  "retryable": true
}
```

## Provider 和环境变量

- `IMAGE_WEB_PROVIDER_MODE=auto|gpt_browser|ai_studio_browser`：默认 `auto`。
- `IMAGE_WEB_BROWSER_CONNECTION_MODE=cdp|persistent`：默认 `cdp`。
- `IMAGE_WEB_GPT_BROWSER_CDP_URL`：默认 `http://127.0.0.1:18792`。
- `IMAGE_WEB_AI_STUDIO_BROWSER_CDP_URL`：默认 `http://127.0.0.1:18793`。
- `IMAGE_WEB_GPT_BROWSER_PROFILE_PATH`：默认沿用
  `~/.local/share/openclaw-gpt-image-web/browser-profile`。
- `IMAGE_WEB_AI_STUDIO_BROWSER_PROFILE_PATH`：默认
  `~/.local/share/openclaw-image-web/ai-studio-browser-profile`。
- 旧 `GPT_IMAGE_WEB_*` 变量保留兼容一个周期；同名能力的新 `IMAGE_WEB_*`
  优先级更高。

浏览器后端不复用日常浏览器 profile。网页登录态能力是实验能力，适合本机研究
和你自己的 QQ 群，不适合作为稳定公开服务承诺。

远端 CDP 是显式高级配置。CDP 可以控制已登录的浏览器页面，默认只建议使用
`127.0.0.1`。如果把 provider CDP URL 指向远端机器，请确认该
通道只对可信网络开放。

## Mac mini 常驻

仓库提供一个单服务 wrapper：

```bash
scripts/astrbot/gpt_image_web_broker_service.sh
```

它会先运行 `launch-browser`，再启动 broker。建议把 broker token 放到：

```bash
~/.openclaw/image-web.env
```

示例内容：

```bash
IMAGE_WEB_BROKER_TOKEN="替换成你的 token"
IMAGE_WEB_PROVIDER_MODE=auto
IMAGE_WEB_BROWSER_CONNECTION_MODE=cdp
IMAGE_WEB_GPT_BROWSER_CDP_URL=http://127.0.0.1:18792
IMAGE_WEB_AI_STUDIO_BROWSER_CDP_URL=http://127.0.0.1:18793
GPT_IMAGE_WEB_PYTHON=/Users/liang/AstrBot/.venv/bin/python
```

LaunchAgent 模板在：

```text
deploy/macos/gpt-image-web-broker.plist.template
```

复制到 `~/Library/LaunchAgents/com.openclaw.gpt-image-web-broker.plist` 后，把
路径替换成 Mac mini 上的真实 repo 和用户目录，再用 `launchctl load` 启动。

## 错误码

- `broker_token_missing`：未设置 broker token。
- `broker_auth_failed`：Bearer token 错误或缺失。
- `oauth_auth_missing`：OAuth 隔离目录没有 auth.json。
- `oauth_image_unsupported`：OAuth 后端未证明支持图片接口。
- `browser_profile_missing`：专用 browser profile 不存在。
- `browser_playwright_missing`：当前环境没有 Playwright 依赖。
- `browser_cdp_unreachable`：CDP 端口不可达，通常是专用 Chrome 没启动。
- `browser_cdp_no_page`：CDP 可达，但当前还没有 ChatGPT tab。
- `browser_cdp_connection_failed`：Playwright 连接 CDP 失败。
- `browser_cdp_remote_warning`：CDP URL 指向远端主机的安全提示。
- `browser_login_required`：专用 profile 打开后仍是未登录页面。
- `browser_profile_locked`：专用 profile 正被另一个 Chrome 窗口占用。
- `browser_composer_missing`：ChatGPT 输入框选择器没有匹配到。
- `browser_edit_unchanged`：ChatGPT 返回了和引用图像素相同的副本。
- `browser_submit_unavailable`：ChatGPT 发送按钮在上传/输入后仍不可点击。
- `browser_upload_missing`：ChatGPT 上传控件选择器没有匹配到。
- `browser_upload_timeout`：引用图已选择，但 ChatGPT 上传预览没有稳定完成。
- `browser_image_not_found`：等待结束前没有找到新生成图片。
- `browser_generation_failed`：ChatGPT 页面报告生成失败。
- `browser_timeout`：浏览器后端执行超时。
- `browser_automation_failed`：浏览器自动化出现未分类错误。
- `provider_invalid`：请求里的 provider 不存在。
- `provider_unavailable`：指定 provider 当前不可用。
- `ai_studio_login_required`：AI Studio 专用 profile 未登录。
- `ai_studio_model_missing`：AI Studio 页面没有找到 Nano Banana 图片模型。
- `ai_studio_composer_missing`：AI Studio 输入框选择器没有匹配到。
- `ai_studio_output_not_found`：等待结束前没有找到 AI Studio 输出图片。
- `ai_studio_generation_failed`：AI Studio 页面报告额度、拒绝或重试类错误。
- `ai_studio_timeout`：AI Studio 浏览器后端执行超时。
- `image_mime_unsupported`：引用图格式不支持。
- `image_too_large`：引用图超过 8 MiB。
- `content_rejected`：本地最小安全策略拒绝。
