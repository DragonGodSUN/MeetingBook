#!/usr/bin/env python3
"""MeetingBook Web 界面 — 本地可视化操作界面（Flask）

用法:
    python tools/webui.py                # 启动服务并自动打开浏览器
    python tools/webui.py --no-browser   # 不自动打开浏览器
    python tools/webui.py --port 8080    # 指定端口（默认 8765）

依赖: pip install flask
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")

from flask import Flask, abort, jsonify, request, send_from_directory  # noqa: E402

from tools import meetingbook as mb  # noqa: E402
from tools.transcribe import transcribe_audio  # noqa: E402

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 上传上限 4GB

# ---------------- 后台任务 ----------------

TASKS: dict[str, dict] = {}
_TLOCK = threading.Lock()


def start_task(name: str, fn, *args, **kwargs) -> str:
    """后台执行 fn(cb, *args, **kwargs)，cb(stage, info) 上报进度。返回 task_id。"""
    tid = uuid.uuid4().hex[:12]
    with _TLOCK:
        TASKS[tid] = {"id": tid, "name": name, "status": "running", "stage": "",
                      "info": None, "result": None, "error": None,
                      "created": time.strftime("%H:%M:%S"), "t0": time.time()}

    def worker():
        try:
            def cb(stage, info=None):
                with _TLOCK:
                    if stage == "live":
                        # LLM 流式输出片段 info={"k": kind, "t": text}，供监视窗口
                        buf = TASKS[tid].setdefault("live", [])
                        buf.append(info)
                        if len(buf) > 500:
                            del buf[:-500]
                    else:
                        TASKS[tid]["stage"] = stage
                        TASKS[tid]["info"] = info
            result = fn(cb, *args, **kwargs)
            with _TLOCK:
                TASKS[tid]["status"] = "done"
                TASKS[tid]["result"] = result
        except Exception as e:  # noqa: BLE001
            with _TLOCK:
                TASKS[tid]["status"] = "error"
                TASKS[tid]["error"] = str(e)

    threading.Thread(target=worker, daemon=True).start()
    return tid


def task_transcribe(cb, meeting, model, language, force):
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    mb.ensure_meeting_structure(m)
    audio_dir, t_dir = os.path.join(m, "audio"), os.path.join(m, "transcript")
    os.makedirs(t_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(audio_dir)
                   if os.path.splitext(f)[1].lower() in mb.MEDIA_EXTS)
    todo = [f for f in files
            if force or not os.path.exists(os.path.join(t_dir, f"{os.path.splitext(f)[0]}-转写.txt"))]
    if not todo:
        return {"done": [], "skipped": len(files), "already": True}
    done, failed = [], []
    mname = os.path.basename(m)
    live: list[str] = []  # 累积实时转写文本（供前端预览）
    for i, f in enumerate(todo):
        cb("transcribing", {"meeting": mname, "file": f, "index": i + 1, "total": len(todo)})

        def prog(s, info, _f=f):
            nonlocal live
            if s == "transcribing" and info.get("text"):
                live.append(f"[{info.get('ts', '')}] {info['text']}")
            cb(s, {**info, "meeting": mname, "file": _f, "live": list(live)})
        try:
            out = transcribe_audio(os.path.join(audio_dir, f), model_size=model,
                                   language=language, output_dir=t_dir, progress_cb=prog)
            if force:
                # 重新转写后，基于旧转写的修正版已失效
                corr = os.path.join(t_dir, mb.corrected_name_for(os.path.basename(out)))
                if os.path.isfile(corr):
                    os.remove(corr)
                    cb("live", {"k": "s", "t": f"旧转写已覆盖，删除过期修正版: {os.path.basename(corr)}"})
            done.append(os.path.basename(out))
        except Exception as e:  # noqa: BLE001
            failed.append({"file": f, "error": str(e)})
    # 转写完成后自动从转写提取会议属性（LLM 失败不影响转写结果）
    if done:
        try:
            cb("autofilling", {"meeting": mname})
            mb.autofill_meeting(m, force=False)
        except Exception:  # noqa: BLE001
            pass
    return {"done": done, "failed": failed, "skipped": len(files) - len(todo)}


def task_autofill(cb, meeting, force):
    """从转写文本提取属性填充 meeting.properties。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    cb("autofilling", {"meeting": mb.parse_meeting(m)["topic"]})
    props = mb.autofill_meeting(m, force=force)
    filled = {k: v for k, v in props.items() if k in mb.AUTOFILL_KEYS and v}
    return {"filled": filled}


