#!/usr/bin/env python3
"""analyze_video.py の検証。ネット不要・GPU不要。

正解が分かっている合成の「会話解説風」動画(図形だけのオリジナルキャラ2体)を作り、
analyze_video.py video で解析して、検出結果を正解と照合する。

合成動画の中身(640x360, 30fps, 600フレーム=20秒):
- 背景: 0-209 シーンA → 210で全面カット → 420-435でシーンCへクロスフェード
- 字幕帯: 画面下の白枠。60フレーム(2秒)ごとに文字(ブロック)が差し替わる
- ポップイン: 90-97 / 300-307 に画面上部中央へ8フレームかけて図形が出現
- 口パク: 台詞ごとに左右交互。発話中(各台詞の先頭45フレーム)は3フレームごとに開閉
- まばたき: 左キャラが50フレーム目から120フレームごとに4フレーム目を閉じる
- 音声: 台詞ごとに1.5秒発話 + 0.5秒の間。常時小さなノイズ(BGM代わり)
- 映像トラックは0.1秒遅れで始まる(先頭オフセットがあってもフレーム番号がズレないかの検証)

usage: python tests/run_video_selftest.py
"""

import csv
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_video as av

W, H, FPS, N = 640, 360, 30, 600
SR = 44100
TEXTURE = ((np.add.outer(np.arange(H), np.arange(W)) // 16) % 2 * 24 - 12).astype(np.float32)
YY, XX = np.ogrid[:H, :W]


def draw_char(img, cx, mouth_open, eyes_closed, body):
    img[225:360, cx - 60:cx + 60] = body
    img[(XX - cx) ** 2 + (YY - 160) ** 2 <= 65 ** 2] = (250, 235, 215)
    for ex in (cx - 18, cx + 18):
        if eyes_closed:
            img[133:136, ex - 5:ex + 5] = (60, 40, 40)
        else:
            img[125:137, ex - 4:ex + 4] = (40, 30, 30)
    if mouth_open:
        img[194:208, cx - 10:cx + 10] = (120, 30, 40)
    else:
        img[199:202, cx - 12:cx + 12] = (120, 30, 40)


def draw_sub(img, line):
    img[285:345, 170:470] = (250, 250, 250)
    rng = np.random.default_rng(1000 + line)
    x = 185
    for _ in range(int(rng.integers(10, 17))):
        w = int(rng.integers(10, 16))
        if x + w > 455:
            break
        img[300:330, x:x + w] = (30, 30, 30) if rng.random() < 0.8 else (200, 40, 40)
        x += w + 4


def draw_popin(img, f, start, kind):
    if f < start:
        return
    p = min(1.0, (f - start + 1) / 8)
    half = 50 * (1 - (1 - p) ** 3)
    if kind == "square":
        img[int(110 - half):int(110 + half), int(320 - half):int(320 + half)] = (250, 210, 40)
    else:
        img[(XX - 320) ** 2 + (YY - 110) ** 2 <= half ** 2] = (40, 200, 230)


def scene(f, name):
    base = {"A": (70, 90, 130), "B": (150, 110, 70), "C": (60, 140, 90)}[name]
    img = np.empty((H, W, 3), np.float32)
    img[:] = base
    img += TEXTURE[..., None]
    if name == "A":
        draw_popin(img, f, 90, "square")
    elif name == "B":
        draw_popin(img, f, 300, "circle")
    line, pos = divmod(f, 60)
    talking_open = pos < 45 and (pos // 3) % 2 == 0
    left_blink = f >= 50 and (f - 50) % 120 < 4
    draw_char(img, 90, talking_open and line % 2 == 0, left_blink, (230, 230, 240))
    draw_char(img, 550, talking_open and line % 2 == 1, False, (220, 235, 225))
    draw_sub(img, line)
    return img


def render(f):
    if f < 210:
        img = scene(f, "A")
    elif f < 420:
        img = scene(f, "B")
    elif f < 435:
        a = (f - 419) / 16
        img = scene(f, "B") * (1 - a) + scene(f, "C") * a
    else:
        img = scene(f, "C")
    return np.clip(img, 0, 255).astype(np.uint8)


def make_audio(path):
    t = np.arange(SR * N // FPS) / SR
    rng = np.random.default_rng(7)
    sig = rng.normal(0, 0.01, len(t))
    for line in range(10):
        m = (t >= line * 2.0) & (t < line * 2.0 + 1.5)
        freq = 220 if line % 2 == 0 else 330
        sig[m] += 0.25 * (0.7 + 0.3 * np.sin(2 * np.pi * 4 * t[m])) * np.sin(2 * np.pi * freq * t[m])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(sig, -1, 1) * 32767).astype("<i2").tobytes())


def make_video(path, wav):
    ff = av.ffmpeg_exe()
    vonly = path.with_name("video_only.mp4")
    proc = subprocess.Popen([ff, "-y", "-hide_banner", "-loglevel", "error",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", "60", "-pix_fmt", "yuv420p",
                             str(vonly)], stdin=subprocess.PIPE)
    for f in range(N):
        proc.stdin.write(render(f).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("合成動画のエンコードに失敗")
    # 映像だけ0.1秒遅らせて音声と多重化(エンコード時の-itsoffsetはタイムスタンプが正規化されて効かない)
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-itsoffset", "0.1", "-i", str(vonly),
                        "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                        str(path)])
    if r.returncode != 0:
        raise SystemExit("合成動画の多重化に失敗")


VTT = """WEBVTT
Kind: captions
Language: ja

00:00:00.000 --> 00:00:02.000 align:start position:0%
こんにちは<00:00:00.500><c>今日は</c>

00:00:02.000 --> 00:00:02.010 align:start position:0%
こんにちは今日は


00:00:02.010 --> 00:00:04.000 align:start position:0%
こんにちは今日は
投資の話を<00:00:03.000><c>します</c>

00:00:04.000 --> 00:00:06.000 align:start position:0%
投資の話をします
よろしく&amp;お願いします
"""


def small_gray(rgb):
    from PIL import Image
    return np.asarray(Image.fromarray(rgb).convert("L").resize((160, 90), Image.BILINEAR), np.float32)


def near(a, b, tol):
    return a is not None and abs(a - b) <= tol


def main() -> int:
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wav, mp4, vtt, out = td / "a.wav", td / "synthetic.mp4", td / "synthetic.ja.vtt", td / "out"
        make_audio(wav)
        make_video(mp4, wav)
        vtt.write_text(VTT, encoding="utf-8")

        # --- 字幕のロールアップ重複排除
        cues = av.parse_subtitles(vtt)
        check([c[2] for c in cues] == ["こんにちは今日は", "投資の話をします", "よろしく&お願いします"],
              f"字幕の重複排除が不正: {[c[2] for c in cues]}")
        check(av.subtitle_stats(cues, 60.0, None)["chars"] == 27, "字幕の文字数カウントが不正")

        # --- 動画情報
        meta = av.probe(mp4)
        check((meta["width"], meta["height"], meta["fps"]) == (W, H, 30.0), f"probe結果が不正: {meta}")
        check(av.snap_fps(23.98) == 24000 / 1001 and av.snap_fps(59.94) == 60000 / 1001, "fpsのNTSC補正が不正")

        # --- 本体をCLIとして実行(end-to-end)
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze_video.py"), "video", str(mp4),
                            "-o", str(out), "--interval", "5", "--strips", "8", "--subs", str(vtt)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("[FAIL] analyze_video.py が異常終了\n" + r.stderr[-3000:])
            return 1
        S = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        with open(out / "events.csv", encoding="utf-8-sig") as f:
            ev = list(csv.DictReader(f))
        for e in ev:
            for k in ("start_frame", "end_frame", "dur_frames"):
                e[k] = int(e[k])
            for k in ("net_bbox", "union_bbox", "main_bbox"):
                e[k] = json.loads(e[k])

        check(S["video"]["frames_analyzed"] == N, f"解析フレーム数 {S['video']['frames_analyzed']} != {N}")

        # --- イベント検出
        cuts = [e for e in ev if e["type"] == "cut"]
        trans = [e for e in ev if e["type"] == "transition"]
        check(len(cuts) == 1 and near(cuts[0]["start_frame"], 210, 1), f"全面カット(210)の検出が不正: {cuts}")
        check(len(trans) == 1 and near(trans[0]["start_frame"], 420, 2) and trans[0]["dur_frames"] >= 10,
              f"クロスフェード(420-435)の検出が不正: {trans}")
        for start in (90, 300):
            hits = [e for e in ev if near(e["start_frame"], start, 1) and e["animated"] == "1"]
            ok = False
            for e in hits:
                bb = e["main_bbox"]  # 同時に起きた口パク・字幕差し替えを除いた主な変化の範囲
                ok |= bool(bb) and e["dur_frames"] >= 6 and near((bb[0] + bb[2]) / 2, 0.5, 0.06) \
                    and (bb[1] + bb[3]) / 2 < 0.45
            check(ok, f"ポップイン({start}〜, 8フレーム)の検出が不正: {hits}")

        # --- 領域
        regs = S["regions"]

        def region_at(x, y):
            return [g for g in regs if g["bbox"][0] <= x <= g["bbox"][2] and g["bbox"][1] <= y <= g["bbox"][3]]

        sub = [g for g in region_at(0.5, 0.87) if g["guess"].startswith("字幕")]
        check(sub and near(sub[0]["burst_interval_median_s"], 2.0, 0.15),
              f"字幕帯(2秒ごと)の検出が不正: {region_at(0.5, 0.87)}")
        mouth = [g for g in region_at(90 / W, 200 / H) if g["guess"] == "口パク?"]
        check(mouth and mouth[0]["events_per_min"] >= 150 and near(mouth[0]["burst_interval_median_s"], 4.0, 0.2),
              f"左キャラの口パク検出が不正: {region_at(90 / W, 200 / H)}")
        blink = [g for g in region_at(72 / W, 131 / H) if g["guess"] == "まばたき?"]
        check(blink and near(blink[0]["burst_interval_median_s"], 4.0, 0.2),
              f"まばたき(4秒ごと)の検出が不正: {region_at(72 / W, 131 / H)}")

        # --- フレーム切り出しの番号ズレ(先頭0.1秒オフセットあり)
        A_pts = np.array([float(row["time"]) for row in csv.DictReader(open(out / "frame_metrics.csv"))])
        check(near(A_pts[0], 0.1, 0.01), f"先頭フレーム時刻が不正: {A_pts[0]}")
        # 前後のフレームと絵が違う(アニメーション中の)番号で、隣と取り違えていないかを見る
        for idx in (0, 91, 94, 302, 305, 425, 431, 599):
            got = av.grab_frames(mp4, A_pts, 30.0, idx, 1, W, H, W)
            if not len(got):
                errors.append(f"フレーム{idx}を切り出せない")
                continue
            g = small_gray(got[0])
            diffs = {k: float(np.abs(g - small_gray(render(k))).mean()) for k in (idx - 1, idx, idx + 1)
                     if 0 <= k < N}
            ok = diffs[idx] < 3 and (idx in (0, N - 1) or all(diffs[idx] < v for k, v in diffs.items() if k != idx))
            check(ok, f"フレーム{idx}の切り出しがズレている: {diffs}")
        strip = av.grab_frames(mp4, A_pts, 30.0, 88, 12, W, H, 320)
        check(len(strip) == 12, f"連番切り出しの枚数が不正: {len(strip)}")

        # --- 音声
        a = S["audio"]
        check(a and -40 < (a["integrated_lufs"] or 0) < -5, f"ラウドネスが不正: {a}")
        check(a and 9 <= a["pauses"] <= 11 and near(a["pause_median_s"], 0.5, 0.1), f"間の検出が不正: {a}")
        check(a and a["never_silent"], "常時ノイズ(BGM代わり)を検出できていない")

        # --- テンポ・字幕・出力ファイル
        t = S["tempo"]
        check(t["scene_changes"] == 2 and near(t["scene_interval_median_s"], 7.0, 0.2),
              f"場面転換の集計が不正: {t}")
        check(S["subtitles"] and S["subtitles"]["chars"] == 27, f"字幕統計が不正: {S['subtitles']}")
        for name in ("report.md", "regions.csv", "transcript.txt", "images/median_layout.png",
                     "images/change_heatmap.png", "images/timeline.png", "images/keyframes_01.jpg",
                     "images/interval_01.jpg"):
            check((out / name).exists(), f"出力がない: {name}")
        check(S["keyframes"] >= 3, f"キーフレームが少ない: {S['keyframes']}")
        strips = list((out / "images").glob("strip_*.jpg"))
        check(len(strips) >= 4, f"連番ストリップが少ない: {len(strips)}")
        check(any(s["why"] == "アニメーション" for s in S["strips"]), "アニメーションの連番ストリップがない")

        if errors:
            print("[FAIL]")
            for e in errors:
                print(f"  - {e}")
            return 1
        print(f"[OK] カット/フェード/ポップイン/字幕帯/口パク/まばたき/音声の間/フレーム番号の一致を確認 "
              f"(イベント{len(ev)}件, 領域{len(regs)}件, ストリップ{len(strips)}枚)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
