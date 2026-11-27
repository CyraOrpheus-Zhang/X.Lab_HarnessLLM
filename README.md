# XueLab Harness

一个以本地 Mac 为主机的个人工作台，功能拆成四个互相独立的模块：日常对话、科研、固定形象短视频、热点捕获。日常对话与文件回传使用本地模型；科研/视频/热点各有自己的设置、入口和任务状态，不跨模块串工作流。高算力视频任务规划为云端按任务突发。


## 安装依赖并管理模型

在 Mac 上执行：

```bash
cd /path/to/agent-harness
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

这个项目会安装 MLX-LM、Hugging Face 下载工具、Slack Bolt 和本地 CLI。当前项目虚拟环境已装好 `mlx-lm 0.32.2`、`mlx 0.32.5`、`slack-bolt 1.30.0` 和其余依赖；主机为 macOS 26.7.3 / Apple Silicon。

下载/导入第一个模型（约 8.31 GB 权重）：

```bash
xue-harness models add mlx-community/Qwen3-14B-4bit --alias qwen3-14b
xue-harness models use qwen3-14b
xue-harness models list
```

以后添加其他 Hugging Face 上的 MLX 模型，只需重复 `models add <仓库ID> --alias <短名称>`；添加时会先询问是否下载，权重保存在 `models/<短名称>/`。使用其他模型前先检查模型卡的许可证、MLX 格式和 `mlx-lm` 架构兼容性。

启动本机推理服务（仅监听 127.0.0.1）：

```bash
xue-harness models serve
```

另开终端启动本机网页控制台或独立模块：

```bash
xue-harness ask "帮我把这篇论文的研究问题、方法和局限整理成表格" --role research_assistant
xue-harness serve-web
xue-harness run research "总结我提供的论文"
xue-harness run video "写一段关于坚持训练的原创口播"
xue-harness modules
```

`xue-harness models serve` 只提供推理 API，不自带网页。运行 `xue-harness serve-web` 并访问 `http://127.0.0.1:8090`，可打开本机 Harness 控制台：包含日常对话、模型导入/选择与参数调节、四模块状态、token 用量/最近生成速率、云端费用台账及本机文件浏览和 Slack 回传。生成参数保存后下次请求生效；MLX KV cache/并发等服务参数需重启模型服务。

