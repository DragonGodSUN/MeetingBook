#!/usr/bin/env python3
"""会议音频转写工具（基于 faster-whisper）

用法:
    python tools/transcribe.py <音频文件> [--model medium] [--language zh] [--output-dir <目录>]

示例:
    python tools/transcribe.py "2026/2026-08-06-产品评审/audio/录音.m4a"
    python tools/transcribe.py audio.m4a --model large-v3 --language zh

默认行为:
    - 模型: medium（可用 large-v3 等；也可传本地模型目录路径）
    - 语言: 自动检测（中文会议建议显式 --language zh 更稳）
    - 输出: 带时间戳的 txt，写到音频所在目录的 ../transcript/ 下
      （命名: <音频名>-转写.txt），可用 --output-dir 指定
    - 设备: 优先 CUDA，失败自动回退 CPU；显存不足可加 --compute-type int8
"""
import argparse
import os
import sys
import time
from datetime import timedelta


def fmt_ts(seconds: float) -> str:
    """秒 -> HH:MM:SS"""
    return str(timedelta(seconds=int(seconds)))


def pick_device(compute_type: str) -> tuple[str, str]:
    """选择设备与计算精度。CUDA 可用则用 GPU，否则回退 CPU。"""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            ct = compute_type if compute_type != "int8_float16" else "int8_float16"
            if compute_type == "auto":
                ct = "int8_float16"
            return "cuda", ct
    except Exception:
        pass
    # CPU 回退：int8 量化最省内存
    return "cpu", "int8"


def main() -> int:
    parser = argparse.ArgumentParser(description="会议音频转写（faster-whisper）")
    parser.add_argument("audio", help="音频文件路径（m4a/wav/mp3 等，需 ffmpeg 支持）")
    parser.add_argument("--model", default="medium",
                        help="模型大小或本地模型目录（small/medium/large-v3/...），默认 medium")
    parser.add_argument("--language", default=None,
                        help="语言代码，如 zh（默认自动检测）")
    parser.add_argument("--output-dir", default=None,
                        help="输出目录（默认: 音频所在目录的上级/transcript）")
    parser.add_argument("--compute-type", default="auto",
                        help="计算精度：auto（GPU 用 int8_float16）/ int8 / float16")
    parser.add_argument("--vad", action="store_true", default=True,
                        help="开启语音活动检测，跳过静音段（默认开启）")
    args = parser.parse_args()

    audio = os.path.abspath(args.audio)
    if not os.path.isfile(audio):
        print(f"错误: 找不到音频文件 {audio}", file=sys.stderr)
        return 1

    # 输出路径：默认 <音频所在会议目录>/transcript/<音频名>-转写.txt
    if args.output_dir:
        out_dir = os.path.abspath(args.output_dir)
    else:
        audio_dir = os.path.dirname(audio)
        out_dir = os.path.join(audio_dir, "..", "transcript")
    out_dir = os.path.normpath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(audio))[0]
    out_path = os.path.join(out_dir, f"{base}-转写.txt")

    # 加载模型
    print(f"[1/3] 加载模型 {args.model} ...")
    t0 = time.time()
    device, compute_type = pick_device(args.compute_type)
    print(f"      设备: {device}, 精度: {compute_type}")
    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    print(f"      模型加载完成 ({time.time()-t0:.1f}s)")

    # 转写
    print(f"[2/3] 转写 {os.path.basename(audio)} ...")
    t0 = time.time()
    segments, info = model.transcribe(
        audio,
        language=args.language,
        vad_filter=args.vad,
        beam_size=5,
    )
    detected_lang = getattr(info, "language", "?")
    print(f"      检测语言: {detected_lang} (置信度 {getattr(info, 'language_probability', 0):.2f})")

    # 写输出
    print(f"[3/3] 写出 {out_path}")
    n_seg = 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# {base}\n")
        f.write(f"- 语言: {detected_lang} | 音频时长: {fmt_ts(info.duration)}\n")
        f.write(f"- 转写时间: {time.strftime('%Y-%m-%d %H:%M')} | 模型: {args.model}\n\n")
        for seg in segments:
            f.write(f"[{fmt_ts(seg.start)} -> {fmt_ts(seg.end)}] {seg.text.strip()}\n")
            n_seg += 1
            if n_seg % 20 == 0:
                print(f"      已转写 {n_seg} 段 ...")

    print(f"完成: {n_seg} 段，耗时 {time.time()-t0:.1f}s")
    print(f"输出文件: {out_path}")
    return 0


# 延迟导入，便于 --help 快速响应
from faster_whisper import WhisperModel  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
