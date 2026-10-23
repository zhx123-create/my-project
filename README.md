# 接话 · Windows 微信回复助手

在独立桌面窗口中，从指定微信窗口截取并框选聊天区域，或导入主动复制的文字 / 截图，按你的意思生成三条可编辑回复，再复制回微信自行发送。

默认工作流改为 **Windows 10/11 本地桌面助手**，无需网页预览或网站部署。原 iPhone 网页版仍保留为可选入口。

## Windows 快速开始

1. 安装官方 Python 3.10+（包含 Tcl/Tk、pip）。
2. 完整解压项目，双击 `start_windows.bat`，首次自动准备本地虚拟环境和 Pillow。
3. 点击桌面窗口右上角“AI 设置”填入 AI API 密钥，不要发在聊天里。默认使用 OpenAI API，需要单独开通 API 额度。
4. 打开对应微信会话 → 查找微信 → 采集选中窗口 → 框选聊天区域 → 确认选区。
5. 填写回复意图和语气，勾选确认提交给 AI → 生成回复 → 编辑和复制 → 回微信粘贴发送。

详见 [Windows 安装与使用说明](docs/WINDOWS.md)。窗口采集使用截图和视觉模型理解消息，尚未在真实 Windows 微信中实测；遇到采集不兼容时可使用复制文字或截图导入。此脚本不会后台监听、读取完整聊天数据库或自动发送消息。

## 开发

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python desktop.py
```

上述为 Linux/macOS 命令；Windows 使用 `.venv\Scripts\python.exe`。桌面 UI 需要可用图形显示和 Tk；微信窗口采集仅支持 Windows。依赖固定为 Pillow 12.0.0，截图、裁剪和密钥均只在内存中处理。剪贴板内容由操作系统管理，AI 服务保留请求的政策另行适用。

桌面窗口允许临时填写接口和密钥，也可通过下列环境变量提供初始值：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `CHAT_API_KEY` | AI 服务密钥 | 无，生成所必需 |
| `CHAT_API_BASE` | HTTPS OpenAI 兼容接口 | `https://api.openai.com/v1` |
| `CHAT_MODEL` | 支持图片和 JSON 输出的模型 | `gpt-4.1-mini` |

API 地址需支持 `/chat/completions`、`image_url` 和 `response_format: json_object`。仅支持文字的模型可用于纯文字输入。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
```

无显示器时 5 项桌面 UI 测试会明确跳过。在本次 Linux 虚拟桌面中，25 项测试已全部执行并通过。测试覆盖桌面剪贴板导入、确认与密钥前置条件、异步生成与失败恢复、草稿编辑和复制、截图裁剪，以及窗口身份变化 / 最小化 / 采集失败的拒绝行为。模型和 Windows 原生捕获在测试中使用替身，不证明真实 API 或真实微信兼容性。

## 可选：原网页版

`python3 server.py` 启动仅监听回环地址的开发服务（默认端口 8000，`HOST` / `PORT` 可覆盖），提供移动页面、文字及最多三张截图的回复生成。网页端密钥必须通过服务进程的 `CHAT_API_KEY` 设置。没有密钥时生成接口明确返回 503。

如需手机使用，必须另行部署到带身份认证的 HTTPS 地址；不要把开发服务器直接暴露到公网。Safari 中可通过“分享 → 添加到主屏幕”添加入口。剪贴板按钮需要 HTTPS 与系统授权，不可用时长按复制粘贴。应用不会直接读取 iPhone 微信。原网页交互已在 Chromium 的手机尺寸模拟中验证，不代表真实 iPhone Safari 已验证。
