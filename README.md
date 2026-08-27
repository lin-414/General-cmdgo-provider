# General cmdgo-provider

[English](#english) | [简体中文](#简体中文)

> Command Code Go 的本地 OpenAI Chat Completions 适配器。非 Command Code 官方软件。

## English

### What this project does

Command Code Go is designed for the Command Code CLI and does not provide a static Provider API key. This project runs a local adapter that translates OpenAI-compatible Chat Completions requests into the private gateway protocol used by the Command Code CLI.

```text
Hermes Studio / Codex / VS Code extension / OpenAI SDK
                         |
                         | OpenAI Chat Completions
                         v
                 cmdgo-provider
              http://127.0.0.1:8787/v1
                         |
                         | Command Code CLI gateway
                         v
                 Command Code Go
```

The adapter is local-only by default and uses the current user's own OAuth session. It is not a shared account server or a public API proxy.

### Changes and features

This is an independent Python implementation focused on local OpenAI-compatible clients. It adds or changes the following compared with the upstream protocol reference:

- Pure Python with standard-library runtime dependencies only.
- Standalone process mode; no Hermes Agent, Hermes plugin, or Node.js is required.
- OpenAI-compatible `POST /v1/chat/completions`.
- Streaming SSE and non-streaming responses.
- Live Go-plan model discovery through `GET /v1/models`.
- Browser OAuth login for Go accounts; no static Go API key is required.
- Per-user token persistence; credentials are never bundled or shared.
- Placeholder-key handling: values such as `cmdgo` do not override the cached OAuth token.
- Conversion for reasoning content, tool calls, finish reasons, and usage metadata.
- Local health check, login, logout, and CORS endpoints.
- Windows start, stop, login, auto-start, and PyInstaller build scripts.
- Local mock tests and GitHub Actions validation.

### Important limitations

- The Go plan does not issue a static Provider API key. Authentication is browser-based OAuth.
- This adapter uses a private CLI gateway rather than a documented Provider API. Command Code may change or restrict that gateway at any time.
- There is no guarantee that this third-party adapter is supported by Command Code or that an account will never be rate-limited or restricted.
- Use your own account, obey the plan limits and service terms, and stop if the service rejects the session.
- The server binds to `127.0.0.1` by default. Do not expose port `8787` to the public internet.
- Never share `%APPDATA%\\cmdgo-provider\\token.json` or Authorization headers.

### Requirements

Source mode requires Python 3.10 or newer. Runtime dependencies are standard-library only; GUI mode requires `pywebview` (optional). Pre-built Windows EXEs do not require Python.

### Run from source

```bash
# GUI mode (native window, requires: pip install pywebview)
python cmdgo_provider.py

# Console-only mode
python cmdgo_provider.py --no-gui
```

The default endpoint is `http://127.0.0.1:8787`. In GUI mode, a native window opens automatically showing the control panel.

In another terminal, start OAuth login:

```bash
python login.py
```

The helper opens the Command Code authorization page, waits for the local callback, and stores the token in the per-user data directory.

### Hermes Studio

Create a custom OpenAI-compatible provider:

```text
Base URL: http://127.0.0.1:8787/v1
API mode: chat_completions
API key: cmdgo
```

`cmdgo` is only a client-side placeholder. The adapter uses the OAuth token saved by `login.py`.

### Codex CLI

Add a custom provider to `~/.codex/config.toml`:

```toml
model = "deepseek/deepseek-v4-flash"
model_provider = "cmdgo"

[model_providers.cmdgo]
name = "Command Code Go"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "chat"
env_key = "CMDGO_API_KEY"
```

Windows PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("CMDGO_API_KEY", "cmdgo", "User")
```

Restart the terminal before starting Codex. This adapter implements Chat Completions, not the Responses API.

### VS Code extensions

VS Code itself has no universal OpenAI provider setting. Extensions that support custom OpenAI-compatible providers can use:

```text
Base URL: http://127.0.0.1:8787/v1
API key: cmdgo
Protocol: OpenAI Chat Completions
```

This can work with extensions such as Continue, Cline, Roo Code, or other extensions that allow a custom endpoint. The built-in GitHub Copilot provider is not automatically redirected by this project.

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(api_key="cmdgo", base_url="http://127.0.0.1:8787/v1")
response = client.chat.completions.create(
    model="deepseek/deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

### API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/healthz` | Local health check |
| `GET` | `/v1/models` | List Go-plan models |
| `POST` | `/v1/chat/completions` | Streaming or non-streaming chat |
| `POST` | `/login` | Start OAuth login |
| `GET` | `/login/status` | Read login state |
| `POST` | `/login/cancel` | Cancel an active login |
| `POST` | `/logout` | Clear the local token |

### Windows scripts

Scripts are in `windows/`:

- `start_proxy.cmd` — start the local adapter.
- `login.cmd` — start the adapter if needed, then begin OAuth login.
- `stop_proxy.cmd` — stop local adapter processes.
- `install_autostart.cmd` — install per-user Windows logon startup.
- `uninstall_autostart.cmd` — remove the startup entry.
- `build_windows.cmd` — build the EXEs with PyInstaller.

A portable EXE folder should contain the EXEs next to the helper scripts. The token is stored outside the repository at `%APPDATA%\\cmdgo-provider\\token.json`.

### Tests

The tests use local mock servers and never call the real Command Code gateway:

```bash
python -m py_compile cmdgo_provider.py run.py login.py tests/test_mock.py tests/test_cached_auth.py
python tests/test_mock.py
python tests/test_cached_auth.py
```

### Build Windows EXEs

```bat
python -m venv .venv
.venv\\Scripts\\activate
python -m pip install pyinstaller pywebview
windows\\build_windows.cmd
```

Build output is written to `dist\\`. The EXE starts in GUI mode by default; use `--no-gui` for console-only.

## 简体中文

### 项目是什么

Command Code Go 主要面向 Command Code CLI，并且不提供静态 Provider API Key。本项目在本地启动一个适配器，把 OpenAI 兼容客户端发送的 Chat Completions 请求转换为 Command Code CLI 使用的私有网关请求。

它默认只监听本机地址，使用当前用户自己的 OAuth 会话，不是共享账号服务器，也不是公共 API 中转站。

### 相比参考项目的改变和新增功能

本仓库是面向本地 OpenAI 兼容客户端的独立 Python 实现，主要改变包括：

- 运行时只依赖 Python 标准库，不需要 Node.js。
- 不依赖 Hermes Agent 或 Hermes 插件，可作为普通独立进程运行。
- 提供 OpenAI 兼容的 `POST /v1/chat/completions`。
- 同时支持流式 SSE 和非流式响应。
- 提供 `GET /v1/models`，自动发现并过滤 Go 套餐模型。
- 提供 Go 账号 OAuth 登录辅助工具，不要求静态 Go API Key。
- token 按用户本地保存，不共享账号凭据。
- 兼容 Hermes Studio 的占位 API Key；`cmdgo` 不会覆盖 OAuth token。
- 支持推理内容、工具调用、结束原因和 usage 信息转换。
- 提供健康检查、登录、登出和 CORS 接口。
- 提供 Windows 启动、停止、登录、开机自启和 PyInstaller 打包脚本。
- 提供流式、非流式、工具调用、usage 和缓存鉴权的 Mock 测试。
- 提供 GitHub Actions 自动测试。

### 重要限制和风险

- Go 套餐不发放静态 Provider API Key，必须通过浏览器 OAuth 登录。
- 本项目使用的是 Command Code CLI 私有网关，不是官方文档中的 Provider API。网关改变或限制后，本项目可能失效。
- 不能保证 Command Code 官方支持这种第三方适配方式，也不能保证账号永远不会被限流或限制。
- 请使用自己的账号，遵守套餐额度和服务条款；如果服务拒绝会话，应立即停止使用。
- 默认只监听 `127.0.0.1`，不要把 8787 端口暴露到公网。
- 不要分享 `%APPDATA%\\cmdgo-provider\\token.json` 或 Authorization 请求头。

### 从源码运行

```bash
# GUI 模式（原生窗口，需安装: pip install pywebview）
python cmdgo_provider.py

# 仅控制台模式
python cmdgo_provider.py --no-gui
```

默认地址：`http://127.0.0.1:8787`。GUI 模式会自动打开原生窗口显示控制面板。

### Hermes Studio 配置

```text
Base URL: http://127.0.0.1:8787/v1
协议模式: chat_completions
API Key: cmdgo
```

`cmdgo` 只是占位符，不是 Command Code API Key。实际请求使用 OAuth 缓存 token。

### Codex CLI 配置

在 `~/.codex/config.toml` 中添加：

```toml
model = "deepseek/deepseek-v4-flash"
model_provider = "cmdgo"

[model_providers.cmdgo]
name = "Command Code Go"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "chat"
env_key = "CMDGO_API_KEY"
```

Windows PowerShell：

```powershell
[Environment]::SetEnvironmentVariable("CMDGO_API_KEY", "cmdgo", "User")
```

重新打开终端后再启动 Codex。本适配器实现 Chat Completions，不实现 Responses API。

### VS Code 扩展

支持自定义 OpenAI 兼容供应商的扩展可以使用：

```text
Base URL: http://127.0.0.1:8787/v1
API Key: cmdgo
协议: OpenAI Chat Completions
```

例如 Continue、Cline、Roo Code 或其他允许自定义 OpenAI 兼容接口的扩展。GitHub Copilot 内置 provider 不会自动使用本项目。

### Windows 脚本

脚本位于 `windows/`：

- `start_proxy.cmd`：启动代理；
- `login.cmd`：代理未运行时自动启动，然后开始 OAuth 登录；
- `stop_proxy.cmd`：停止代理；
- `install_autostart.cmd`：设置当前用户登录时自动启动；
- `uninstall_autostart.cmd`：取消开机自启；
- `build_windows.cmd`：使用 PyInstaller 构建 EXE。

Token 保存位置：

```text
%APPDATA%\\cmdgo-provider\\token.json
```

### 测试

```bash
python -m py_compile cmdgo_provider.py run.py login.py tests/test_mock.py tests/test_cached_auth.py
python tests/test_mock.py
python tests/test_cached_auth.py
```

## 来源与许可证

本项目与 [`Ajwyunsx/dsh-cmdgo-provider`](https://github.com/Ajwyunsx/dsh-cmdgo-provider) 的关系是：上游项目提供了 Command Code Go 协议、OAuth 流程、请求头和模型规则方面的参考，本仓库在此基础上实现了独立的 Python 本地适配器和跨客户端使用方式。

本项目保留上游项目的 MIT 许可证与版权声明，详见 [`LICENSE`](LICENSE)。本项目不代表 Command Code 官方，也不代表上游项目作者。
