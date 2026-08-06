# MeetingBook — 会议音频、纪要及相关文件仓库

集中存放会议录音、转写文本、纪要与附件的仓库。

## 目录结构

```
MeetingBook/
├── README.md
├── .gitignore
└── <年份>/
    └── <YYYY-MM-DD>-<会议主题>/        # 一次会议一个文件夹
        ├── agenda.md                  # 会议议程（可选）
        ├── audio/                     # 录音文件（git 已忽略，仅本地）
        ├── transcript/                # 语音转写文本
        ├── notes/                     # 纪要 / 笔记（Markdown）
        └── attachments/               # 附件（演示文稿、图片、文档等）
```

## 命名约定

- **会议文件夹**：`YYYY-MM-DD-主题`，如 `2026-08-06-产品评审`
- **纪要文件**：`notes/YYYY-MM-DD-主题-纪要.md`（或直接 `notes/纪要.md`）
- **音频**：放入 `audio/`，如 `2026-08-06-产品评审-录音.m4a`
- 同一主题一天多场会议可加后缀 `-1`、`-2`

## 使用说明

1. **新建会议**：复制 `2026-08-06-示例会议/` 的结构，或按命名约定手动创建。
2. **录音**：录音文件只放 `audio/` 本地保存，**不会进入 git**（见 `.gitignore`）。
3. **纪要**：用 Markdown 写纪要，放入 `notes/`；转写文本放 `transcript/`。
4. **提交**：`git add . && git commit -m "..."` —— 只提交文本类内容。

## 音频转写（faster-whisper）

用本地 Whisper 模型自动把 `audio/` 里的录音转成带时间戳的文本，输出到 `transcript/`。

```powershell
# 基本用法（默认 medium 模型，自动检测语言）
python tools/transcribe.py "2026/2026-08-06-产品评审/audio/录音.m4a"

# 指定中文与更准的模型
python tools/transcribe.py audio.m4a --model large-v3 --language zh

# 指定输出目录
python tools/transcribe.py audio.m4a --output-dir "2026/2026-08-06-产品评审/transcript"
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

## MeetingBook 会议助手（终端程序）

一站式管理会议：**导入音频 → 转写 → 摘要 → 检索提问**。

```powershell
# 一键启动（推荐）：双击根目录的「启动会议助手.bat」，或命令行运行
.\启动会议助手.bat            # 交互式主菜单
.\启动会议助手.bat search "性能优化"   # 可直接带子命令参数

# 或直接调用
python tools/meetingbook.py   # 交互式主菜单
python tools/meetingbook.py list                                  # 列出所有会议
python tools/meetingbook.py import 录音.m4a --meeting 产品评审    # 导入音频（自动归档到 日期-主题/audio/）
python tools/meetingbook.py transcribe --all                      # 转写所有音频（默认只转写未转写的）
python tools/meetingbook.py summarize --all                       # 为转写生成纪要（存 notes/）
python tools/meetingbook.py search "性能优化"                     # 关键词检索转写/纪要
python tools/meetingbook.py ask "上周决定了什么？"                # 检索 + LLM 问答
```

> 启动脚本 `启动会议助手.bat` / `start_meetingbook.ps1` 会自动检查依赖、应用国内网络环境变量，缺包时自动安装。

**LLM 配置**（`summarize` / `ask` 需要，用 DeepSeek API）：
1. 注册 [DeepSeek 开放平台](https://platform.deepseek.com) 获取 API Key
2. 设置环境变量 `DEEPSEEK_API_KEY=sk-xxx`（或在项目根目录建 `.env` 文件写入，`.env` 已被 git 忽略）

**依赖**：`pip install faster-whisper openai jieba`（首次已装好）



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
