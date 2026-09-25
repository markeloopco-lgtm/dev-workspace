"""参考動画の取得(yt-dlp)。**YouTubeの利用規約はダウンロードを禁止している**ので、使うかは本人の判断。

  - 著作権の面: 数値の統計を取るための複製は著作権法30条の4(情報解析)の範囲と考えられる
  - 規約の面  : YouTube利用規約(2023-12-15版)は許可の無いダウンロードと自動取得を禁止。
                違反はYouTubeとの契約違反で、アカウント停止のリスクがある(法的助言ではない)
  - 代替手段  : `vlab watch <URL>` はダウンロードせずURLのままGeminiに分析させる(推奨の第一歩)

取得する場合も、解析に必要な最小限(720p以下・少数本)に留め、分析後は `vlab purge` で消す。
メインのGoogleアカウントのCookieは絶対に使わない。
"""

import shutil
import subprocess
import sys
from pathlib import Path

NOTICE = """\
【重要】YouTubeの利用規約は、許可の無い動画のダウンロードを禁止しています。
  ・実行すると規約違反(契約違反)になり、アカウント停止などのリスクがあります
  ・著作権の面では、統計を取るための複製は著作権法30条の4の範囲と考えられますが、
    法的助言ではありません。再配布・共有・制作素材への流用は絶対にしないでください
  ・ダウンロードせずに済ませたい場合: python scripts/vlab.py watch <URL>
  ・解析には720pで十分なので、それ以上は取得しません。解析後は vlab purge で削除してください"""

# 解析しやすいH.264/720p以下/m4a音声を優先(AV1はOpenCV等で読めないことがある。
# 解析は横640pxで行うので720pを超える解像度は不要 = 必要最小限の複製に留める)
FORMAT_SORT = "vcodec:h264,res:720,acodec:m4a"
FORMAT = "bv*[height<=720]+ba/b[height<=720]"


def _ytdlp_cmd():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        raise FileNotFoundError(
            "yt-dlp が見つかりません。次のどちらかを実行してください:\n"
            '  pip install -U "yt-dlp[default,deno]"\n'
            "  winget install -e --id yt-dlp.yt-dlp")


def confirm(assume_yes: bool = False) -> bool:
    print(NOTICE, flush=True)
    if assume_yes:
        return True
    try:
        ans = input("\n上記を理解した上で、自己責任でダウンロードしますか? [y/N]: ").strip().lower()
    except EOFError:
        ans = ""
    return ans in ("y", "yes")


def fetch(url: str, out_dir: Path = Path("refs"), subs: bool = True, cookies_from: str = None,
          assume_yes: bool = False) -> Path:
    if not confirm(assume_yes):
        raise RuntimeError("ダウンロードを中止しました(vlab watch <URL> ならダウンロード不要です)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _ytdlp_cmd() + [
        "-f", FORMAT, "-S", FORMAT_SORT, "--merge-output-format", "mp4",
        "--sleep-requests", "1", "--min-sleep-interval", "5", "--max-sleep-interval", "10",
        "--write-info-json", "--no-playlist",
        "-o", str(out_dir / "%(id)s.%(ext)s"),     # 日本語ファイル名を避けてIDで保存
        "--print", "after_move:filepath",
    ]
    if subs:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "ja,ja-orig", "--sub-format",
                "vtt"]
    if not shutil.which("ffmpeg"):
        # PATHにffmpegが無くても imageio-ffmpeg 同梱のものを使って映像と音声を結合させる
        from .ffmpeg_util import find_ffmpeg
        try:
            cmd += ["--ffmpeg-location", find_ffmpeg()]
        except FileNotFoundError:
            pass
    if cookies_from:
        cmd += ["--cookies-from-browser", cookies_from]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError("yt-dlp が失敗しました:\n" + res.stderr[-2000:] +
                           "\n  よくある原因: yt-dlpが古い(pip install -U \"yt-dlp[default,deno]\")、"
                           "JavaScriptランタイム(deno)が無い、一時的なアクセス制限")
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    path = Path(lines[-1]) if lines else None
    if not path or not path.exists():
        cands = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise RuntimeError("ダウンロードしたファイルが見つかりません")
        path = cands[-1]
    return path
