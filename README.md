# BreezeTTS for AstrBot

支持 **BreezeBlue 官方 API** 和 **本地 Breeze TTS API** 的 AstrBot 插件。内置独立的音频工作台，可上传参考录音、编辑文字稿、管理音色、生成试听和下载成品。

作者：清蒸云鸭  
许可证：[AGPL-3.0](LICENSE)

![BreezeTTS 工作台](docs/images/studio-desktop.png)

## 能力

| 功能 | 本地 API | BreezeBlue 官方 API |
| --- | --- | --- |
| 鉴权 | 无鉴权 / Bearer API Key | Bearer API Key |
| 语音合成 | 表单 / multipart，PCM 自动封装 WAV | JSON，支持同步、流式接收和后台任务 |
| 音色复刻 | 参考录音 + 准确文字稿 | 查询个人 / 公共音色，参考录音克隆 |
| 声音设计 | 描述声音，直接生成语音 | 生成音色预览，试听后保存到账号 |
| 声音控制 | instruction、cfg_scale、seed | 表演指令、指导强度、语速、音量、语言、相似度增强 |
| 音频文件 | 上传、试听、下载、删除 | 上传参考音频，下载预览及合成成品 |

工作台支持明暗主题和手机布局，通过 AstrBot 的受限 iframe bridge 通信，无需额外端口、外部字体、CDN 或前端构建步骤。文件操作使用 Dashboard 身份鉴权。

## 安装

要求 **AstrBot >= 4.29.3**、Python >= 3.10。插件使用该版本的 `astrbot.api.web` 和可视化视图接口，不支持缺少这些接口的旧版本。

在 AstrBot WebUI 的插件管理中，选择从 GitHub 仓库安装：

```text
https://github.com/WhiteCloudOL/astrbot_plugin_breezetts
```

也可以将仓库克隆到 `data/plugins/astrbot_plugin_breezetts` 后重载插件。AstrBot 会安装 `requirements.txt` 中的 `aiohttp` 和 `soundfile`。本插件不需要安装模型、PyTorch 或另一个 TTS 服务进程。

打开 **插件 → BreezeTTS 详情 → BreezeTTS 工作台**。原有的 AstrBot 标准“插件配置”入口仍可编辑相同设置；新增的工作台把文件和音色操作集中在一个入口中。

## 配置本地服务

1. 打开“连接与设置”，当前服务选“本地 API”。
2. 无鉴权部署通常填写 `http://127.0.0.1:7860`，API Key 留空。鉴权部署通常填写 `http://127.0.0.1:7861`，填写服务端的密钥本身，不加 `Bearer `。
3. 点击“保存并检查连接”。检查会调用 `/health`，并携带同一份可选鉴权头。
4. 使用音色复刻时，前往“音色与录音”上传参考录音，填写与录音完全一致的文字稿，然后点击“设为本地音色”。
5. 返回“合成试听”，输入文字并生成。也可在设置中选“声音设计”，填写声音描述；“普通朗读”允许不传参考录音和描述。

支持根地址、`/v1` 或完整 `/v1/audio/speech` 地址，并保留反向代理路径前缀。Docker 中的 `127.0.0.1` 指向容器自身，请使用容器能够访问的主机地址。

本地接口的 `ref_audio`、`ref_text` 成对发送；没有参考录音的模式不会误传文字稿。普通朗读和纯音色复刻建议 CFG=1，带指令可尝试 CFG=4。本地接口没有独立语速倍率，可通过声音描述调整朗读节奏。

返回的 `audio/pcm` 按 `X-Sample-Rate` 和 `X-Sample-Format` 校验，在完整接收后封装为 16-bit、单声道 WAV，支持跨采样边界的网络分块。

## 配置官方服务

1. 当前服务选择“BreezeBlue 官方 API”，默认地址为 `https://api.breeze.blue/v1`。
2. 填写官方开发者 API Key，点击“保存并检查连接”。检查只读取模型列表，不发起付费合成。
3. 在“音色与录音”查询音色，可筛选全部、个人或公共目录，支持搜索和继续加载分页。点击“使用此音色”，或在设置中直接填写 `voice_id`。
4. 模型 ID 留空由官方选择，也可从连接检查得到的模型列表中选择。语言可留空自动识别，或填写 `zh`、`en`、`ja` 等代码。
5. 根据需要调整语速、音量、表演指令和交付方式，再到合成页试听。

官方账号默认单次文本上限为 1000 字，获准提高额度的账号可在插件设置中提高至 2000 字。插件不会自动截断、拆分或重试付费请求。

“流式接收”会接收官方流式音频，在完整落盘后播放和发送；不代表边生成边播放。后台交付会提交任务、校验任务 ID，并在同一服务地址下轮询和下载音频。不会跟随上游返回的任意下载 URL，也不会跨重定向发送密钥。

默认输出 WAV，另支持官方文档列出的 MP3、FLAC、PCM、AAC、Opus 及采样率 / 码率变体。PCM 自动转为 WAV。浏览器或聊天平台不支持某些编码时，可选择 WAV / MP3 或下载原文件播放。

### 克隆和设计官方音色

