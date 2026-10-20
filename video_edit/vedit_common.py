"""video_edit 共通の小道具（動画情報の取得・時間の扱い・HDR→SDR変換など）"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
PROJECTS_DIR = HERE / "projects"
WORK_DIR = HERE.parent / "work"


def log(msg: str) -> None:
    print(msg, flush=True)


def setup_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def tc(sec: float) -> str:
    """秒 → H:MM:SS.ss"""
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def load_json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save_json(p: Path, obj) -> None:
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_rate(s) -> Fraction:
    try:
        n, d = str(s).split("/")
        return Fraction(int(n), int(d)) if int(d) else Fraction(0)
    except (ValueError, ZeroDivisionError):
        try:
            return Fraction(str(s))
        except ValueError:
            return Fraction(0)


def probe(path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                        str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s.get("codec_type") == "video")
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    fps = parse_rate(v.get("avg_frame_rate")) or parse_rate(v.get("r_frame_rate"))
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"])))
    if (v.get("tags") or {}).get("rotate"):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:  # ffmpegは自動で回転させるので、表示上の縦横で扱う
        w, h = h, w
    trc = v.get("color_transfer")
    return {
        "file": Path(path).name,
        "width": w, "height": h, "rotation": rot,
        "fps": float(fps), "fps_str": f"{fps.numerator}/{fps.denominator}",
        "duration": float(d["format"].get("duration") or v.get("duration") or 0),
        "size_bytes": int(d["format"].get("size") or 0),
        "vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
        "color_transfer": trc, "color_primaries": v.get("color_primaries"), "color_space": v.get("color_space"),
        "hdr": trc in ("arib-std-b67", "smpte2084"),
        "has_audio": a is not None,
        "sample_rate": int(a.get("sample_rate", 48000)) if a else None,
        "channels": int(a.get("channels", 2)) if a else None,
    }


def output_fps(info: dict) -> str:
    """出力fps。60fps系の素材は半分（30fps系）にする。トーク動画はこれで十分滑らか"""
    fps = parse_rate(info["fps_str"])
    if fps > 31:
        fps = fps / 2
    return f"{fps.numerator}/{fps.denominator}"


def tonemap_filter(info: dict) -> str:
    """HDR（iPhoneのHLGなど）をYouTube向けの通常の色(SDR)に変換するフィルタ。SDRなら空文字"""
    if not info.get("hdr"):
        return ""
    tin = "arib-std-b67" if info["color_transfer"] == "arib-std-b67" else "smpte2084"
    return (f"zscale=tin={tin}:min=bt2020nc:pin=bt2020:rin=tv:t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def ffmpeg_major() -> int:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
        m = re.search(r"ffmpeg version n?(\d+)", out)
        return int(m.group(1)) if m else 6
    except OSError:
        raise SystemExit("ffmpeg が見つかりません。インストールしてPATHを通してください")


def filter_script_args(path: str) -> list[str]:
    """フィルタ定義をファイルから読ませる引数（ffmpeg 7以降は書式が変わった）"""
    return ["-/filter_complex", path] if ffmpeg_major() >= 7 else ["-filter_complex_script", path]


def run_ffmpeg_with_progress(cmd: list[str], total_sec: float, label: str, cwd=None) -> None:
    """ffmpeg を実行し、進み具合を%で表示する"""
    cmd = cmd[:1] + ["-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
    last = -10.0
    import threading
    err_lines = []
    t = threading.Thread(target=lambda: err_lines.extend(p.stderr), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=")[1])
            except ValueError:
                continue
            pct = 100 * us / 1e6 / max(total_sec, 1e-6)
            if pct - last >= 5:
                last = pct
                log(f"[{label}] {min(pct, 100):.0f}%")
    p.wait()
    t.join(timeout=5)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg が失敗しました:\n{''.join(err_lines[-30:])}")
