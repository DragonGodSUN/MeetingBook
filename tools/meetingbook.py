#!/usr/bin/env python3
"""MeetingBook 会议助手 — 终端程序

一站式管理会议音频：导入 -> 转写 -> 摘要 -> 检索提问。

用法:
    python tools/meetingbook.py                     # 交互式主菜单
    python tools/meetingbook.py list                # 列出所有会议
    python tools/meetingbook.py import <音频> [--meeting 主题] [--date YYYY-MM-DD]
    python tools/meetingbook.py transcribe [--meeting 主题] [--all] [--model medium]
    python tools/meetingbook.py summarize [--meeting 主题] [--all] [--no-save]
    python tools/meetingbook.py search <关键词> [--top-k 5] [--meeting 主题]
    python tools/meetingbook.py ask <问题>

LLM 配置（摘要/提问需要）:
    设置环境变量 DEEPSEEK_API_KEY，或在项目根目录建 .env 文件:
        DEEPSEEK_API_KEY=sk-xxxx
"""
import argparse
import math
import os
import re
import sys
import shutil
import warnings
from collections import Counter, defaultdict
from datetime import date, datetime

# 静音 jieba/旧依赖的弃用警告
warnings.filterwarnings("ignore", message="pkg_resources is deprecated.*")
warnings.filterwarnings("ignore", message=".*Building prefix dict.*")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # 保证 `from tools.xxx import ...` 可从任意 cwd 工作
def deepseek_base_url() -> str:
    """DeepSeek API 地址（DEEPSEEK_BASE_URL，可运行时调整以更换服务商）。"""
    return os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def deepseek_model() -> str:
    """LLM 模型名（DEEPSEEK_MODEL，可运行时调整）。"""
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")


def summary_max_tokens() -> int:
    """纪要生成的最大输出 token（SUMMARY_MAX_TOKENS，可运行时调整；长会议可调大）。"""
    return int(os.environ.get("SUMMARY_MAX_TOKENS", "4096"))


def summary_max_input_chars() -> int:
    """送入模型的转写文本最大字符数（SUMMARY_MAX_INPUT_CHARS，可运行时调整；防超上下文）。"""
    return int(os.environ.get("SUMMARY_MAX_INPUT_CHARS", "30000"))

# ---------- 终端输出 ----------

def c(text: str, code: str = "0") -> str:
    """带颜色输出（ANSI）。"""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def info(msg): print(c(msg, "36"))       # 青
def ok(msg):   print(c(msg, "32"))       # 绿
def warn(msg): print(c(msg, "33"))       # 黄
def err(msg):  print(c(msg, "31"), file=sys.stderr)  # 红

# ---------- 配置 ----------

def env_file_path() -> str:
    """本地密钥配置文件 .env 的路径（已被 .gitignore 忽略）。"""
    return os.path.join(ROOT, ".env")


def load_env() -> None:
    """加载项目根目录 .env（KEY=VALUE 简单格式），不覆盖已有环境变量。"""
    env_path = env_file_path()
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def save_api_key(key: str) -> str:
    """把 API Key 写入 .env（已有则更新，否则追加），返回 .env 路径。"""
    key = key.strip().strip('"').strip("'")
    env_path = env_file_path()
    lines = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("DEEPSEEK_API_KEY="):
            lines[i] = f"DEEPSEEK_API_KEY={key}"
            found = True
            break
    if not found:
        lines.append(f"DEEPSEEK_API_KEY={key}")
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return env_path


def clear_api_key() -> bool:
    """从 .env 移除 DEEPSEEK_API_KEY；返回是否发生了删除。"""
    env_path = env_file_path()
    if not os.path.isfile(env_path):
        return False
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    keep = [l for l in lines if not l.strip().startswith("DEEPSEEK_API_KEY=")]
    if len(keep) == len(lines):
        return False
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
    return True


def mask_key(key: str) -> str:
    """掩码显示：sk-abc…wxyz。"""
    if len(key) <= 10:
        return key[:4] + "…"
    return key[:6] + "…" + key[-4:]


def get_api_key() -> str:
    load_env()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "未配置 DeepSeek API Key。\n"
            "  保存: python tools/meetingbook.py config --set sk-xxx\n"
            "  或设置系统环境变量 DEEPSEEK_API_KEY=sk-xxx"
        )
    return key


def get_llm() -> "OpenAI":
    from openai import OpenAI
    return OpenAI(api_key=get_api_key(), base_url=deepseek_base_url())


def fetch_models(timeout: float = 20.0) -> list:
    """从 API 服务商拉取可用模型名列表（OpenAI 兼容 /models）。失败抛异常给上层提示。"""
    client = get_llm()
    resp = client.models.list(timeout=timeout)
    models = sorted({m.id for m in resp.data if getattr(m, "id", None)})
    if not models:
        raise RuntimeError("API 未返回任何模型")
    return models


def llm_chat(system: str, user: str, temperature: float = 0.3, max_tokens: int = 2048) -> str:
    """调用 DeepSeek chat 模型，返回文本。"""
    client = get_llm()
    resp = client.chat.completions.create(
        model=deepseek_model(),
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


# ---------- 会议目录 ----------

# 会议文件夹名：YYYY-MM-DD-NNN（NNN 当天从 001 递增）
MEETING_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{3})$")