云端台账的费用由用户手动登记；此版本没有 RunPod/Modal 账单 API 或自动创建 GPU 的执行器，因此不会自动订阅或产生费用。控制台中的注册和价格按钮只打开服务商官网；RunPod 当前官方 Serverless 价目随 GPU 型号和时间变化，面板展示的参考价格不是报价承诺。[RunPod 官方价目](https://www.runpod.io/product/serverless) · [Modal 官方价目](https://modal.com/pricing)。

模型导入会下载权重，但不会自动改写模型。生成参数中文注释在 [`config/model.yaml`](config/model.yaml)：温度控制稳定/创意程度，`top_p` 控制候选词范围，`max_tokens` 限制输出长度；4-bit 指权重精度，不等于 4GB。科研/视频模块还分别覆盖自己的生成参数。MLX-LM 提供类 OpenAI `/v1/chat/completions` API；官方文档提示服务端仅有基础安全检查，所以监听地址固定在 `127.0.0.1`，不能直接暴露公网。[MLX-LM server 文档](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md)

## 目前包含

- `config/modules/`：四个独立模块的配置；各自维护自己的工具和流程。
- `config/agents.yaml`：研究、学术核验、创作策划、内容安全、日常助理角色定义。
- `config/model.yaml`：当前模型和生成参数，中文逐项解释。
- `config/file_roots.yaml`：Slack 手机回传允许访问的目录与文件大小限制。
- `config/cloud_policy.yaml`：云端突发计算预算和清理规则样例（默认关闭）。
- `slack-app-manifest.json`：创建个人 Slack App 时可导入的最小权限配置。
- `src/xue_harness/`：直接对接本地模型的 CLI、角色读取和逐步工作流执行器。
- `web/index.html`：本机控制台前端。模型请求的 token 数与输出速度来自 MLX 返回用量；旧请求不会伪造回填。
- `data/`：本地用量/运行数据、云费用台账和控制台覆盖配置。
- `.env.example`：默认只使用本机 MLX 服务，不要求任何付费模型 API key。

## Slack 手机操作和文件回传

Slack 入口支持私聊、@提及、对话记忆、列出共享目录、发送单个文件，以及把多个文件/文件夹压成 ZIP 后回传。控制台可浏览授权目录并逐次选择文件发给白名单中的第一个 Slack 成员。文件通过 Slack `files_upload_v2` 上传；浏览器面板使用 Slack Bot 的 `files:write`，并复用你主动发起的既有私聊，不申请机器人主动新建 DM 的权限。首次使用请先在 iPhone 给机器人发一条私聊消息。包含 `.env`、私钥和证书的文件会被隐藏或阻止回传。[Slack Python SDK 文件上传](https://docs.slack.dev/tools/python-slack-sdk/web/)

1. 在 Slack API 的 Your Apps 导入 `slack-app-manifest.json`；scope 变化后需重新安装 App。
2. 开启 Socket Mode，创建带 `connections:write` 的 App-Level Token，再安装 App 到你的工作区。[官方步骤](https://docs.slack.dev/tools/bolt-python/creating-an-app/)
3. 从 Bot User OAuth Token、App-Level Token 和你的 Slack 用户资料复制 ID，分别填到 `.env` 的 `SLACK_BOT_TOKEN`、`SLACK_APP_TOKEN`、`SLACK_ALLOWED_USER_IDS`。令牌只保存在本机，不要贴到聊天里。
4. 在 [`config/file_roots.yaml`](config/file_roots.yaml) 删除或调整你不希望从手机访问的目录；启动 `xue-harness serve-slack`，然后在 iPhone Slack 私聊机器人输入 `帮助`。

更细的一次性安装步骤、最小手机验收清单和故障排查见 [`docs/SLACK_SETUP_ZH.md`](docs/SLACK_SETUP_ZH.md)。配置好本机 `.env` 后，也可以运行 `./scripts/start_local_stack.sh` 一次启动模型和 Slack 入口。

手机命令示例：

```text
文件列表
文件列表 downloads:
发送 downloads:论文.pdf
打包 codex:课程项目
打包 photos:旅行 photos:家人/合照.jpg
```

用户消息必须来自白名单 ID。路径只能落在配置的目录中，无法用 `../` 跳出；普通聊天不会读取或上传文件。文件回传默认限 100 MB，目录打包默认扫描总量不超过 1 GB，ZIP 使用本机临时目录并在上传后删除。Slack 工作区自身的文件上限可能更小，可在配置中调低。

如果控制台目录状态为“macOS 未授权”，请给启动 harness 的 Terminal/iTerm 授予对应“文件与文件夹”权限，再完全退出并重开该终端；通过控制台再次检查目录可访问状态。

四个模块当前状态：日常对话和文件回传为可运行代码，需配置 Slack 凭据；科研和短视频已有相互独立的角色流程骨架；热点捕获有独立配置骨架。Zotero/知网工具、视频剪辑导出、SVC、定时热点采集和云 GPU 执行器尚未接通，不能把这些骨架误认为已自动运行。

## 路线图

1. 本地 CLI + MLX-LM：确认模型速度、输出质量和内存余量。
2. **优先收入实验**：做出一条可重复的固定动漫形象短视频流程，先用立绘/循环动作、口播与字幕完成整片；测出单条制作耗时、云 GPU 花费和发布后的数据，再决定要不要引入更重的视频生成。
3. 本地资料仓：Zotero Connector + 附件目录 + 文档/网页抽取；知网/期刊只通过你有权限的页面、导出或官方接口采集；写入 Zotero 前显示预览。
4. 手机入口：Slack Bot 走 Socket Mode、文件操作限定在明确的共享目录；私聊和文件回传先做用户身份白名单。
5. 云 GPU 队列：先手动提交并验证实际单任务花费；再启用价格阈值、延迟窗口、月度硬上限、运行超时和自动终止。
6. 低频趋势采集：优先 RSS、公开榜单与官方接口；下载和保存视频要遵守平台规则、版权和个人数据边界。任何收藏/发布操作由你确认。
7. 常驻运行：macOS `launchd` + 自动重启 + 本地备份；远程访问使用 Tailscale/VPN 或 Slack Socket Mode，不开放公网服务端口。

## 云端突发计算

Mac 本地负责常驻对话、科研和日常剪辑；字幕、时间线拼接等工作不必默认上传云端。云 GPU 只接收高算力、可批处理的任务，例如 AI 图像/视频生成或批量音视频处理。优先使用可缩到零、按秒计费的 Serverless GPU；需要完整自定义镜像时，再用临时 GPU Pod，完成后终止并清理持久盘。

Runpod 当前列出的 Serverless Worker 可按任务伸缩并在空闲时缩到零，起价为 $0.58/小时、按秒计费；更大显存的 GPU 单价更高，冷启动/模型加载等待和存储也要计入。[Runpod Serverless 说明和价格](https://www.runpod.io/product/serverless)。每月 $10 只够数小时 GPU 计算，不是整月在线服务器。很多 GPU SKU 的单价是固定的，并不存在“半夜一定更便宜”；所以默认按实时可用报价选低价卡，夜间窗口只作为可选的延迟队列策略。云策略样例默认关闭，预算上限为 `$10/月`、`$2/任务`、90 分钟；执行器接好并验证之后才能自动排队，超出预算的任务留在本地。AI API/订阅支出仍单独限制在你提出的 ¥68/月内。

## 数据与操作边界

- 先走只读分析；写入、收藏、发送文件、发布视频等副作用动作必须由你明确触发或确认。
- 文件 agent 未来只允许操作配置的工作目录，默认不开放整个磁盘。
- 不自动绕过登录、验证码、访问限制或平台风控；采集建议只面向许可范围内的公开数据。
- MediaCrawler 仓库 README 明确写明仅供学习研究并禁止商业用途，因此不应把它直接用于你的商业自媒体热点采集管线。[MediaCrawler README](https://github.com/NanmiCoder/MediaCrawler)
- 短视频采用 ArcReel 的“阶段式制作、每阶段可复核/重做、成本可见、最终人工审片”思路；它支持输出剪映草稿，但 README 表示 CapCut 兼容性仍未确认。[ArcReel](https://github.com/ArcReel/ArcReel)

## 未来版本

实现远程管理主机文件并回传，优化日常AIGC图片任务，完善多agent协调逻辑层
