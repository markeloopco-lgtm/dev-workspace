#!/usr/bin/env python3
"""edit.json（カット位置）と telops.ass（テロップ）から動画を書き出す。

確認用（軽い動画。プロキシから作る）:
    python video_edit/render.py --name <名前> --preview
完成版（PCで元動画から。NVIDIAのGPUがあれば自動で使う）:
    python video_edit/render.py --name <名前> --source "D:\\動画\\元動画.MP4"

仕組み:
- 映像: 元動画から残すフレームだけを選び（select）、寄り/引きを切り替え、テロップを焼き込む
- 音声: 残す区間をつなぎ、つなぎ目は数ミリ秒のフェードで「プツッ」を防ぎ、音量をYouTube標準(-14LUFS)に揃える
- 映像と音声は同じフレーム境界で切るので、ずれない
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from vedit_common import (FONTS_DIR, PROJECTS_DIR, WORK_DIR, filter_script_args, load_json, log, parse_rate, probe,
                          run_ffmpeg_with_progress, setup_console, tc, tonemap_filter)

SR = 48000
FADE_MS = 4


def build_audio(source: Path, edit: dict, out_wav: Path, fps: float):
    """残す区間の音声をつないだ wav を作る"""
    raw = out_wav.with_suffix(".raw")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0", "-ac", "2",
                    "-ar", str(SR), "-af", "aresample=async=1:first_pts=0", "-f", "s16le", str(raw)], check=True)
    src = np.memmap(raw, dtype="<i2", mode="r")
    src = src[: len(src) // 2 * 2].reshape(-1, 2)
    total = int(round(edit["out_frames"] * SR / fps))
    out = np.zeros((total, 2), np.int16)
    fade = int(SR * FADE_MS / 1000)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
    prev_f1 = None
    for sg in edit["segments"]:
        o_a = int(round(sg["o0"] * SR / fps))
        o_b = int(round((sg["o0"] + sg["f1"] - sg["f0"]) * SR / fps))
        s_a = int(round(sg["f0"] * SR / fps))
        n = o_b - o_a
        chunk = np.zeros((n, 2), np.float32)
        avail = src[s_a:s_a + n]
        chunk[:len(avail)] = avail
        if prev_f1 is None or prev_f1 != sg["f0"]:  # 本当のつなぎ目だけフェード
            chunk[:fade] *= ramp[:min(fade, n)]
            if prev_f1 is not None and o_a >= fade:
                out[o_a - fade:o_a] = (out[o_a - fade:o_a].astype(np.float32) * ramp[::-1]).astype(np.int16)
        out[o_a:o_b] = np.clip(chunk, -32768, 32767).astype(np.int16)
        prev_f1 = sg["f1"]
    del src
    raw.unlink(missing_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(out.tobytes())
    return total / SR


def measure_loudness(wav: Path, target: float):
    """2回方式の音量調整の1回目（測るだけ）"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(wav), "-af",
                        f"highpass=f=70,loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    return json.loads(m.group(0)) if m else None


def nvenc_ok() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def even(x):
    return int(round(x / 2)) * 2


