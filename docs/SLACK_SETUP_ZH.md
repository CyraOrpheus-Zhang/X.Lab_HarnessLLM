# iPhone Slack 接入（本机模型 + 文件回传）

本流程让 Mac Studio 主动通过 Slack Socket Mode 建立出站连接；无需把模型 API 暴露到公网。机器人只响应 `.env` 中白名单列出的 Slack 用户 ID。

## 一次性创建机器人

1. 打开 Slack API 的 **Your Apps**，创建新 App，并选择从本项目导入 `slack-app-manifest.json`。
2. 在 **Socket Mode** 开启连接，创建 App-Level Token，添加 `connections:write` scope。复制以 `xapp-` 开头的 token。
3. 在 **OAuth & Permissions** 安装 App 到自己的工作区，复制 Bot User OAuth Token（以 `xoxb-` 开头）。Bot scope 需要 manifest 中的 `app_mentions:read`、`chat:write`、`im:history`、`files:write`。
4. 在 iPhone Slack 中打开自己的个人资料，复制成员 ID（通常以 `U` 开头）。把三个值仅填到本机项目 `.env`：

   ```dotenv
   SLACK_BOT_TOKEN=xoxb-本机粘贴
   SLACK_APP_TOKEN=xapp-本机粘贴
   SLACK_ALLOWED_USER_IDS=U你的成员ID
   ```

   token 不要发到聊天、截图、Git 仓库或共享文档。更新权限后若 Slack 提示重装 App，先重新安装再启动机器人。

注意：`.env` 是以点开头的隐藏文件，Finder 默认可能不显示；可按 `Command + Shift + .` 显示隐藏文件。部分 macOS 文本编辑器保存时会把文件名改成 `.env.txt`，程序不会把它当作配置文件。推荐在项目目录用终端执行 `nano .env` 编辑；保存按 `Control + O`、回车，再按 `Control + X` 退出。

## 启动和 iPhone 验收

在 Mac 上执行：

```bash
cd /path/to/agent-harness
./scripts/start_local_stack.sh
```

Mac 需要保持开机并联网。第一次载入模型会慢一些，启动日志位于 `logs/mlx-server.log`。终端显示 Slack Socket Mode 已启动后，在 iPhone Slack 打开机器人私聊，依次试：

1. `帮助`：确认机器人回复指令。
2. `文件列表 downloads:`：确认仅列出配置共享目录。
3. `发送 downloads:文件名.pdf`：确认单文件在私聊中收到。
4. `打包 codex:一个目录`：确认 ZIP 在私聊中收到，解压检查文件名与内容。
5. 普通中文问题：确认经本机 MLX 推理后收到回复。

`config/file_roots.yaml` 控制共享目录；开始使用前删掉不打算从手机访问的目录。单文件/压缩包默认上限 100 MB，文件夹展开内容上限 1 GB。机器人服务默认不会读取或上传普通聊天提及之外的文件。

如果目录显示“macOS 未授权”，这是 macOS 文件隐私权限阻止了运行服务的终端进程。打开 **系统设置 → 隐私与安全性 → 文件与文件夹**，给运行 harness 的 Terminal/iTerm 勾选需要的“下载”“桌面”“文稿”等权限，再完全退出并重开该终端、重启 harness。只开放你需要回传的目录；不要为此把服务监听到公网。

首次使用浏览器控制台上传前，先启动 `serve-slack`，并在 iPhone Slack 主动给机器人发一条私聊消息。Harness 会记住这个已存在的私聊频道，再把你在控制台选中的文件回传到该私聊；不会申请 `im:write` 来主动新建私聊。

如果测试失败：

- `not_authed`：检查本机 `.env` 的 token 与前后空格。
- `missing_scope`：确认 Bot scope 包含 manifest 的 `files:write` 等权限，并重新安装 App。
- 模型连接失败：检查 `logs/mlx-server.log`，确认本地 API 监听 `127.0.0.1:8080`。
- 文件太大：缩小目录或在 Slack 本身允许的限制内调整 `config/file_roots.yaml`。

实际 iPhone 工作区联测必须使用你的个人 Slack App 和 token；本机模拟上传已经验证 SDK 调用、ZIP 内容和临时包清理，但尚未代替真实工作区验收。
