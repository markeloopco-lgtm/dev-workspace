"""ffmpeg / ffprobe の検出と薄いラッパー。

ffmpegは PATH → 環境変数FFMPEG → imageio-ffmpeg同梱バイナリ の順に探す。
ffprobeが無い環境(imageio-ffmpegのみ)でも動くよう、probeは `ffmpeg -i` の
出力解析にフォールバックする。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


def pip_cmd(args: str) -> str:
    """今動いているPython(= .venv-video)に入れるための pip コマンド文字列。

    手順書は仮想環境を有効化せず .venv-video\\Scripts\\python.exe を直接使うので、
    素の `pip install` だと別のPythonに入ってしまう。
    """
    exe = sys.executable
    try:
        rel = os.path.relpath(exe)
        if not rel.startswith(".."):
            exe = rel
    except ValueError:          # Windowsで別ドライブ
        pass
    if " " in exe:
        exe = f'& "{exe}"' if os.name == "nt" else f'"{exe}"'
    return f"{exe} -m pip install {args}"


def _install_hint() -> str:
    return ("ffmpegが見つかりません。Windowsなら PowerShell で\n"
            "  winget install -e --id Gyan.FFmpeg\n"
            f"を実行してPowerShellを開き直すか、 {pip_cmd('imageio-ffmpeg')} を実行してください。")


def find_ffmpeg() -> str:
    for cand in (os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if cand and Path(cand).exists():
            return cand
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise FileNotFoundError(_install_hint())


def find_ffprobe():
    for cand in (os.environ.get("FFPROBE"), shutil.which("ffprobe")):
        if cand and Path(cand).exists():
            return cand
    try:
        sibling = Path(find_ffmpeg()).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.exists():
            return str(sibling)
    except FileNotFoundError:
        pass
    return None


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    n_frames: int          # duration*fpsからの推定値(デコード時に実数で上書き)
    has_audio: bool
    vcodec: str
    acodec: str

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_rate(rate: str) -> float:
    if not rate or rate in ("0/0", "N/A"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def probe(path) -> VideoInfo:
    path = str(path)
    if not Path(path).exists():
        raise FileNotFoundError(path)
    ffprobe = find_ffprobe()
    if ffprobe:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0 or not res.stdout.strip():
            raise ValueError(f"動画を読み込めません(壊れているか、ダウンロード途中の可能性): {path}\n"
                             f"{(res.stderr or '').strip()[-500:]}")
        data = json.loads(res.stdout)
        v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
        a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
        if v is None:
            raise ValueError(f"映像ストリームがありません: {path}")
        fps = _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate"))
        duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
        width, height = int(v["width"]), int(v["height"])
        # 縦動画などで回転メタデータがある場合は表示上の縦横に合わせる
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(float(sd["rotation"])))
        if rot % 180 == 90:
            width, height = height, width
        return VideoInfo(path, width, height, fps, duration, int(round(duration * fps)),
                         a is not None, v.get("codec_name", "?"),
                         a.get("codec_name", "") if a else "")

    # ffprobe無し: ffmpeg -i のstderrを解析
    err = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", path],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    vm = re.search(r"Stream #.*?Video:\s*(\w+).*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) (?:fps|tbr)", err)
    if not vm:
        raise ValueError(f"映像ストリームを解析できません: {path}")
    am = re.search(r"Stream #.*?Audio:\s*(\w+)", err)
    fps = float(vm[4])
    w, h = int(vm[2]), int(vm[3])
    rm = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", err) or \
        re.search(r"rotate\s*:\s*(-?\d+)", err)
    if rm and abs(int(round(float(rm[1])))) % 180 == 90:
        w, h = h, w       # 縦向きのスマホ動画など(ffmpegは自動で回転してから出力する)
    return VideoInfo(path, w, h, fps, duration, int(round(duration * fps)),
                     am is not None, vm[1], am[1] if am else "")


def even(x: float) -> int:
    """yuv420系で扱えるよう偶数に丸める。"""
    return max(2, int(round(x / 2.0)) * 2)


def iter_frames(path, width: int, height: int, start: float = None, duration: float = None,
                pix_fmt: str = "rgb24"):
    """動画を指定解像度のRGB(またはgray)フレームとして1枚ずつ返す。

    タイムスタンプはCFR前提(YouTube配信動画はCFR)で idx/fps として扱う。
    """
    channels = {"rgb24": 3, "gray": 1}[pix_fmt]
    cmd = [find_ffmpeg(), "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-an", "-sn", "-vf", f"scale={width}:{height}:flags=area",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
    frame_bytes = width * height * channels
    # stderrはパイプにせず一時ファイルへ(壊れた動画でエラーが大量に出てもffmpegが止まらない)
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, bufsize=frame_bytes * 4)
    finished = False
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            arr = np.frombuffer(buf, dtype=np.uint8)
            yield arr.reshape(height, width, channels) if channels > 1 else arr.reshape(height, width)
        finished = True
    finally:
        if not finished:  # 呼び出し側が途中で読むのをやめた
            proc.kill()
        proc.stdout.close()
        rc = proc.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
        errf.close()
    if rc != 0:
        raise RuntimeError(f"ffmpegのデコードに失敗しました ({path}):\n{err[-1500:]}")


def grab_frame(path, t: float, width: int, height: int) -> np.ndarray:
    """時刻tの1フレームをRGBで取得する(-ss をinput前に置く高速かつ正確なシーク)。"""
    for frame in iter_frames(path, width, height, start=max(0.0, t), duration=None):
        return frame.copy()
    raise ValueError(f"{t:.2f}秒のフレームを取得できません: {path}")


def read_audio(path, sr: int = 16000, channels: int = 1) -> np.ndarray:
    """音声をfloat32で読み込む。shape: (n,) または (n, channels)。音声無しなら空配列。"""
    cmd = [find_ffmpeg(), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-sn",
           "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    res = subprocess.run(cmd, capture_output=True)
    data = np.frombuffer(res.stdout, dtype="<f4").astype(np.float32)
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels)
    return data


def ebur128(path) -> dict:
    """ffmpegのebur128フィルタでラウドネスを測る(EBU R128 / ITU-R BS.1770準拠)。

    返り値: integrated(LUFS), lra(LU), true_peak(dBFS), momentary/short_term(0.1秒刻み)
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-nostats", "-nostdin", "-v", "verbose",
           "-i", str(path), "-vn", "-sn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stderr
    line_re = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf|nan)\s+S:\s*(-?[\d.]+|-inf|nan)")
    t, m, s = [], [], []
    for mt in line_re.finditer(err):
        t.append(float(mt[1]))
        m.append(float(mt[2]) if mt[2] not in ("-inf", "nan") else -120.0)
        s.append(float(mt[3]) if mt[3] not in ("-inf", "nan") else -120.0)
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""

    def grab(pattern, default=None):
        mm = re.search(pattern, summary)
        return float(mm[1]) if mm else default

    return {
        "integrated": grab(r"I:\s*(-?[\d.]+) LUFS"),
        "lra": grab(r"LRA:\s*(-?[\d.]+) LU"),
        "true_peak": grab(r"Peak:\s*(-?[\d.]+|-inf) dBFS"),
        "t": t, "momentary": m, "short_term": s,
    }


def run(cmd, **kw):
    """ffmpegを実行し、失敗時はstderr末尾を含めて例外にする。"""
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", **kw)
    if res.returncode != 0:
        raise RuntimeError(f"コマンド失敗 ({res.returncode}): {' '.join(map(str, cmd[:6]))} ...\n"
                           f"{res.stderr[-2000:]}")
    return res


def imwrite(path, img_bgr, params=None) -> bool:
    """cv2.imwrite の代わり。Windowsの日本語パスでも書ける(imencode + tofile)。"""
    import cv2

    ok, buf = cv2.imencode(Path(path).suffix or ".png", img_bgr, params or [])
    if ok:
        buf.tofile(str(path))
    return bool(ok)


def imread(path, flags=None):
    """cv2.imread の代わり。Windowsの日本語パスでも読める。読めなければNone。"""
    import cv2

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)
