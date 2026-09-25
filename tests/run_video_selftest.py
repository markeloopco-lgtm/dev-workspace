#!/usr/bin/env python3
"""動画編集解析 (scripts/analyze_video.py) の検証。

正解が分かっている合成動画(tests/synth_video.py)を作って解析し、
カット・ズームカット・ジャンプカット・ディゾルブ・黒フェード・白フラッシュ・ケンバーンズ・シェイク・
テロップ(位置/色/縁/高さ/出方/表示時間)・常駐ロゴ・BGM・無音の間・効果音の同期 を
許容誤差つきで照合する。GPU不要・ネット接続不要。1分程度。

usage: python tests/run_video_selftest.py [--keep DIR]
"""

import argparse
import csv
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import synth_video as sv  # noqa: E402
import analyze_video  # noqa: E402
import video_io  # noqa: E402

GT = sv.GROUND_TRUTH


def rgb(hexcol):
    return [int(hexcol[i:i + 2], 16) for i in (1, 3, 5)]


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path, help="合成動画と解析結果をこのフォルダに残す")
    args = ap.parse_args()
    tmp = args.keep or Path(tempfile.mkdtemp(prefix="video_selftest_"))
    tmp.mkdir(parents=True, exist_ok=True)
    video = sv.make_video(tmp / "synth.mp4")
    out = tmp / "analysis"
    errors = []
    check = lambda ok, msg: None if ok else errors.append(msg)

    # 0) 前提: 全フレーム走査と指定フレーム抽出でフレーム番号が一致する
    info = video_io.probe(video)
    size = info.size_for(320)
    want = [0, 89, 90, 450, 899]
    scan = {i: f for i, f in enumerate(video_io.iter_frames(info, size)) if i in want}
    grab = dict(video_io.grab_frames(info, want, size))
    check(sorted(grab) == want, f"指定フレーム抽出の番号が欠けた: {sorted(grab)}")
    for i in want:
        if i in grab and i in scan:
            check(np.array_equal(scan[i], grab[i]), f"フレーム{i}: 走査と抽出で画像が違う(番号ずれ)")

    r, F, S, A = analyze_video.analyze(video, out, {"title": "synthetic"}, log=lambda *a: None)
    near = lambda x, y, tol=1: abs(x - y) <= tol

    # 1) カット: ハードカット3 + ズームカット1。余計なカットがないこと
    cuts = {ci["frame"]: ci for ci in S.cut_info}
    for f in GT["hard_cuts"]:
        hit = [c for c in cuts if near(c, f)]
        check(hit and cuts[hit[0]]["type"] == "cut", f"ハードカット{f}が見つからない/種類違い: {sorted(cuts)}")
    zc = [cuts[c] for c in cuts if near(c, GT["zoom_cut"]["frame"])]
    check(bool(zc) and zc[0]["type"] == "zoom_in", f"ズームカット{GT['zoom_cut']['frame']}が見つからない: {zc}")
    if zc and zc[0].get("scale"):
        check(abs(zc[0]["scale"] - GT["zoom_cut"]["scale"]) <= 0.05, f"ズームカット倍率 {zc[0]['scale']}")
        c = zc[0].get("center") or [0, 0]
        check(abs(c[0] - 0.6) <= 0.05 and abs(c[1] - 0.4) <= 0.05, f"ズームの中心 {c} (正解 0.6, 0.4)")
    check(len(cuts) == 4, f"カット数 {len(cuts)} (正解4): {sorted(cuts)}")

    # 2) ジャンプカット(人物だけ位置が飛ぶ)
    jumps = [ev["start"] for ev in S.events if (ev.get("overlay") or {}).get("kind") == "jump"]
    jumps += [c for c, ci in cuts.items() if ci["type"] == "jump"]
    check(any(near(j, GT["jump_cut"]) for j in jumps), f"ジャンプカット{GT['jump_cut']}が見つからない: {jumps}")

    # 3) トランジション
    tr = {t["kind"]: t for t in S.transitions}
    check(len(S.transitions) == 3, f"トランジション数 {len(S.transitions)}: {[(t['kind'], t['start']) for t in S.transitions]}")
    d = tr.get("dissolve")
    check(d and near(d["start"], GT["dissolve"][0], 2) and near(d["end"], GT["dissolve"][1], 2), f"ディゾルブ: {d}")
    fb = tr.get("fade_black")
    check(fb and near(fb["start"], GT["fade"][0], 2) and near(fb["end"], GT["fade"][1], 2), f"黒フェード: {fb}")
    fl = tr.get("flash")
    check(fl and near(fl["start"], GT["flash"]) and fl.get("cut"), f"白フラッシュ: {fl}")

    # 4) カメラワーク
    kb = GT["ken_burns"]
    z = [m for m in S.motion["zooms"] if m["start"] < kb["end"] and m["end"] > kb["start"]]
    check(z and abs(z[0]["scale"] - kb["scale"]) <= 0.03, f"ケンバーンズ(1.2倍)が見つからない: {S.motion['zooms']}")
    check(len(S.motion["zooms"]) == 1, f"ズーム区間が余計にある: {S.motion['zooms']}")
    sh = [m for m in S.motion["shakes"] if m["start"] < GT["shake"][1] and m["end"] > GT["shake"][0]]
    check(bool(sh), f"シェイクが見つからない: {S.motion['shakes']}")

    # 5) テロップ: 出現・差し替え・消えるタイミングと位置・出方
    ovs = [(ev["start"], ev["overlay"]) for ev in S.events if (ev.get("overlay") or {}).get("kind") == "text"]
    ins = [(s, o) for s, o in ovs if o["change"] != "disappear"]
    outs = [(s, o) for s, o in ovs if o["change"] == "disappear"]
    check(len(ins) == len(GT["telops"]), f"テロップ出現数 {len(ins)} (正解{len(GT['telops'])}): {[s for s, _ in ins]}")
    starts = {t[0] for t in GT["telops"]}
    for start, end, text, pos, anim, fill, outline in GT["telops"]:
        hit = [o for s, o in ins if near(s, start, 2)]
        if not hit:
            errors.append(f"テロップ '{text}' の出現({start})が見つからない")
            continue
        o = hit[0]
        cy = 0.88 if pos == "bottom" else 0.45
        check(abs(o["center"][1] - cy) <= 0.05, f"'{text}' の位置 y={o['center'][1]} (正解{cy})")
        a = (o.get("animation") or {})
        want_anim = {"cut": ("cut",), "fade": ("fade",), "pop": ("pop_overshoot", "pop")}[anim]
        check(a.get("type") in want_anim, f"'{text}' の出方 {a.get('type')} (正解{want_anim[0]})")
        if anim != "cut":
            check(near(a.get("frames", 0), 6), f"'{text}' のアニメ長 {a.get('frames')}フレーム (正解6)")
        if end not in starts:  # 次のテロップに差し替わらず消えるもの
            check(any(near(s, end, 2) for s, _ in outs), f"テロップ '{text}' の消え({end})が見つからない")

    # 6) テロップのスタイル(2種類): 見た目の数値
    styles = r["telop"]["styles"]
    check(len(styles) == 2, f"スタイル数 {len(styles)} (正解2)")
    bottom = next((st for st in styles if st["center"][1] > 0.8), None)
    center = next((st for st in styles if 0.35 < st["center"][1] < 0.6), None)
    if bottom:
        check(min(rgb(bottom["fill"])) >= 220, f"下テロップの塗り {bottom['fill']} (正解 白)")
        check(bottom["outline"] and max(rgb(bottom["outline"])) <= 40, f"下テロップの縁 {bottom['outline']} (正解 黒)")
        check(60 <= bottom["line_h_px1080"] <= 80, f"下テロップの高さ {bottom['line_h_px1080']}px (正解≒70)")
        check(bottom["outline_px1080"] and 5 <= bottom["outline_px1080"] <= 10,
              f"下テロップの縁の太さ {bottom['outline_px1080']}px (正解≒7.5)")
        check(bottom["display_sec_median"] and 2.0 <= bottom["display_sec_median"] <= 3.0,
              f"下テロップの表示時間 {bottom['display_sec_median']}秒 (正解 2〜3秒)")
    else:
        errors.append("下テロップのスタイルがない")
    if center:
        f_, o_ = rgb(center["fill"]), rgb(center["outline"] or "#000000")
        check(f_[0] >= 200 and f_[1] >= 180 and f_[2] <= 80, f"中央テロップの塗り {center['fill']} (正解 黄)")
        check(o_[0] >= 150 and o_[1] <= 60 and o_[2] <= 60, f"中央テロップの縁 {center['outline']} (正解 赤)")
        check(115 <= center["line_h_px1080"] <= 160, f"中央テロップの高さ {center['line_h_px1080']}px (正解≒140)")
    else:
        errors.append("中央テロップのスタイルがない")

    # 7) 常駐ロゴ
    x, y, w, h = GT["logo"]
    logo = [x / sv.W, y / sv.H, (x + w) / sv.W, (y + h) / sv.H]
    po = r["persistent_overlays"]
    check(any(iou(p["bbox"], logo) >= 0.6 for p in po), f"常駐ロゴが見つからない: {po}")

    # 8) 音声
    au = A
    bgm = au.get("bgm") or {}
    check(bgm.get("present") and abs(bgm["level_dbfs"] - GT["bgm_dbfs"]) <= 3, f"BGM: {bgm}")
    gs = au.get("gap_stats") or {}
    check(gs and 0.2 <= gs["median"] <= 0.4, f"間の中央値 {gs.get('median')} (正解≒0.3秒)")
    strong = au["onsets"]["strong_times"]
    matched = sum(any(abs(t - s) <= 0.05 for t in strong) for s in sv.se_times())
    check(matched >= 5, f"効果音 {matched}/{len(sv.se_times())} 個しか見つからない: {strong}")
    sync = au["se_sync"]["by_event"].get("overlay_in", {})
    check(sync.get("rate", 0) >= 0.6 and au["se_sync"]["chance_rate"] < 0.2,
          f"テロップと効果音の同期率 {sync} / 偶然 {au['se_sync']['chance_rate']}")
    lufs = (au.get("loudness") or {}).get("integrated_lufs")
    check(lufs is not None and -30 < lufs < -5, f"ラウドネス {lufs}")

    # 9) テンポと出力ファイル
    check(r["pacing"]["edit_points"] == 8, f"編集点 {r['pacing']['edit_points']} (正解8)")
    for rel in ("report.md", "recipe.json", "analysis.json", "frames.csv", "sheets/shots_01.jpg",
                "sheets/telops_01.jpg", "sheets/telop_styles.jpg", "sheets/cuts_zoom_jump_01.jpg", "timeline_01.png"):
        check((out / rel).exists(), f"出力がない: {rel}")
    if (out / "frames.csv").exists():
        with open(out / "frames.csv", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        check(len(rows) == sv.N + 1, f"frames.csv の行数 {len(rows)} (正解{sv.N + 1})")
    if (out / "recipe.json").exists():
        rec = json.loads((out / "recipe.json").read_text(encoding="utf-8"))
        check(len(rec["telop"]["styles"]) == 2, "recipe.json にスタイルが2つない")

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        print(f"  (結果: {out})")
        return 1
    print(f"[OK] 合成動画({sv.N}フレーム)のカット・トランジション・ズーム・テロップ・ロゴ・音声を正解と照合 ({out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
