#!/usr/bin/env python3
"""videolab コマンド: 参考動画のフレーム単位分析 → 目標スタイル → 自作動画の制作と採点。

よく使う流れ(詳しくは docs/06, docs/07):
  python scripts/vlab.py doctor                          # 環境チェック
  python scripts/vlab.py watch https://www.youtube.com/watch?v=XXXX   # ダウンロード不要の分析
  python scripts/vlab.py fetch https://www.youtube.com/watch?v=XXXX   # ※規約上の注意あり
  python scripts/vlab.py analyze refs/XXXX.mp4           # → analysis/XXXX/report.html
  python scripts/vlab.py purge analysis/XXXX --video refs/XXXX.mp4    # 分析後に複製物を削除
  python scripts/vlab.py aggregate analysis/A analysis/B analysis/C -o configs/style_profile.yaml
  python scripts/vlab.py produce episodes/sample_moon_half.yaml --check
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    # Windowsの既定コンソール(cp932)でも日本語・記号で落ちないようにする
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")


def cmd_doctor(a):
    from videolab import doctor
    return doctor.run()


def cmd_fetch(a):
    from videolab import fetch
    path = fetch.fetch(a.url, Path(a.out), subs=not a.no_subs, cookies_from=a.cookies_from,
                       assume_yes=a.yes)
    print(f"保存しました: {path}\n次: python scripts/vlab.py analyze {path}")


def cmd_watch(a):
    from videolab import vlm
    path = vlm.watch_youtube(a.url, a.out, a.model, a.start, a.end, a.fps)
    print(f"分析結果: {path.with_suffix('.md')}")


def cmd_analyze(a):
    from videolab import pipeline
    out = pipeline.run_analysis(a.video, a.out, subs=a.subs, whisper=a.whisper, step=a.step,
                                max_seconds=a.max_seconds, report=not a.no_report)
    if a.vlm:
        _annotate(out, a.model, 10, a.yes)
    if a.purge:
        removed = pipeline.purge(out, a.video)
        print(f"\n複製物を{len(removed)}件削除しました(数値データは残っています)")
        print(f"\n完了: {out}")
    else:
        print(f"\n完了: {out}\n  ブラウザで {out / 'report.html'} を開いてください")
        print("  研究が済んだら: python scripts/vlab.py purge "
              f"{out} --video {a.video}  (参考動画の複製を削除)")


def _annotate(out_dir, model, batch, yes):
    from videolab import pipeline, report, vlm
    print(vlm.ANNOTATE_WARNING)
    if not yes:
        try:
            ok = input("送信してよいですか? [y/N]: ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            print("Geminiへの送信を取りやめました")
            return None
    path = vlm.annotate_shots(out_dir, model, batch=batch)
    pipeline.rebuild_profile(out_dir)
    report.write_report(Path(out_dir))
    print(f"保存しました: {path}")
    return path


def cmd_annotate(a):
    _annotate(a.dir, a.model, a.batch, a.yes)


def cmd_purge(a):
    from videolab import pipeline
    removed = pipeline.purge(a.dir, a.video)
    for p in removed:
        print(f"削除: {p}")
    print(f"{len(removed)}件削除しました。profile.json など数値データは残っています")


def cmd_report(a):
    from videolab import report
    print(report.write_report(Path(a.dir)))


def cmd_frames(a):
    from videolab import filmstrip
    outs = filmstrip.filmstrip(a.video, a.start, a.end, a.out, every=a.every, cols=a.cols,
                               diff=a.diff)
    for p in outs:
        print(f"保存しました: {p}")


def cmd_aggregate(a):
    from videolab import profile
    profs = [profile.load_profile(d) for d in a.dirs]
    agg = profile.aggregate(profs)
    profile.save_profile(agg, a.out)
    print(f"{len(profs)}本から目標スタイルを作成しました: {a.out}")


def cmd_compare(a):
    from videolab import profile
    target = profile.load_profile(a.target)
    cand = profile.load_profile(a.candidate)
    res = profile.compare(target, cand)
    md = profile.compare_markdown(res, str(a.target), str(a.candidate))
    out = Path(a.out) if a.out else Path(a.candidate) / "gap_report.md" if Path(a.candidate).is_dir() \
        else Path(a.candidate).with_name("gap_report.md")
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"保存しました: {out}")


def cmd_voices(a):
    from videolab.produce import tts
    eng = tts.make_engine(a.engine, a.url)
    for sid, name in eng.speakers():
        print(f"{sid:>4}  {name}")


def cmd_new_episode(a):
    tpl = Path(__file__).resolve().parent.parent / "episodes" / "_template.yaml"
    text = tpl.read_text(encoding="utf-8").replace("{{TITLE}}", a.title)
    out = Path(a.out)
    if out.exists() and not a.force:
        sys.exit(f"既にあります: {out} (上書きするなら --force)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"作成しました: {out}\n次: 台本を書いたら python scripts/vlab.py produce {out} --draft")


def cmd_produce(a):
    from videolab.produce.compose import produce_episode
    res = produce_episode(a.episode, a.style, a.out, tts=a.tts, engine=a.engine,
                          encoder=a.encoder, draft=a.draft)
    if a.check:
        from videolab import pipeline, profile
        video = Path(res["video"])
        out_dir = Path("analysis") / f"_mine_{video.stem}"
        print("\n[採点] 完成動画を同じ物差しで解析します")
        pipeline.run_analysis(video, out_dir, subs=res["srt"])
        style = a.style or "configs/style_profile.yaml"
        r = profile.compare(profile.load_profile(style), profile.load_profile(out_dir))
        md = profile.compare_markdown(r, str(style), str(video))
        (out_dir / "gap_report.md").write_text(md, encoding="utf-8")
        print(md)
        print(f"差分レポート: {out_dir / 'gap_report.md'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vlab", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="環境チェック")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("fetch", help="参考動画をダウンロード(個人の分析用)")
    s.add_argument("url")
    s.add_argument("--out", default="refs")
    s.add_argument("--no-subs", action="store_true", help="字幕を取らない")
    s.add_argument("--cookies-from", help="ログイン必須の動画のみ。捨てアカウントのfirefox等に限る")
    s.add_argument("--yes", action="store_true", help="規約上の注意への同意確認を省略")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("watch", help="YouTubeのURLをGeminiで分析(ダウンロード不要)")
    s.add_argument("url")
    s.add_argument("--out")
    s.add_argument("--model")
    s.add_argument("--start", help="開始秒(例: 60)")
    s.add_argument("--end", help="終了秒(例: 180)")
    s.add_argument("--fps", type=float, help="Geminiに見せるフレームレート(既定1)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("analyze", help="動画をフレーム単位で解析してレポートを作る")
    s.add_argument("video")
    s.add_argument("--out", help="出力フォルダ(既定 analysis/<動画名>)")
    s.add_argument("--subs", help="字幕ファイル(.vtt/.srt)。省略時は動画と同じ場所から自動で探す")
    s.add_argument("--whisper", help="字幕が無い時に文字起こしするモデル(例: small, large-v3-turbo)")
    s.add_argument("--step", type=int, default=1, help="Nフレームに1回だけ解析(既定1=全フレーム)")
    s.add_argument("--max-seconds", type=float, help="先頭だけ解析(お試し用)")
    s.add_argument("--vlm", action="store_true",
                   help="Geminiで各ショットを分類(要APIキー。フレーム画像を送信する)")
    s.add_argument("--model", help="Geminiのモデル名")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--purge", action="store_true",
                   help="解析後すぐ元動画・字幕・キーフレーム等を削除し数値だけ残す")
    s.add_argument("--yes", action="store_true", help="確認を省略")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("annotate", help="解析済みフォルダの各ショットをGeminiで分類(画像を送信)")
    s.add_argument("dir")
    s.add_argument("--model")
    s.add_argument("--batch", type=int, default=10)
    s.add_argument("--yes", action="store_true", help="送信の確認を省略")
    s.set_defaults(func=cmd_annotate)

    s = sub.add_parser("purge", help="参考動画の複製(キーフレーム・文字起こし等)を削除")
    s.add_argument("dir", help="解析フォルダ")
    s.add_argument("--video", help="元動画も消す場合はそのパス(同名の字幕・情報ファイルも消す)")
    s.set_defaults(func=cmd_purge)

    s = sub.add_parser("report", help="レポートを作り直す")
    s.add_argument("dir")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("frames", help="区間の連続フレームを一覧画像にする(コマ送り研究)")
    s.add_argument("video")
    s.add_argument("start", help="開始 (秒 または mm:ss)")
    s.add_argument("end", help="終了 (秒 または mm:ss)")
    s.add_argument("--every", type=int, default=1, help="Nコマに1枚")
    s.add_argument("--cols", type=int, default=6)
    s.add_argument("--diff", action="store_true", help="前コマから動いた場所を赤で表示")
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_frames)

    s = sub.add_parser("aggregate", help="複数の解析結果から目標スタイルを作る")
    s.add_argument("dirs", nargs="+")
    s.add_argument("-o", "--out", default="configs/style_profile.yaml")
    s.set_defaults(func=cmd_aggregate)

    s = sub.add_parser("compare", help="解析結果を目標スタイルと比べて採点")
    s.add_argument("candidate", help="解析フォルダ または profile.json")
    s.add_argument("--target", default="configs/style_profile.yaml")
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("voices", help="音声エンジンの話者ID一覧")
    s.add_argument("--engine", default="voicevox", choices=["voicevox", "aivis"])
    s.add_argument("--url")
    s.set_defaults(func=cmd_voices)

    s = sub.add_parser("new-episode", help="台本の雛形を作る")
    s.add_argument("title")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new_episode)

    s = sub.add_parser("produce", help="台本YAMLから動画を作る")
    s.add_argument("episode")
    s.add_argument("--style", help="目標スタイル(既定 configs/style_profile.yaml)")
    s.add_argument("--out", help="出力mp4")
    s.add_argument("--tts", choices=["voicevox", "aivis", "sbv2", "dummy"],
                   help="全話者のTTSエンジンを一時的に切り替える(dummy=音声エンジン無しで試す)")
    s.add_argument("--engine", default="auto", choices=["auto", "2d", "blender"],
                   help="宇宙シーンの描画(auto=Blenderがあれば使う)")
    s.add_argument("--encoder", default="x264", choices=["x264", "nvenc"],
                   help="nvenc=NVIDIAのGPUで高速エンコード")
    s.add_argument("--draft", action="store_true", help="半分の解像度で速く試す")
    s.add_argument("--check", action="store_true", help="完成後に解析して目標スタイルと比較")
    s.set_defaults(func=cmd_produce)

    a = ap.parse_args(argv)
    try:
        rc = a.func(a)
    except KeyboardInterrupt:
        print("\n中断しました")
        return 130
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"\nエラー: {e}", file=sys.stderr)
        return 1
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