def task_summarize(cb, meeting, force, only_file=None):
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    mb.ensure_meeting_structure(m)
    t_dir = os.path.join(m, "transcript")
    if not os.path.isdir(t_dir):
        raise RuntimeError("该会议没有转写文本——请先转写音频，再生成纪要。")
    md = mb.parse_meeting(m)
    files = sorted(f for f in os.listdir(t_dir) if f.endswith("-转写.txt"))
    if not files:
        raise RuntimeError("该会议没有转写文本——请先转写音频，再生成纪要。")
    # 有修正版时优先用修正版生成纪要
    src_for = {}
    for f in files:
        corr = os.path.join(t_dir, mb.corrected_name_for(f))
        src_for[f] = (corr, os.path.basename(corr)) if os.path.isfile(corr) \
            else (os.path.join(t_dir, f), f)
    if only_file:
        # 重新生成指定转写文件的纪要
        todo = [f for f in files if f == only_file]
        if not todo:
            raise RuntimeError(f"未找到转写文件: {only_file}")
    else:
        todo = [f for f in files
                if force or not os.path.exists(os.path.join(m, "notes", mb.note_name_for(f)))]
    if not todo:
        return {"done": [], "already": True}
    done, failed = [], []

    def live(kind, piece):
        cb("live", {"k": {"reasoning": "r", "content": "c"}.get(kind, "c"), "t": str(piece)})

    for i, f in enumerate(todo):
        cb("summarizing", {"meeting": md["name"], "file": src_for[f][1],
                           "index": i + 1, "total": len(todo)})
        try:
            out = mb.summarize_transcript(src_for[f][0], md, save=True, on_delta=live)
            done.append(os.path.basename(out))
        except Exception as e:  # noqa: BLE001
            failed.append({"file": f, "error": str(e)})
    return {"done": done, "failed": failed, "already": False}


def task_correct(cb, meeting, force):
    """LLM 修正转写（谐音纠错 + 合并零散句），生成 <名>-修正.txt。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    mb.ensure_meeting_structure(m)
    t_dir = os.path.join(m, "transcript")
    if not os.path.isdir(t_dir):
        raise RuntimeError("该会议没有转写文本——请先转写音频。")
    files = sorted(f for f in os.listdir(t_dir) if f.endswith("-转写.txt"))
    todo = [f for f in files
            if force or not os.path.exists(os.path.join(t_dir, mb.corrected_name_for(f)))]
    if not todo:
        return {"done": [], "skipped": len(files), "already": True}
    mname = mb.parse_meeting(m)["topic"]
    done = []

    def live(kind, piece):
        cb("live", {"k": {"reasoning": "r", "content": "c", "sep": "s"}.get(kind, "c"),
                    "t": str(piece)})

    for i, f in enumerate(todo):
        cb("correcting", {"meeting": mname, "file": f, "index": i + 1, "total": len(todo)})
        out = mb.correct_transcript(
            os.path.join(t_dir, f), force=force,
            progress_cb=lambda d, n, _i=i, _f=f: cb("correcting", {
                "meeting": mname, "file": _f, "index": _i + 1, "total": len(todo),
                "chunk": d, "chunks": n}),
            delta_cb=live)
        if out:
            done.append(os.path.basename(out))
    return {"done": done, "skipped": len(files) - len(todo)}


def task_analyze(cb, meeting, force):
    """AI 按内容把会议拆成多个部分并附简述，写 analysis.md。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    cb("analyzing", {"meeting": mb.parse_meeting(m)["topic"]})

    def live(kind, piece):
        cb("live", {"k": {"reasoning": "r", "content": "c"}.get(kind, "c"), "t": str(piece)})

    return mb.analyze_meeting(m, force=force, on_delta=live)


def task_ask(cb, question, meeting, top_k):
    idx = mb.build_index(meeting)
    if not idx.docs:
        raise RuntimeError("仓库中没有可检索的转写/纪要文本，请先转写。")
    hits = idx.search(question, top_k=top_k)
    cb("searching", {"hits": len(hits)})
    if not hits:
        return {"answer": "未检索到相关内容，无法回答。可换个问法或先转写更多会议。", "hits": []}
    context = "\n\n".join(f"【来源: {h['meeting']} / {h['file']}】\n{h['text']}" for h in hits)
    system = ("你是会议档案助手。根据用户提供的会议材料（含来源标注）回答问题。"
              "只能基于材料内容回答；材料不足时明确说明“材料中没有提到”。"
              "回答用中文，简洁有条理，必要时引用来源会议。")
    cb("llm", {})

    def live(kind, piece):
        if kind == "content":  # 问答窗口只看正文
            cb("live", {"k": "c", "t": str(piece)})

    answer = mb.llm_chat(system, f"问题: {question}\n\n相关会议材料:\n{context}",
                         temperature=0.2, max_tokens=1024, on_delta=live)
    return {"answer": answer,
            "hits": [{"meeting": h["meeting"], "file": h["file"], "kind": h["kind"],
                      "score": round(h["score"], 2), "text": h["text"]} for h in hits]}


# ---------------- 页面与静态文件 ----------------

@app.get("/")
def index():
    return send_from_directory(WEBUI_DIR, "index.html")


