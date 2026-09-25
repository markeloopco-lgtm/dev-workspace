#!/usr/bin/env python3
"""videolab(動画分析・制作)の検証。GPU・ネット・音声エンジン不要。

正解の分かっている合成動画を作って解析し、検出結果を正解と突き合わせる。
さらに台本 → 制作 → 再解析のラウンドトリップで「作った通りに測れる」ことを確かめる
(= 目標スタイルとの比較採点が信頼できることの裏付け)。

usage: python tests/run_video_selftest.py [--quick]
"""

import json
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import numpy as np  # noqa: E402

import video_fixtures as vf  # noqa: E402
from videolab import analyze, audio, ffmpeg_util as ff, pipeline, profile  # noqa: E402

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)
    return cond


def near(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def test_analyzer(tmp: Path):
    video = tmp / "fixture.mp4"
    truth = vf.build_fixture(video)
    info, fa, arrays, trans, shots = analyze.analyze_frames(video, quiet=True)
    check(info.n_frames == truth["n_frames"], f"フレーム数 {info.n_frames} != {truth['n_frames']}")
    cuts = [t.frame for t in trans if t.kind == "cut"]
    for c in truth["cuts"]:
        check(any(abs(c - x) <= 1 for x in cuts), f"カット未検出: frame {c} (検出: {cuts})")
    check(len(cuts) == len(truth["cuts"]), f"カット数 {len(cuts)} != {len(truth['cuts'])} ({cuts})")
    diss = [t for t in trans if t.kind == "dissolve"]
    d = truth["dissolves"][0]
    check(any(d["start"] - 2 <= t.frame <= d["end"] + 2 for t in diss), f"ディゾルブ未検出 {d} ({diss})")
    fades = [t for t in trans if t.kind == "fade_black"]
    fd = truth["fades"][0]
    check(any(fd["start"] <= t.frame <= fd["end"] for t in fades), f"暗転未検出 {fd}")
    flashes = [t.frame for t in trans if t.kind == "flash"]
    check(any(abs(truth["flashes"][0] - f) <= 1 for f in flashes), "フラッシュ未検出(カット扱いされた?)")
    check(len(shots) == len(truth["shots"]), f"ショット数 {len(shots)} != {len(truth['shots'])}")
    for sh, tr in zip(shots, truth["shots"]):
        cam = sh.stats.get("camera")
        check(cam == tr["camera"], f"shot{sh.index} カメラ {cam} != {tr['camera']}")
        has_text = (sh.stats.get("text_ratio") or 0) > 0.5
        check(has_text == tr["telop"], f"shot{sh.index} テロップ判定 {has_text} != {tr['telop']}")
    z = shots[1].stats.get("zoom_total")
    check(near(z, 1.12, 0.02), f"ズーム量 {z} (正解1.12)")
    px = shots[2].stats.get("pan_x_total")
    check(near(px, -0.104, 0.01), f"パン量 {px} (正解-0.104)")
    check(shots[2].stats.get("easing") == "ease_in_out", f"イージング {shots[2].stats.get('easing')}")
    zo = shots[4].stats.get("zoom_total")
    check(near(zo, 1.03 / 1.15, 0.02), f"ズームアウト量 {zo} (正解{1.03 / 1.15:.3f})")
    return len(shots)


def test_audio(tmp: Path):
    wav = tmp / "speech.wav"
    truth = vf.build_audio_fixture(wav)
    mp4 = tmp / "speech.mp4"
    ff.run([ff.find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
            "color=black:s=320x180:r=30:d=12", "-i", str(wav), "-shortest", "-c:v", "libx264",
            "-c:a", "aac", "-b:a", "192k", str(mp4)])
    info = ff.probe(mp4)
    r = audio.analyze_audio(mp4, info)
    import pyloudnorm
    import soundfile as sf
    y, sr = sf.read(str(wav))
    ref = pyloudnorm.Meter(sr).integrated_loudness(y)
    check(near(r["lufs_integrated"], ref, 0.5), f"ラウドネス {r['lufs_integrated']} (基準 {ref:.2f})")
    segs = r["segments"]
    check(len(segs) == len(truth["speech_segments"]), f"発話区間数 {len(segs)} != 3")
    for (s, e), g in zip(truth["speech_segments"], segs):
        check(near(g["start"], s, 0.15) and near(g["end"], e, 0.15), f"発話区間 {g} (正解 {s}-{e})")
    # YouTube自動字幕風のVTT(流れる重複行)の解析
    vtt = tmp / "x.ja.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:00.500 --> 00:00:02.000 align:start position:0%\n"
        "もしも<00:00:00.900><c>地球が</c>\n\n"
        "00:00:02.000 --> 00:00:02.010\nもしも地球が\n\n"
        "00:00:02.010 --> 00:00:04.000\nもしも地球が\n止まったら\n\n", encoding="utf-8")
    cues = audio.parse_subtitles(vtt)
    check([c["text"] for c in cues] == ["もしも地球が", "止まったら"], f"VTT解析 {cues}")


