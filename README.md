# MeetingBook — 会议音频、纪要及相关文件仓库

集中存放会议录音、转写文本、纪要与附件的仓库。

## 目录结构

```
MeetingBook/
├── README.md
├── .gitignore
├── .env                     # API Key（本地，已被 git 忽略）
├── tools/                   # 程序代码（meetingbook.py / transcribe.py / webui.py）
├── scripts/                 # 启动/关闭脚本（双击运行）
│   ├── 启动会议助手.bat
│   ├── 启动可视化界面.bat
│   ├── 关闭可视化界面.bat
│   ├── start_meetingbook.ps1
│   └── stop_webui.ps1
└── meetings/                # 会议数据统一根目录
    └── <年>/                # 如 2026
        └── <年-月>/         # 如 2026-08
            └── <YYYY-MM-DD>-<会议主题>/   # 一次会议一个文件夹（导入时自动创建）
                ├── meeting.properties        # 会议属性（显示名称等，Web 可改）
                ├── agenda.md                  # 议程模板（自动生成，可编辑）
                ├── audio/                     # 录音文件（git 已忽略，仅本地）
                ├── transcript/                # 语音转写文本
                ├── notes/                     # 纪要 / 笔记（Markdown）
                └── attachments/               # 附件（演示文稿、图片、文档等)
```

## 命名约定

- **会议文件夹**：`YYYY-MM-DD-序号`（序号当天从 001 递增），如 `2026-08-06-001`
- **会议名称**：显示名存 `meeting.properties`（`name=产品评审`），文件夹名不含名称，可在 Web 界面随时改名
- **转写文件**：`transcript/<音频名>-转写.txt`
- **纪要文件**：`notes/<音频名>-纪要.md`（由转写自动生成时自动去掉冗余的“-转写”）
- **音频**：放入 `audio/`，如 `产品评审会录音.m4a`

> 每个会议目录固定四子目录（audio / transcript / notes / attachments）+ agenda.md + meeting.properties，
> 导入音频或转写/摘要时会自动补齐，无需手动创建。

## 使用说明

1. **新建会议**：复制 `2026-08-06-示例会议/` 的结构，或按命名约定手动创建。
2. **录音**：录音文件只放 `audio/` 本地保存，**不会进入 git**（见 `.gitignore`）。
3. **纪要**：用 Markdown 写纪要，放入 `notes/`；转写文本放 `transcript/`。
4. **提交**：`git add . && git commit -m "..."` —— 只提交文本类内容。

## 音频转写（faster-whisper）

用本地 Whisper 模型自动把 `audio/` 里的录音转成带时间戳的文本，输出到 `transcript/`。

```powershell
# 基本用法（默认 medium 模型，自动检测语言）
python tools/transcribe.py "meetings/2026/2026-08/2026-08-06-产品评审/audio/录音.m4a"

# 指定中文与更准的模型
python tools/transcribe.py audio.m4a --model large-v3 --language zh

# 指定输出目录
python tools/transcribe.py audio.m4a --output-dir "meetings/2026/2026-08/2026-08-06-产品评审/transcript"
```

**环境依赖**（首次已配置好）：
- Python 3.12 + `pip install faster-whisper`
- FFmpeg（解码 m4a/mp3/wav 等）
- 模型自动从 HuggingFace 下载，缓存于 `%USERPROFILE%\.cache\huggingface`
- 已持久化的环境变量（国内网络必需）：
  - `HF_ENDPOINT=https://hf-mirror.com` — 模型镜像源
  - `HF_HUB_DISABLE_XET=1` — 禁用镜像不支持的 xet 下载协议
  - `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` — 指向 certifi 根证书，修复 Python SSL 校验

**模型大小参考**（RTX 2060 6GB）：
| 模型 | 显存占用 | 速度 | 中文准确率 |
| ---- | ------- | ---- | --------- |
| small | ~1GB | 极快 | 一般 |
| medium | ~2.5GB | 快 | 好（默认） |
| large-v3 | ~5GB | 中等 | 最好 |