@app.get("/files/<path:filepath>")
def serve_file(filepath):
    """服务数据目录内文件（用于音频播放），限制在 MEETINGS_ROOT 内（支持项目外/中文路径）。"""
    try:
        full = mb.safe_join(mb.MEETINGS_ROOT, filepath)
    except ValueError:
        abort(404)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(os.path.dirname(full), os.path.basename(full))


THUMB_MAX_PX = 2560  # 预览图最长边（灯箱查看足够清晰，兼顾转换耗时）


@app.get("/thumb/<path:filepath>")
def serve_thumb(filepath):
    """HEIC/HEIF 等浏览器不支持的图片 → JPEG 预览（磁盘缓存，原图变更自动失效）。"""
    try:
        full = mb.safe_join(mb.MEETINGS_ROOT, filepath)
    except ValueError:
        abort(404)
    if not os.path.isfile(full) or os.path.splitext(full)[1].lower() not in CONVERT_IMAGE_EXTS:
        abort(404)
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
    except ImportError:
        return ("服务器缺少 pillow-heif，无法预览 HEIC 图片。请运行: pip install pillow-heif", 503)
    st = os.stat(full)
    cache_key = hashlib.md5(f"{os.path.realpath(full)}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    cache_dir = os.path.join(mb.MEETINGS_ROOT, ".thumbcache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, cache_key + ".jpg")
    if not os.path.isfile(cache_file):
        register_heif_opener()
        with Image.open(full) as im:
            im = im.convert("RGB") if im.mode != "RGB" else im.copy()
            im.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
            im.save(cache_file, "JPEG", quality=88)
    return send_from_directory(cache_dir, cache_key + ".jpg", mimetype="image/jpeg")


# ---------------- API ----------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
# 浏览器不能直接显示、需服务端转码的图片格式（HEIC/HEIF：华为/苹果高效压缩格式）
CONVERT_IMAGE_EXTS = {".heic", ".heif"}


def _file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS or ext in CONVERT_IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "other"


def meeting_detail(m: str) -> dict:
    md = mb.parse_meeting(m)
    audio, transcripts, notes, attachments = [], [], [], []
    agenda = ""
    ag = os.path.join(m, "agenda.md")
    if os.path.isfile(ag):
        agenda = _read_text(ag)
    for sub, out in (("audio", audio), ("transcript", transcripts),
                     ("notes", notes), ("attachments", attachments)):
        d = os.path.join(m, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                # 跳过占位文件与子目录（如留言板侧车目录 .comments）
                if f == ".gitkeep" or not os.path.isfile(os.path.join(d, f)):
                    continue
                rel = os.path.relpath(os.path.join(d, f), mb.MEETINGS_ROOT).replace("\\", "/")
                item = {"name": f, "path": rel}
                if sub == "audio":
                    item["url"] = f"/files/{rel}"
                    item["size_mb"] = round(os.path.getsize(os.path.join(d, f)) / 1048576, 1)
                    item["kind"] = "video" if os.path.splitext(f)[1].lower() in mb.VIDEO_EXTS else "audio"
                elif sub == "attachments":
                    item["url"] = f"/files/{rel}"
                    item["size"] = os.path.getsize(os.path.join(d, f))
                    item["kind"] = _file_kind(f)
                    # HEIC/HEIF 浏览器无法直接显示，走服务端转码预览
                    if os.path.splitext(f)[1].lower() in CONVERT_IMAGE_EXTS:
                        item["preview"] = f"/thumb/{rel}"
                elif sub == "transcript":
                    item["content"] = _read_text(os.path.join(d, f))
                    item["corrected"] = f.endswith("-修正.txt")
                else:
                    item["content"] = _read_text(os.path.join(d, f))
                out.append(item)
    return {"name": md["name"], "date": md["date"], "seq": md["seq"],
            "title": md["topic"], "props": mb.read_props(m),
            "agenda": agenda,
            "audio": audio, "transcripts": transcripts, "notes": notes,
            "attachments": attachments, "analysis": mb.read_analysis(m)}


def _read_text(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


@app.get("/api/meetings")
def api_meetings():
    items = []
    for m in mb.find_meetings():
        d = meeting_detail(m)
        items.append({"name": d["name"], "date": d["date"], "seq": d["seq"],
                      "title": d["title"],
                      "audio": len(d["audio"]), "transcripts": len(d["transcripts"]),
                      "notes": len(d["notes"]), "attachments": len(d["attachments"])})
    return jsonify(items)


@app.get("/api/meeting/<name>")
def api_meeting(name):
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    return jsonify(meeting_detail(m))


def task_agenda_autofill(cb, meeting):
    """从转写文本生成议程（议题/备注），返回 {topics, notes}。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    cb("agenda_llm", {"meeting": mb.parse_meeting(m)["topic"]})
    return mb.autofill_agenda(m)


@app.post("/api/meeting/<name>/agenda")
def api_meeting_agenda(name):
    """保存议程：更新头部属性（props）+ 议题/备注（重写 agenda.md）。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    data = request.get_json(force=True)
    # 头部字段 → meeting.properties
    props = {}
    for k in ("time", "location", "organizer", "participants"):
        if k in data:
            props[k] = str(data.get(k) or "").strip()
    if "title" in data:
        props["name"] = str(data.get("title") or "").strip()
    if props:
        mb.write_props(m, props)
    # 议题/备注 → agenda.md
    topics = str(data.get("topics") or "").strip()
    notes = str(data.get("notes") or "").strip()
    mb.write_agenda(m, topics=topics, notes=notes)
    return jsonify({"ok": True})


@app.post("/api/meeting/<name>/agenda/autofill")
def api_meeting_agenda_autofill(name):
    """AI 从转写生成议程（议题/备注），返回 task（生成后前端确认保存）。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    tid = start_task("agenda_autofill", task_agenda_autofill, name)
    return jsonify({"task": tid})


@app.post("/api/meeting/<name>/rename")
def api_meeting_rename(name):
    """修改会议显示名称（写入 meeting.properties，不改文件夹名）。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    data = request.get_json(force=True)
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "名称不能为空"}), 400
    mb.write_meeting_name(m, new_name)
    return jsonify({"ok": True, "name": new_name})


