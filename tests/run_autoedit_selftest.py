#!/usr/bin/env python3
"""auto_edit.py の自己検証。

前半: 純ロジック(無音ログ解析・カット計画・タイムライン写像・改行・
ASS/SRT生成)をffmpegなしで検証する。
後半: ffmpegがあれば合成動画(有音2秒+無音2秒の繰り返し)を作り、
analyze→render(テロップ+BGM+ダッキング)まで通して出力の長さを検証する。
ffmpegが無い環境では後半をスキップし、その旨を表示して成功扱いにする。

usage: python tests/run_autoedit_selftest.py
"""

import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import auto_edit
from auto_edit import TelopEvent

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def seg_eq(got, want, tol=1e-6):
    return (len(got) == len(want)
            and all(abs(a - c) < tol and abs(b - d) < tol
                    for (a, b), (c, d) in zip(got, want)))


# ---------------------------------------------------------------- 純ロジック検証

def test_parse_silencedetect():
    log = """
[silencedetect @ 0x1] silence_start: 2.005
[silencedetect @ 0x1] silence_end: 3.998 | silence_duration: 1.993
[silencedetect @ 0x1] silence_start: -0.01
[silencedetect @ 0x1] silence_end: 0.5 | silence_duration: 0.51
[silencedetect @ 0x1] silence_start: 7.5
"""
    got = auto_edit.parse_silencedetect(log, 8.0)
    check(got == [(2.005, 3.998), (0.0, 0.5), (7.5, 8.0)],
          f"silencedetect解析が想定外: {got}")


def test_build_keep_segments():
    # 8秒中、[2,4]と[7.5,8]が無音 → 残りに0.1秒パディング
    keeps = auto_edit.build_keep_segments(
        [(2.0, 4.0), (7.5, 8.0)], 8.0, pad=0.1, join_gap=0.15, min_keep=0.3)
    check(seg_eq(keeps, [(0.0, 2.1), (3.9, 7.6)]), f"keep区間が想定外: {keeps}")

    # パディングで接近した区間はjoin_gapで結合される
    keeps = auto_edit.build_keep_segments(
        [(1.0, 1.3)], 3.0, pad=0.1, join_gap=0.15, min_keep=0.3)
    check(seg_eq(keeps, [(0.0, 3.0)]), f"join_gap結合が効いていない: {keeps}")

    # min_keep未満の孤立区間は捨てる
    keeps = auto_edit.build_keep_segments(
        [(0.0, 1.0), (1.2, 5.0)], 5.0, pad=0.0, join_gap=0.05, min_keep=0.3)
    check(keeps == [], f"min_keep除去が効いていない: {keeps}")

    # 無音なし → 全編1区間
    keeps = auto_edit.build_keep_segments([], 5.0, 0.1, 0.15, 0.3)
    check(keeps == [(0.0, 5.0)], f"無音なしの扱いが想定外: {keeps}")


def test_remap():
    keeps = [(0.0, 2.0), (4.0, 6.0)]
    check(auto_edit.output_duration(keeps) == 4.0, "output_durationが想定外")
    for t, want in [(0.0, 0.0), (1.5, 1.5), (2.0, 2.0), (3.0, 2.0),
                    (4.0, 2.0), (5.0, 3.0), (6.0, 4.0), (99.0, 4.0)]:
        got = auto_edit.remap_time(t, keeps)
        check(abs(got - want) < 1e-9, f"remap_time({t})={got} (期待{want})")

    # カットをまたぐ字幕は縮み、丸ごとカット内の字幕は消える
    events = [TelopEvent(1.5, 4.5, "またぐ"), TelopEvent(2.5, 3.5, "消える"),
              TelopEvent(4.2, 4.4, "短い")]
    out = auto_edit.remap_events(events, keeps, min_duration=1.0)
    check(len(out) == 2, f"remap_eventsの件数が想定外: {[e.text for e in out]}")
    # またぐ字幕は(1.5,2.5)に縮み、さらに次イベント開始(2.2)で切られる
    check(abs(out[0].start - 1.5) < 1e-6 and abs(out[0].end - 2.2) < 1e-6,
          f"またぎ字幕の写像が想定外: {out[0]}")
    # min_durationで延長されるが上限は動画終端
    check(abs(out[1].start - 2.2) < 1e-6 and abs(out[1].end - 3.2) < 1e-6,
          f"min_duration延長が想定外: {out[1]}")


