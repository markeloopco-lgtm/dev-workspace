#!/usr/bin/env python3
"""動画解析用のI/Oヘルパー (ffmpeg / yt-dlp)。

ffmpegは imageio-ffmpeg 同梱のバイナリを優先して使う(Windowsでも別途インストール不要)。
ffprobeは同梱されないため、メタデータは `ffmpeg -i` の出力とOpenCVから読む。

フレーム番号は常に「fpsフィルタでCFR化した後の通し番号」で扱う。
全フレーム走査(iter_frames)と指定フレーム抽出(grab_frames)で同じフィルタ列を使うので、
可変フレームレートの動画でも両者の番号が一致する。
"""

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np


def imwrite_jpg(path, bgr, quality=88):
    """cv2.imwriteはWindowsで日本語などを含むパスに書けないため、エンコードしてから書き込む"""
    ok, buf = cv2_mod().imencode(".jpg", bgr, [cv2_mod().IMWRITE_JPEG_QUALITY, quality])
    if ok:
        Path(path).write_bytes(buf.tobytes())
    return ok


def imread(path):
    """cv2.imreadの日本語パス対応版(読めなければNone)"""
    try:
        data = np.fromfile(str(path), np.uint8)
    except OSError:
        return None
    return cv2_mod().imdecode(data, cv2_mod().IMREAD_COLOR) if data.size else None


def cv2_mod():
    import cv2
    return cv2


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise RuntimeError("ffmpegが見つかりません。`pip install imageio-ffmpeg` を実行してください")


def _ffmpeg_major() -> int:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-version"],
                         capture_output=True, text=True, errors="replace").stdout
    m = re.search(r"ffmpeg version n?(\d+)", out)
    return int(m.group(1)) if m else 0


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: Fraction       # 解析に使うフレームレート(CFR)
    src_fps: float      # 元動画の表示上のフレームレート
    duration: float     # 秒
    n_frames: int       # fps×durationからの推定値(実数は走査後に確定)
    vcodec: str
    has_audio: bool
    audio_rate: int = 0

    def size_for(self, long_side: int) -> tuple:
        """長辺をlong_sideに縮小したときの(w, h)。偶数に丸める。元より大きくはしない"""
        scale = min(1.0, long_side / max(self.width, self.height))
        w = max(2, int(round(self.width * scale / 2)) * 2)
        h = max(2, int(round(self.height * scale / 2)) * 2)
        return w, h


def _exact_fps(fps: float) -> Fraction:
    """29.97などのNTSC系は正確な有理数(30000/1001)に直す"""
    for base in (24, 25, 30, 48, 50, 60, 120):
        if abs(fps - base * 1000 / 1001) < 0.005:
            return Fraction(base * 1000, 1001)
    return Fraction(fps).limit_denominator(1000)


def probe(path, max_fps: float = 60.0) -> VideoInfo:
    res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = res.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError(f"動画として読めません: {path}\n{err[-800:]}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    vline = next((ln for ln in err.splitlines() if "Stream #" in ln and "Video:" in ln), None)
    if vline is None:
        raise RuntimeError(f"映像ストリームがありません: {path}")
    width, height = map(int, re.search(r"\b(\d{2,5})x(\d{2,5})\b", vline).groups())
    vcodec = re.search(r"Video:\s*([\w-]+)", vline).group(1)
    fm = re.search(r"([\d.]+)\s*fps", vline) or re.search(r"([\d.]+)\s*tbr", vline)
    src_fps = float(fm.group(1)) if fm else 30.0
    try:  # OpenCVのほうが桁が正確(29.97002997...)
        import cv2
        cap = cv2.VideoCapture(str(path))
        cv_fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if cv_fps and 1 < cv_fps < 1000 and abs(cv_fps - src_fps) < 0.1:
            src_fps = cv_fps
    except Exception:
        pass

    aline = next((ln for ln in err.splitlines() if "Stream #" in ln and "Audio:" in ln), None)
    arate = int(re.search(r"(\d+)\s*Hz", aline).group(1)) if aline and re.search(r"(\d+)\s*Hz", aline) else 0

    fps = _exact_fps(min(src_fps, max_fps))
    return VideoInfo(path=str(path), width=width, height=height, fps=fps, src_fps=src_fps,
                     duration=duration, n_frames=int(round(duration * float(fps))),
                     vcodec=vcodec, has_audio=aline is not None, audio_rate=arate)


def _video_cmd(info: VideoInfo, size: tuple, filter_file: Path = None) -> list:
    w, h = size
    cmd = [ffmpeg_exe(), "-hide_banner", "-v", "error", "-nostdin", "-i", info.path, "-an", "-sn"]
    if filter_file is None:
        cmd += ["-vf", f"fps={info.fps.numerator}/{info.fps.denominator},scale={w}:{h}:flags=area"]
    elif _ffmpeg_major() >= 7:
        cmd += ["-/filter:v", str(filter_file)]
    else:
        cmd += ["-filter_script:v", str(filter_file)]
    return cmd + ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def _read_frames(cmd: list, size: tuple):
    w, h = size
    nbytes = w * h * 3
    # stderrはファイルに逃がす(PIPEだと壊れた動画で大量のエラーが出た時に詰まる)
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, bufsize=nbytes * 4)
        finished = False
        try:
            while True:
                buf = proc.stdout.read(nbytes)
                if len(buf) < nbytes:
                    finished = True
                    break
                yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        # 呼び出し側が途中で読むのをやめた場合(ffmpegはBroken pipeで終わる)はエラー扱いしない
        if finished and rc != 0:
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace")
            raise RuntimeError(f"ffmpegが失敗しました(rc={rc}):\n{err[-800:]}")