@app.post("/api/meeting/<name>/remove")
def api_meeting_remove(name):
    """删除会议：移入回收站 .trash/（可恢复）。需 confirm=true。"""
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "缺少确认标记 confirm=true"}), 400
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    dst = mb.trash_meeting(m)
    return jsonify({"ok": True, "trash": mb.display_path(dst).replace("\\", "/")})


@app.post("/api/meeting/<name>/props")
def api_meeting_props(name):
    """批量更新会议通用属性（写入 meeting.properties）。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    data = request.get_json(force=True)
    props = data.get("props") or {}
    if not isinstance(props, dict):
        return jsonify({"error": "props 须为对象"}), 400
    # 只接受通用属性键（防写入任意键）
    allowed = set(mb.MEETING_PROP_KEYS)
    clean = {k: str(v).strip() for k, v in props.items() if k in allowed}
    if "name" in clean and not clean["name"]:
        return jsonify({"error": "名称不能为空"}), 400
    mb.write_props(m, clean)
    return jsonify({"ok": True, "props": mb.read_props(m)})


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n]', "_", name)
    return name or "upload"


def _unique_name(d: str, name: str) -> str:
    """目录内重名时追加序号：photo.jpg -> photo (1).jpg"""
    if not os.path.exists(os.path.join(d, name)):
        return name
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(d, f"{base} ({i}){ext}")):
        i += 1
    return f"{base} ({i}){ext}"


@app.post("/api/meeting/<name>/attachments")
def api_attachment_upload(name):
    """上传一个或多个附件（图片/PDF/任意文件），保存到会议 attachments/ 目录。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    files = [f for f in request.files.getlist("file") if f.filename]
    if not files:
        return jsonify({"error": "未选择文件"}), 400
    adir = os.path.join(m, "attachments")
    os.makedirs(adir, exist_ok=True)
    saved = []
    try:
        for f in files:
            fname = _unique_name(adir, _sanitize_filename(f.filename))
            f.save(os.path.join(adir, fname))
            saved.append(fname)
    except OSError as e:
        return jsonify({"error": f"保存失败: {e}", "saved": saved}), 500
    return jsonify({"ok": True, "saved": saved})


@app.post("/api/meeting/<name>/attachments/delete")
def api_attachment_delete(name):
    """删除单个附件文件。需 confirm=true。"""
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "缺少确认标记 confirm=true"}), 400
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    fname = _sanitize_filename(str(data.get("file") or ""))
    try:
        full = mb.safe_join(m, "attachments", fname)
    except ValueError:
        return jsonify({"error": "非法文件名"}), 400
    if not os.path.isfile(full):
        return jsonify({"error": "文件不存在"}), 404
    try:
        os.remove(full)
    except OSError as e:
        return jsonify({"error": f"删除失败: {e}"}), 500
    # 同步清理该图片的留言板 / 问答会话侧车文件
    for sub in (".comments", ".chat"):
        try:
            side = mb.safe_join(m, "attachments", sub, fname + ".json")
            if os.path.isfile(side):
                os.remove(side)
        except (ValueError, OSError):
            pass
    return jsonify({"ok": True})


