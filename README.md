# 📖 MeetingBook — 会议音频 · 转写 · 纪要 · 检索

> **版本：beta0.0.1** ｜ 一站式会议管理工具：**导入录音 → 自动转写 → AI 生成纪要 → 全文检索/问答**。
> 提供 Web 可视化界面与终端命令两种操作方式，会议数据本地保存、隐私可控。

---

## 📑 目录

- [功能总览](#-功能总览)
- [快速开始（3 分钟上手）](#-快速开始3-分钟上手)
- [启动方式](#-启动方式)
- [使用流程](#-使用流程)
- [终端命令参考](#-终端命令参考)
- [目录结构与命名](#-目录结构与命名)
- [会议数据目录配置（可放项目外）](#-会议数据目录配置可放项目外)
- [API Key 配置](#-api-key-配置)
- [删除与恢复](#-删除与恢复)
- [隐私与安全](#-隐私与安全)
- [环境依赖（首次安装）](#-环境依赖首次安装)
- [纪要模板](#-纪要模板)
- [常见问题 FAQ](#-常见问题-faq)

---

## ✨ 功能总览

| 功能 | Web 界面 | 终端命令 | 说明 |
|------|:---:|:---:|------|
| 导入音频 | ✅ 文件上传 | `import` | 自动归档到 `日期-序号` 会议目录 |
| 语音转写 | ✅ 实时进度条 | `transcribe` | faster-whisper，GPU 加速，中文友好 |
| 转写修正 | ✅ 一键修正 | `correct` | AI 修正谐音错别字 + 零散短句合并成通顺长句（保留原件，另存修正版） |
| 视频转写 | ✅ | `transcribe` | 支持 mp4/mkv/webm 等，自动提取音轨 |
| AI 生成纪要 | ✅ 一键生成 | `summarize` | DeepSeek，含待办表格 |
| 会议分析 | ✅ 点击跳转 | `analyze` | AI 按内容拆分会议为多个部分并附简述，点击跳转对应转写位置（音频同步定位） |
| 全文检索 | ✅ | `search` | 中文分词 + BM25 关键词检索 |
| LLM 问答 | ✅ 带来源引用 | `ask` | 基于会议材料回答 |
| 音频播放 | ✅ 浏览器直放 | — | — |
| 图集/附件 | ✅ 拖拽保存 | — | 图片/PDF 灯箱预览、翻页；HEIC 自动转码预览；任意文件拖入即存 `attachments/` |
| 会议属性编辑 | ✅ 名称/时间/参会人… | — | 存 `meeting.properties` |
| API Key 管理 | ✅ | `config` | 存 `.env`，不入库 |
| 删除会议 | ✅ 二次确认 | `remove` | 进回收站 `.trash`，可恢复 |

---

## 🚀 快速开始（3 分钟上手）

```powershell
# 1. 安装依赖（首次）
pip install faster-whisper openai jieba flask
#   （另需 FFmpeg；国内网络需配置镜像环境变量，见文末）

# 2. 启动 Web 界面（自动打开浏览器）
双击项目根目录的 `启动.bat`，或：
.\启动.bat

# 3. 在页面里：导入音频 → 转写 → 生成纪要 → 检索提问
```

终端用户：双击 `启动CLI.bat`（或命令行 `.\启动CLI.bat`）进入交互菜单。

---

## 🚀 启动方式

| 入口 | 命令 / 双击 | 说明 |
|------|------------|------|
| **Web 界面**（推荐） | 双击 `启动.bat` | 浏览器操作，默认 http://127.0.0.1:8765 |
| **终端菜单** | 双击 `启动CLI.bat` | 交互式数字菜单 |
| 强制关闭 Web 服务 | 双击 `强制关闭.bat` | 按端口/进程精确终止 |

命令行直接启动：

```powershell
python tools/webui.py                    # Web（--no-browser 不弹浏览器，--port 改端口）
python tools/meetingbook.py              # 终端菜单
```

> 启动脚本会自动检查依赖（缺包自动安装）、应用国内网络环境变量。

---

## 🔄 使用流程

```
导入音频 → 转写 → 修正（可选） → 生成纪要 → 检索提问
  ①        ②       ③            ④         ⑤
```

**① 导入**：把录音（音频或视频，mp4/mkv/webm 等）拖入 Web 页面（或 `import 录音.m4a`），自动创建会议目录 `meetings/<年>/<年月>/<日期-序号>/`。视频文件自动提取音轨转写。

**② 转写**：点「转写音频」→ 顶部进度条显示**音频内真实百分比 + 实时识别文本**（像字幕一样逐句冒出）。完成后 `transcript/` 生成带时间戳的文本。

**③ 修正（可选）**：点「✨ 修正转写」→ AI 分段修正谐音/同音字识别错误，并把零散短句合并成通顺长句，另存为 `X-修正.txt`（原始转写保留不动）。之后生成纪要会**优先使用修正版**。需配置 API Key，长录音会分片多次调用、有进度显示。

**会议分析**：会议详情切到「分析」标签页 → 点「📊 AI 分析会议」→ AI 按内容把整场会议拆成多个部分（标题 + 时间范围 + 一句话简述），结果存 `analysis.md`。点击任一部分即跳转到「转写」页对应位置（高亮定位），音频/视频同步定位到该部分起点。

**④ 纪要**：点「生成纪要」→ DeepSeek 依据转写（优先修正版）+ 会议属性（参会人/时间/地点）生成结构化纪要，存 `notes/`，含待办表格。

**⑤ 检索/问答**：在「检索提问」输入问题：
- **LLM 问答**：先检索相关片段，再由 DeepSeek 基于材料回答（带来源引用）
- **关键词检索**：仅返回命中片段

**图集**：会议详情切到「图集」标签页，可查看会议拍摄的图片与 PDF（点击灯箱预览，←/→ 翻页）；其他文件悬停可下载。HEIC/HEIF（华为/苹果压缩格式）浏览器无法直接显示，服务端自动转成 JPEG 预览（缓存在数据目录 `.thumbcache/`，需 `pip install pillow-heif`）。把任意文件**直接拖进图集区域**（或点「＋ 添加文件」）即保存到该会议的 `attachments/`，重名自动加序号。

---

## 💻 终端命令参考

```powershell
python tools/meetingbook.py list                    # 列出所有会议
python tools/meetingbook.py import 录音.m4a --meeting 产品评审   # 导入（名称存属性文件）
python tools/meetingbook.py transcribe --all        # 转写所有未转写音频（--meeting 指定，--force 重转）
python tools/meetingbook.py correct --meeting 产品评审   # AI 修正转写（谐音纠错+合并长句，--force 重修）
python tools/meetingbook.py analyze --meeting 产品评审   # AI 会议分析（拆分部分+简述，--force 重析）
python tools/meetingbook.py summarize --all         # 生成全部纪要（--no-save 只预览）
python tools/meetingbook.py search "性能优化"        # 关键词检索
python tools/meetingbook.py ask "上周决定了什么？"    # LLM 问答
python tools/meetingbook.py remove 2026-08-06-001    # 删除会议（入回收站）
python tools/meetingbook.py config                   # 查看 API Key 状态
python tools/meetingbook.py config --set sk-xxx      # 保存 API Key
python tools/meetingbook.py config --clear           # 清除 API Key
```

`--meeting` 支持按**日期、序号或显示名**模糊匹配；转写/摘要幂等（已处理过的自动跳过）。

---

## 📁 目录结构与命名

```
MeetingBook/
├── tools/            # 程序代码（meetingbook.py 主程序 / transcribe.py 转写 / webui.py Web）
├── scripts/          # 启动脚本实现（ps1，由根目录 .bat 调用）
├── .env              # API Key 等本地配置（git 已忽略）
└── meetings/         # 会议数据（全部不入库，仅本地）
    └── <年>/ <年-月>/
        └── 2026-08-06-001/            # 日期-序号（当天从 001 递增）
            ├── meeting.properties     # 会议通用属性（Web 可编辑）
            ├── agenda.md              # 议程模板（自动生成）
            ├── audio/                 # 录音（不入库）
            ├── transcript/            # 转写文本（不入库）
            ├── notes/                 # 纪要（不入库）
            └── attachments/           # 附件（不入库）
```

**会议通用属性**（`meeting.properties`，Web「编辑属性」或直接编辑）：

| 键 | 说明 | 示例 |
|----|------|------|
| `name` | 显示名称 | 产品评审 |
| `date` | 日期 | 2026-08-06 |
| `time` | 开始时间 | 14:00 |
| `location` | 地点 | 3F 会议室 |
| `organizer` | 主持人 | 张三 |
| `participants` | 参会人（逗号分隔） | 张三, 李四 |
| `created` | 创建时间 | 2026-08-06 19:10 |

> 命名规范：文件夹用**序号**（改名不影响路径）；显示名存属性文件，Web 随时改。
> 新会议自动初始化全部属性与四目录，无需手动创建。

---

## 🗂 会议数据目录配置（可放项目外）

默认数据在项目内 `meetings/`；放别处（独立盘、中文路径、自定义目录名均可）：

**方式一：Web 界面（推荐）** — 右上角「⚙️ 设置」→ 输入目录 → 「保存目录」，立即生效并持久化。

**方式二：`.env` 配置**

```properties
MEETINGS_ROOT=D:\会议数据        # 绝对路径
MEETINGS_ROOT=会议数据            # 相对路径（以项目根为基准，自定义目录名）
```

- 支持中文/跨盘路径，启动自动识别，Web 启动时打印当前数据目录
- 切换后**立即生效**（无需重启），并写入 `.env` 持久化；会议列表自动刷新
- 更换目录后需手动迁移原数据（如把 `meetings/2026` 整个移过去）

---

## 🔑 API Key 配置

`summarize` / `ask` 需要 DeepSeek API：

1. 注册 [DeepSeek 开放平台](https://platform.deepseek.com) 获取 Key
2. 保存方式任选：
   - Web：右上角「API Key 配置」
   - 终端：`python tools/meetingbook.py config --set sk-xxx`
   - 菜单：交互菜单选 **[6] 配置 API Key**
3. Key 存项目根 `.env`（git 已忽略），程序自动加载；也可用系统环境变量 `DEEPSEEK_API_KEY`

---

## 🗑 删除与恢复

删除**不会永久删除**，先进回收站：

```powershell
python tools/meetingbook.py remove 2026-08-06-001   # 打印清单 → 输入会议名确认 → 入回收站
```

- Web：详情页「删除会议」（两次确认）
- **恢复**：把目录从 `meetings/.trash/` 移回 `meetings/<年>/<年月>/`
- 彻底清理：手动删除 `meetings/.trash/`

---

## 🔒 隐私与安全

- **会议数据一律不入库**：`.gitignore` 屏蔽 `meetings/**`（音频/转写/纪要/属性），仓库只有代码与空目录结构，克隆/推送不会携带会议内容
- **路径防护**：所有读写经数据目录范围校验（`safe_join`），上传文件名清洗，路径穿越（`../`）一律拒绝
- **API Key 本地保存**：`.env` 不入库

---

## 🛠 环境依赖（首次安装）

**软件**：Python 3.10+ · FFmpeg · （可选）NVIDIA GPU

```powershell
pip install faster-whisper openai jieba flask
# 图集预览 HEIC/HEIF（华为/苹果压缩图片）需额外安装：
pip install pillow-heif
```

**国内网络必需的环境变量**（本机已持久化）：

| 变量 | 值 | 作用 |
|------|-----|------|
| `HF_ENDPOINT` | `https://hf-mirror.com` | Whisper 模型镜像源 |
| `HF_HUB_DISABLE_XET` | `1` | 镜像不支持 xet 协议时必需 |
| `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` | certifi 的 `cacert.pem` | 修复 Python SSL 校验 |

**Whisper 模型参考**（6GB 显存）：

| 模型 | 显存 | 速度 | 中文准确率 |
|------|------|------|-----------|
| small | ~1GB | 极快 | 一般 |
| medium | ~2.5GB | 快 | 好（默认） |
| large-v3 | ~5GB | 中等 | 最好 |

---

## 📝 纪要模板

```markdown
# <会议名称> 纪要

- **日期**：YYYY-MM-DD
- **时间**：HH:MM - HH:MM
- **参会人**：A、B、C

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
```

---

## ❓ 常见问题 FAQ

**Q：换电脑/克隆仓库后会议数据会同步吗？**
不会。会议数据（音频/转写/纪要）只存在本地 `meetings/`，仓库只含代码。需要迁移时直接复制 `meetings/` 目录（或配置 `MEETINGS_ROOT` 指向共享盘）。

**Q：转写太慢/不准？**
换大模型重转：CLI 用 `transcribe --meeting X --force --model large-v3 --language zh`；Web 的「转写音频」按钮可重新转写（点“确定”覆盖全部，固定 medium）。显存不足用 `small`。

**Q：`summarize` / `ask` 报网络错误？**
检查 `DEEPSEEK_API_KEY` 是否配置、能否访问 `api.deepseek.com`（部分网络需代理，可设 `HTTPS_PROXY` 或换 `DEEPSEEK_BASE_URL` 到兼容服务）。

**Q：误删了会议？**
删除走回收站，从 `meetings/.trash/` 移回即可；若手动删了文件，尽快用数据恢复软件（如 Recuva）扫描。

**Q：会议改名会影响路径/引用吗？**
不会。文件夹是序号，显示名存属性文件，改名只改 `meeting.properties`。