# 会议属性文件：存显示名称等元数据（名称与文件夹名解耦，可在 Web 随意修改）
MEETING_PROPS_FILE = "meeting.properties"

# 会议数据统一根目录：meetings/年/年月/会议
# 可通过 .env 或环境变量 MEETINGS_ROOT 指向项目外目录（支持中文路径/自定义目录名/相对路径）
load_env()  # 确保 .env 中 MEETINGS_ROOT 等变量可用
_meetings_env = os.environ.get("MEETINGS_ROOT", "").strip()
if _meetings_env:
    _meetings_path = _meetings_env if os.path.isabs(_meetings_env) else os.path.join(ROOT, _meetings_env)
    MEETINGS_ROOT = os.path.abspath(_meetings_path)
else:
    MEETINGS_ROOT = os.path.join(ROOT, "meetings")


def save_env(key: str, value: str) -> str:
    """写入/更新项目根 .env（用于持久化 MEETINGS_ROOT 等配置），返回 .env 路径。"""
    key, value = key.strip(), str(value).strip()
    env_path = env_file_path()
    lines = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    found = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.partition("=")[0].strip() == key:
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return env_path


def set_meetings_root(path: str) -> str:
    """运行时切换会议数据目录：校验可创建/可写，更新模块全局，返回新路径（绝对）。"""
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        raise ValueError("数据目录路径不能为空")
    if not os.path.isabs(p):
        p = os.path.join(ROOT, p)  # 相对路径以项目根为基准（支持自定义目录名，如“会议数据”）
    p = os.path.abspath(p)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        raise ValueError(f"无法创建目录: {e}") from e
    if not os.access(p, os.W_OK):
        raise ValueError(f"目录不可写: {p}")
    global MEETINGS_ROOT
    MEETINGS_ROOT = p
    return p


def safe_join(root: str, *parts: str) -> str:
    """安全拼接路径：解析后必须位于 root 内，否则抛 ValueError（防路径穿越/越权）。"""
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, *parts))
    if full != root_real and not full.startswith(root_real + os.sep):
        raise ValueError(f"路径越权: {os.path.join(*parts)} 超出数据目录")
    return full


def display_path(p: str) -> str:
    """显示友好路径：与项目同盘用相对 ROOT，否则用相对数据目录，再否则绝对路径（支持跨盘/中文路径）。"""
    try:
        return os.path.relpath(p, ROOT)
    except ValueError:
        try:
            return os.path.relpath(p, MEETINGS_ROOT)
        except ValueError:
            return p


def read_props(folder: str) -> dict:
    """读取会议属性文件的全部键值。"""
    props = {}
    p = os.path.join(folder, MEETING_PROPS_FILE)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
        except OSError:
            pass
    return props


def write_props(folder: str, props: dict) -> str:
    """合并写属性到属性文件（保留未知键与注释行），返回文件路径。"""
    existing = read_props(folder)
    existing.update({str(k).strip(): str(v).strip() for k, v in props.items()})
    p = os.path.join(folder, MEETING_PROPS_FILE)
    lines = []
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, written = [], set()
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
            continue
        k = s.partition("=")[0].strip()
        if k in existing:
            out.append(f"{k}={existing[k]}")
            written.add(k)
    for k, v in existing.items():
        if k not in written:
            out.append(f"{k}={v}")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return p


def read_meeting_name(folder: str) -> str:
    """从属性文件读取会议显示名称；无则返回空串。"""
    return read_props(folder).get("name", "")


def write_meeting_name(folder: str, name: str) -> str:
    """写/更新会议显示名称到属性文件，返回属性文件路径。"""
    return write_props(folder, {"name": name})


def _next_seq(ym_dir: str, d: str) -> int:
    """当天已有会议的最大序号 + 1（从 001 开始）。"""
    seq = 1
    if os.path.isdir(ym_dir):
        for n in os.listdir(ym_dir):
            m = re.match(rf"^{re.escape(d)}-(\d{{3}})$", n)
            if m:
                seq = max(seq, int(m.group(1)) + 1)
    return seq


def find_meetings() -> list[str]:
    """返回所有会议目录绝对路径，按日期倒序。

    结构：<ROOT>/meetings/<年>/<年月>/<YYYY-MM-DD-NNN>/
    """
    meetings = []
    if not os.path.isdir(MEETINGS_ROOT):
        return meetings
    for y in sorted(os.listdir(MEETINGS_ROOT), reverse=True):
        ydir = os.path.join(MEETINGS_ROOT, y)
        if not (os.path.isdir(ydir) and re.fullmatch(r"\d{4}", y)):
            continue
        for ym in sorted(os.listdir(ydir), reverse=True):
            ym_dir = os.path.join(ydir, ym)
            if not (os.path.isdir(ym_dir) and re.fullmatch(r"\d{4}-\d{2}", ym)):
                continue
            for name in sorted(os.listdir(ym_dir), reverse=True):
                if os.path.isdir(os.path.join(ym_dir, name)) and MEETING_RE.match(name):
                    meetings.append(os.path.join(ym_dir, name))
    return meetings


def parse_meeting(path: str) -> dict:
    """从目录名解析会议信息。topic 为显示名称（属性文件），缺省“会议 NNN”。"""
    name = os.path.basename(path)
    m = MEETING_RE.match(name)
    date_str, seq = "", 0
    if m:
        date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        seq = int(m.group(4))
    display = read_meeting_name(path) or (f"会议 {seq:03d}" if seq else name)
    return {"path": path, "name": name, "date": date_str, "seq": seq, "topic": display}