@app.post("/api/import")
def api_import():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    meeting = (request.form.get("meeting") or "").strip() or None
    d = (request.form.get("date") or "").strip() or None
    tmpdir = tempfile.mkdtemp(prefix="mb_up_")
    tmp = os.path.join(tmpdir, _sanitize_filename(f.filename))
    try:
        f.save(tmp)
        mb.cmd_import(mb.SimpleNamespace(audio=tmp, meeting=meeting, date=d, move=False))
    except SystemExit as e:
        return jsonify({"error": str(e) or "导入失败"}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/transcribe")
def api_transcribe():
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    model = data.get("model", "medium")
    language = data.get("language") or None
    force = bool(data.get("force"))
    tid = start_task("transcribe", task_transcribe, meeting, model, language, force)
    return jsonify({"task": tid})


@app.post("/api/correct")
def api_correct():
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    tid = start_task("correct", task_correct, meeting, bool(data.get("force")))
    return jsonify({"task": tid})


@app.post("/api/summarize")
def api_summarize():
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    only_file = data.get("file") or None
    tid = start_task("summarize", task_summarize, meeting, bool(data.get("force")), only_file)
    return jsonify({"task": tid})


@app.post("/api/analyze")
def api_analyze():
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    tid = start_task("analyze", task_analyze, meeting, bool(data.get("force")))
    return jsonify({"task": tid})


@app.post("/api/ask")
def api_ask():
    data = request.get_json(force=True)
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    meeting = data.get("meeting") or None
    top_k = int(data.get("top_k", 5))
    tid = start_task("ask", task_ask, question, meeting, top_k)
    return jsonify({"task": tid})


# ---------------- AI 看图问答（多模态 LLM） ----------------

VISION_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
VISION_INLINE_MAX_BYTES = 6 * 1024 * 1024  # 小于此大小的常见格式直接原样发送
VISION_MAX_PX = 2048                       # 过大图片压缩到最长边（省 token/带宽）


def _image_data_url(path: str) -> str:
    """图片 → data URL（base64）供多模态模型使用。

    HEIC/HEIF 先转 JPEG；超过 VISION_INLINE_MAX_BYTES 或未知格式的图用 Pillow
    转码压缩（最长边 VISION_MAX_PX）。SVG 不支持。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        raise RuntimeError("暂不支持 SVG 图片的 AI 问答")
    mime = VISION_IMAGE_MIME.get(ext)
    if mime and os.path.getsize(path) <= VISION_INLINE_MAX_BYTES:
        with open(path, "rb") as f:
            return f"data:{mime};base64," + base64.b64encode(f.read()).decode()
    return _image_data_url_pil(path, heic=ext in CONVERT_IMAGE_EXTS)


def _image_data_url_pil(path: str, heic: bool) -> str:
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
    except ImportError:
        need = "pillow-heif" if heic else "pillow"
        raise RuntimeError(
            f"该图片需要转码但服务器缺少 {need}。请运行: pip install pillow-heif")
    if heic:
        register_heif_opener()
    with Image.open(path) as im:
        im = im.convert("RGB") if im.mode != "RGB" else im.copy()
        im.thumbnail((VISION_MAX_PX, VISION_MAX_PX), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------- 图片留言板 ----------------

def _comments_path(meeting: str, fname: str) -> str:
    """某附件留言板的侧车文件路径：attachments/.comments/<文件名>.json。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    try:
        return mb.safe_join(m, "attachments", ".comments", fname + ".json")
    except ValueError:
        raise ValueError("非法文件名")


def _load_comments(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("comments") if isinstance(data, dict) and isinstance(data.get("comments"), list) else []
    except (OSError, ValueError):
        return []


def _save_comments(path: str, fname: str, comments: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"file": fname, "comments": comments}, f, ensure_ascii=False, indent=2)


COMMENT_TAG_RE = re.compile(r"#([^\s#]{1,20})")


def _extract_tags(text: str) -> list:
    """从留言文本提取 #标签（去重、最多 10 个），如「白板要点 #待办 #重点」。"""
    tags = []
    for t in COMMENT_TAG_RE.findall(text):
        if t not in tags:
            tags.append(t)
        if len(tags) >= 10:
            break
    return tags


@app.get("/api/meeting/<name>/attachment-comments")
def api_attachment_comments(name):
    """读取某附件的留言列表。"""
    try:
        path = _comments_path(name, _sanitize_filename(request.args.get("file", "")))
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), (404 if isinstance(e, RuntimeError) else 400)
    return jsonify({"comments": _load_comments(path)})


@app.post("/api/meeting/<name>/attachment-comments/add")
def api_attachment_comments_add(name):
    """为附件追加一条留言 {file, text}。"""
    data = request.get_json(force=True)
    fname = _sanitize_filename(str(data.get("file") or ""))
    text = str(data.get("text") or "").strip()
    if not fname or not text:
        return jsonify({"error": "缺少图片或留言内容"}), 400
    try:
        path = _comments_path(name, fname)
        m = mb.pick_meeting(name)
        if not m or not os.path.isfile(mb.safe_join(m, "attachments", fname)):
            return jsonify({"error": "图片不存在"}), 404
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), (404 if isinstance(e, RuntimeError) else 400)
    comments = _load_comments(path)
    comments.append({"id": str(int(time.time() * 1000)), "time": time.strftime("%Y-%m-%d %H:%M"),
                     "text": text, "tags": _extract_tags(text), "collapsed": False})
    _save_comments(path, fname, comments)
    return jsonify({"ok": True, "comments": comments})