在音色页底部选择“参考录音克隆”或“文字设计音色”，填写名称和语言。克隆使用已上传的录音，转换后须至少 3 秒且不超过 5 MB；设计需要 3–500 字的声音描述和试听文字。

点击“生成音色预览”后，可以先试听，再明确点击“保存到官方账号”。保存成功后自动填入官方音色 ID。需要官方验证的克隆会显示预览 ID，必须先到官方控制台完成验证，再查询账号音色。生成和保存会调用官方服务，按官方计费规则消耗额度。

## 聊天中使用

```text
/breezetts 你好，欢迎使用 BreezeTTS。
/btts Hello world, keep the spaces.
/语音合成 今天也要开心。
```

命令沿用 AstrBot 的唤醒前缀和命令权限管理；插件默认允许使用命令的会话调用当前已配置的服务。

`breeze_tts` LLM 工具可在用户要求时发送语音，开关位于“聊天与运行”。同一事件通过工具发送语音后，不会再通过自动语音钩子重复合成。

“自动将 LLM 回复转为语音”默认关闭，仅处理已完成的非流式文字回复；请关闭 AstrBot 自带 TTS 和聊天流式输出后再开启。超长回复保持原文，合成失败保留原文；关闭“保留自动回复文字”也只会在合成成功后移除文字组件，其他组件保留。

语音通过 AstrBot 标准 `Record` 组件发送，最终的文件上传、格式转换和消息呈现由所用平台适配器负责。远程协议端需要具有相应的文件上传能力；本项目没有对所有实际平台账号逐一投递测试。

## 文件与密钥

- 音频与索引保存在 `data/plugin_data/astrbot_plugin_breezetts/`，不写入插件源码目录。
- 参考录音支持 WAV、MP3、FLAC、OGG，原始上传上限 20 MiB、时长 0.1–120 秒、最多双声道；解码后统一转为单声道 PCM16 WAV。下载的是规范化后的 WAV。
- 最多保留 100 条参考录音，手动管理。成品与预览默认保留 50 条，可调整为 1–200；新成品生成后清理最旧文件。正在使用的默认参考录音不能直接删除。
- 最多同时处理 2 个请求；同一本地服务的推理串行执行。最多接受 8 个合成请求 / WebUI 任务，防止无限排队。默认总超时为 600 秒。
- WebUI 合成采用任务提交与轮询，避免长合成占用单次 bridge 请求。重载会取消正在运行的插件任务；已生成文件仍可从列表中找到。官方后台任务可能继续运行，超时后请先检查官方控制台再重新提交。
- 密钥由 AstrBot 配置保存，工作台只返回“是否已配置”。密钥输入留空保留原值，替换时输入新值；删除必须勾选“清除”。AstrBot 标准配置本身仍由宿主管理，应妥善保护配置目录。
- 请求失败不回显上游正文、密钥或完整异常 URL。不会自动重试合成、克隆或保存，避免重复计费。

## 开发与测试

```powershell
uv venv --python 3.12
uv pip install -r requirements-dev.txt
uv run ruff format .
uv run ruff check .
uv run pytest -q
```

常规测试使用真实 HTTP 测试服务，覆盖本地可选鉴权、multipart 字段、PCM 封装、官方同步 / 流式 / 后台任务、音色分页、克隆与设计、超时取消、密钥脱敏和文件持久化。

v1.0.0 在 Windows / Python 3.12 上通过 65 项测试，包含真实 AstrBot 4.29.3 加载、Chromium 工作台交互，以及启用鉴权的本地模型合成；Python 3.10 另通过 60 项协议与存储测试。官方服务已按公开文档通过协议测试，尚未使用真实官方账号密钥进行付费调用。Linux 由仓库 CI 持续验证。

验证真实 AstrBot loader、重载、命令组件和鉴权 Web API：

```powershell
$env:PYTHONPATH = 'C:\path\to\AstrBot'
# Use an environment containing AstrBot's dependencies.
python -m pytest tests/test_host.py -q
```

浏览器交互测试另需 `playwright`、`uvicorn`、`httpx`、可用 Chromium 和 AstrBot 环境：

```powershell
$env:BREEZE_BROWSER_TEST = '1'
python -m pytest tests/test_host.py -q
```

可选真实本地模型测试由 `tests/test_live.py` 提供，设置 `BREEZE_LIVE_LOCAL_URL` 与 `BREEZE_LIVE_LOCAL_KEY` 或 `BREEZE_LIVE_LOCAL_KEY_FILE`。可设置 `BREEZE_LIVE_REFERENCE` 与 `BREEZE_LIVE_REFERENCE_TEXT` 测试参考音色。凭据和音频只存于环境变量 / 临时测试目录，不进入仓库。

详见[中文使用说明](docs/zh/usage.md)、[English guide](docs/en/usage.md) 和[更新记录](CHANGELOG.md)。

接口依据：[AstrBot 插件视图指南](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)、[BreezeBlue 官方接口](https://docs.breezeblue.ai/api-reference/introduction)、[官方鉴权](https://docs.breezeblue.ai/authentication)、[Breeze TTS 开源服务](https://github.com/breezeblue-ai/breeze-tts)。
