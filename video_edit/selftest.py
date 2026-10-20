#!/usr/bin/env python3
"""video_edit の検証（ネットワーク・GPU不要）。

合成した「しゃべり動画」と、時刻が分かっている文字起こしを使って
下書き作成 → Markdownプランの手直し → カット計算 → 書き出し を通しで動かし、
・言いよどみ/言い直し/長い間がカットされるか
・映像と音声がずれないか（同じ瞬間に入れた白フラッシュとノイズ音で確認）
・テロップ・見出しが画面に出るか
・HDR素材の色変換が動くか
を確かめる。

usage: python video_edit/selftest.py
"""

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_edit  # noqa: E402
import prepare  # noqa: E402
import render  # noqa: E402
import vedit_common  # noqa: E402

W, H, FPS, DUR = 1280, 720, 30, 15.0
SENTS = [
    [("えー、", 0.5, 0.9), ("こんにちは、", 1.0, 1.6), ("まどかです。", 1.7, 2.4)],
    [("今日は", 3.4, 3.8), ("ジャーナリング", 3.85, 4.6), ("について", 4.65, 5.0), ("話します。", 5.05, 5.8)],
    [("それでは", 6.0, 6.5), ("はじめ", 6.55, 7.0)],
    [("それでは", 7.8, 8.3), ("はじめます。", 8.35, 9.2)],
    [("ポイントは", 12.0, 12.6), ("三つ", 12.65, 13.0), ("あります。", 13.05, 13.8)],
]
MARKS = [1.2, 4.0, 8.8, 12.8]  # 白フラッシュ＋ノイズ音を同時に入れる時刻
FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'NG'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAIL.append(name)


def make_source(path: Path, hdr=False, dur=DUR):
    sr = 48000
    t = np.arange(int(dur * sr)) / sr
    x = np.random.default_rng(1).normal(0, 0.0003, len(t))
    for sent in SENTS:
        for _, a, b in [w for w in sent if w[2] <= dur]:
            m = (t >= a) & (t < b)
            x[m] += 0.25 * sum(np.sin(2 * np.pi * 180 * k * t[m]) / k for k in range(1, 6))
    for mk in [m for m in MARKS if m < dur - 0.1]:
        i = int(mk * sr)
        x[i:i + 480] = np.random.default_rng(2).choice([-0.95, 0.95], 480)
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    color = ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"] if hdr else []
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
                          "-r", str(FPS), "-i", "-", "-i", str(wav), "-c:v", "libx264", "-crf", "18",
                          "-preset", "veryfast", "-pix_fmt", "yuv420p", *color, "-c:a", "aac", "-b:a", "192k",
                          "-shortest", str(path)], stdin=subprocess.PIPE)
    marks = {int(round(m * FPS)) for m in MARKS if m < dur - 0.1}
    for n in range(int(dur * FPS)):
        f = np.full((H, W), 60, np.uint8)
        x0 = 100 + (n * 7) % (W - 300)
        f[250:450, x0:x0 + 200] = 120
        if n in marks:
            f[:] = 255
        p.stdin.write(f.tobytes())
    p.stdin.close()
    assert p.wait() == 0


def fake_transcript():
    segs = []
    for i, sent in enumerate(SENTS):
        segs.append({"id": i, "start": sent[0][1], "end": sent[-1][2], "text": "".join(w for w, _, _ in sent),
                     "words": [{"start": a, "end": b, "word": w, "p": 0.9} for w, a, b in sent]})
    return {"model": "fake", "language": "ja", "duration": DUR, "segments": segs}