@app.post("/api/meeting/<name>/attachment-comments/update")
def api_attachment_comments_update(name):
    """更新附件留言：{file, id, collapsed?, tags?, all?=true}。
    all=true 时把 collapsed 应用到该图片的全部留言（收起/展开全部）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _comments_path(name, _sanitize_filename(str(data.get("file") or "")))
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), (404 if isinstance(e, RuntimeError) else 400)
    comments = _load_comments(path)
    if not comments:
        return jsonify({"error": "留言不存在"}), 404
    targets = comments if data.get("all") else \
        [c for c in comments if str(c.get("id")) == str(data.get("id") or "")]
    if not targets:
        return jsonify({"error": "留言不存在"}), 404
    if "collapsed" in data:
        collapsed = bool(data.get("collapsed"))
        for c in targets:
            c["collapsed"] = collapsed
    if "tags" in data and not data.get("all"):
        tags = data.get("tags")
        if not isinstance(tags, list):
            return jsonify({"error": "tags 须为字符串数组"}), 400
        clean = []
        for t in tags[:10]:
            t = re.sub(r"[^\w\u4e00-\u9fff-]", "", str(t)).strip()
            if t and t not in clean:
                clean.append(t)
        targets[0]["tags"] = clean
    _save_comments(path, str(data.get("file") or ""), comments)
    return jsonify({"ok": True, "comments": comments})


@app.post("/api/meeting/<name>/attachment-comments/delete")
def api_attachment_comments_delete(name):
    """删除附件的一条留言 {file, id}。需 confirm=true。"""
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "缺少确认标记 confirm=true"}), 400
    try:
        path = _comments_path(name, _sanitize_filename(str(data.get("file") or "")))
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), (404 if isinstance(e, RuntimeError) else 400)
    cid = str(data.get("id") or "")
    comments = _load_comments(path)
    kept = [c for c in comments if str(c.get("id")) != cid]
    if len(kept) == len(comments):
        return jsonify({"error": "留言不存在"}), 404
    _save_comments(path, str(data.get("file") or ""), kept)
    return jsonify({"ok": True, "comments": kept})


# ---------------- AI 看图问答会话 ----------------

def _chat_path(meeting: str, fname: str) -> str:
    """某附件问答会话的侧车文件路径：attachments/.chat/<文件名>.json。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    try:
        return mb.safe_join(m, "attachments", ".chat", fname + ".json")
    except ValueError:
        raise ValueError("非法文件名")


def _load_sessions(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sessions") if isinstance(data, dict) and isinstance(data.get("sessions"), list) else []
    except (OSError, ValueError):
        return []


def _save_sessions(path: str, fname: str, sessions: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"file": fname, "sessions": sessions}, f, ensure_ascii=False, indent=2)


def _find_session(sessions: list, sid) -> "dict | None":
    return next((s for s in sessions if str(s.get("id")) == str(sid or "")), None)


def _sorted_sessions(sessions: list) -> list:
    """会话按最近更新时间倒序（时序排列，最新在前）。"""
    return sorted(sessions, key=lambda s: str(s.get("updated", "")), reverse=True)


def _chat_file_route_guard(name: str, fname: str) -> str:
    try:
        return _chat_path(name, fname)
    except RuntimeError as e:
        abort(404, description=str(e))
    except ValueError as e:
        abort(400, description=str(e))


@app.get("/api/meeting/<name>/attachment-chats")
def api_attachment_chats(name):
    """读取某附件的全部问答会话（按最近更新倒序）。"""
    path = _chat_file_route_guard(name, _sanitize_filename(request.args.get("file", "")))
    return jsonify({"sessions": _sorted_sessions(_load_sessions(path))})


@app.post("/api/meeting/<name>/attachment-chats/new")
def api_attachment_chats_new(name):
    """新建一个空会话。"""
    data = request.get_json(silent=True) or {}
    fname = _sanitize_filename(str(data.get("file") or ""))
    path = _chat_file_route_guard(name, fname)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sess = {"id": str(int(time.time() * 1000)),
            "title": (str(data.get("title") or "").strip() or "新会话")[:30],
            "created": now, "updated": now, "messages": []}
    sessions = _load_sessions(path)
    sessions.append(sess)
    _save_sessions(path, fname, sessions)
    return jsonify({"ok": True, "session": sess})