def test_wrap_and_split():
    lines = auto_edit.wrap_lines("今日の天気です。晴れます。", 10)
    check(lines == ["今日の天気です。", "晴れます。"],
          f"句読点改行が想定外: {lines}")

    lines = auto_edit.wrap_lines("あ" * 25, 10)
    check(lines == ["あ" * 10, "あ" * 10, "あ" * 5], f"強制改行が想定外: {lines}")

    # 2行目が1文字だけ残るような改行は半々に割ってバランスさせる
    lines = auto_edit.wrap_lines("本日の主なニュースをお伝えします", 15)
    check(lines == ["本日の主なニュー", "スをお伝えします"],
          f"バランス改行が想定外: {lines}")

    # 半角は0.5幅で数える
    check(auto_edit.display_width("abcd") == 2.0, "半角幅の計算が想定外")

    # 2行を超えると時間按分で複数テロップに分かれる
    ev = TelopEvent(0.0, 6.0, "一二三四五六七八。九十。")
    out = auto_edit.split_telop(ev, max_chars=4, max_lines=2, strip_period=True)
    check(len(out) == 2, f"分割数が想定外: {[e.text for e in out]}")
    # 各テロップ末尾の「。」は放送字幕の慣習どおり落とす(行中の「。」は残る)
    check(out[0].text == "一二三四\\N五六七八", f"1枚目が想定外: {out[0].text!r}")
    check(out[1].text == "九十", f"2枚目(。除去)が想定外: {out[1].text!r}")
    check(abs(out[-1].end - 6.0) < 1e-9, "最終イベントの終端がずれた")
    check(out[0].end == out[1].start and out[0].start == 0.0, "按分が連続でない")


def test_srt_roundtrip():
    events = [TelopEvent(0.5, 2.25, "こんにちは"), TelopEvent(3.0, 4.5, "テスト")]
    text = auto_edit.format_srt(events)
    back = auto_edit.parse_srt(text)
    check(len(back) == 2 and back[0].text == "こんにちは"
          and abs(back[1].start - 3.0) < 1e-3, f"SRT往復が想定外: {back}")
    # 番号行なし・複数行本文も読める
    back = auto_edit.parse_srt("00:00:01,000 --> 00:00:02,000\n上段\n下段\n")
    check(back and back[0].text == "上段 下段", f"SRT読み込みが想定外: {back}")


def test_ass_build():
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG)
    events = [TelopEvent(0.0, 2.0, "ニュース速報{}"), TelopEvent(2.0, 65.5, "二枚目")]
    ass = auto_edit.build_ass(events, cfg, 1920, 1080, 70.0, title_text="番組名")
    check("PlayResX: 1920" in ass and "PlayResY: 1080" in ass, "PlayResが無い")
    check(ass.count("Dialogue:") == 2 * 3 + 1,
          f"イベント数が想定外(帯+アクセント+本文x2+タイトル): {ass.count('Dialogue:')}")
    check("1:05.50" in ass, "ASS時刻書式が想定外")
    check("ニュース速報()" in ass and "速報{}" not in ass, "波括弧のエスケープ漏れ")
    check("番組名" in ass, "タイトルが入っていない")
    check(auto_edit.ass_color("FFB400", 1.0) == "&H0000B4FF", "色変換が想定外")
    check(auto_edit.ass_color("000000", 0.5) == "&H80000000", "透明度変換が想定外")


