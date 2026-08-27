# cmdgo-provider

[English](#english) | [简体中文](#简体中文)

> 非 Command Code 官方软件。请在使用或发布前确认其服务条款是否允许这种本地适配方式。

## English

`cmdgo-provider` is a local, zero-dependency OpenAI-compatible adapter for the Command Code Go plan. It translates:

- OpenAI-compatible `POST /v1/chat/completions`
- OpenAI-compatible `GET /v1/models`
- Command Code OAuth login

into the private CLI gateway used by the Command Code client (`POST /alpha/generate`). It supports non-streaming responses, SSE streaming, reasoning content, tool calls, and usage metadata.

### Important limitations

- The Go plan does not provide a static Provider API key. Login uses the browser-based OAuth flow and stores a per-user token locally.
- This project uses a private CLI gateway rather than a documented Provider API. It may stop working if Command Code changes the gateway and may be unsupported by Command Code.
- Do not expose the local server to the public internet. It binds to `127.0.0.1` by default.
- Every user must authenticate with their own Command Code account. Never share `token.json`.

### Run from source

Python 3.10+ is enough; runtime dependencies are standard-library only.

```bash
python run.py
```

The local server listens on `http://127.0.0.1:8787` by default.

Start OAuth login in another terminal:

```bash
python login.py
```

### Hermes Studio configuration

Create a custom OpenAI-compatible provider:

```text
Base URL: http://127.0.0.1:8787/v1
API mode: chat_completions
API key: cmdgo
```

`cmdgo` is only a placeholder required by clients such as Hermes Studio. The adapter uses the cached OAuth token instead.

### Windows helper scripts

The scripts are in `windows/`:

- `start_proxy.cmd` — start the local proxy
- `login.cmd` — start the proxy if needed, then open OAuth login
- `stop_proxy.cmd` — stop local proxy processes
- `install_autostart.cmd` — install per-user Windows logon startup
- `uninstall_autostart.cmd` — remove the startup entry
- `build_windows.cmd` — build the two EXE files with PyInstaller

For a portable EXE distribution, place these files in one folder together:

```text
cmdgo-provider.exe
cmdgo-login.exe
start_proxy.vbs
start_proxy.cmd
login.cmd
install_autostart.cmd
uninstall_autostart.cmd
stop_proxy.cmd
```

The token is stored outside the repository at:

```text
%APPDATA%\\cmdgo-provider\\token.json
```

### Tests

The tests use a local mock gateway and never call the real Command Code gateway:

```bash
python tests/test_mock.py
python tests/test_cached_auth.py
```

### Build EXEs

Install PyInstaller in a virtual environment, then run on Windows:

```bat
python -m pip install pyinstaller
windows\build_windows.cmd
```

Copy `dist\cmdgo-provider.exe` and `dist\cmdgo-login.exe` into the same folder as the Windows helper scripts before distributing them.

## 简体中文

> 建议将本仓库作为 [`Ajwyunsx/dsh-cmdgo-provider`](https://github.com/Ajwyunsx/dsh-cmdgo-provider) 的 Fork 发布，而不是伪装成官方项目。本仓库是 Python 独立实现，保留了上游 MIT 许可证和来源说明。

`cmdgo-provider` 是一个纯 Python、零运行时依赖的本地 OpenAI 兼容适配器，用于把 Command Code Go 接入 Hermes Studio 等支持 OpenAI 兼容接口的客户端。

它把以下接口转换为 Command Code CLI 使用的私有网关：

- `POST /v1/chat/completions`
- `GET /v1/models`
- 浏览器 OAuth 登录

支持非流式、SSE 流式、推理内容、工具调用和 usage 信息。

### 重要说明

- Go 套餐不提供静态 Provider API Key，必须通过浏览器 OAuth 登录。
- 本项目调用的是 CLI 使用的私有网关，不是 Command Code 文档中的 Provider API。网关变化后可能失效，也可能不受官方支持。
- 默认只监听 `127.0.0.1`，不要把端口暴露到公网。
- 每个使用者必须使用自己的 Command Code 账号，不能分享 `token.json`。

### 从源码运行

需要 Python 3.10 或更高版本，运行时只使用标准库：

```bash
python run.py
python login.py
```

默认地址：

```text
http://127.0.0.1:8787
```

### Hermes Studio 配置

添加自定义 OpenAI 兼容供应商：

```text
Base URL: http://127.0.0.1:8787/v1
协议模式: chat_completions
API Key: cmdgo
```

`cmdgo` 只是客户端要求填写的占位符，代理实际使用 OAuth 缓存 token。

### Windows 脚本

脚本位于 `windows/`：

- `start_proxy.cmd`：启动代理
- `login.cmd`：代理未运行时自动启动，然后打开 OAuth 登录
- `stop_proxy.cmd`：停止代理
- `install_autostart.cmd`：设置当前用户登录时自动启动
- `uninstall_autostart.cmd`：取消开机自启
- `build_windows.cmd`：使用 PyInstaller 构建 EXE

Token 保存位置：

```text
%APPDATA%\\cmdgo-provider\\token.json
```

### 测试

测试使用本地 Mock 网关，不会调用真实 Command Code 服务：

```bash
python tests/test_mock.py
python tests/test_cached_auth.py
```

## Relationship to the upstream project

This repository is intended to be published as a fork of [`Ajwyunsx/dsh-cmdgo-provider`](https://github.com/Ajwyunsx/dsh-cmdgo-provider), not as an official Command Code repository.

The upstream project is the reference for the Command Code Go protocol, OAuth flow, request fingerprint, and model filtering. This fork adds a standalone Python implementation, an OpenAI Chat Completions adapter, Hermes Studio/Codex/other-client configuration, Windows helper scripts, and portable EXE build instructions.

The upstream project is MIT-licensed. The protocol and model-filtering implementation is adapted from it; the upstream copyright and license notice are preserved in `LICENSE`.
