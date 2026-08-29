#!/usr/bin/env python3
"""make_short.py のエンドツーエンド検証。

合成した立ち絵PNGと台詞WAVから実際にMP4を書き出し、
尺・解像度・音声トラックの有無を確認する。GPU不要。

usage: python tests/test_make_short.py
"""

import subprocess
import sys
import tempfile
import wave
from types import SimpleNamespace
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import make_short

FPS = 30
RATE = 24000


def write_tone(path: Path, seconds: float, freq: float = 220.0) -> None:
    """口パク判定が動くよう、前半に音があり後半が無音のWAVを作る。"""
    t = np.arange(int(RATE * seconds)) / RATE
    wave_data = 0.6 * np.sin(2 * np.pi * freq * t).astype(np.float32)
    wave_data[len(wave_data) // 2 :] = 0.0  # 後半は無音 = 口を閉じるはず
    pcm = np.clip(wave_data * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def write_char(path: Path, color) -> None:
    img = Image.new("RGBA", (400, 700), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (300, 600), color), (50, 50))
    img.save(path)


def probe(ffmpeg: str, mp4: Path) -> dict:
    """ffprobeが無い環境もあるので、ffmpegのログから情報を拾う。"""
    res = subprocess.run([ffmpeg, "-i", str(mp4)], capture_output=True, text=True)
    return {"log": res.stderr}


def check(cond: bool, label: str) -> None:
    print(f"  {'OK  ' if cond else 'NG  '} {label}")
    if not cond:
        raise AssertionError(label)


def test_mouth_states() -> None:
    print("[1] 口パク生成")
    t = np.arange(RATE) / RATE
    samples = 0.6 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    samples[RATE // 2 :] = 0.0
    states = make_short.mouth_states(samples, RATE, FPS, make_short.DEFAULTS["mouth"])
    check(len(states) == FPS, f"フレーム数が一致する ({len(states)})")
    check(any(states[: FPS // 2]), "音がある前半は口が開く")
    check(not any(states[FPS // 2 + 2 :]), "無音の後半は口が閉じる")

    silent = make_short.mouth_states(np.zeros(RATE, np.float32), RATE, FPS,
                                     make_short.DEFAULTS["mouth"])
    check(not any(silent), "完全無音では口が開かない")


def test_wrap() -> None:
    print("[2] 字幕の折り返し")
    check(make_short.wrap_text("あいうえおかきくけこ", 4) ==
          ["あいうえ", "おかきく", "けこ"], "文字数で折り返す")
    check(make_short.wrap_text("いち\nに", 16) == ["いち", "に"], "明示的な改行を尊重する")
    check(make_short.wrap_text("", 16) == [""], "空文字でも落ちない")

    # 実測幅での折り返し: 文字数だけだと1080pxからはみ出していた回帰の防止
    font = make_short.resolve_font(None, int(1920 * 0.055))
    max_px = 1080 * 0.88
    text = "結論から言うと、いちばん大事なのは順番です"
    naive = make_short.wrap_text(text, 16)
    check(any(font.getlength(l) > max_px for l in naive),
          "文字数だけの折り返しは画面幅を超える(修正前の挙動)")
    fitted = make_short.wrap_text(text, 16, font, max_px)
    check(all(font.getlength(l) <= max_px for l in fitted),
          f"実測幅で折ると全行が画面内に収まる ({len(fitted)}行)")
    check("".join(fitted) == text.replace("\n", ""), "折り返しても文字が欠けない")

    # 半角のみの行は日本語より多く入る(文字数固定では入りきらない)
    ascii_lines = make_short.wrap_text("abcdefghijklmnopqrstuvwxyz", 40, font, max_px)
    check(all(font.getlength(l) <= max_px for l in ascii_lines), "半角文字でも収まる")

    # 禁則処理: 行頭に句読点が来ない
    k = make_short.wrap_text("人気声優・〇〇さん(39)、最高すぎると話題に", 13)
    check(all(not l[0] in "、。" for l in k), "行頭に句読点が来ない(ぶら下げ)")
    check("".join(k) == "人気声優・〇〇さん(39)、最高すぎると話題に", "禁則処理で文字が欠けない")


def test_rate_mismatch(tmp: Path) -> None:
    print("[3] サンプリングレート混在の検出")
    a, b = tmp / "a.wav", tmp / "b.wav"
    write_tone(a, 0.4)
    with wave.open(str(b), "wb") as w:  # 別レートのWAV
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(np.zeros(4800, dtype="<i2").tobytes())
    lines = []
    for p in (a, b):
        s, r = make_short.read_wav_mono(p)
        lines.append({"_samples": s, "_rate": r, "_sec": len(s) / r})
    try:
        make_short.build_voice_track(lines, 0.1, tmp / "out.wav")
        check(False, "混在はエラーになる")
    except make_short.ScriptError as e:
        check("サンプリングレート" in str(e), "分かるエラーメッセージが出る")


def test_end_to_end(tmp: Path) -> None:
    print("[4] 台本 → MP4 の書き出し")
    proj = tmp / "proj"
    (proj / "voice").mkdir(parents=True)
    (proj / "assets").mkdir(parents=True)

    durations = [0.8, 0.6]
    for i, sec in enumerate(durations, 1):
        write_tone(proj / "voice" / f"{i:03d}.wav", sec)
    write_char(proj / "assets" / "closed.png", (200, 120, 160, 255))
    write_char(proj / "assets" / "open.png", (220, 140, 180, 255))

    script = proj / "script.yaml"
    script.write_text(
        "title: テスト\n"
        f"fps: {FPS}\n"
        "size: [540, 960]\n"          # テストを速くするため小さめ
        "gap_sec: 0.2\n"
        "background:\n  color: '#202040'\n"
        "character:\n"
        "  closed: assets/closed.png\n"
        "  open: assets/open.png\n"
        "subtitle:\n  max_chars_per_line: 8\n"
        "lines:\n"
        "  - text: 'いちぎょうめのせりふ'\n    audio: voice/001.wav\n"
        "  - text: 'にぎょうめ'\n    audio: voice/002.wav\n",
        encoding="utf-8",
    )

    cfg, lines, base_dir = make_short.load_script(script)
    check(len(lines) == 2, "台詞が2本読めた")
    check(abs(lines[0]["_sec"] - durations[0]) < 0.01, "1本目の尺が正しい")
    check(base_dir == proj, "相対パスの基準が台本の場所になる")

    expected = sum(durations) + 0.2
    out = proj / "out.mp4"

    args = SimpleNamespace(script=str(script), output=str(out), probe=False)
    rc = make_short.build(args)
    check(rc == 0, "build が正常終了する")
    check(out.exists() and out.stat().st_size > 0, "MP4が出力された")

    # 字幕ブロックの下端がYouTubeのUI帯(下18%)にかからないこと
    st = cfg["subtitle"]
    h = cfg["size"][1]
    check(h - int(h * st["bottom_ratio"]) <= h * 0.82,
          "字幕の下端がUIの重なる範囲より上にある")

    log = probe(make_short.find_ffmpeg(), out)["log"]
    check("540x960" in log, "解像度が 540x960 になっている")
    check("Video: h264" in log, "H.264の映像トラックがある")
    check("Audio: aac" in log, "AACの音声トラックがある")

    import re
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log)
    check(m is not None, "尺が読み取れる")
    got = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    check(abs(got - expected) < 0.35, f"尺が台詞の合計とほぼ一致 ({got:.2f}s / 期待 {expected:.2f}s)")


def test_matome(tmp: Path) -> None:
    print("[6] まとめ型 (見出し+写真ズーム+反応カード)")
    proj = tmp / "matome"
    (proj / "voice").mkdir(parents=True)
    (proj / "assets").mkdir(parents=True)

    # 写真のダミー(グラデーション)
    grad = np.linspace(40, 220, 640, dtype=np.uint8)
    photo = np.stack([np.tile(grad, (480, 1))] * 3, axis=-1)
    Image.fromarray(photo).save(proj / "assets" / "photo1.png")

    write_tone(proj / "voice" / "001.wav", 0.7)

    script = proj / "script.yaml"
    script.write_text(
        f"fps: {FPS}\n"
        "size: [540, 960]\n"
        "gap_sec: 0.2\n"
        "banner:\n  text: 'テスト見出し、話題に'\n"
        "image:\n  zoom_to: 1.08\n  zoom_step_frames: 3\n"
        "lines:\n"
        "  - text: 'ナレーション'\n    audio: voice/001.wav\n"
        "    image: assets/photo1.png\n"
        "  - style: comment\n    text: 'これは伝説'\n    duration: 1.0\n"
        "  - style: comment\n    name: 'なんJ'\n    text: 'すごいわ'\n    duration: 1.0\n",
        encoding="utf-8",
    )

    cfg, lines, base_dir = make_short.load_script(script)
    check(lines[1]["_sec"] == 1.0, "duration行(無音)が読める")
    check(lines[1]["_rate"] == lines[0]["_rate"], "無音行は音声行とレートが揃う")

    ctxs = make_short.build_contexts(lines, cfg, base_dir)
    check(ctxs[0]["image"] is not None, "1行目で写真が出る")
    check(ctxs[2]["image"] == ctxs[0]["image"], "写真は後の行にも残る(スライド式)")
    check(len(ctxs[1]["comments"]) == 1 and len(ctxs[2]["comments"]) == 2,
          "コメントが積み上がる")
    check(ctxs[2]["comments"][1]["no"] == 2 and ctxs[2]["comments"][1]["name"] == "なんJ",
          "コメント番号と名前が正しい")

    out = proj / "out.mp4"
    rc = make_short.build(SimpleNamespace(script=str(script), output=str(out), probe=False))
    check(rc == 0 and out.exists() and out.stat().st_size > 0, "まとめ型のMP4が出力された")

    import re
    log = probe(make_short.find_ffmpeg(), out)["log"]
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log)
    got = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    expected = 0.7 + 1.0 + 1.0 + 0.2 * 2
    check(abs(got - expected) < 0.35, f"尺が想定と一致 ({got:.2f}s / 期待 {expected:.2f}s)")

    # ズームで絵が変わることをレンダラ単体で確認
    r = make_short.SceneRenderer(cfg, base_dir)
    f1 = r.render(ctxs[0], False, 1.0)
    f2 = r.render(ctxs[0], False, 1.08)
    check(f1.size == (540, 960), "フレームサイズが正しい")
    check(f1.tobytes() != f2.tobytes(), "ズームで絵が変わる")

    # max_visible を超えたら古いカードから消える
    cfg2 = make_short.deep_merge(cfg, {"comment": {"max_visible": 1}})
    ctxs2 = make_short.build_contexts(lines, cfg2, base_dir)
    check(len(ctxs2[2]["comments"]) == 1 and ctxs2[2]["comments"][0]["no"] == 2,
          "max_visibleで古いカードが消える")

    # 話題転換(talk行+image)でカードがリセットされる
    lines2 = [dict(l) for l in lines] + [
        {"style": "talk", "text": "次の話題", "image": "assets/photo1.png",
         "_sec": 1.0, "_samples": lines[0]["_samples"], "_rate": lines[0]["_rate"]},
        {"style": "comment", "text": "新しい反応", "_sec": 1.0,
         "_samples": lines[1]["_samples"], "_rate": lines[1]["_rate"]},
    ]
    ctxs3 = make_short.build_contexts(lines2, cfg, base_dir)
    check(len(ctxs3[3]["comments"]) == 0, "talk行+imageで前のカードが消える")
    check(len(ctxs3[4]["comments"]) == 1 and ctxs3[4]["comments"][0]["no"] == 1,
          "リセット後はカード番号が1から振り直される")


def test_missing_audio(tmp: Path) -> None:
    print("[5] 台本の不備を分かるエラーにする")
    script = tmp / "bad.yaml"
    script.write_text("lines:\n  - text: 'x'\n    audio: nope.wav\n", encoding="utf-8")
    try:
        make_short.load_script(script)
        check(False, "存在しない音声はエラーになる")
    except make_short.ScriptError as e:
        check("見つかりません" in str(e), "見つからない旨のメッセージが出る")

    script.write_text("lines: []\n", encoding="utf-8")
    try:
        make_short.load_script(script)
        check(False, "空の台本はエラーになる")
    except make_short.ScriptError as e:
        check("lines" in str(e), "linesが無い旨のメッセージが出る")

    script.write_text("lines:\n  - text: 'x'\n", encoding="utf-8")
    try:
        make_short.load_script(script)
        check(False, "audioもdurationも無い行はエラーになる")
    except make_short.ScriptError as e:
        check("duration" in str(e), "audioかdurationが必要な旨が出る")


def main() -> int:
    print("make_short 検証\n")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_mouth_states()
        test_wrap()
        test_rate_mismatch(tmp)
        test_end_to_end(tmp)
        test_matome(tmp)
        test_missing_audio(tmp)
    print("\n全項目パス")
    return 0


if __name__ == "__main__":
    sys.exit(main())
