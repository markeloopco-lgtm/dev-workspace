#!/usr/bin/env python3
"""動画1本を、最初から最後まで自動で編集する。

    python video_edit/auto_edit.py "D:\\動画\\元動画.mp4"
    python video_edit/auto_edit.py "D:\\動画\\元動画.mp4" --title "タイトル1行目／2行目"

Windows ならリポジトリ直下の「自動編集.bat」に動画をドラッグ＆ドロップするだけでよい。

やること（終わっている工程は飛ばすので、途中で止まっても同じコマンドで続きから再開できる）:
  0. 初回だけ: 作業用のPython環境(.venv)と必要なライブラリを用意、ffmpeg が無ければ入れる
  1. prepare      … 作業用の軽い動画と音声を作る
  2. transcribe   … 文字起こし（Whisper。初回はモデル約1.6GBを自動ダウンロード）
  3. 編集プラン   … 言いよどみ・言い直しを自動でカット済みにした edit_plan.md を作る
  4. build_edit   … カット位置とテロップを計算
  5. 演出         … キラキラ・白発光の素材を作り、カットの頭に配置
  6. render       … 元動画の画質で書き出し（NVIDIAのGPUがあれば自動で使う）

出来上がりは元動画と同じフォルダの「<元の名前>_編集済み.mp4」。
仕上がりを直したいときは video_edit/projects/<名前>/edit_plan.md を直して、同じコマンドをもう一度。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV = ROOT / ".venv"
NEEDS = ["numpy", "faster-whisper"]          # pip のパッケージ名
NEEDS_IMPORT = ["numpy", "faster_whisper"]   # import 名

RESTART_RE = re.compile(r"^(\s*[-*]\s*\d{1,4}\s*(?:[（(][^）)]*[）)])?\s*)(.*?)"
                        r"(\s*[［\[]\s*メモ\s*[：:][^］\]]*言い直し候補[^］\]]*[］\]])\s*$")


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 0. 環境の用意

def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def bootstrap(argv: list[str]) -> int | None:
    """.venv を用意して、その Python で自分自身を実行し直す。既に .venv の中なら None"""
    if in_venv():
        return None
    py = venv_python()
    if not py.exists():
        say("[準備] 作業用のPython環境(.venv)を作っています（初回だけ）…")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    probe = subprocess.run([str(py), "-c", "import " + ", ".join(NEEDS_IMPORT)],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        say("[準備] 必要なライブラリを入れています（初回だけ。数分かかります）…")
        subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", *NEEDS], check=True)
    return subprocess.run([str(py), str(Path(__file__).resolve()), *argv]).returncode


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if os.name == "nt" and shutil.which("winget"):
        say("[準備] ffmpeg が無いので入れています（初回だけ）…")
        subprocess.run(["winget", "install", "-e", "--id", "Gyan.FFmpeg",
                        "--accept-source-agreements", "--accept-package-agreements"])
        # winget は PATH を次回から通すので、今回の実行のために置き場所を足す
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
        os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            return
        raise SystemExit("ffmpeg を入れました。このウィンドウを閉じて、もう一度実行してください。")
    raise SystemExit("ffmpeg が見つかりません。入れてから、もう一度実行してください。\n"
                     "  Windows: winget install Gyan.FFmpeg / Mac: brew install ffmpeg")


def pick_file() -> str | None:
    """引数なしで起動されたら、ファイル選択の画面を出す"""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title="編集する動画を選んでください",
                                      filetypes=[("動画", "*.mp4 *.mov *.m4v *.mkv *.MP4 *.MOV *.MTS *.mts"),
                                                 ("すべて", "*.*")])
    root.destroy()
    return path or None


# ---------------------------------------------------------------- 3. 編集プランの自動手直し

def project_name(src: Path) -> str:
    """プロジェクト名は英数字にする（日本語のファイル名でも安全に扱うため）"""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", src.stem).strip("_")[:40]
    if len(slug) < 3:
        slug = "video_" + hashlib.md5(src.name.encode("utf-8")).hexdigest()[:8]
    return slug


def auto_fix_plan(path: Path, title: str = "") -> dict:
    """人が手で直す部分のうち、機械で決めてよいものを済ませる。

    - 「言い直し候補」の文（次の文とほぼ同じ＝言い直す前の失敗）を丸ごとカット
    - タイトルがあれば、画面上部にずっと出す見出しとして先頭に入れる
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out, restarts, section, title_done = [], 0, None, not title
    for ln in lines:
        m_sec = re.match(r"^##\s+(.+?)\s*$", ln)
        if m_sec and not ln.startswith("###"):
            section = m_sec.group(1)
        if section == "設定" and title and ln.lstrip("-* ").startswith("見出しの登場演出"):
            ln = re.sub(r"[:：]\s*[^（(]*", "： なし", ln, count=1)   # 出しっぱなしのタイトルなので登場演出は不要
        if section == "本編":
            m = RESTART_RE.match(ln)
            if m:
                struck = "~~" + m.group(2).replace("~~", "").strip() + "~~"
                if m.group(2).strip() != struck:      # 既に丸ごとカット済みなら触らない
                    ln = f"{m.group(1)}{struck}{m.group(3)}"
                    restarts += 1
            if not title_done and re.match(r"^\s*[-*]\s*\d", ln):
                out.append(f"### {title}")
                title_done = True
        out.append(ln)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"restarts": restarts, "title": bool(title)}