def pick_meeting(keyword: str | None) -> str | None:
    """按关键词（日期/主题子串）匹配会议目录；无匹配时提示。"""
    meetings = find_meetings()
    if not meetings:
        warn("仓库里还没有会议。先用 import 导入音频创建会议。")
        return None
    if not keyword:
        return None
    kw = keyword.strip()
    hits = [p for p in meetings
            if kw in os.path.basename(p) or kw in parse_meeting(p)["topic"]]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        warn(f"“{kw}”匹配到多个会议，请更精确。候选:")
        for p in hits:
            info(f"  - {os.path.basename(p)}")
        return None
    warn(f"未找到含“{kw}”的会议。现有会议:")
    for p in meetings[:20]:
        info(f"  - {os.path.basename(p)}")
    return None


def meeting_choose_interactive(prompt: str = "选择会议") -> str | None:
    """交互式选择会议（在菜单模式用）。"""
    meetings = find_meetings()
    if not meetings:
        warn("仓库里还没有会议，请先导入音频。")
        return None
    print(f"{c(prompt + ':', '36')}")
    for i, p in enumerate(meetings, 1):
        md = parse_meeting(p)
        print(f"  [{i}] {md['date']} {md['topic']}  ({os.path.basename(p)})")
    try:
        n = int(input(f"输入序号 (1-{len(meetings)}, 0 取消): ").strip())
    except (ValueError, EOFError):
        return None
    if 1 <= n <= len(meetings):
        return meetings[n - 1]
    return None


# ---------- 导入 ----------

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus", ".amr", ".3gp"}
# 视频格式：转写时自动提取音轨（faster-whisper/PyAV 直接解码）
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m2ts", ".wmv"}
# 媒体白名单（音频 + 视频），用于导入/转写/统计
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

# 会议目录统一结构：audio(录音) / transcript(转写) / notes(纪要) / attachments(附件)
MEETING_SUBDIRS = ("audio", "transcript", "notes", "attachments")

# 会议通用属性（存 meeting.properties，Web 可编辑）：
#   name=显示名称  date=日期  time=开始时间  location=地点
#   organizer=主持人  participants=参会人(逗号分隔)  created=创建时间
MEETING_PROP_KEYS = ("name", "date", "time", "location", "organizer", "participants", "created")

AGENDA_TEMPLATE = """# {topic} 议程

- **日期**：{date}
- **时间**：{time}
- **参会人**：{participants}
- **地点**：{location}

## 议题
{topics}

## 备注
{notes}
"""


