#!/usr/bin/env python3
"""元動画から、編集作業用の軽い動画（プロキシ）と音声を作る。

    python video_edit/prepare.py <元動画> --name <プロジェクト名>

出力:
    work/<name>/proxy.mp4      … 540pの軽い動画（HDRは通常の色に変換済み・出力と同じfps）
    work/<name>/audio16k.wav   … 文字起こし用の音声（モノラル16kHz）
    video_edit/projects/<name>/source.json … 元動画の情報（完成版の書き出し時に照合する）

時間軸は元動画と同じ（プロキシで決めたカット位置がそのまま元動画に使える）。
"""

import argparse

from vedit_common import (PROJECTS_DIR, WORK_DIR, log, output_fps, probe, run_ffmpeg_with_progress, save_json,
                          setup_console, tonemap_filter, tc)


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="編集作業用のプロキシ動画と音声を作る")
    ap.add_argument("source")
    ap.add_argument("--name", required=True, help="プロジェクト名（英数字推奨）")
    ap.add_argument("--height", type=int, default=540, help="プロキシの高さpx（既定540）")
    args = ap.parse_args(argv)

    info = probe(args.source)
    ofps = output_fps(info)
    log(f"[元動画] {info['file']}: {info['width']}x{info['height']} {info['fps']:.3f}fps {tc(info['duration'])} "
        f"{info['vcodec']} {'HDR(' + info['color_transfer'] + ')' if info['hdr'] else 'SDR'} → 出力 {ofps}fps")
    work = WORK_DIR / args.name
    proj = PROJECTS_DIR / args.name
    work.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)

    tm = tonemap_filter(info)
    # 縮小してからHDR変換（変換処理を軽くするため）
    vf = f"fps={ofps},scale=-2:{args.height}:flags=bicubic" + ("," + tm if tm else "") + ",format=yuv420p"
    cmd = ["ffmpeg", "-y", "-i", str(args.source), "-map", "0:v:0", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-g", "60",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-af", "aresample=async=1:first_pts=0"]
    cmd += ["-movflags", "+faststart", str(work / "proxy.mp4")]
    run_ffmpeg_with_progress(cmd, info["duration"], "プロキシ作成")
    if info["has_audio"]:
        run_ffmpeg_with_progress(["ffmpeg", "-y", "-i", str(args.source), "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                                  "-af", "aresample=async=1:first_pts=0", str(work / "audio16k.wav")],
                                 info["duration"], "音声抽出")
    info["output_fps"] = ofps
    save_json(proj / "source.json", info)
    log(f"[完了] {work / 'proxy.mp4'}")


if __name__ == "__main__":
    main()
