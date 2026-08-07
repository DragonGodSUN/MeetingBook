#!/usr/bin/env python3
"""MeetingBook Web 界面 — 本地可视化操作界面（Flask）

用法:
    python tools/webui.py                # 启动服务并自动打开浏览器
    python tools/webui.py --no-browser   # 不自动打开浏览器
    python tools/webui.py --port 8080    # 指定端口（默认 8765）

依赖: pip install flask
"""
import argparse
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
                      "created": time.strftime("%H:%M:%S")}

    def worker():
        try:
            def cb(stage, info=None):
                with _TLOCK:
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
    for i, f in enumerate(todo):
        cb("summarizing", {"meeting": md["name"], "file": f, "index": i + 1, "total": len(todo)})
        try:
            out = mb.summarize_transcript(os.path.join(t_dir, f), md, save=True)
            done.append(os.path.basename(out))
        except Exception as e:  # noqa: BLE001
            failed.append({"file": f, "error": str(e)})
    return {"done": done, "failed": failed, "already": False}


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
    answer = mb.llm_chat(system, f"问题: {question}\n\n相关会议材料:\n{context}",
                         temperature=0.2, max_tokens=1024)
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


# ---------------- API ----------------

def meeting_detail(m: str) -> dict:
    md = mb.parse_meeting(m)
    audio, transcripts, notes = [], [], []
    agenda = ""
    ag = os.path.join(m, "agenda.md")
    if os.path.isfile(ag):
        agenda = _read_text(ag)
    for sub, out in (("audio", audio), ("transcript", transcripts), ("notes", notes)):
        d = os.path.join(m, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f == ".gitkeep":
                    continue
                rel = os.path.relpath(os.path.join(d, f), mb.MEETINGS_ROOT).replace("\\", "/")
                item = {"name": f, "path": rel}
                if sub == "audio":
                    item["url"] = f"/files/{rel}"
                    item["size_mb"] = round(os.path.getsize(os.path.join(d, f)) / 1048576, 1)
                    item["kind"] = "video" if os.path.splitext(f)[1].lower() in mb.VIDEO_EXTS else "audio"
                elif sub == "transcript":
                    item["content"] = _read_text(os.path.join(d, f))
                else:
                    item["content"] = _read_text(os.path.join(d, f))
                out.append(item)
    return {"name": md["name"], "date": md["date"], "seq": md["seq"],
            "title": md["topic"], "props": mb.read_props(m),
            "agenda": agenda,
            "audio": audio, "transcripts": transcripts, "notes": notes}


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
                      "notes": len(d["notes"])})
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


@app.post("/api/summarize")
def api_summarize():
    data = request.get_json(force=True)
    meeting = data.get("meeting", "")
    if not meeting:
        return jsonify({"error": "缺少会议"}), 400
    only_file = data.get("file") or None
    tid = start_task("summarize", task_summarize, meeting, bool(data.get("force")), only_file)
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
        "model": mb.DEEPSEEK_MODEL,
        "base_url": mb.DEEPSEEK_BASE_URL,
        "env_file": os.path.basename(mb.env_file_path()),
        "data_dir": mb.MEETINGS_ROOT,
        "summary_max_input_chars": mb.summary_max_input_chars(),
        "summary_max_tokens": mb.summary_max_tokens(),
    })


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
        allowed = {"SUMMARY_MAX_INPUT_CHARS", "SUMMARY_MAX_TOKENS"}
        if key not in allowed:
            return jsonify({"error": f"不支持的设置键: {key}"}), 400
        try:
            value = str(int(data.get("value", "")))
        except ValueError:
            return jsonify({"error": "必须是正整数"}), 400
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
    print(f"MeetingBook Web 界面已启动: {url}  (Ctrl+C 退出)")
    print(f"会议仓库: {ROOT}")
    print(f"会议数据目录: {mb.MEETINGS_ROOT}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    sys.exit(main())