def iter_frames(info: VideoInfo, size: tuple):
    """全フレームをRGB uint8 (h, w, 3) で順に返す"""
    yield from _read_frames(_video_cmd(info, size), size)


def grab_frames(info: VideoInfo, indices, size: tuple):
    """指定番号のフレームだけを (番号, RGB画像) で昇順に返す。デコードは1回の順次読みで済む"""
    idx = sorted(set(int(i) for i in indices if 0 <= i))
    if not idx:
        return
    # 連続区間をbetween()にまとめて式を短くする
    runs, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i != prev + 1:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    expr = "+".join(f"eq(n\\,{a})" if a == b else f"between(n\\,{a}\\,{b})" for a, b in runs)
    w, h = size
    graph = (f"fps={info.fps.numerator}/{info.fps.denominator},"
             f"select='{expr}',scale={w}:{h}:flags=area")
    with tempfile.TemporaryDirectory() as td:
        ff = Path(td) / "select.txt"
        ff.write_text(graph, encoding="utf-8")
        for n, frame in zip(idx, _read_frames(_video_cmd(info, size, ff), size)):
            yield n, frame


def load_audio(info: VideoInfo, sr: int = 16000):
    """モノラルfloat32で音声を返す。音声なしならNone"""
    if not info.has_audio:
        return None
    res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-v", "error", "-nostdin", "-i", info.path,
                          "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-800:])
    return np.frombuffer(res.stdout, np.float32).copy()


_EBU_FRAME = re.compile(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)\s+S:\s*(-?[\d.]+|-inf)")


def loudness(info: VideoInfo) -> dict:
    """EBU R128 (YouTubeの音量基準と同じ尺度) の統合ラウドネス・LRA・ピークと100ms毎の推移"""
    if not info.has_audio:
        return {}
    res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", info.path, "-vn",
                          "-af", "ebur128=peak=true:framelog=info", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = res.stderr
    t, mom, short = [], [], []
    for m in _EBU_FRAME.finditer(err):
        t.append(float(m.group(1)))
        mom.append(float(m.group(2)) if m.group(2) != "-inf" else -120.0)
        short.append(float(m.group(3)) if m.group(3) != "-inf" else -120.0)
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""

    def pick(pattern):
        mm = re.search(pattern, summary)
        return float(mm.group(1)) if mm else None

    return {
        "integrated_lufs": pick(r"I:\s*(-?[\d.]+)\s*LUFS"),
        "lra_lu": pick(r"LRA:\s*(-?[\d.]+)\s*LU"),
        "true_peak_dbfs": pick(r"Peak:\s*(-?[\d.]+)\s*dBFS"),
        "t": t, "momentary": mom, "short_term": short,
    }


def _js_runtimes() -> dict:
    """yt-dlpのYouTube対応に必要なJavaScriptランタイム(deno/node/bun)を探す"""
    found = {}
    venv_bin = Path(sys.executable).parent
    for name in ("deno", "node", "bun"):
        exe = shutil.which(name)
        if not exe:
            cand = venv_bin / (name + (".exe" if sys.platform == "win32" else ""))
            exe = str(cand) if cand.exists() else None
        if exe:
            found[name] = {"path": exe}
    return found


def download(url: str, out_dir: Path, max_height: int = 1080, cookies_from_browser: str = None):
    """yt-dlpで動画を落とす。戻り値は (動画パス, メタデータdict)"""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    h = max_height
    opts = {
        # 互換性の高いH.264+AACを優先し、無ければ何でも取る
        "format": (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba"
                   f"/b[height<={h}]/bv*+ba/b"),
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(out_dir / "%(id)s.%(ext)s")},
        "ffmpeg_location": ffmpeg_exe(),
        "noplaylist": True,
        "retries": 3,
    }
    runtimes = _js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    else:
        print("[warn] JavaScriptランタイム(deno/node)が見つかりません。"
              "YouTubeの取得に失敗したら `pip install deno` を実行してください", file=sys.stderr)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        reqs = info.get("requested_downloads") or []
        path = Path(reqs[0]["filepath"]) if reqs and reqs[0].get("filepath") else Path(ydl.prepare_filename(info))
    meta = {k: info.get(k) for k in ("id", "title", "channel", "uploader", "upload_date", "duration",
                                     "width", "height", "fps", "vcodec", "acodec", "webpage_url",
                                     "chapters", "tags")}
    return path, meta
