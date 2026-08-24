#!/usr/bin/env python3
"""生成した動画クリップをSNS投稿用（縦型9:16）に仕上げる。

クラウド無料GPU(notebooks/video_gen_free_gpu.ipynb)が出力したクリップは
解像度もfpsもまちまちで、そのままでは投稿に向かない。このスクリプトが
1080x1920・30fps・音声つきの規格に揃え、必要なら複数本をつなぐ。

GPU不要。ffmpegのみに依存する（scripts/setup_mac_video.sh が導入する）。

usage:
  python scripts/finish_reel.py video_input/clip_001.mp4
  python scripts/finish_reel.py video_input/*.mp4 --concat -o video_out/reel.mp4
  python scripts/finish_reel.py clip.mp4 --bgm assets/bgm.mp3
  python scripts/finish_reel.py --selftest
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "video_out"

# Reels / Shorts / TikTok 共通の推奨値
TARGET_W, TARGET_H = 1080, 1920
TARGET_FPS = 30
LOUDNESS_LUFS = -14.0  # Instagram/YouTubeのラウドネス基準


def find_ffmpeg():
    """ffmpeg/ffprobeの場所を返す。PATHになければimageio-ffmpeg同梱版を試す。"""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg:
        return ffmpeg, ffprobe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe(), ffprobe
    except Exception:
        return None, ffprobe


def build_vf(fit: str, width: int, height: int, fps: int) -> str:
    """映像フィルタ文字列を組み立てる（ffmpeg非依存＝テストしやすい）。

    cover  : はみ出す分を切って画面いっぱいに（被写体が大きく写る・端が切れる）
    contain: 全体を収めて余白を黒で埋める（切れないが上下に帯が出る）
    """
    if fit == "cover":
        geom = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    elif fit == "contain":
        geom = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        raise ValueError(f"未知のfit指定: {fit}")
    # setsar=1 が無いと元動画の非正方形ピクセル比を引き継ぎ、9:16として正しく表示されない
    return f"{geom},fps={fps},setsar=1,format=yuv420p"


def has_audio(ffmpeg, ffprobe, path: Path) -> bool:
    """入力に音声トラックがあるか調べる。

    ffprobeが無い環境(imageio-ffmpeg同梱版など)でも動くよう、
    ffmpegの解析出力を読む方法にフォールバックする。
    """
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        return bool(proc.stdout.strip())

    proc = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    return bool(re.search(r"Stream #\d+:\d+.*: Audio:", proc.stderr))


def build_normalize_cmd(ffmpeg, src: Path, dst: Path, vf: str, with_audio: bool) -> list:
    """1本のクリップを規格に揃えるコマンド。

    出力は必ず「映像1本＋音声1本」にする。音声トラックの数がクリップ間で
    食い違うと、結合時にストリーム数が合わず壊れるため。
    元が無音の場合は無音トラックを作って付ける。
    """
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
    if not with_audio:
        # 無音ソースを追加入力にする（長さは -shortest で映像に合わせる）
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    cmd += ["-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]"]
    cmd += ["-map", "0:a:0"] if with_audio else ["-map", "1:a:0"]
    cmd += [
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(dst),
    ]
    return cmd


def build_concat_cmd(ffmpeg, list_file: Path, dst: Path) -> list:
    """規格を揃えたクリップ同士をつなぐ（再エンコード不要なのでほぼ一瞬）。"""
    return [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart",
        str(dst),
    ]


def build_bgm_cmd(ffmpeg, src: Path, bgm: Path, dst: Path) -> list:
    """BGMを乗せてラウドネスを揃える。BGMが短ければ動画の長さまで繰り返す。"""
    return [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(src),
        "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", f"[1:a]loudnorm=I={LOUDNESS_LUFS}:TP=-1.5:LRA=11[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(dst),
    ]


def run(cmd, what: str):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[x] {what} に失敗しました\n")
        sys.stderr.write((proc.stderr or "").strip()[-2000:] + "\n")
        raise SystemExit(1)


def process(args) -> int:
    ffmpeg, ffprobe = find_ffmpeg()
    if not ffmpeg:
        sys.stderr.write(
            "[x] ffmpeg が見つかりません。\n"
            "    Mac: brew install ffmpeg   （または bash scripts/setup_mac_video.sh）\n"
        )
        return 1

    sources = [Path(p) for p in args.inputs]
    missing = [p for p in sources if not p.is_file()]
    if missing:
        sys.stderr.write("[x] 入力が見つかりません: " + ", ".join(str(m) for m in missing) + "\n")
        return 1

    vf = build_vf(args.fit, args.width, args.height, args.fps)

    if args.output:
        out_path = Path(args.output)
    else:
        stem = sources[0].stem if not args.concat else "reel_concat"
        out_path = DEFAULT_OUT_DIR / f"{stem}_reel.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1) 各クリップを同じ規格へ
        normalized = []
        for i, src in enumerate(sources, 1):
            dst = tmp / f"norm_{i:03d}.mp4"
            snd = has_audio(ffmpeg, ffprobe, src)
            print(f"[{i}/{len(sources)}] 規格化: {src.name} -> {args.width}x{args.height} "
                  f"@{args.fps}fps ({'音声あり' if snd else '無音'})")
            run(build_normalize_cmd(ffmpeg, src, dst, vf, snd), f"{src.name} の変換")
            normalized.append(dst)

        # 2) 結合（--concat 指定時のみ）
        if args.concat and len(normalized) > 1:
            list_file = tmp / "concat.txt"
            list_file.write_text(
                "".join(f"file '{p}'\n" for p in normalized), encoding="utf-8"
            )
            merged = tmp / "merged.mp4"
            print(f"[*] {len(normalized)}本を結合しています")
            run(build_concat_cmd(ffmpeg, list_file, merged), "結合")
            stage = [merged]
        else:
            if args.concat:
                print("[!] 入力が1本のみのため結合はスキップします")
            stage = normalized

        # 3) BGM（任意）と書き出し
        outputs = []
        for i, src in enumerate(stage, 1):
            final = out_path if len(stage) == 1 else out_path.with_name(
                f"{out_path.stem}_{i:03d}{out_path.suffix}"
            )
            if args.bgm:
                bgm = Path(args.bgm)
                if not bgm.is_file():
                    sys.stderr.write(f"[x] BGMが見つかりません: {bgm}\n")
                    return 1
                print(f"[*] BGMを合成しています: {bgm.name}")
                run(build_bgm_cmd(ffmpeg, src, bgm, final), "BGM合成")
            else:
                shutil.copyfile(src, final)
            outputs.append(final)

    print("\n完成:")
    for o in outputs:
        print(f"  {o}  ({o.stat().st_size / 1e6:.1f} MB)")
    return 0


def selftest() -> int:
    """ffmpegが無くても通る範囲の検証。setup_mac_video.sh から呼ばれる。"""
    ok = True

    # フィルタ文字列の組み立て
    cover = build_vf("cover", 1080, 1920, 30)
    assert "force_original_aspect_ratio=increase" in cover, cover
    assert "crop=1080:1920" in cover, cover
    assert "pad=" not in cover, cover

    contain = build_vf("contain", 1080, 1920, 30)
    assert "force_original_aspect_ratio=decrease" in contain, contain
    assert "pad=1080:1920" in contain, contain
    assert "crop=" not in contain, contain

    for vf in (cover, contain):
        # setsar=1 が無いと9:16として正しく表示されない（実機テストで検出した不具合）
        assert vf.endswith("fps=30,setsar=1,format=yuv420p"), vf

    try:
        build_vf("stretch", 1080, 1920, 30)
    except ValueError:
        pass
    else:
        raise AssertionError("不正なfit指定がエラーにならない")

    # コマンド組み立て
    # 音声の有無にかかわらず「映像1＋音声1」になること
    for with_audio in (True, False):
        cmd = build_normalize_cmd("ffmpeg", Path("a.mp4"), Path("b.mp4"), cover, with_audio)
        assert "-shortest" in cmd and "libx264" in cmd, cmd
        assert cmd.count("-map") == 2, cmd
        assert ("anullsrc" in " ".join(cmd)) is (not with_audio), cmd
        assert ("-map" in cmd and ("0:a:0" in cmd) is with_audio), cmd

    cmd = build_concat_cmd("ffmpeg", Path("l.txt"), Path("o.mp4"))
    assert "concat" in cmd and "-c" in cmd, cmd

    cmd = build_bgm_cmd("ffmpeg", Path("v.mp4"), Path("b.mp3"), Path("o.mp4"))
    assert "-stream_loop" in cmd and f"loudnorm=I={LOUDNESS_LUFS}" in " ".join(cmd), cmd

    print("[o] フィルタ/コマンド組み立て: OK")

    ffmpeg, _ = find_ffmpeg()
    if ffmpeg:
        print(f"[o] ffmpeg: {ffmpeg}")
    else:
        print("[!] ffmpeg が見つかりません（brew install ffmpeg が必要）")
        ok = False

    print("セルフテスト完了" if ok else "セルフテスト完了（要対応の項目あり）")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(
        description="生成クリップをSNS投稿用の縦型動画に仕上げる",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("usage:")[-1],
    )
    p.add_argument("inputs", nargs="*", help="入力のmp4（複数可・ワイルドカード可）")
    p.add_argument("-o", "--output", help=f"出力先（既定: {DEFAULT_OUT_DIR}/<名前>_reel.mp4）")
    p.add_argument("--concat", action="store_true", help="複数の入力を1本につなぐ")
    p.add_argument("--bgm", help="重ねるBGMの音声ファイル")
    p.add_argument("--fit", choices=["cover", "contain"], default="cover",
                   help="cover=切り抜いて画面いっぱい（既定） / contain=全体を収めて黒帯")
    p.add_argument("--width", type=int, default=TARGET_W)
    p.add_argument("--height", type=int, default=TARGET_H)
    p.add_argument("--fps", type=int, default=TARGET_FPS)
    p.add_argument("--selftest", action="store_true", help="環境と組み立てロジックの検証のみ")

    args = p.parse_args()

    if args.selftest:
        return selftest()
    if not args.inputs:
        p.print_help()
        return 1
    return process(args)


if __name__ == "__main__":
    raise SystemExit(main())