@app.post("/api/meeting/<name>/attachment-chats/rename")
def api_attachment_chats_rename(name):
    """重命名会话 {file, id, title}。"""
    data = request.get_json(silent=True) or {}
    fname = _sanitize_filename(str(data.get("file") or ""))
    path = _chat_file_route_guard(name, fname)
    title = str(data.get("title") or "").strip()[:30]
    if not title:
        return jsonify({"error": "会话名不能为空"}), 400
    sessions = _load_sessions(path)
    sess = _find_session(sessions, data.get("id"))
    if not sess:
        return jsonify({"error": "会话不存在"}), 404
    sess["title"] = title
    _save_sessions(path, fname, sessions)
    return jsonify({"ok": True, "session": sess})


@app.post("/api/meeting/<name>/attachment-chats/clear")
def api_attachment_chats_clear(name):
    """清空会话的全部消息 {file, id}（会话保留，可继续提问）。"""
    data = request.get_json(silent=True) or {}
    fname = _sanitize_filename(str(data.get("file") or ""))
    path = _chat_file_route_guard(name, fname)
    sessions = _load_sessions(path)
    sess = _find_session(sessions, data.get("id"))
    if not sess:
        return jsonify({"error": "会话不存在"}), 404
    sess["messages"] = []
    sess["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_sessions(path, fname, sessions)
    return jsonify({"ok": True, "session": sess})


@app.post("/api/meeting/<name>/attachment-chats/delete")
def api_attachment_chats_delete(name):
    """删除会话 {file, id}。需 confirm=true。"""
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "缺少确认标记 confirm=true"}), 400
    fname = _sanitize_filename(str(data.get("file") or ""))
    path = _chat_file_route_guard(name, fname)
    sessions = _load_sessions(path)
    kept = [s for s in sessions if str(s.get("id")) != str(data.get("id") or "")]
    if len(kept) == len(sessions):
        return jsonify({"error": "会话不存在"}), 404
    _save_sessions(path, fname, kept)
    return jsonify({"ok": True})


def task_vision_ask(cb, meeting, fname, question, session_id):
    """多模态 LLM 看图问答：读取会议附件图片，结合会话历史回答。

    会话持久化在 attachments/.chat/<文件名>.json；session_id 不存在时自动
    新建会话（标题取问题前 20 字），成功后才把本轮问答写回会话。"""
    m = mb.pick_meeting(meeting)
    if not m:
        raise RuntimeError(f"未找到会议: {meeting}")
    try:
        full = mb.safe_join(m, "attachments", fname)
    except ValueError:
        raise RuntimeError("非法文件名")
    if not os.path.isfile(full):
        raise RuntimeError(f"附件不存在: {fname}")
    cb("preparing", {"file": fname})
    data_url = _image_data_url(full)
    content = [{"type": "image_url", "image_url": {"url": data_url}},
               {"type": "text", "text": question}]

    def now_ts() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    try:
        chat_path = _chat_path(meeting, fname)
        sessions = _load_sessions(chat_path)
    except (RuntimeError, ValueError):
        chat_path, sessions = None, []
    sess = _find_session(sessions, session_id) if session_id else None
    if sess is None:
        sess = {"id": str(int(time.time() * 1000)), "title": question[:20] or "新会话",
                "created": now_ts(), "updated": now_ts(), "messages": []}
        sessions.append(sess)
    msgs = sess.setdefault("messages", [])
    msgs.append({"role": "user", "content": question, "time": now_ts()})
    history = [{"role": x.get("role"), "content": x.get("content")}
               for x in msgs[:-1] if x.get("role") in ("user", "assistant")][-20:]

    system = ("你是会议图片助手。用户会给你一张会议相关的图片（照片/截图/白板等），"
              "请根据图片内容回答问题。回答用中文，简洁准确；"
              "图片中看不到或无法确定的信息要明确说明。")
    cb("llm", {"file": fname})

    def live(kind, piece):
        if kind == "content":  # 悬浮窗只看正文流
            cb("live", {"k": "c", "t": str(piece)})

    try:
        answer = mb.llm_chat(system, content, temperature=0.3, max_tokens=2048,
                             history=history, on_delta=live)
    except SystemExit as e:  # get_api_key 未配置 Key 时抛 SystemExit
        raise RuntimeError(str(e).strip().splitlines()[0] if str(e).strip() else "未配置 API Key")
    msgs.append({"role": "assistant", "content": answer, "time": now_ts()})
    sess["updated"] = now_ts()
    if chat_path:
        _save_sessions(chat_path, fname, sessions)
    return {"answer": answer, "session": sess}


@app.post("/api/meeting/<name>/attachments/ask")
def api_attachment_ask(name):
    """AI 看图问答：对会议附件图片提问（多模态 LLM），会话自动持久化，返回 task。"""
    m = mb.pick_meeting(name)
    if not m:
        return jsonify({"error": "会议不存在"}), 404
    try:
        mb.get_api_key()
    except SystemExit as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else "未配置 API Key"
        return jsonify({"error": msg}), 400
    data = request.get_json(force=True)
    fname = _sanitize_filename(str(data.get("file") or ""))
    question = (data.get("question") or "").strip()
    if not fname or not question:
        return jsonify({"error": "缺少图片或问题"}), 400
    session_id = str(data.get("session") or "") or None
    tid = start_task("vision_ask", task_vision_ask, name, fname, question, session_id)
    return jsonify({"task": tid})