def ensure_meeting_structure(folder: str) -> None:
    """确保会议目录包含统一四目录、属性文件与 agenda.md 议程模板。"""
    md = parse_meeting(folder)
    for sub in MEETING_SUBDIRS:
        os.makedirs(os.path.join(folder, sub), exist_ok=True)

    # 属性文件：合并默认属性（只补缺失键，不覆盖已有值）
    props = read_props(folder)
    today = date.today().strftime("%Y-%m-%d")
    defaults = {
        "name": md["topic"],
        "date": md["date"] or today,
        "time": "",
        "location": "",
        "organizer": "",
        "participants": "",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    missing = {k: v for k, v in defaults.items() if k not in props}
    if missing:
        write_props(folder, missing)

    # 议程模板：用属性填充
    agenda = os.path.join(folder, "agenda.md")
    if not os.path.exists(agenda):
        props = read_props(folder)
        with open(agenda, "w", encoding="utf-8") as f:
            f.write(AGENDA_TEMPLATE.format(
                topic=props.get("name") or md["topic"] or "会议",
                date=props.get("date") or md["date"] or date.today().strftime("%Y-%m-%d"),
                time=props.get("time", ""),
                participants=props.get("participants", ""),
                location=props.get("location", ""),
                topics="",
                notes=""))


def write_agenda(folder: str, topics: str = "", notes: str = "") -> str:
    """用当前属性 + 用户议题/备注重写 agenda.md，返回路径。"""
    md = parse_meeting(folder)
    props = read_props(folder)
    agenda = os.path.join(folder, "agenda.md")
    with open(agenda, "w", encoding="utf-8") as f:
        f.write(AGENDA_TEMPLATE.format(
            topic=props.get("name") or md["topic"] or "会议",
            date=props.get("date") or md["date"] or date.today().strftime("%Y-%m-%d"),
            time=props.get("time", ""),
            participants=props.get("participants", ""),
            location=props.get("location", ""),
            topics=topics.strip(),
            notes=notes.strip()))
    return agenda


def cmd_import(args) -> int:
    src = os.path.abspath(args.audio)
    if not os.path.isfile(src):
        err(f"找不到文件: {src}")
        return 1
    ext = os.path.splitext(src)[1].lower()
    if ext not in MEDIA_EXTS:
        warn(f"文件后缀 {ext or '(无)'} 不在媒体列表 {sorted(MEDIA_EXTS)} 内，仍将尝试导入。")

    # 确定日期与主题
    d = args.date or date.today().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        err(f"日期格式应为 YYYY-MM-DD，收到: {d}")
        return 1

    # 文件夹名 = 日期-序号（当天从 001 递增）；显示名称存属性文件
    display = (args.meeting or "").strip()
    year, month = d[:4], d[:7]
    ym_dir = safe_join(MEETINGS_ROOT, year, month)
    os.makedirs(ym_dir, exist_ok=True)
    seq = _next_seq(ym_dir, d)
    folder = safe_join(MEETINGS_ROOT, year, month, f"{d}-{seq:03d}")
    audio_dir = os.path.join(folder, "audio")
    ensure_meeting_structure(folder)  # 统一四目录 + agenda.md
    os.makedirs(audio_dir, exist_ok=True)
    write_meeting_name(folder, display or f"会议 {seq:03d}")  # 属性文件存显示名称

    # 重名处理：加序号
    dst = os.path.join(audio_dir, os.path.basename(src))
    n = 2
    while os.path.exists(dst):
        stem, e = os.path.splitext(os.path.basename(src))
        dst = os.path.join(audio_dir, f"{stem}-{n}{e}")
        n += 1

    if args.move:
        shutil.move(src, dst)
    else:
        shutil.copy2(src, dst)
    ok(f"已导入: {display_path(dst)}")
    info(f"会议目录: {display_path(folder)}")
    if not args.move:
        info("原文件已保留（如需移动可用 --move）。")
    return 0


# ---------- 转写 ----------

def cmd_transcribe(args) -> int:
    from tools.transcribe import transcribe_audio

    meetings = find_meetings()
    if args.meeting:
        target = pick_meeting(args.meeting)
        if not target:
            return 1
        meetings = [target]
    elif not args.all:
        # 默认只转写仍有未转写音频的最新会议
        candidates = [p for p in meetings
                      if _has_untranscribed_audio(p)]
        if not candidates:
            warn("没有需要转写的会议（音频都已转写或仓库为空）。可加 --all 或 --meeting。")
            return 0
        meetings = candidates[:1]

    total = 0
    for m in meetings:
        ensure_meeting_structure(m)
        audio_dir = os.path.join(m, "audio")
        if not os.path.isdir(audio_dir):
            continue
        transcript_dir = os.path.join(m, "transcript")
        os.makedirs(transcript_dir, exist_ok=True)
        for f in sorted(os.listdir(audio_dir)):
            if os.path.splitext(f)[1].lower() not in MEDIA_EXTS:
                continue
            base = os.path.splitext(f)[0]
            out = os.path.join(transcript_dir, f"{base}-转写.txt")
            if os.path.exists(out) and not args.force:
                info(f"跳过（已转写）: {f}")
                continue
            info(f"转写: {display_path(os.path.join(m, 'audio', f))}")
            try:
                transcribe_audio(os.path.join(audio_dir, f),
                                 model_size=args.model,
                                 language=args.language,
                                 output_dir=transcript_dir)
                total += 1
            except Exception as e:
                err(f"转写失败 {f}: {e}")
    ok(f"转写完成，共 {total} 个文件。")
    return 0


def _has_untranscribed_audio(meeting: str) -> bool:
    audio_dir = os.path.join(meeting, "audio")
    t_dir = os.path.join(meeting, "transcript")
    if not os.path.isdir(audio_dir):
        return False
    for f in os.listdir(audio_dir):
        if os.path.splitext(f)[1].lower() not in MEDIA_EXTS:
            continue
        base = os.path.splitext(f)[0]
        if not os.path.exists(os.path.join(t_dir, f"{base}-转写.txt")):
            return True
    return False


# ---------- 摘要 ----------

SUMMARY_SYSTEM = (
    "你是一名专业的会议纪要整理助手。根据提供的会议转写文本，生成结构化的中文会议纪要。"
    "要求：忠实于原文，不编造内容；语言简洁；用 Markdown 格式；"
    "待办事项必须用 Markdown 表格呈现（列：事项 | 负责人 | 截止日期）。"
)


def note_name_for(transcript_file: str) -> str:
    """由转写文件名推导纪要文件名：X-转写.txt -> X-纪要.md（去掉冗余的“-转写”）。"""
    base = os.path.splitext(transcript_file)[0]
    if base.endswith("-转写"):
        base = base[:-3]
    return f"{base}-纪要.md"


def summarize_transcript(txt_path: str, meeting: dict, save: bool = True) -> str:
    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read()

    if len(text.strip()) < 50:
        warn(f"转写内容过短，跳过: {os.path.basename(txt_path)}")
        return ""
    if len(text) > summary_max_input_chars():
        warn(f"转写文本过长（{len(text)} 字符），已截断到 {summary_max_input_chars()} 字符供模型处理，纪要可能不完整。")
        text = text[:summary_max_input_chars()] + "\n…（此处为截断）"

    props = read_props(meeting["path"])
    meta = [f"会议名称: {meeting['topic']}",
            f"会议日期: {meeting['date']}"]
    if props.get("time"):
        meta.append(f"会议时间: {props['time']}")
    if props.get("location"):
        meta.append(f"会议地点: {props['location']}")
    if props.get("organizer"):
        meta.append(f"主持人: {props['organizer']}")
    if props.get("participants"):
        meta.append(f"参会人: {props['participants']}")
    user = ("\n".join(meta) + "\n\n"
            "以下是语音转写文本，请生成会议纪要（包含：会议概况、讨论要点、决议/结论、待办事项表）：\n\n"
            f"{text}")
    info(f"  调用 DeepSeek 生成摘要 ...")
    summary = llm_chat(SUMMARY_SYSTEM, user, temperature=0.3, max_tokens=summary_max_tokens())

    if save:
        notes_dir = os.path.join(meeting["path"], "notes")
        os.makedirs(notes_dir, exist_ok=True)
        out = os.path.join(notes_dir, note_name_for(os.path.basename(txt_path)))
        with open(out, "w", encoding="utf-8") as f:
            f.write(summary + "\n")
        return out
    return summary


def cmd_summarize(args) -> int:
    meetings = find_meetings()
    if args.meeting:
        target = pick_meeting(args.meeting)
        if not target:
            return 1
        meetings = [target]

    total = 0
    for m in meetings:
        ensure_meeting_structure(m)
        t_dir = os.path.join(m, "transcript")
        t_files = [f for f in os.listdir(t_dir) if f.endswith("-转写.txt")] if os.path.isdir(t_dir) else []
        if not t_files:
            warn(f"「{parse_meeting(m)['topic']}」没有转写文本——请先转写音频，再生成纪要。")
            continue
        md = parse_meeting(m)
        for f in t_files:
            txt = os.path.join(t_dir, f)
            note = os.path.join(m, "notes", note_name_for(f))
            if os.path.exists(note) and not args.force:
                info(f"跳过（已有纪要）: {f}")
                continue
            info(f"摘要: {f}")
            out = summarize_transcript(txt, md, save=not args.no_save)
            if out:
                ok(f"  已生成: {display_path(out)}")
                total += 1
    ok(f"摘要完成，共 {total} 份。")
    return 0


# ---------- 属性自动填充（从转写文本提取） ----------

AUTOFILL_SYSTEM = (
    "你是会议信息提取助手。根据提供的会议转写文本，提取会议元信息。"
    "只输出一个 JSON 对象，字段：time（开始时间，格式如 14:00）、location（地点）、"
    "organizer（主持人姓名）、participants（参会人姓名，逗号分隔）。"
    "提取不到的信息用空字符串。不要输出 JSON 以外的任何内容。"
)

AUTOFILL_KEYS = ("time", "location", "organizer", "participants")


def _parse_llm_json(text: str) -> dict:
    """容错解析 LLM 输出的 JSON（去除 ```json 围栏，降级按行解析）。"""
    import json as _json
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        t = m.group(0)
    try:
        data = _json.loads(t)
        return data if isinstance(data, dict) else {}
    except Exception:
        result = {}
        for line in t.splitlines():
            mm = re.match(r'["\']?(\w+)["\']?\s*[:：]\s*["\']?([^"\'\n]*)["\']?', line.strip())
            if mm:
                result[mm.group(1)] = mm.group(2).strip()
        return result


def autofill_meeting(folder: str, force: bool = False) -> dict:
    """从转写文本用 LLM 提取属性并写入 meeting.properties（缺失或空值才填，force 覆盖）。"""
    md = parse_meeting(folder)
    t_dir = os.path.join(folder, "transcript")
    texts = []
    if os.path.isdir(t_dir):
        for f in sorted(os.listdir(t_dir)):
            if f.endswith("-转写.txt"):
                with open(os.path.join(t_dir, f), "r", encoding="utf-8", errors="ignore") as fh:
                    texts.append(fh.read())
    if not texts:
        raise RuntimeError("该会议没有转写文本——请先转写音频。")
    props = read_props(folder)
    if not force and all(props.get(k) for k in AUTOFILL_KEYS):
        return props  # 属性已齐全，跳过
    user = (f"会议名称: {md['topic']}\n会议日期: {md['date']}\n\n转写文本：\n"
            + "\n".join(texts))
    resp = llm_chat(AUTOFILL_SYSTEM, user, temperature=0.0, max_tokens=512)
    extracted = _parse_llm_json(resp)
    to_write = {}
    for k in AUTOFILL_KEYS:
        v = str(extracted.get(k, "")).strip()
        if v and (force or not props.get(k)):
            to_write[k] = v
    if to_write:
        write_props(folder, to_write)
    return read_props(folder)


def cmd_autofill(args) -> int:
    meetings = find_meetings()
    if args.meeting:
        target = pick_meeting(args.meeting)
        if not target:
            return 1
        meetings = [target]
    for m in meetings:
        md = parse_meeting(m)
        info(f"从转写提取属性: {md['date']} {md['topic']} ...")
        try:
            props = autofill_meeting(m, force=args.force)
            filled = [f"{k}={v}" for k, v in props.items() if k in AUTOFILL_KEYS and v]
            ok(f"  属性: {', '.join(filled) if filled else '(未提取到可填充项)'}")
        except Exception as e:  # noqa: BLE001
            err(f"  {e}")
    return 0


# ---------- 议程自动填充（从转写文本生成议题/备注） ----------

AGENDA_LLM_SYSTEM = (
    "你是会议议程整理助手。根据提供的会议转写文本，生成会议议程。"
    "输出一个 JSON 对象：topics（议题列表，每行一条，用“1. ”“2. ”编号，行间用换行符）、"
    "notes（备注，字符串）。提取不到相关内容就留空字符串。不要输出 JSON 以外的任何内容。"
)


def autofill_agenda(folder: str) -> dict:
    """用 LLM 从转写生成议程（议题/备注），返回 {topics, notes}（不直接写入）。"""
    md = parse_meeting(folder)
    t_dir = os.path.join(folder, "transcript")
    texts = []
    if os.path.isdir(t_dir):
        for f in sorted(os.listdir(t_dir)):
            if f.endswith("-转写.txt"):
                with open(os.path.join(t_dir, f), "r", encoding="utf-8", errors="ignore") as fh:
                    texts.append(fh.read())
    if not texts:
        raise RuntimeError("该会议没有转写文本——请先转写音频。")
    user = (f"会议名称: {md['topic']}\n会议日期: {md['date']}\n\n转写文本：\n"
            + "\n".join(texts))
    resp = llm_chat(AGENDA_LLM_SYSTEM, user, temperature=0.2, max_tokens=1024)
    parsed = _parse_llm_json(resp)
    return {
        "topics": str(parsed.get("topics", "")).strip(),
        "notes": str(parsed.get("notes", "")).strip(),
    }


# ---------- 检索 ----------

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "就", "都", "而", "等", "我们", "你们", "他们",
    "这个", "那个", "一个", "什么", "怎么", "进行", "一下", "大家", "可以", "需要", "然后", "但是",
    "因为", "所以", "如果", "没有", "不是", "就是", "还是", "应该", "已经", "这样", "那边", "这边",
    "嗯", "啊", "哦", "那个", "对吧", "是吧", "就是说", "然后呢", "觉得", "认为", "看看", "比如",
}


