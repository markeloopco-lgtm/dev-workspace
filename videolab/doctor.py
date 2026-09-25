"""環境診断: 必要なソフトが揃っているかを一覧表示し、足りないものの入れ方を示す。"""

import importlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

OK, NG, OPT = "OK", "NG", "--"


def _ver(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except Exception:
        return None


def _http_ok(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as r:
            return r.status < 500
    except Exception:
        return False


def run() -> int:
    rows = []

    def add(status, name, detail, hint=""):
        rows.append((status, name, detail, hint))

    py = sys.version_info
    add(OK if py >= (3, 10) else NG, "Python", platform.python_version(),
        "" if py >= (3, 10) else "Python 3.12 を入れてください: winget install -e --id Python.Python.3.12")

    for mod, pip_name, required in [
        ("numpy", "numpy", True), ("cv2", "opencv-python", True), ("scipy", "scipy", True),
        ("yaml", "PyYAML", True), ("PIL", "Pillow", True), ("soundfile", "soundfile", True),
        ("pyloudnorm", "pyloudnorm", True),
        ("yt_dlp", '"yt-dlp[default,deno]"', False), ("faster_whisper", "faster-whisper", False),
        ("rapidocr", "rapidocr onnxruntime", False), ("scenedetect", "scenedetect", False),
    ]:
        try:
            m = importlib.import_module(mod)
            add(OK, mod, getattr(m, "__version__", "installed"))
        except Exception:
            add(NG if required else OPT, mod, "未インストール",
                f"pip install {pip_name}" + ("" if required else "  (任意)"))

    from . import ffmpeg_util as ff
    try:
        exe = ff.find_ffmpeg()
        add(OK, "ffmpeg", _ver([exe, "-version"]) or exe)
    except FileNotFoundError:
        add(NG, "ffmpeg", "見つからない", "winget install -e --id Gyan.FFmpeg → PowerShellを開き直す")
    fp = ff.find_ffprobe()
    add(OK if fp else OPT, "ffprobe", fp or "無し(ffmpegで代用)")

    ytdlp = shutil.which("yt-dlp")
    deno = shutil.which("deno")
    if not deno:   # pip install "yt-dlp[default,deno]" は仮想環境のScriptsフォルダに入れる
        cand = Path(sys.executable).parent / ("deno.exe" if os.name == "nt" else "deno")
        deno = str(cand) if cand.exists() else None
    add(OK if ytdlp or _has("yt_dlp") else OPT, "yt-dlp", ytdlp or ("python -m yt_dlp" if _has("yt_dlp") else "無し"),
        "" if ytdlp or _has("yt_dlp") else 'pip install -U "yt-dlp[default,deno]"  (参考動画の取得に必要)')
    add(OK if deno else OPT, "deno(yt-dlp用)", deno or "無し",
        "" if deno else 'pip install -U "yt-dlp[default,deno]" で一緒に入ります')

    try:
        from .produce.blender_runner import find_blender
        b = find_blender()
        add(OK if b else OPT, "Blender", b or "無し",
            "" if b else "3DCGを使うなら https://www.blender.org/download/ (5.2 LTS推奨)。無くても2D版で動作")
    except Exception as e:  # noqa: BLE001
        add(OPT, "Blender", f"確認できず({e.__class__.__name__})")

    for name, url, hint in [
        ("VOICEVOX", "http://127.0.0.1:50021/version", "VOICEVOXアプリを起動すると使えます"),
        ("AivisSpeech", "http://127.0.0.1:10101/version", "AivisSpeechを使う場合のみ"),
        ("Style-Bert-VITS2", "http://127.0.0.1:5000/docs", "SBV2を使う場合のみ(docs/04 Step3)"),
    ]:
        up = _http_ok(url)
        add(OK if up else OPT, name, "起動中" if up else "未起動", "" if up else hint)

    try:
        from .produce.telop import find_font
        add(OK, "日本語フォント", find_font())
    except Exception as e:  # noqa: BLE001
        add(NG, "日本語フォント", str(e))

    from .vlm import load_env
    load_env()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    add(OK if key else OPT, "Gemini APIキー", "設定済み" if key else "未設定",
        "" if key else "任意。 .env に GEMINI_API_KEY=... (AI Studioで無料発行)")

    cwd = str(Path.cwd())
    ascii_ok = all(ord(c) < 128 for c in cwd)
    add(OK if ascii_ok else OPT, "作業フォルダ名", cwd,
        "" if ascii_ok else "日本語を含むパスは一部ツールで不具合の元。C:\\work\\ など英数字のみを推奨")
    if os.name == "nt":
        utf8 = os.environ.get("PYTHONUTF8") == "1"
        add(OK if utf8 else OPT, "PYTHONUTF8", "1" if utf8 else "未設定",
            "" if utf8 else '[Environment]::SetEnvironmentVariable("PYTHONUTF8","1","User") で文字化け予防')
    free = shutil.disk_usage(Path.cwd()).free / 1e9
    add(OK if free > 20 else NG, "空き容量", f"{free:.0f} GB",
        "" if free > 20 else "動画・レンダリング用に20GB以上空けてください")

    width = max(len(r[1]) for r in rows) + 2
    print("videolab 環境診断\n")
    for st, name, detail, hint in rows:
        mark = {"OK": "[OK]", "NG": "[NG]", "--": "[--]"}[st]
        print(f"{mark} {name:<{width}} {detail}")
        if hint:
            print(f"      → {hint}")
    ng = sum(1 for r in rows if r[0] == NG)
    print(f"\n[NG]={ng}件（[--]は任意機能。無くても基本機能は動きます）")
    return 1 if ng else 0


def _has(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False
