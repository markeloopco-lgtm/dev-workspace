#!/usr/bin/env python3
"""analyze_video.py の検証。答えの分かっている合成動画をffmpegで作って分析させる。GPU・ネット不要。

合成動画A (320x180 / 30fps / 6秒):
  0.0-2.0s 青背景 + 横に動く白い四角（1.0s = 30フレーム目だけ全面白のフラッシュ）
  2.0-3.5s 茶背景 + 縦に動く白い四角
  3.5-4.0s 黒
  4.0-6.0s 緑背景 + 横に動く白い四角
  全編: 右上に黄色の固定ロゴ ／ 音: 440Hz、2.5-3.5sだけ無音
  期待: 画面の切り替え(カット検出) = 60, 105, 120フレーム目 ／ フラッシュ1回（カットに数えない）／
        黒0.5秒 ／ 素材の切り替えは「カット1回(2.0s)・暗転1回(黒を挟む)」で素材3本 ／
        ロゴ部分は固定 ／ 実効fps≈30 ／ 無音 2.5-3.5s
合成動画B: 60fpsの入れ物に30fps分の絵（同じ絵が2回ずつ） → 実効fps≈30
合成動画C: 青→橙へ1.5〜2.5秒でディゾルブ → カットではなく「クロスフェード」の切り替え(2.0秒)
合成動画D: 横に流れる模様(パン) → 大きな変化だが切り替えには数えない
合成動画E: 動いている素材どうしを1.5秒から0.5秒でクロスフェード → 1.75秒にクロスフェード1回
合成動画Z: ズームし続ける映像 → 切り替えには数えない

usage: python tests/run_video_selftest.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import analyze_video as av

FPS = 30
EXPECTED_CUTS = [60, 105, 120]
LOGO = (250, 10, 60, 20)  # x, y, w, h

VIDEO_A = (
    "color=c=0x3050a0:s=320x180:r=30:d=2[bgA];color=c=white:s=40x40:r=30:d=2[sqA];"
    "[bgA][sqA]overlay=x='20+t*100':y=70:shortest=1[A];"
    "color=c=0xa05030:s=320x180:r=30:d=1.5[bgB];color=c=white:s=40x40:r=30:d=1.5[sqB];"
    "[bgB][sqB]overlay=x=140:y='10+t*80':shortest=1[B];"
    "color=c=black:s=320x180:r=30:d=0.5[K];"
    "color=c=0x30a050:s=320x180:r=30:d=2[bgC];color=c=white:s=40x40:r=30:d=2[sqC];"
    "[bgC][sqC]overlay=x='260-t*100':y=100:shortest=1[C];"
    "[A][B][K][C]concat=n=4:v=1:a=0,"
    f"drawbox=x={LOGO[0]}:y={LOGO[1]}:w={LOGO[2]}:h={LOGO[3]}:color=yellow:t=fill,"
    "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='eq(n,30)',format=yuv420p[v];"
    "sine=f=440:d=2.5[a1];anullsrc=r=44100:cl=mono,atrim=duration=1[a2];sine=f=440:d=2.5[a3];"
    "[a1][a2][a3]concat=n=3:v=0:a=1[a]"
)
VIDEO_B = (
    "color=c=0x404040:s=320x180:r=30:d=3[bg];color=c=white:s=40x40:r=30:d=3[sq];"
    "[bg][sq]overlay=x='20+t*90':y=70:shortest=1,fps=60,format=yuv420p[v]"
)
VIDEO_C = (
    "color=c=0x3050a0:s=320x180:r=30:d=3[x];color=c=0xd08030:s=320x180:r=30:d=3[y];"
    "[x][y]xfade=transition=fade:duration=1:offset=1.5,format=yuv420p[v]"
)
VIDEO_D = "testsrc2=s=960x180:r=30:d=3,crop=320:180:x='t*200':y=0,format=yuv420p[v]"
VIDEO_E = (
    "testsrc2=s=640x180:r=30:d=3,crop=320:180:x='t*40':y=0[x];"
    "smptehdbars=s=640x180:r=30:d=3,crop=320:180:x='100-t*30':y=0[y];"
    "[x][y]xfade=transition=fade:duration=0.5:offset=1.5,format=yuv420p[v]"
)
VIDEO_Z = "testsrc2=s=320x180:r=30:d=3,zoompan=z='1+0.3*on/90':d=1:s=320x180:fps=30,format=yuv420p[v]"


def encode(graph, out: Path, audio: bool):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-filter_complex", graph, "-map", "[v]"]
    if audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(out)]
    subprocess.run(cmd, check=True)


def main() -> int:
    av.require_tools("ffmpeg", "ffprobe")
    tmp = Path(tempfile.mkdtemp(prefix="video_selftest_"))
    va, vb, vc, vd, ve, vz = (tmp / f"{k}.mp4" for k in "abcdez")
    encode(VIDEO_A, va, audio=True)
    encode(VIDEO_B, vb, audio=False)
    encode(VIDEO_C, vc, audio=False)
    encode(VIDEO_D, vd, audio=False)
    encode(VIDEO_E, ve, audio=False)
    encode(VIDEO_Z, vz, audio=False)
    errors = []

    # 1) メタデータ
    info = av.probe(va)
    vi = info["video"]
    if (vi["width"], vi["height"], round(vi["fps"])) != (320, 180, FPS):
        errors.append(f"probe: {vi['width']}x{vi['height']} {vi['fps']}fps")
    if abs(info["duration_sec"] - 6.0) > 0.1:
        errors.append(f"probe: 長さ {info['duration_sec']}")

    # 2) 走査とカット検出(フラッシュはカットに数えない)
    sc = av.scan_video(va, info, verbose=False)
    if len(sc.t) != 6 * FPS:
        errors.append(f"フレーム数 {len(sc.t)} (期待 {6 * FPS})")
    cuts, guard = av.detect_cuts(sc)
    if len(cuts) != len(EXPECTED_CUTS) or any(abs(c - e) > 1 for c, e in zip(cuts, EXPECTED_CUTS)):
        errors.append(f"カット位置 {cuts} (期待 {EXPECTED_CUTS})")
    flashes = av.detect_flash_events(sc, guard)
    if len(flashes) != 1 or abs(flashes[0]["start"] - 1.0) > 0.05:
        errors.append(f"フラッシュ {flashes} (期待: 1.0秒に1回)")
    black = av.detect_black(sc)
    if len(black) != 1 or abs(black[0][0] - 105) > 1 or abs(black[0][1] - 120) > 1:
        errors.append(f"黒画面 {black} (期待: 105-120フレーム)")

    # 3) 固定部分(ロゴ)と動く部分(四角の通り道)
    x, y, w, h = LOGO
    logo_std = float(sc.std_map[y + 4:y + h - 4, x + 4:x + w - 4].max())
    if logo_std >= av.STATIC_STD:
        errors.append(f"ロゴ部分が固定とみなされない: std={logo_std:.2f}")
    if float(sc.motion_map[80:100, 60:200].mean()) <= float(sc.motion_map[y:y + h, x:x + w].mean()):
        errors.append("四角の通り道よりロゴ部分の方が動いていることになっている")
    regions = av.dynamic_regions(sc.motion_map)
    if not regions or regions[0]["area"] > 60:
        errors.append(f"動く領域がカットの画面切り替えに引きずられている: {regions[:1]}")
    elif any(r["x0"] <= 80 <= r["x1"] and r["y0"] <= 10 for r in regions if r["y1"] <= 17):
        errors.append(f"固定ロゴが動く領域に入っている: {regions}")

    # 3b) ディゾルブはカットではなく「クロスフェード」の切り替え、パンは切り替えに数えない
    sc_c = av.scan_video(vc, av.probe(vc), verbose=False)
    cuts_c, _ = av.detect_cuts(sc_c)
    changes_c = av.detect_changes(sc_c, cuts_c, [])
    bounds_c = av.merge_boundaries(sc_c, cuts_c, changes_c)
    if cuts_c:
        errors.append(f"ディゾルブをカットと判定: {cuts_c}")
    if len(bounds_c) != 1 or bounds_c[0][1] != "dissolve" or abs(sc_c.t[bounds_c[0][0]] - 2.0) > 0.15:
        errors.append(f"ディゾルブの切り替え {bounds_c} / 区間 {changes_c} (期待: 2.0秒にクロスフェード1回)")
    for name, video in (("パン", vd), ("ズーム", vz)):
        sc_m = av.scan_video(video, av.probe(video), verbose=False)
        cuts_m, _ = av.detect_cuts(sc_m)
        changes_m = av.detect_changes(sc_m, cuts_m, [])
        summary = [(c["kind"], c["blend"]) for c in changes_m]
        if cuts_m or av.merge_boundaries(sc_m, cuts_m, changes_m):
            errors.append(f"{name}を切り替えと判定: cuts={cuts_m} changes={summary}")
        if not changes_m or any(c["kind"] != "motion" for c in changes_m):
            errors.append(f"{name}が「大きな変化」として出ない: {summary}")
    sc_e = av.scan_video(ve, av.probe(ve), verbose=False)
    cuts_e, _ = av.detect_cuts(sc_e)
    bounds_e = av.merge_boundaries(sc_e, cuts_e, av.detect_changes(sc_e, cuts_e, []))
    if len(bounds_e) != 1 or bounds_e[0][1] != "dissolve" or abs(sc_e.t[bounds_e[0][0]] - 1.75) > 0.15:
        errors.append(f"動く素材どうしのクロスフェード {bounds_e} / cuts {cuts_e} (期待: 1.75秒にクロスフェード1回)")
    rep_c = av.analyze(vc, tmp / "report_c", sheets=False, verbose=False)
    ed_c = rep_c["editing"]
    if (ed_c["transitions"], ed_c["dissolves"], ed_c["shots"]) != (1, 1, 2) \
            or rep_c["editing"]["shot_list"][1]["transition_in"] != "dissolve":
        errors.append(f"ディゾルブ動画のレポート: 切り替え{ed_c['transitions']} クロスフェード{ed_c['dissolves']}"
                      f" ショット{ed_c['shots']} (期待 1/1/2)")
    if not (tmp / "report_c" / "shots.csv").read_text(encoding="utf-8-sig").count("クロスフェード") == 1:
        errors.append("shots.csv にクロスフェードの行がない")

    # 4) 実効fps: A は 30fps のまま、B は 60fps 入れ物で中身 30fps
    eff = av.effective_fps(sc, cuts)
    if not eff or abs(eff["fps"] - 30) > 2:
        errors.append(f"実効fps(A) {eff} (期待 ≈30)")
    info_b = av.probe(vb)
    sc_b = av.scan_video(vb, info_b, verbose=False)
    eff_b = av.effective_fps(sc_b, av.detect_cuts(sc_b)[0])
    if round(info_b["video"]["fps"]) != 60 or not eff_b or abs(eff_b["fps"] - 30) > 3:
        errors.append(f"実効fps(B) {eff_b} / 入れ物 {info_b['video']['fps']}fps (期待: 60fps中身≈30)")

    # 5) 一括分析: 無音区間・ラウドネス・出力ファイル
    rep = av.analyze(va, tmp / "report", verbose=False)
    aud = rep["audio"] or {}
    if aud.get("lufs") is None or not -40 < aud["lufs"] < -10:
        errors.append(f"ラウドネス {aud.get('lufs')}")
    db = av.scan_audio(va)
    pauses = av._runs(db < av.QUIET_DB, min_len=3)
    if len(pauses) != 1 or abs(pauses[0][0] * av.AUDIO_BLOCK - 2.5) > 0.15 \
            or abs(pauses[0][1] * av.AUDIO_BLOCK - 3.5) > 0.15:
        errors.append(f"無音区間 {[(a * av.AUDIO_BLOCK, b * av.AUDIO_BLOCK) for a, b in pauses]} (期待 2.5-3.5s)")
    ed = rep["editing"]
    if (ed["transitions"], ed["cuts"], ed["fades"], ed["shots"]) != (2, 1, 1, 3):
        errors.append(f"レポートの切り替え/カット/暗転/素材数 {ed['transitions']}/{ed['cuts']}/{ed['fades']}/"
                      f"{ed['shots']} (期待 2/1/1/3)")
    if [s["transition_in"] for s in ed["shot_list"] if not s["black"]] != ["start", "cut", "fade"]:
        errors.append(f"素材の入り方 {[(s['transition_in'], s['black']) for s in ed['shot_list']]}")
    out = tmp / "report"
    for name in ["report.md", "report.json", "timeline.png", "palette.png", "layout_motion.png",
                 "layout_static.png", "sheet_shots_01.jpg", "sheet_timeline_01.jpg"]:
        if not (out / name).exists():
            errors.append(f"出力がない: {name}")
    md = (out / "report.md").read_text(encoding="utf-8")
    for must in ["## 1. 基本スペック", "## 7. フレーム単位で確認したい区間", "--start"]:
        if must not in md:
            errors.append(f"report.md に {must!r} がない")
    json.loads((out / "report.json").read_text(encoding="utf-8"))

    # 6) 区間指定の分析: 時刻がファイル先頭基準のまま出るか
    part = av.analyze(va, tmp / "report_part", start=1.5, duration=2.5, sheets=False, verbose=False)
    part_tr = [(x["at"], x["kind"]) for x in part["editing"]["transition_list"]]
    if len(part_tr) != 2 or abs(part_tr[0][0] - 2.0) > 0.05 or abs(part_tr[1][0] - 3.5) > 0.05 \
            or [k for _, k in part_tr] != ["cut", "fade"]:
        errors.append(f"区間分析の切り替え {part_tr} (期待 [(2.0, cut), (3.5, fade)])")

    # 7) frames: 指定区間の全フレームと時刻
    fr = av.extract_frames(va, 1.9, count=6, outdir=tmp / "frames", verbose=False)
    ts = [f["t"] for f in fr["frames"]]
    if len(ts) != 6 or abs(ts[0] - 1.9) > 0.02 or abs(ts[1] - ts[0] - 1 / FPS) > 0.005:
        errors.append(f"frames の時刻 {ts}")
    if not (tmp / "frames" / "filmstrip_01.jpg").exists():
        errors.append("frames の一覧画像がない")

    # 8) compare: 自分自身との比較は差ゼロ、音量差は調整案が出る
    same = av.compare_reports(rep, rep)
    if "大きな差はありません" not in same:
        errors.append("同じレポート同士の比較で差が出ている")
    louder = json.loads(json.dumps(rep))
    louder["audio"]["lufs"] = rep["audio"]["lufs"] + 6
    if "ゲイン" not in av.compare_reports(louder, rep):
        errors.append("音量差があるのにゲイン調整の案が出ない")

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] カット/フラッシュ/黒画面/ディゾルブ/固定部分と動く領域/実効fps/無音/区間分析/frames/compare を確認 ({tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