def _tokenize(text: str) -> list[str]:
    import jieba
    jieba.setLogLevel(60)  # 静音 jieba 初始化日志
    toks = []
    for t in jieba.lcut(text.lower()):
        t = t.strip()
        if len(t) < 2 or t in STOPWORDS or not re.search(r"[\u4e00-\u9fff0-9a-z]", t):
            continue
        toks.append(t)
    return toks


def _split_segments(text: str) -> list[str]:
    """把转写/纪要文本切成检索段：按时间戳行切，附前后上下文。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    segs, cur = [], []
    for l in lines:
        if re.match(r"^\[\d+:\d{2}:\d{2}.*\]", l) and cur:
            segs.append("\n".join(cur))
            cur = []
        cur.append(l)
    if cur:
        segs.append("\n".join(cur))
    return segs or [text]


class BM25Index:
    """轻量 BM25 检索：jieba 分词 + 文档频率统计。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.docs: list[dict] = []
        self.df: Counter = Counter()
        self.doc_len: list[int] = []
        self.avgdl = 0.0
        self.k1, self.b = k1, b

    def add_docs(self, docs: list[dict]) -> None:
        """docs: [{id, meeting, file, text}]"""
        for d in docs:
            toks = _tokenize(d["text"])
            uniq = set(toks)
            for t in uniq:
                self.df[t] += 1
            self.docs.append(d)
            self.doc_len.append(len(toks))
        self.avgdl = sum(self.doc_len) / max(len(self.doc_len), 1)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        qtoks = _tokenize(query)
        if not qtoks:
            return []
        n = len(self.docs)
        scores = []
        for i, d in enumerate(self.docs):
            toks = _tokenize(d["text"])
            tf = Counter(toks)
            score = 0.0
            for t in set(qtoks):
                if t not in self.df:
                    continue
                f = tf.get(t, 0)
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                score += idf * f * (self.k1 + 1) / denom
            if score > 0:
                scores.append((score, i))
        scores.sort(reverse=True)
        return [{"score": s, **self.docs[i]} for s, i in scores[:top_k]]