def test_profile(tmp: Path, n_shots_expected: int):
    out = pipeline.run_analysis(tmp / "fixture.mp4", tmp / "an_fixture", report=True, quiet=True)
    for name in ("meta.json", "frames.csv", "shots.json", "shots.csv", "audio.json", "profile.json",
                 "contact_sheet.jpg", "report.html"):
        check((out / name).exists(), f"成果物が無い: {name}")
    p = profile.load_profile(out)
    check(p["editing"]["n_shots"] == n_shots_expected, f"profile n_shots {p['editing']['n_shots']}")
    self_cmp = profile.compare(p, p)
    check(self_cmp["score"] == 100.0, f"自己比較スコアが100でない: {self_cmp['score']}")
    agg = profile.aggregate([p, p])
    check(agg["editing"]["shot_len_median"] == p["editing"]["shot_len_median"], "aggregateの中央値")
    check(abs(sum(agg["camera"]["move_mix"].values()) - 1.0) < 0.01, "aggregateの構成比の合計")
    yml = tmp / "agg.yaml"
    profile.save_profile(agg, yml)
    check(profile.load_profile(yml)["source"]["n"] == 2, "YAML保存/読込")
    html = (out / "report.html").read_text(encoding="utf-8")
    check("<svg" in html and "shot_0000.jpg" in html, "レポートにグラフ/キーフレームが無い")


def test_timeline_units():
    from videolab.produce.telop import split_telop
    from videolab.produce.timeline import DeficitPicker
    pk = DeficitPicker({"cut": 0.8, "dissolve": 0.2})
    seq = [pk.pick() for _ in range(50)]
    check(seq.count("dissolve") == 10, f"DeficitPickerの比率 {seq.count('dissolve')}/50")
    parts = split_telop("これはとても長い台詞なので二つに分かれるはずです。", 22)
    check(parts == ["これはとても長い台詞なので", "二つに分かれるはずです。"], f"テロップ分割 {parts}")
    from videolab.produce.sources import CameraMove
    c = CameraMove("pan_right", 0.1)
    check(c.at(0)[1] > c.at(1)[1], "pan_right は中身が左へ流れる")