def test_filter_script():
    keeps = [(0.0, 2.1), (3.9, 7.6)]
    # テロップ+BGM+ダッキング
    fs = auto_edit.build_filter_script(
        keeps, True, "telop.ass",
        {"volume_db": -18, "fade": 1.5, "ducking": True}, 5.8)
    for frag in ["concat=n=2:v=1:a=1[vc][ac]", "subtitles=telop.ass[vout]",
                 "sidechaincompress", "amix=inputs=2:duration=first:normalize=0[aout]",
                 "volume=-18dB", "afade=t=out:st=4.300:d=1.5"]:
        check(frag in fs, f"filter_scriptに {frag} が無い")
    # 最小構成 (テロップ・BGMなし)
    fs = auto_edit.build_filter_script([(0.0, 5.0)], False, "", None, 5.0)
    check("[vc]null[vout]" in fs and "[ac]anull[aout]" in fs,
          f"最小構成のfilter_scriptが想定外:\n{fs}")


# ---------------------------------------------------------------- ffmpeg統合検証

def write_tone_wav(path: Path, pattern: list, sr: int = 16000,
                   freq: float = 440.0, amp: float = 0.6):
    """pattern = [(秒数, 発音するか)] のサイン波WAVを書く(依存なし)。"""
    frames = bytearray()
    for seconds, on in pattern:
        n = int(seconds * sr)
        for i in range(n):
            v = amp * math.sin(2 * math.pi * freq * i / sr) if on else 0.0
            frames += struct.pack("<h", int(v * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def test_e2e(tmp: Path):
    # 声2秒 → 無音2秒 → 声2秒 → 無音2秒 の8秒動画
    voice = tmp / "voice.wav"
    write_tone_wav(voice, [(2, True), (2, False), (2, True), (2, False)])
    src = tmp / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=8:r=24",
         "-i", str(voice), "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-shortest", str(src)], check=True)

    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG)
    plan = auto_edit.analyze(src, cfg)
    keeps = [tuple(k) for k in plan["keep_segments"]]
    check(len(keeps) == 2, f"e2e: keep区間数が想定外: {keeps}")
    total = auto_edit.output_duration(keeps)
    check(3.5 < total < 5.0, f"e2e: 編集後の想定長が想定外: {total}")

    # カット前タイムラインの字幕(2つ目はカットをまたぐ)
    srt_events = [TelopEvent(0.2, 1.8, "一つ目のテロップです"),
                  TelopEvent(4.2, 5.8, "二つ目、ニュース風の帯付き")]
    events = auto_edit.make_telop_events(srt_events, plan, cfg)
    check(len(events) == 2, f"e2e: テロップ数が想定外: {events}")

    bgm = tmp / "bgm.wav"
    write_tone_wav(bgm, [(3, True)], freq=220.0, amp=0.2)  # 短い→ループ検証
    out = tmp / "out.mp4"
    wd = tmp / "work"
    wd.mkdir(exist_ok=True)
    auto_edit.render(src, plan, events, cfg, wd, out, bgm_path=bgm,
                     title_text="テスト番組")

    got = ffprobe_duration(out)
    check(abs(got - total) < 0.35, f"e2e: 出力長 {got} が計画 {total} とずれ")
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    check("video" in streams and "audio" in streams, f"e2e: ストリーム欠落: {streams}")


def main() -> int:
    test_parse_silencedetect()
    test_build_keep_segments()
    test_remap()
    test_wrap_and_split()
    test_srt_roundtrip()
    test_ass_build()
    test_filter_script()

    ran_e2e = False
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        tmp = Path(tempfile.mkdtemp(prefix="autoedit_selftest_"))
        test_e2e(tmp)
        ran_e2e = True
    else:
        print("[skip] ffmpegが無いため統合検証をスキップ(純ロジックのみ検証)")

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    scope = "純ロジック+ffmpeg統合" if ran_e2e else "純ロジック"
    print(f"[OK] auto_edit自己検証({scope})に成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