> 转写后建议对照 `transcript/` 的文本整理 `notes/` 纪要，可用下方模板。

## MeetingBook 可视化界面（Web）

本地 Web 界面：可视化操作全部功能（导入、转写进度、纪要、音频播放、检索问答、API Key 配置）。

```powershell
# 双击 scripts/ 下的「启动可视化界面.bat」自动打开浏览器，或命令行：
python tools/webui.py                # 启动并自动打开浏览器（默认 http://127.0.0.1:8765）
python tools/webui.py --no-browser --port 8080

# 关闭界面：双击 scripts/ 下的「关闭可视化界面.bat」（按端口 8765 / webui.py 进程自动定位并终止）
```

界面功能：
- 左侧会议列表，点击查看详情；音频可直接在浏览器播放
- 「转写音频」「生成纪要」带实时进度条（后台任务，可继续操作其他页面）
- 「检索提问」支持关键词检索与 LLM 问答（基于会议材料，带来源引用）
- 导入音频：上传文件 + 会议主题，自动归档到 `日期-主题/audio/`
- 右上角「API Key 配置」：查看/保存/清除 DeepSeek Key（存 `.env`，不入库）

## 会议助手（终端程序）

一站式管理会议：**导入音频 → 转写 → 摘要 → 检索提问**。

```powershell
# 一键启动（推荐）：双击 scripts/ 下的 bat，或命令行运行
.\scripts\启动会议助手.bat          # 交互式主菜单
.\scripts\启动会议助手.bat search "性能优化"   # 可直接带子命令参数
.\scripts\启动可视化界面.bat        # Web 可视化界面（自动开浏览器）
.\scripts\关闭可视化界面.bat        # 停止 Web 界面服务

# 或直接调用
python tools/meetingbook.py   # 交互式主菜单
python tools/meetingbook.py list                                  # 列出所有会议
python tools/meetingbook.py import 录音.m4a --meeting 产品评审    # 导入音频（自动归档到 日期-主题/audio/）
python tools/meetingbook.py transcribe --all                      # 转写所有音频（默认只转写未转写的）
python tools/meetingbook.py summarize --all                       # 为转写生成纪要（存 notes/）
python tools/meetingbook.py search "性能优化"                     # 关键词检索转写/纪要
python tools/meetingbook.py ask "上周决定了什么？"                # 检索 + LLM 问答
python tools/meetingbook.py config                                # 查看 API Key 状态
python tools/meetingbook.py config --set sk-xxx                   # 保存 API Key（写入 .env，不入库）
python tools/meetingbook.py config --clear                        # 清除 .env 中的 API Key
```

> 启动脚本 `scripts/启动会议助手.bat` / `scripts/start_meetingbook.ps1` 会自动检查依赖、应用国内网络环境变量，缺包时自动安装。

**API Key 管理**（`summarize` / `ask` 需要，用 DeepSeek API）：
1. 注册 [DeepSeek 开放平台](https://platform.deepseek.com) 获取 API Key
2. 保存 key 二选一：
   - 交互菜单：选择 **[6] 配置 API Key** 粘贴保存
   - 命令行：`python tools/meetingbook.py config --set sk-xxx`
3. Key 保存在项目根 `.env`（已被 git 忽略，不入库），程序自动加载；也可改用系统环境变量 `DEEPSEEK_API_KEY`
4. `config` 无参数查看当前状态（key 掩码显示）；`config --clear` 清除



## 建议的纪要模板

```markdown
# <会议主题> 纪要

- **日期**：YYYY-MM-DD
- **时间**：HH:MM - HH:MM
- **参会人**：A、B、C
- **记录人**：A

## 议程
1. ...

## 讨论要点
- ...

## 决议 / 结论
- ...

## 待办事项
| 事项 | 负责人 | 截止日期 |
| ---- | ------ | -------- |
| ...  | ...    | ...      |

## 下次会议
- 时间：
- 议题：
```