def test_roundtrip(tmp: Path, quick: bool):
    """台本 → 制作 → 再解析。計画したカット・カメラワーク・音量が測り直して一致するか。"""
    import cv2
    from videolab.produce.compose import produce_episode

    for i in range(2):
        ff.imwrite(tmp / f"img{i}.png", cv2.cvtColor(vf.texture(30 + i, 1600, 900), cv2.COLOR_RGB2BGR))
    visuals_b = ["{type: image, path: img1.png, camera: pan_right}",
                 "{type: color, color: '#0b1d3a', color2: '#1e5aa8'}"]
    if not quick:
        visuals_b.append("{type: space, template: planet, params: {preset: earth}, engine: 2d, "
                         "camera: zoom_in}")
    ep = tmp / "rt.yaml"
    ep.write_text(f"""
title: ラウンドトリップ
voices:
  ナレーター: {{engine: dummy}}
  教授: {{engine: dummy}}
scenes:
  - id: a
    title: テスト
    lines:
      - {{speaker: ナレーター, text: "もしも、月が今の半分の距離まで近づいたら……？"}}
      - {{speaker: 教授, text: "夜空の景色も、海の姿も、まるで別の星になってしまうぞ。"}}
    visuals:
      - {{type: image, path: img0.png, camera: zoom_in}}
  - id: b
    lines:
      - {{speaker: ナレーター, text: "まず、見た目の大きさは今の2倍。面積にすると4倍です。"}}
      - {{speaker: 教授, text: "満月の明るさも、およそ4倍になる。夜道で本が読めるほどじゃ。", callout: "明るさ 4倍"}}
    visuals: [{", ".join(visuals_b)}]
""", encoding="utf-8")
    style = profile.load_profile(ROOT / "configs" / "style_profile.yaml")
    res = produce_episode(ep, ROOT / "configs" / "style_profile.yaml", tmp / "rt.mp4", draft=True,
                          quiet=True)
    plan = json.loads(Path(res["plan"]).read_text(encoding="utf-8"))
    out = pipeline.run_analysis(res["video"], tmp / "an_rt", subs=res["srt"], report=False,
                                quiet=True)
    shots = json.loads((out / "shots.json").read_text(encoding="utf-8"))
    aud = json.loads((out / "audio.json").read_text(encoding="utf-8"))
    info = ff.probe(res["video"])
    check(near(info.duration, plan["duration"], 0.1), f"尺 {info.duration} != 計画 {plan['duration']}")
    check(len(shots) == len(plan["shots"]), f"再解析のショット数 {len(shots)} != 計画 {len(plan['shots'])}")
    for ps in plan["shots"]:
        mid = (ps["start"] + ps["end"]) / 2
        got = next((s for s in shots if s["start"] <= mid < s["end"]), None)
        if not check(got is not None, f"計画shot{ps['index']}に対応するショットが無い"):
            continue
        kind, amt = ps["camera"]["kind"], ps["camera"]["amount"]
        if ps["visual"]["type"] == "color":
            continue   # 無地は特徴点が無くカメラを測れない(unknown)のが正しい
        if kind == "zoom_in" and ps["visual"]["type"] == "space":
            # 宇宙シーンは星空に奥行き(パララックス)があり、画面全体の拡大率は惑星より小さく出る
            check(got["zoom_total"] is not None and got["zoom_total"] > 1 + 0.3 * amt,
                  f"計画shot{ps['index']} 宇宙 zoom_in {1 + amt:.3f} → 実測 {got['zoom_total']}")
        elif kind == "zoom_in":
            check(near(got["zoom_total"], 1 + amt, 0.02 + 0.2 * amt),
                  f"計画shot{ps['index']} zoom_in {1 + amt:.3f} → 実測 {got['zoom_total']}")
        elif kind in ("pan_right", "pan_left"):
            exp = -amt if kind == "pan_right" else amt
            check(near(got["pan_x_total"], exp, 0.01 + 0.25 * amt),
                  f"計画shot{ps['index']} {kind} {exp:.3f} → 実測 {got['pan_x_total']}")
        check(got["camera"] == ("static" if kind == "static" else kind) or
              (kind == "static" and got["camera"] in ("static", "static_action")),
              f"計画shot{ps['index']} {kind} → 実測 {got['camera']}")
    target = style["audio"]["lufs_integrated"]
    check(near(aud["lufs_integrated"], target, 1.0), f"ラウドネス {aud['lufs_integrated']} (目標 {target})")
    check(aud["true_peak"] is not None and aud["true_peak"] <= -0.9, f"トゥルーピーク {aud['true_peak']}")
    check(aud["speech_source"] == "transcript", "自作動画は字幕(SRT)から発話区間を取れるはず")
    check(Path(res["credits"]).exists() and Path(res["srt"]).exists(), "字幕/クレジットが無い")
    check(len(plan.get("callouts", [])) == 1, f"強調テキストの数 {plan.get('callouts')}")
    # 参考動画フォルダの素材は拒否される
    from videolab.produce.episode import EpisodeError, load_episode
    bad = tmp / "refs"
    bad.mkdir(exist_ok=True)
    (bad / "x.png").write_bytes((tmp / "img0.png").read_bytes())
    ep_bad = tmp / "bad.yaml"
    ep_bad.write_text("scenes:\n  - lines: [テスト]\n    visuals: [{type: image, path: refs/x.png}]\n",
                      encoding="utf-8")
    try:
        load_episode(ep_bad)
        check(False, "refs/ の素材が拒否されなかった")
    except EpisodeError:
        pass


def main() -> int:
    quick = "--quick" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="vlab_selftest_"))
    t0 = time.time()
    steps = [("解析(合成動画の正解照合)", lambda: test_analyzer(tmp))]
    n = None
    for name, fn in steps:
        n = fn()
    test_audio(tmp)
    test_profile(tmp, n)
    test_timeline_units()
    import os
    cwd = os.getcwd()
    os.chdir(tmp)   # renders/cache を一時フォルダに作らせる
    try:
        test_roundtrip(tmp, quick)
    finally:
        os.chdir(cwd)
    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] 解析の正解照合・音声・プロファイル・制作ラウンドトリップを確認 "
          f"({time.time() - t0:.0f}秒, {tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