def build_index(meeting_filter: str | None = None) -> BM25Index:
    idx = BM25Index()
    meetings = [pick_meeting(meeting_filter)] if meeting_filter else find_meetings()
    meetings = [m for m in meetings if m]
    for m in meetings:
        md = parse_meeting(m)
        for sub, kind in (("transcript", "转写"), ("notes", "纪要")):
            d = os.path.join(m, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if not (f.endswith(".txt") or f.endswith(".md")):
                    continue
                with open(os.path.join(d, f), "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                for si, seg in enumerate(_split_segments(text)):
                    idx.add_docs([{
                        "id": f"{m}|{sub}|{f}|{si}",
                        "meeting": md["topic"],
                        "file": f,
                        "kind": kind,
                        "text": seg,
                    }])
    return idx


def print_hits(hits: list[dict], show_text: bool = True) -> None:
    for i, h in enumerate(hits, 1):
        print(f"{c(f'[{i}]', '36')} {h['meeting']} / {h['file']} ({(h['kind'])})  命中度 {h['score']:.2f}")
        if show_text:
            text = re.sub(r"^\[[^\]]*\]\s*", "", h["text"], flags=re.M)
            print(f"    {c(text[:160].replace(chr(10), ' '), '90')}")


def cmd_search(args) -> int:
    idx = build_index(args.meeting)
    if not idx.docs:
        warn("没有可检索的转写/纪要文本，请先转写。")
        return 1
    hits = idx.search(args.query, top_k=args.top_k)
    if not hits:
        warn(f"未找到与“{args.query}”相关的内容。")
        return 0
    ok(f"共找到 {len(hits)} 条相关片段（关键词检索）:")
    print_hits(hits)
    return 0


def cmd_ask(args) -> int:
    idx = build_index(args.meeting)
    if not idx.docs:
        warn("没有可检索的文本，请先转写。")
        return 1
    hits = idx.search(args.question, top_k=args.top_k)
    if not hits:
        warn(f"未检索到相关内容，无法回答。可换个问法或先转写更多会议。")
        return 0

    print_hits(hits)
    print()
    context = "\n\n".join(
        f"【来源: {h['meeting']} / {h['file']}】\n{h['text']}" for h in hits)
    system = ("你是会议档案助手。根据用户提供的会议材料（含来源标注）回答问题。"
              "只能基于材料内容回答；材料不足时明确说明“材料中没有提到”。"
              "回答用中文，简洁有条理，必要时引用来源会议。")
    info("调用 DeepSeek 生成回答 ...")
    answer = llm_chat(system, f"问题: {args.question}\n\n相关会议材料:\n{context}", temperature=0.2, max_tokens=1024)
    print(c("==== 回答 ====", "32"))
    print(answer)
    return 0


# ---------- 列表 ----------

def cmd_list(args) -> int:
    meetings = find_meetings()
    if not meetings:
        warn("仓库里还没有会议。")
        return 0
    print(f"{c(f'共 {len(meetings)} 个会议:', '36')}")
    for m in meetings:
        md = parse_meeting(m)
        audio_n = len([f for f in os.listdir(os.path.join(m, "audio"))
                       if os.path.splitext(f)[1].lower() in MEDIA_EXTS]) if os.path.isdir(os.path.join(m, "audio")) else 0
        t_dir, n_dir = os.path.join(m, "transcript"), os.path.join(m, "notes")
        t_n = len([f for f in os.listdir(t_dir) if f != ".gitkeep"]) if os.path.isdir(t_dir) else 0
        note_n = len([f for f in os.listdir(n_dir) if f != ".gitkeep"]) if os.path.isdir(n_dir) else 0
        print(f"  {c(md['date'], '33')}  {md['topic']}  "
              f"[音频 {audio_n} | 转写 {t_n} | 纪要 {note_n}]")
    return 0


# ---------- 删除（安全删除：移入回收站 .trash，可恢复） ----------

def trash_dir() -> str:
    """回收站目录（跟随当前数据目录）。"""
    return os.path.join(MEETINGS_ROOT, ".trash")


def trash_meeting(folder: str) -> str:
    """把会议目录移入回收站 .trash/<会议名>-<时间戳>/，返回回收站路径。"""
    os.makedirs(trash_dir(), exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(trash_dir(), f"{os.path.basename(folder)}-{ts}")
    shutil.move(folder, dst)
    return dst


def cmd_remove(args) -> int:
    """删除会议：打印内容清单 → 输入会议名确认 → 移入回收站（可恢复）。"""
    m = pick_meeting(args.meeting)
    if not m:
        return 1
    md = parse_meeting(m)
    print(f"{c('即将删除会议:', '33')} {md['date']} {md['topic']} ({os.path.basename(m)})")
    has_content = False
    for sub, label in (("audio", "音频"), ("transcript", "转写"),
                       ("notes", "纪要"), ("attachments", "附件")):
        d = os.path.join(m, sub)
        files = [f for f in os.listdir(d) if f != ".gitkeep"] if os.path.isdir(d) else []
        if files:
            has_content = True
            print(f"  {label}: {', '.join(files)}")
    if not has_content:
        warn("  该会议目录为空。")
    try:
        confirm = input(f"输入会议名「{md['topic']}」确认移入回收站（可恢复），直接回车取消: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        confirm = ""
    if confirm != md["topic"]:
        warn("已取消，未做任何修改。")
        return 0
    dst = trash_meeting(m)
    ok(f"已移入回收站: {display_path(dst)}")
    info("如需恢复：把该目录从 .trash/ 移回 meetings/<年>/<年月>/ 即可。")
    return 0


# ---------- API Key 配置 ----------

def cmd_config(args) -> int:
    load_env()
    current = os.environ.get("DEEPSEEK_API_KEY", "").strip()

    if args.path:
        info(f".env 路径: {env_file_path()}")
        return 0

    if args.set:
        key = args.set.strip().strip('"').strip("'")
        if not key:
            err("Key 不能为空。")
            return 1
        if not key.startswith("sk-"):
            warn("Key 通常以 sk- 开头，请确认复制完整。")
        path = save_api_key(key)
        os.environ["DEEPSEEK_API_KEY"] = key  # 当前会话立即生效
        ok(f"API Key 已保存: {mask_key(key)}")
        info(f"  文件: {display_path(path)}（已被 git 忽略，不会入库）")
        return 0

    if args.clear:
        removed = clear_api_key()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        if removed:
            ok("已清除 .env 中的 API Key。")
        else:
            info(".env 中本来就没有 API Key。")
        if os.environ.get("DEEPSEEK_API_KEY"):
            info("系统环境变量中的 DEEPSEEK_API_KEY 仍然生效；如需彻底移除，请删除系统环境变量。")
        return 0

    # 默认：显示状态
    if current:
        ok(f"API Key 已配置: {mask_key(current)}")
        info(f"  来源: 环境变量或 .env（{env_file_path()}）")
    else:
        warn("API Key 未配置。")
        info("  保存: python tools/meetingbook.py config --set sk-xxx")
        info("  或设置系统环境变量 DEEPSEEK_API_KEY=sk-xxx")
    return 0


# ---------- 交互菜单 ----------

def main_menu() -> int:
    print()
    print(c("========== MeetingBook 会议助手 ==========", "1;36"))
    print("  [1] 导入音频     [2] 转写")
    print("  [3] 生成摘要     [4] 检索提问")
    print("  [5] 列出会议     [6] 配置 API Key")
    print("  [0] 退出")
    while True:
        try:
            ch = input(c("\n选择 (0-6): ", "36")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if ch == "0":
            return 0
        if ch == "1":
            path = input("音频文件路径: ").strip().strip('"')
            if not path:
                continue
            topic = input("会议主题（回车用文件名）: ").strip()
            cmd_import(SimpleNamespace(audio=path, meeting=topic or None,
                                       date=None, move=False))
        elif ch == "2":
            m = meeting_choose_interactive("选择要转写的会议")
            if not m:
                continue
            cmd_transcribe(SimpleNamespace(meeting=os.path.basename(m), all=False,
                                           model="medium", language=None, force=False))
        elif ch == "3":
            m = meeting_choose_interactive("选择要生成摘要的会议")
            if not m:
                continue
            cmd_summarize(SimpleNamespace(meeting=os.path.basename(m), all=False,
                                          force=False, no_save=False))
        elif ch == "4":
            q = input("提问或关键词: ").strip()
            if not q:
                continue
            mode = input("a) 检索片段   b) LLM 问答  [a/b, 回车=a]: ").strip().lower()
            if mode == "b":
                cmd_ask(SimpleNamespace(question=q, meeting=None, top_k=5))
            else:
                cmd_search(SimpleNamespace(query=q, meeting=None, top_k=5))
        elif ch == "5":
            cmd_list(SimpleNamespace())
        elif ch == "6":
            load_env()
            cur = os.environ.get("DEEPSEEK_API_KEY", "").strip()
            if cur:
                info(f"当前已配置: {mask_key(cur)}")
                act = input("操作: s) 覆盖  c) 清除  回车返回: ").strip().lower()
                if act == "c":
                    cmd_config(SimpleNamespace(set=None, clear=True, path=False))
                elif act == "s":
                    k = input("粘贴新的 API Key (sk-...): ").strip()
                    if k:
                        cmd_config(SimpleNamespace(set=k, clear=False, path=False))
            else:
                k = input("粘贴 API Key (sk-...，回车跳过): ").strip()
                if k:
                    cmd_config(SimpleNamespace(set=k, clear=False, path=False))
        else:
            warn("无效选择。")
    return 0


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------- 入口 ----------

def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="meetingbook",
        description="MeetingBook 会议助手：导入音频、转写、摘要、检索提问",
        epilog="不带子命令时进入交互式主菜单。")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("import", help="导入音频文件到会议目录")
    p.add_argument("audio", help="音频文件路径")
    p.add_argument("--meeting", help="会议主题（默认用文件名）")
    p.add_argument("--date", help="会议日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--move", action="store_true", help="移动而非复制原文件")

    p = sub.add_parser("transcribe", help="转写会议音频")
    p.add_argument("--meeting", help="指定会议（日期或主题子串）")
    p.add_argument("--all", action="store_true", help="转写所有会议的音频")
    p.add_argument("--model", default="medium", help="Whisper 模型（small/medium/large-v3）")
    p.add_argument("--language", default=None, help="语言代码（默认自动检测）")
    p.add_argument("--force", action="store_true", help="覆盖已有转写")

    p = sub.add_parser("summarize", help="为转写生成会议纪要")
    p.add_argument("--meeting", help="指定会议（日期或主题子串）")
    p.add_argument("--all", action="store_true", help="处理所有会议")
    p.add_argument("--force", action="store_true", help="覆盖已有纪要")
    p.add_argument("--no-save", action="store_true", help="只打印不保存")

    p = sub.add_parser("search", help="关键词检索转写/纪要")
    p.add_argument("query", help="关键词")
    p.add_argument("--meeting", help="限定会议")
    p.add_argument("--top-k", type=int, default=5, help="返回条数（默认5）")

    p = sub.add_parser("ask", help="基于会议材料向 LLM 提问")
    p.add_argument("question", help="问题")
    p.add_argument("--meeting", help="限定会议")
    p.add_argument("--top-k", type=int, default=5)

    sub.add_parser("list", help="列出所有会议")

    p = sub.add_parser("config", help="查看/设置/清除 DeepSeek API Key")
    p.add_argument("--set", metavar="sk-xxx", help="保存 API Key 到 .env（本地，不入库）")
    p.add_argument("--clear", action="store_true", help="清除 .env 中的 API Key")
    p.add_argument("--path", action="store_true", help="显示 .env 文件路径")

    p = sub.add_parser("remove", help="删除会议（移入回收站 .trash，可恢复）")
    p.add_argument("meeting", help="会议名（日期/序号/显示名子串）")

    p = sub.add_parser("autofill", help="从转写文本自动提取并填充会议属性")
    p.add_argument("--meeting", help="指定会议（默认最新）")
    p.add_argument("--force", action="store_true", help="覆盖已填写的属性")

    args = parser.parse_args()
    if not args.cmd:
        return main_menu()

    handlers = {
        "import": cmd_import, "transcribe": cmd_transcribe,
        "summarize": cmd_summarize, "search": cmd_search,
        "ask": cmd_ask, "list": cmd_list, "config": cmd_config,
        "remove": cmd_remove, "autofill": cmd_autofill,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
