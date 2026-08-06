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
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

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
    return OpenAI(api_key=get_api_key(), base_url=DEEPSEEK_BASE_URL)


def llm_chat(system: str, user: str, temperature: float = 0.3, max_tokens: int = 2048) -> str:
    """调用 DeepSeek chat 模型，返回文本。"""
    client = get_llm()
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


# ---------- 会议目录 ----------

MEETING_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)$")


def find_meetings() -> list[str]:
    """返回所有会议目录绝对路径，按日期倒序。"""
    meetings = []
    years = [d for d in os.listdir(ROOT)
             if os.path.isdir(os.path.join(ROOT, d)) and re.fullmatch(r"\d{4}", d)]
    for y in sorted(years, reverse=True):
        ydir = os.path.join(ROOT, y)
        for name in sorted(os.listdir(ydir), reverse=True):
            if os.path.isdir(os.path.join(ydir, name)) and MEETING_RE.match(name):
                meetings.append(os.path.join(ydir, name))
    return meetings


def parse_meeting(path: str) -> dict:
    """从目录名解析会议信息。"""
    name = os.path.basename(path)
    m = MEETING_RE.match(name)
    date_str = topic = ""
    if m:
        date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        topic = m.group(4)
    return {"path": path, "name": name, "date": date_str, "topic": topic}


def pick_meeting(keyword: str | None) -> str | None:
    """按关键词（日期/主题子串）匹配会议目录；无匹配时提示。"""
    meetings = find_meetings()
    if not meetings:
        warn("仓库里还没有会议。先用 import 导入音频创建会议。")
        return None
    if not keyword:
        return None
    kw = keyword.strip()
    hits = [p for p in meetings if kw in os.path.basename(p)]
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
        print(f"  [{i}] {os.path.basename(p)}")
    try:
        n = int(input(f"输入序号 (1-{len(meetings)}, 0 取消): ").strip())
    except (ValueError, EOFError):
        return None
    if 1 <= n <= len(meetings):
        return meetings[n - 1]
    return None


# ---------- 导入 ----------

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus", ".amr", ".3gp", ".webm"}


def cmd_import(args) -> int:
    src = os.path.abspath(args.audio)
    if not os.path.isfile(src):
        err(f"找不到文件: {src}")
        return 1
    ext = os.path.splitext(src)[1].lower()
    if ext not in AUDIO_EXTS:
        warn(f"文件后缀 {ext or '(无)'} 不在音频列表 {sorted(AUDIO_EXTS)} 内，仍将尝试导入。")

    # 确定日期与主题
    d = args.date or date.today().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        err(f"日期格式应为 YYYY-MM-DD，收到: {d}")
        return 1
    topic = args.meeting or os.path.splitext(os.path.basename(src))[0]
    topic = topic.strip().strip("-")

    year = d[:4]
    folder = os.path.join(ROOT, year, f"{d}-{topic}")
    audio_dir = os.path.join(folder, "audio")
    os.makedirs(audio_dir, exist_ok=True)

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
    ok(f"已导入: {os.path.relpath(dst, ROOT)}")
    info(f"会议目录: {os.path.relpath(folder, ROOT)}")
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
        audio_dir = os.path.join(m, "audio")
        if not os.path.isdir(audio_dir):
            continue
        transcript_dir = os.path.join(m, "transcript")
        os.makedirs(transcript_dir, exist_ok=True)
        for f in sorted(os.listdir(audio_dir)):
            if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                continue
            base = os.path.splitext(f)[0]
            out = os.path.join(transcript_dir, f"{base}-转写.txt")
            if os.path.exists(out) and not args.force:
                info(f"跳过（已转写）: {f}")
                continue
            info(f"转写: {os.path.relpath(os.path.join(m, 'audio', f), ROOT)}")
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
        if os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
            continue
        base = os.path.splitext(f)[0]
        if not os.path.exists(os.path.join(t_dir, f"{base}-转写.txt")):
            return True
    return False


# ---------- 摘要 ----------

SUMMARY_SYSTEM = (
    "你是一名专业的会议纪要整理助手。根据提供的会议转写文本，生成结构化的中文会议纪要。"
    "要求：忠实于原文，不编造内容；语言简洁；用 Markdown 格式。"
)


def summarize_transcript(txt_path: str, meeting: dict, save: bool = True) -> str:
    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read()

    if len(text.strip()) < 50:
        warn(f"转写内容过短，跳过: {os.path.basename(txt_path)}")
        return ""

    user = (f"会议名称: {meeting['name']}\n"
            f"会议日期: {meeting['date']}\n\n"
            "以下是语音转写文本，请生成会议纪要（包含：会议概况、讨论要点、决议/结论、待办事项表）：\n\n"
            f"{text}")
    info(f"  调用 DeepSeek 生成摘要 ...")
    summary = llm_chat(SUMMARY_SYSTEM, user, temperature=0.3, max_tokens=2048)

    if save:
        notes_dir = os.path.join(meeting["path"], "notes")
        os.makedirs(notes_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(txt_path))[0]
        out = os.path.join(notes_dir, f"{base}-纪要.md")
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
        t_dir = os.path.join(m, "transcript")
        if not os.path.isdir(t_dir):
            continue
        md = parse_meeting(m)
        for f in sorted(os.listdir(t_dir)):
            if not f.endswith("-转写.txt"):
                continue
            txt = os.path.join(t_dir, f)
            base = os.path.splitext(f)[0]
            note = os.path.join(m, "notes", f"{base}-纪要.md")
            if os.path.exists(note) and not args.force:
                info(f"跳过（已有纪要）: {f}")
                continue
            info(f"摘要: {f}")
            out = summarize_transcript(txt, md, save=not args.no_save)
            if out:
                ok(f"  已生成: {os.path.relpath(out, ROOT)}")
                total += 1
    ok(f"摘要完成，共 {total} 份。")
    return 0


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
                        "meeting": md["name"],
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
                       if os.path.splitext(f)[1].lower() in AUDIO_EXTS]) if os.path.isdir(os.path.join(m, "audio")) else 0
        t_dir, n_dir = os.path.join(m, "transcript"), os.path.join(m, "notes")
        t_n = len([f for f in os.listdir(t_dir) if f != ".gitkeep"]) if os.path.isdir(t_dir) else 0
        note_n = len([f for f in os.listdir(n_dir) if f != ".gitkeep"]) if os.path.isdir(n_dir) else 0
        print(f"  {c(md['date'], '33')}  {md['topic']}  "
              f"[音频 {audio_n} | 转写 {t_n} | 纪要 {note_n}]")
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
        info(f"  文件: {os.path.relpath(path, ROOT)}（已被 git 忽略，不会入库）")
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

    args = parser.parse_args()
    if not args.cmd:
        return main_menu()

    handlers = {
        "import": cmd_import, "transcribe": cmd_transcribe,
        "summarize": cmd_summarize, "search": cmd_search,
        "ask": cmd_ask, "list": cmd_list, "config": cmd_config,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