@app.post("/api/search")
def api_search():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "关键词不能为空"}), 400
    idx = mb.build_index(data.get("meeting") or None)
    if not idx.docs:
        return jsonify({"hits": [], "error": "没有可检索的转写/纪要文本"}), 200
    hits = idx.search(query, top_k=int(data.get("top_k", 5)))
    return jsonify({"hits": [{"meeting": h["meeting"], "file": h["file"], "kind": h["kind"],
                              "score": round(h["score"], 2), "text": h["text"]} for h in hits]})


@app.post("/api/autofill")
def api_autofill():
    """从转写文本自动提取会议属性并填充。"""
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    tid = start_task("autofill", task_autofill, meeting, bool(data.get("force")))
    return jsonify({"task": tid})


@app.get("/api/task/<tid>")
def api_task(tid):
    with _TLOCK:
        t = TASKS.get(tid)
        if t:
            t = {**t, "elapsed": round(time.time() - t.get("t0", time.time()))}
    if not t:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(t)


@app.get("/api/config")
def api_config():
    mb.load_env()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return jsonify({
        "configured": bool(key),
        "masked": mb.mask_key(key) if key else None,
        "model": mb.deepseek_model(),
        "base_url": mb.deepseek_base_url(),
        "env_file": os.path.basename(mb.env_file_path()),
        "data_dir": mb.MEETINGS_ROOT,
        "summary_max_input_chars": mb.summary_max_input_chars(),
        "summary_max_tokens": mb.summary_max_tokens(),
    })


@app.post("/api/models")
def api_models():
    """从当前 base_url/key 拉取可用模型列表（OpenAI 兼容 /models）。"""
    try:
        models = mb.fetch_models()
        return jsonify({"ok": True, "models": models})
    except SystemExit as e:
        return jsonify({"error": str(e).strip() or "未配置 API Key"}), 400
    except Exception as e:
        msg = str(e).strip()
        if len(msg) > 300:
            msg = msg[:300] + "…"
        return jsonify({"error": msg or "获取模型列表失败"}), 400


@app.post("/api/config")
def api_config_set():
    data = request.get_json(force=True)
    action = data.get("action")
    if action == "set":
        key = (data.get("key") or "").strip()
        if not key:
            return jsonify({"error": "Key 不能为空"}), 400
        mb.save_api_key(key)
        os.environ["DEEPSEEK_API_KEY"] = key
        return jsonify({"ok": True, "masked": mb.mask_key(key)})
    if action == "clear":
        mb.clear_api_key()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        return jsonify({"ok": True})
    if action == "set_dir":
        path = data.get("path") or ""
        try:
            new_dir = mb.set_meetings_root(path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        mb.save_env("MEETINGS_ROOT", new_dir)  # 持久化到 .env，重启后仍生效
        return jsonify({"ok": True, "data_dir": new_dir})
    if action == "set_setting":
        # 通用设置键保存：写 .env + 更新当前进程环境变量（立即生效）
        key = (data.get("key") or "").strip()
        allowed = {"SUMMARY_MAX_INPUT_CHARS", "SUMMARY_MAX_TOKENS",
                    "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"}
        if key not in allowed:
            return jsonify({"error": f"不支持的设置键: {key}"}), 400
        if key in ("SUMMARY_MAX_INPUT_CHARS", "SUMMARY_MAX_TOKENS"):
            # 数字类设置
            try:
                value = str(int(data.get("value", "")))
            except ValueError:
                return jsonify({"error": "必须是正整数"}), 400
            if key == "SUMMARY_MAX_TOKENS" and int(value) > mb.LLM_MAX_OUTPUT_TOKENS:
                return jsonify({"error": f"输出上限最大 {mb.LLM_MAX_OUTPUT_TOKENS}（DeepSeek 384K）"}), 400
        else:
            # 字符串类设置（模型名 / API 地址）
            value = str(data.get("value", "")).strip().strip('"').strip("'")
            if not value:
                return jsonify({"error": "值不能为空"}), 400
        mb.save_env(key, value)
        os.environ[key] = value
        return jsonify({"ok": True, key.lower(): value})
    return jsonify({"error": "未知操作"}), 400


# ---------------- 启动 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="MeetingBook Web 界面")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"MeetingBook Web 界面已启动: {url}  (Ctrl+C 退出)  [v{mb.APP_VERSION}]")
    print(f"项目根目录: {ROOT}")
    print(f"会议数据目录: {mb.MEETINGS_ROOT}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    sys.exit(main())