# ---------------------------------------------------------------- 本体

def run_pipeline(args) -> Path:
    sys.path.insert(0, str(HERE))
    import build_edit
    import make_assets
    import make_fx_plan
    import prepare
    import render
    import vedit_common
    from vedit_common import load_json, probe, tc

    src = Path(args.video).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"動画が見つかりません: {src}")
    name = args.name or project_name(src)
    proj = build_edit.PROJECTS_DIR / name
    work = build_edit.WORK_DIR / name
    t_start = time.time()
    say(f"\n=== 自動編集: {src.name}（プロジェクト名 {name}）===\n")

    # 同じ名前で別の動画を作業していたら止める（取り違え防止）
    sj = proj / "source.json"
    if sj.exists() and not args.redo:
        old = load_json(sj)
        now = probe(src)
        if old.get("file") != now["file"] or abs(float(old.get("duration", 0)) - now["duration"]) > 1.0:
            raise SystemExit(f"プロジェクト「{name}」は別の動画（{old.get('file')}、長さ {tc(float(old.get('duration', 0)))}）"
                             f"で使われています。今回の動画は長さ {tc(now['duration'])} です。\n"
                             f"  別の動画なら --name で別の名前を、同じ動画をやり直すなら --redo を付けてください。")

    # 1. 作業用の動画と音声
    if args.redo or not (sj.exists() and (work / "proxy.mp4").exists()):
        say("[1/6] 作業用の軽い動画と音声を作っています…")
        prepare.main([str(src), "--name", name])
    else:
        say("[1/6] 作業用の動画: 作成済みなので飛ばします")
    if not (work / "audio16k.wav").exists():
        raise SystemExit("この動画には音声がありません。しゃべりの自動カットには音声が必要です。")

    # 2. 文字起こし
    tr = proj / "transcript.json"
    if args.redo or not tr.exists():
        say(f"[2/6] 文字起こし中…（{tc(load_json(sj)['duration'])} の動画。CPUで動画の長さと同じくらいかかります）")
        import transcribe
        transcribe.main(["--name", name, "--model", args.model]
                        + (["--prompt", args.prompt] if args.prompt else []))
    else:
        say("[2/6] 文字起こし: 済んでいるので飛ばします")

    # 3. 編集プラン（既にあれば、人が直した可能性があるので上書きしない）
    plan = proj / "edit_plan.md"
    if args.redo or args.redo_plan or not plan.exists():
        say("[3/6] 編集プランを作っています（言いよどみ・言い直しは自動でカット）…")
        trj = load_json(tr)
        sents = build_edit.load_sentences(trj)
        build_edit.write_plan(plan, name, load_json(sj), sents, trj)
        title = args.title
        if title is None and sys.stdin and sys.stdin.isatty() and not args.yes:
            try:
                title = input("  画面上部にずっと出すタイトル（「／」で2行に。空のままEnterで無し）: ").strip()
            except EOFError:
                title = ""
        fixed = auto_fix_plan(plan, title or "")
        say(f"  {len(sents)}文 / 言い直しを {fixed['restarts']}か所カット"
            + (" / タイトルあり" if fixed["title"] else ""))
    else:
        say(f"[3/6] 編集プラン: 既にあるのでそれを使います（{plan}）")

    # 4. カット位置とテロップ
    say("[4/6] カット位置とテロップを計算しています…")
    edit = build_edit.build(name)

    # 5. 演出
    if args.no_fx:
        say("[5/6] 演出: 乗せない設定なので飛ばします")
    else:
        assets = proj / "assets"
        if not ((assets / "sparkle_pink.mov").exists() and (assets / "glow_white.mov").exists()):
            say("[5/6] キラキラ・白発光の素材を作っています…")
            make_assets.main(["-o", str(assets), "--fps", f"{float(vedit_common.parse_rate(edit['output']['fps'])):.3f}"])
        else:
            say("[5/6] 演出の素材: 作成済み")
        fx_args = ["--name", name]
        if args.from_analysis:
            fx_args += ["--from-analysis", str(args.from_analysis)]
        make_fx_plan.main(fx_args)

    # 6. 書き出し
    rargs = ["--name", name, "--source", str(src)]
    if args.preview:
        rargs.append("--preview")
    if args.out:
        rargs += ["-o", str(args.out)]
    if args.no_fx:
        rargs.append("--no-fx")
    if args.cpu:
        rargs.append("--cpu")
    say(f"[6/6] {'確認用の動画' if args.preview else '完成版'}を書き出しています…")
    out = render.main(rargs)

    el = time.time() - t_start
    w, h = edit["output"]["width"], edit["output"]["height"]
    say(f"\n=== 完成 ===\n  {out}\n"
        f"  元 {tc(edit['source']['duration'])} → 仕上がり {tc(edit['out_sec'])}"
        f"（{'縦型' if h > w else '横長'} {w}x{h}） / かかった時間 {el / 60:.1f}分\n"
        f"  直したいときは {plan} を書き換えて、同じ操作をもう一度してください。")
    return Path(out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="動画1本を最初から最後まで自動で編集する")
    ap.add_argument("video", nargs="?", help="元動画（省略するとファイル選択画面が出る）")
    ap.add_argument("--name", help="プロジェクト名（省略時はファイル名から自動）")
    ap.add_argument("--title", help="画面上部にずっと出すタイトル。「／」で2行")
    ap.add_argument("--prompt", default="", help="文字起こしのヒント（固有名詞・専門用語など）")
    ap.add_argument("--model", default="large-v3-turbo", help="Whisperのモデル")
    ap.add_argument("--no-fx", action="store_true", help="キラキラ等の演出を乗せない")
    ap.add_argument("--from-analysis", type=Path, help="お手本の video_style 解析フォルダ（演出の頻度を真似る）")
    ap.add_argument("--preview", action="store_true", help="確認用の軽い動画だけ作る")
    ap.add_argument("-o", "--out", type=Path, help="出力ファイル")
    ap.add_argument("--cpu", action="store_true", help="書き出しにGPUを使わない")
    ap.add_argument("--redo", action="store_true", help="全部の工程をやり直す")
    ap.add_argument("--redo-plan", action="store_true", help="編集プランだけ作り直す（手で直した内容は消える）")
    ap.add_argument("-y", "--yes", action="store_true", help="質問せずに進める（タイトルは無し）")
    ap.add_argument("--no-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.video:
        args.video = pick_file()
        if not args.video:
            raise SystemExit("動画が選ばれませんでした。")
        argv = [args.video] + argv
    if not args.no_venv:
        code = bootstrap(argv)
        if code is not None:
            return code
    ensure_ffmpeg()
    run_pipeline(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