def decode_luma(path: Path):
    info = vedit_common.probe(path)
    w, h = info["width"], info["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w), info


def decode_rgb_frame(path: Path, sec: float):
    info = vedit_common.probe(path)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{sec:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(info["height"], info["width"], 3)


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for mod in (build_edit, prepare, render):
            mod.PROJECTS_DIR = td / "projects"
            mod.WORK_DIR = td / "work"
        src = td / "talk.mp4"
        print("合成動画を作成中…")
        make_source(src)
        name = "selftest"
        prepare.main([str(src), "--name", name])
        proj = td / "projects" / name
        vedit_common.save_json(proj / "transcript.json", fake_transcript())

        print("\n下書き作成:")
        build_edit.main(["--name", name, "--init"])
        plan = (proj / "edit_plan.md").read_text(encoding="utf-8")
        check("言いよどみ「えー」が自動でカット済み", "~~えー、~~こんにちは" in plan)
        check("言い直し候補のメモ", "言い直し候補" in plan.split("- 003")[1].split("\n")[0])

        # 人が直す想定の編集: 言い直し(003)を消す・強調・見出し・大テロップ・ズーム間隔
        plan = plan.replace("- 003 (00:06) それでははじめ ［メモ：言い直し候補（次の文とほぼ同じ）］",
                            "- 003 (00:06) ~~それでははじめ~~")
        plan = plan.replace("- 002 (00:03) 今日はジャーナリングについて話します。",
                            "### ■ 本題\n- 002 (00:03) 今日は**ジャーナリング**について話します。")
        plan = plan.replace("- 005 (00:12) ポイントは三つあります。", "- 005 (00:12) ポイントは三つあります。\n> ポイントは**3つ**！")
        plan = plan.replace("- ズーム間隔: 6", "- ズーム間隔: 2")
        (proj / "edit_plan.md").write_text(plan, encoding="utf-8")
        check("手直し（取り消し線・見出し・大テロップ）を反映", "~~それでははじめ~~" in plan and "### ■ 本題" in plan)

        print("\nカット計算:")
        edit = build_edit.build(name)
        segs = edit["segments"]
        span = [(s["f0"] / FPS, s["f1"] / FPS) for s in segs]
        check("区間数4（長い間・言い直しで区切られる）", len(segs) == 4, str([(round(a, 2), round(b, 2)) for a, b in span]))
        inside = lambda t: any(a <= t < b for a, b in span)
        check("言いよどみ（0.5-0.9秒）がカット", not inside(0.7))
        check("言い直し（6.0-7.0秒）がカット", not any(inside(t) for t in (6.2, 6.8)))
        check("長い無音（9.4-11.8秒）がカット", not any(inside(t) for t in (10.0, 11.0)))
        check("話している所は残る", all(inside(t) for t in (1.3, 2.2, 3.5, 5.7, 7.9, 9.1, 12.1, 13.7)))
        exp = sum(b - a for a, b in span)
        check("仕上がりの長さ 約7.9秒", 7.3 <= edit["out_sec"] <= 8.5, f"{edit['out_sec']}秒")
        check("寄りの区間がある", any(s["zoom"] > 1 for s in segs))
        ass = (proj / "telops.ass").read_text(encoding="utf-8-sig")
        check("強調ワードに色指定", "{\\1c&H4DE1FF&}ジャーナリング{\\1c&HFFFFFF&}" in ass)
        check("見出し・大テロップ", ",Heading,," in ass and ",Impact,," in ass)
        check("句点はテロップから除く", "まどかです。" not in ass and "まどかです" in ass)

        print("\n書き出し（確認用）:")
        out = td / "preview.mp4"
        render.main(["--name", name, "--preview", "--source", str(src), "-o", str(out)])
        lum, info = decode_luma(out)
        dur = info["duration"]
        check("書き出しの長さ", abs(len(lum) / FPS - edit["out_sec"]) < 0.05, f"{len(lum)}フレーム / {edit['out_sec']}秒")
        flash = [i for i in range(len(lum)) if lum[i].mean() > 200]
        groups = []
        for f in flash:
            if not groups or f - groups[-1][-1] > 1:
                groups.append([f])
            else:
                groups[-1].append(f)
        vt = [g[0] / FPS for g in groups]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
        a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
        a = np.abs(np.diff(a))  # 高い音の成分だけ見る（ノイズ音は高音が強く、声役の低い音は弱い）
        env = np.convolve(a, np.ones(240) / 240, mode="same")
        pk = env > 0.5 * env.max()
        at = []
        for i in np.where(pk)[0]:
            if not at or i / 48000 - at[-1] > 0.2:
                at.append(i / 48000)
        check("フラッシュ4回が残っている", len(vt) == 4, str([round(v, 3) for v in vt]))
        check("ノイズ音4回が残っている", len(at) == 4, str([round(v, 3) for v in at]))
        if len(vt) == len(at) == 4:
            off = [abs(v - x) for v, x in zip(vt, at)]
            check("映像と音声のずれ 1フレーム未満", max(off) < 1 / FPS, f"最大 {max(off) * 1000:.0f}ms")
        # テロップと見出し（2本目の文の途中、フラッシュを避けた時刻）
        t_mid = (segs[1]["o0"] + 10) / FPS
        fr = decode_rgb_frame(out, t_mid)
        bottom = fr[int(fr.shape[0] * 0.78):, :, :].astype(int)
        white = ((bottom > 225).all(axis=2)).mean()
        check("テロップ（下部の白文字）", white > 0.003, f"{white:.4f}")
        tl = fr[:int(fr.shape[0] * 0.15), :int(fr.shape[1] * 0.35)].astype(int)
        pink = ((abs(tl[..., 0] - 0xE8) < 30) & (abs(tl[..., 1] - 0x73) < 30) & (abs(tl[..., 2] - 0x8A) < 30)).mean()
        check("見出し（左上のテーマ色の帯）", pink > 0.02, f"{pink:.3f}")
        Path(HERE.parent / "work").mkdir(exist_ok=True)
        try:
            import shutil
            shutil.copy(out, HERE.parent / "work" / "selftest_preview.mp4")
        except OSError:
            pass

        print("\nHDR素材の色変換:")
        hsrc = td / "hdr.mp4"
        make_source(hsrc, hdr=True, dur=2.0)
        try:
            prepare.main([str(hsrc), "--name", "hdr"])
            pinfo = vedit_common.probe(td / "work" / "hdr" / "proxy.mp4")
            check("HDR→SDR変換したプロキシ", pinfo["color_transfer"] in (None, "bt709") and pinfo["height"] == 540,
                  f"{pinfo['color_transfer']} {pinfo['width']}x{pinfo['height']}")
        except Exception as e:  # noqa: BLE001
            check("HDR→SDR変換したプロキシ", False, str(e)[:200])

    print("\n結果:", "すべてOK" if not FAIL else f"NG {len(FAIL)}件: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