def video_graph(edit, info, W, H, fps_str):
    fps = float(parse_rate(fps_str))
    eps = 0.5 / fps
    segs = edit["segments"]
    sel = "+".join(f"between(t,{(s['f0']) / fps - eps:.5f},{s['f1'] / fps - eps:.5f})" for s in segs)
    zf = max(1.0, float(edit["zoom"]["factor"]))
    zoomed = [s for s in segs if s["zoom"] > 1.001]
    IW, IH = even(W * zf), even(H * zf)
    tm = tonemap_filter(info)
    # 元の縦横比が違っても画面いっぱいに（はみ出しは中央で切る）
    chain = (f"[0:v]fps={fps_str},select='{sel}',setpts=N/({fps_str})/TB,"
             f"scale={IW}:{IH}:flags=lanczos:force_original_aspect_ratio=increase,crop={IW}:{IH}"
             + ("," + tm if tm else "") + ",setsar=1")
    if zoomed:
        cx = min(max(edit["zoom"]["cx"], 0.0), 1.0)
        cy = min(max(edit["zoom"]["cy"], 0.0), 1.0)
        en = "+".join(f"between(t,{s['o0'] / fps - eps:.5f},{(s['o0'] + s['f1'] - s['f0']) / fps - eps:.5f})"
                      for s in zoomed)
        g = (chain + ",split=2[b][z];"
             f"[b]scale={W}:{H}:flags=lanczos[b2];"
             f"[z]crop={W}:{H}:{int((IW - W) * cx)}:{int((IH - H) * cy)}[z2];"
             f"[b2][z2]overlay=0:0:enable='{en}'[v1];")
    else:
        g = chain + f",scale={W}:{H}:flags=lanczos[v1];"
    g += "[v1]ass=telops.ass:fontsdir=fonts,format=yuv420p[vout]"
    return g


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="編集データから動画を書き出す")
    ap.add_argument("--name", required=True)
    ap.add_argument("--source", help="元動画（完成版）。--preview のときは省略でプロキシを使う")
    ap.add_argument("--preview", action="store_true", help="確認用の軽い動画（960x540）を作る")
    ap.add_argument("-o", "--out", help="出力ファイル")
    ap.add_argument("--cpu", action="store_true", help="GPUを使わない")
    args = ap.parse_args(argv)

    proj = PROJECTS_DIR / args.name
    edit = load_json(proj / "edit.json")
    if args.preview:
        source = Path(args.source) if args.source else WORK_DIR / args.name / "proxy.mp4"
        W, H = 960, 540
        out = Path(args.out) if args.out else WORK_DIR / args.name / "preview.mp4"
    else:
        if not args.source:
            raise SystemExit("--source に元動画を指定してください")
        source = Path(args.source)
        W, H = edit["output"]["width"], edit["output"]["height"]
        out = Path(args.out) if args.out else source.with_name(f"{source.stem}_編集済み.mp4")
    info = probe(source)
    exp = edit["source"]["duration"]
    if abs(info["duration"] - exp) > 2.0:
        raise SystemExit(f"動画の長さが編集データと違います（{tc(info['duration'])} / 想定 {tc(exp)}）。別のファイルでは？")
    fps_str = edit["output"]["fps"]
    fps = float(parse_rate(fps_str))
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vedit_") as td:
        td = Path(td)
        shutil.copy(proj / "telops.ass", td / "telops.ass")
        shutil.copytree(FONTS_DIR, td / "fonts")
        log("[音声] 残す区間をつないでいます…")
        wav = td / "audio.wav"
        build_audio(source, edit, wav, fps)
        target = float(edit.get("audio", {}).get("loudness", -14))
        meas = measure_loudness(wav, target)
        if meas:
            af = (f"highpass=f=70,loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={meas['input_i']}:"
                  f"measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}:"
                  f"measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true,aresample={SR}")
        else:
            af = f"highpass=f=70,loudnorm=I={target}:TP=-1.5:LRA=11,aresample={SR}"
        (td / "graph.txt").write_text(video_graph(edit, info, W, H, fps_str) + f";[1:a]{af}[aout]", encoding="utf-8")

        use_gpu = (not args.preview) and (not args.cpu) and nvenc_ok()
        if args.preview:
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-b:a", "96k"]
        elif use_gpu:
            venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "12M",
                    "-maxrate", "20M", "-bufsize", "40M", "-profile:v", "high", "-b:a", "192k"]
        else:
            venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-b:a", "192k"]
        dec = ["-hwaccel", "auto"] if use_gpu else []
        cmd = (["ffmpeg", "-y"] + dec + ["-i", str(source.resolve()), "-i", "audio.wav"]
               + filter_script_args("graph.txt")
               + ["-map", "[vout]", "-map", "[aout]"] + venc
               + ["-c:a", "aac", "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
                  "-colorspace", "bt709", "-movflags", "+faststart", str(out.resolve())])
        log(f"[書き出し] {'確認用' if args.preview else '完成版'} {W}x{H} {'GPU' if use_gpu else 'CPU'} → {out}")
        try:
            run_ffmpeg_with_progress(cmd, edit["out_sec"], "書き出し", cwd=td)
        except RuntimeError:
            if not dec:
                raise
            log("[書き出し] GPUでの読み込みに失敗したのでCPUでやり直します")
            run_ffmpeg_with_progress([c for c in cmd if c not in dec], edit["out_sec"], "書き出し", cwd=td)
    log(f"[完了] {out}（{tc(edit['out_sec'])}）")
    return out


if __name__ == "__main__":
    main()
