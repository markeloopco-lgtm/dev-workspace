#!/usr/bin/env python3
"""auto_edit.py の自己検証。

前半: 純ロジック(無音ログ解析・カット計画・タイムライン写像・改行・
ASS/SRT生成)をffmpegなしで検証する。
後半: ffmpegがあれば合成動画(有音2秒+無音2秒の繰り返し)を作り、
analyze→render(テロップ+BGM+ダッキング)まで通して出力の長さを検証する。
ffmpegが無い環境では後半をスキップし、その旨を表示して成功扱いにする。

usage: python tests/run_autoedit_selftest.py
"""

import copy
import math
import re
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

    # 小書き仮名・長音符を行頭にしない (「…がき/ょう…」を防ぐ)
    for text, limit in [("自動編集ツールがきょう公開されました", 15),
                        ("シャッターチャンスを逃さないコツ", 12)]:
        lines = auto_edit.wrap_lines(text, limit)
        check(all(l[0] not in auto_edit.KINSOKU_HEAD for l in lines[1:]),
              f"禁則文字が行頭に来た: {lines}")

    # 2行に収まる長さは行長を揃える(貪欲だと2行目が極端に短くなる)
    lines = auto_edit.wrap_lines("本日の主なニュースをお伝えします", 15)
    check(lines == ["本日の主なニュー", "スをお伝えします"],
          f"バランス改行が想定外: {lines}")
    lines = auto_edit.wrap_lines("Live2Dモデルの量産計画が明らかになりました", 15)
    widths = [auto_edit.display_width(l) for l in lines]
    check(len(lines) == 2 and abs(widths[0] - widths[1]) <= 1.0,
          f"半角混じりのバランス改行が想定外: {lines} {widths}")
    check(all(w <= 15 for w in widths), f"最大幅を超えている: {widths}")

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


def test_single_line_split():
    """max_lines=1 のとき、語の途中で切らず文節の切れ目で分ける。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    check(cfg["telop"]["max_lines"] == 1, "talk が1行設定になっていない")
    limit = auto_edit.line_budget(cfg, 1920, 1080)

    cases = [
        ("前職ではどのくらいの年収でしたか", ["前職ではどのくらいの", "年収でしたか"]),
        ("今日はエンジニアの転職について聞いていきます",
         ["今日はエンジニアの", "転職について聞いていきます"]),
        ("新卒で入った会社では、およそ380万円でした",
         ["新卒で入った会社では、", "およそ380万円でした"]),
    ]
    for text, want in cases:
        got = [e.text for e in auto_edit.split_telop(
            TelopEvent(0, 5, text), limit, 1, True)]
        check(got == want, f"分け方が想定外: {text} → {got}")

    # 1行設定では行長を揃えない (揃えると語の途中で切れやすい)
    check(auto_edit.wrap_lines("前職ではどのくらいの年収でしたか", limit,
                               balance=False)[0] == "前職ではどのくらいの",
          "balance=False が効いていない")

    # 助詞の後ろでも、続きが漢字/カタカナでなければ切らない
    check(auto_edit._natural_break("年収でした", 3) is False,
          "「で/した」を切れ目と判定している")
    check(auto_edit._natural_break("どのくらいの年収", 6) is True,
          "「の/年収」を切れ目と判定していない")


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
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "news")
    events = [TelopEvent(0.0, 2.0, "ニュース速報{}"), TelopEvent(2.0, 65.5, "二枚目")]
    ass = auto_edit.build_ass(events, cfg, 1920, 1080, 70.0, title_text="番組名",
                              font="TestFont")
    check("PlayResX: 1920" in ass and "PlayResY: 1080" in ass, "PlayResが無い")
    check(ass.count(",Telop,") == len(events),
          f"本文イベント数が想定外: {ass.count(',Telop,')}")
    check(ass.count(",Shape,") >= 2 * 2 + 1, "帯・アクセント・タイトルバーが足りない")
    check("1:05.50" in ass, "ASS時刻書式が想定外")
    check("ニュース速報()" in ass and "速報{}" not in ass, "波括弧のエスケープ漏れ")
    check("番組名" in ass, "タイトルが入っていない")
    def fontnames(text):
        return {l.split(",")[1] for l in text.splitlines() if l.startswith("Style: ")}
    check(fontnames(ass) == {"TestFont"}, f"フォント名が反映されていない: {fontnames(ass)}")
    # font: auto をそのままフォント名として書き出さない (候補の先頭に解決する)
    auto = auto_edit.build_ass(events, cfg, 1920, 1080, 70.0)
    check(fontnames(auto) == {cfg["style"]["font_candidates"][0]},
          f"font: auto の解決が想定外: {fontnames(auto)}")
    check(auto_edit.ass_color("FFB400", 1.0) == "&H0000B4FF", "色変換が想定外")
    check(auto_edit.ass_color("000000", 0.5) == "&H80000000", "透明度変換が想定外")

    # 寸法は画面高さに追従する (1080p と 720p で比率が一致)
    small = auto_edit.build_ass(events, cfg, 1280, 720, 70.0, font="TestFont")
    def fontsize(text):
        line = [l for l in text.splitlines() if l.startswith("Style: Telop,")][0]
        return float(line.split(",")[2])
    check(abs(fontsize(small) / 720 - fontsize(ass) / 1080) < 0.002,
          "解像度で文字サイズ比が変わってしまう")


def test_ass_gradient_band():
    """帯のグラデーションは矩形を下端まで重ねて作る(隣接だと継ぎ目に縞が出る)。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "news")
    cfg["band"].update({"gradient": 0.3, "gradient_steps": 8, "opacity": 0.8})
    ass = auto_edit.build_ass([TelopEvent(0.0, 2.0, "帯")], cfg, 1920, 1080, 5.0,
                              font="TestFont")
    rects = re.findall(r"\\p1\\pos\(0,0\)[^}]*}m \d+ (\d+) l \d+ \d+ \d+ (\d+)",
                       ass)
    band = [(int(a), int(b)) for a, b in rects]
    check(len(band) >= 8, f"グラデーションの段数が反映されていない: {len(band)}")
    bottoms = {b for _, b in band[:8]}
    check(bottoms == {1080}, f"帯の矩形が下端(1080)で揃っていない: {bottoms}")
    tops = [t for t, _ in band[:8]]
    check(tops == sorted(tops), f"帯の矩形が上から順に積まれていない: {tops}")

    # 累積した不透明度が設定値に一致する (1-(1-a1)(1-a2)... == opacity)
    alphas = [int(m, 16) for m in re.findall(r"\\1a&H([0-9A-F]{2})&", ass)]
    acc = 1.0
    for a in alphas[:8]:
        acc *= 1.0 - (1.0 - a / 255.0)
    check(abs((1.0 - acc) - 0.8) < 0.02,
          f"帯の最終的な不透明度が設定と合わない: {1 - acc:.3f}")


def test_reading_speed():
    """表示時間が読める速さ(毎秒4文字)に合わせて伸びる。"""
    keeps = [(0.0, 30.0)]
    events = [TelopEvent(0.0, 0.5, "あ" * 20)]   # 20文字 → 5秒必要
    out = auto_edit.remap_events(events, keeps, min_duration=1.2,
                                 max_duration=6.5, reading_speed=4.0)
    check(abs(out[0].end - 5.0) < 1e-6, f"読み速度での延長が想定外: {out[0].end}")

    events = [TelopEvent(0.0, 20.0, "短い")]     # 上限6.5秒で頭打ち
    out = auto_edit.remap_events(events, keeps, 1.2, 6.5, 4.0)
    check(abs(out[0].end - 6.5) < 1e-6, f"最大表示時間が効いていない: {out[0].end}")


def test_presets():
    """preset で様式が切り替わる (business=座布団なし / talk,news=座布団あり)。"""
    biz = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "business")
    news = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "news")
    talk = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    check(biz["band"]["enabled"] is False, "business preset で座布団が消えていない")
    check(talk["band"]["enabled"] is False, "talk preset で座布団が消えていない")
    check(news["band"]["enabled"] is True, "news preset で座布団が出ていない")
    # マコなり社長系の実測値: 紺の縁取り + 太め
    check(biz["style"]["outline_color"] == "294069", "business の縁取り色が想定外")
    check(biz["style"]["outline_em"] > news["style"]["outline_em"],
          "business の縁取りが news より太くない")
    # 上書きしていない項目は元の値のまま残る
    check(biz["encode"]["crf"] == news["encode"]["crf"], "preset適用で無関係な値が壊れた")
    check(auto_edit.deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}}) ==
          {"a": {"x": 1, "y": 9}}, "deep_mergeが想定外")

    # 座布団なしのときは下端揃え(\an2)、ありのときは中央揃え(\an5)
    ev = [TelopEvent(0.0, 2.0, "テスト")]
    check("\\an2" in auto_edit.build_ass(ev, biz, 1920, 1080, 5.0, font="F"),
          "座布団なしで下端揃えになっていない")
    check("\\an5" in auto_edit.build_ass(ev, news, 1920, 1080, 5.0, font="F"),
          "座布団ありで中央揃えになっていない")


def test_talk_preset():
    """年収チャンネル系: 話者色分けの座布団・テキスト幅に合わせた角丸・詰めたカット。"""
    talk = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    check(talk["band"]["enabled"] is False, "talk の座布団が無効になっていない")
    check(talk["jetcut"]["keep_padding_frames"] == 1.5,
          "talk のカット余白がフレーム指定になっていない")
    # 発話の前後に1.5フレームずつ残す。カット後はその2つが隣り合うので、
    # つなぎ目に残る無音はちょうど3フレームになる(消える区間の長さではない)
    pad = talk["jetcut"]["keep_padding_frames"] / 30.0
    keeps = auto_edit.build_keep_segments(
        [(2.0, 4.0)], 6.0, pad, talk["jetcut"]["join_gap"], talk["jetcut"]["min_keep"])
    kept_silence = (keeps[0][1] - 2.0) + (4.0 - keeps[1][0])
    check(abs(kept_silence * 30 - 3.0) < 0.01,
          f"つなぎ目に残る無音が3フレームでない: {kept_silence * 30:.2f}f")
    # join_gap は2フレーム以下 (これより大きいと3フレームの間まで結合されてしまう)
    check(talk["jetcut"]["join_gap"] < 3 / 30.0,
          "join_gap が大きすぎて狙った間隔でカットされない")

    # 座布団なしなので、発言者は文字色で分ける
    check(talk["speaker_color_target"] == "text",
          "talk の話者色が文字色に割り当てられていない")
    guest = auto_edit.build_ass([TelopEvent(0, 2, "はい", "ゲスト")], talk,
                                1920, 1080, 5.0, font="F")
    check(f"\\1c{auto_edit.ass_color(talk['speakers']['ゲスト'])}" in guest,
          "話者ごとの文字色が反映されていない")
    host = auto_edit.build_ass([TelopEvent(0, 2, "はい", "株本")], talk,
                               1920, 1080, 5.0, font="F")
    check(guest != host, "話者が変わっても見た目が同じ")

    # 文字色に使うので、話者の色は明るいこと (暗いと読めない)
    for name, color in talk["speakers"].items():
        check(auto_edit.relative_luminance(color) >= 0.30,
              f"話者 {name} の文字色が暗すぎる: {color}")
    check(auto_edit.warn_speaker_colors(talk) == [], "既定色で警告が出ている")
    dark = copy.deepcopy(talk)
    dark["speakers"] = {"誰か": "101010"}
    check(auto_edit.warn_speaker_colors(dark), "暗すぎる文字色を検出できていない")

    # outline 指定なら縁取りに反映される
    outlined = copy.deepcopy(talk)
    outlined["speaker_color_target"] = "outline"
    ass = auto_edit.build_ass([TelopEvent(0, 2, "はい", "ゲスト")], outlined,
                              1920, 1080, 5.0, font="F")
    check(f"\\3c{auto_edit.ass_color(talk['speakers']['ゲスト'])}" in ass,
          "outline 指定で縁取りに反映されない")

    # 座布団を有効に戻したときは、文字幅に追従する角丸ボックスになる
    boxed = copy.deepcopy(talk)
    boxed["band"]["enabled"] = True
    check(boxed["band"]["fit_width"] and boxed["band"]["corner_radius_em"] > 0,
          "座布団を有効にしても文字幅追従・角丸になっていない")
    check(boxed["band"]["gradient"] == 0, "talk の座布団にグラデーションが入っている")

    def band_width(text):
        out = auto_edit.build_ass([TelopEvent(0, 2, text, "株本")], boxed,
                                  1920, 1080, 5.0, font="F")
        xs = [int(v) for v in re.findall(r"\\p1\\pos\(0,0\)[^}]*}m (\d+) ", out)]
        return 1920 - 2 * min(xs)
    check(band_width("短い") < band_width("こちらはずっと長い発言になります"),
          "座布団の幅が文字数に追従していない")

    # 座布団に使うなら白文字が沈まないよう暗い色でないと警告する
    boxed_band = copy.deepcopy(boxed)
    boxed_band["speaker_color_target"] = "band"
    boxed_band["speakers"] = {"誰か": "FFEE88"}
    check(auto_edit.warn_speaker_colors(boxed_band), "明るい座布団色を検出できていない")


def fs_of(ass_text):
    """本文Dialogueに乗った \\fs の値(無ければStyleの値)を返す。"""
    body = [l for l in ass_text.splitlines() if l.startswith("Dialogue: 3,")][0]
    m = re.search(r"\\fs([0-9.]+)", body)
    if m:
        return float(m.group(1))
    style = [l for l in ass_text.splitlines() if l.startswith("Style: Telop,")][0]
    return float(style.split(",")[2])


def test_scenes():
    """行頭の記号でシーンを切り替え、フォント・大きさ・色を差し替える。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    ev = auto_edit.prepare_event(TelopEvent(0, 2, "株本: !それ盛ってませんか"), cfg)
    check(ev.speaker == "株本" and ev.scene == "punch"
          and ev.text == "それ盛ってませんか", f"シーン指定の解釈が想定外: {ev}")
    check(auto_edit.prepare_event(TelopEvent(0, 2, "#結論はこれです"), cfg).scene
          == "point", "「#」で要点シーンにならない")
    check(auto_edit.prepare_event(TelopEvent(0, 2, "普通の発言"), cfg).scene == "",
          "記号が無いのにシーン扱いになっている")
    # 文末が「？」の行は自動で質問シーンになる
    check(auto_edit.prepare_event(TelopEvent(0, 2, "いくらでした？"), cfg).scene
          == "question", "質問の自動判定が効いていない")

    # 拡大され、シーン専用フォントに切り替わる
    normal = auto_edit.build_ass([TelopEvent(0, 2, "それ盛ってませんか")], cfg,
                                 1920, 1080, 5.0, font="F")
    punch = auto_edit.build_ass([ev], cfg, 1920, 1080, 5.0, font="F")
    check(fs_of(punch) > fs_of(normal), "ツッコミで文字が拡大されていない")
    want = auto_edit.resolve_font_from(
        cfg["scenes"]["punch"]["font_candidates"], quiet=True)
    if want and want != "F":
        check(f"\\fn{want}" in punch, f"シーンのフォント切替が出ていない: {want}")

    # シーン定義が無ければ通常表示のまま
    bare = copy.deepcopy(cfg)
    bare["scenes"] = {}
    check("\\fn" not in auto_edit.build_ass(
        [TelopEvent(0, 2, "ふつう")], bare, 1920, 1080, 5.0, font="F"),
        "シーン未定義なのにフォント切替が出ている")


def test_wrapped_font_size():
    """1行と2行で文字サイズを切り替え、はみ出す長さは自動で縮める。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    st = cfg["style"]
    check(st["font_size_ratio_wrapped"] < st["font_size_ratio"],
          "2行のほうが大きい設定になっている")

    one = auto_edit.build_ass([TelopEvent(0, 2, "短い一行")], cfg, 1920, 1080, 5.0,
                              font="F")
    two = auto_edit.build_ass([TelopEvent(0, 2, "一行目です\\N二行目です")], cfg,
                              1920, 1080, 5.0, font="F")
    check(fs_of(two) < fs_of(one), "2行で文字が小さくなっていない")
    check(abs(fs_of(one) - st["font_size_ratio"] * 1080) < 1.5,
          f"1行の文字サイズが設定と合わない: {fs_of(one)}")

    # セーフマージンを超える長さは自動で縮む (\q2 で折り返さないため)
    text = "あ" * 30
    long_line = auto_edit.build_ass([TelopEvent(0, 2, text)], cfg,
                                    1920, 1080, 5.0, font="F")
    got_fs = fs_of(long_line)
    width_px = auto_edit.estimate_text_width(text, got_fs,
                                             st["tracking_em"] * got_fs)
    avail = 1920 * (1 - 2 * st["safe_margin_ratio"])
    check(width_px <= avail + 1,
          f"長い1行が画面幅に収まらない: {width_px:.0f}px > {avail:.0f}px")
    check(got_fs < st["font_size_ratio"] * 1080, "長い1行が縮んでいない")


def test_round_rect():
    """角丸の描画コマンドが閉じた図形になっている。"""
    path = auto_edit._round_rect(100, 200, 500, 300, 20)
    check(path.startswith("m ") and " b " in path, f"ベジェが使われていない: {path}")
    nums = [int(n) for n in re.findall(r"-?\d+", path)]
    xs, ys = nums[0::2], nums[1::2]
    check(min(xs) == 100 and max(xs) == 500, f"横の範囲が想定外: {min(xs)}〜{max(xs)}")
    check(min(ys) == 200 and max(ys) == 300, f"縦の範囲が想定外: {min(ys)}〜{max(ys)}")
    # 半径0なら普通の矩形にフォールバック
    check(auto_edit._round_rect(0, 0, 10, 10, 0) == auto_edit._rect(0, 0, 10, 10),
          "半径0で矩形に戻らない")


def test_emphasis():
    """キーワード・数字・*囲み* が強調色になる。"""
    cfg = {"emphasis": {"enabled": True, "color": "FFE14D",
                        "auto_numbers": True, "keywords": ["圧倒的"]}}
    runs = auto_edit.emphasis_runs("年収1000万円は圧倒的に少数です", cfg)
    emph = [t for t, f in runs if f]
    check("1000万円" in emph, f"数字+単位が強調されていない: {runs}")
    check("圧倒的" in emph, f"キーワードが強調されていない: {runs}")
    check("".join(t for t, _ in runs) == "年収1000万円は圧倒的に少数です",
          f"本文が欠けている: {runs}")

    runs = auto_edit.emphasis_runs("これは*ここだけ*強調", cfg)
    check([t for t, f in runs if f] == ["ここだけ"], f"*囲み*が想定外: {runs}")
    check("*" not in "".join(t for t, _ in runs), "マーカーが残っている")

    # 改行はそのまま残り、強調は行ごとに閉じる
    runs = auto_edit.emphasis_runs("1つ目\\N2つ目", cfg)
    check(("\\N", False) in runs, f"改行が保持されていない: {runs}")

    # enabled: false なら素通し
    off = {"emphasis": {"enabled": False, "auto_numbers": True}}
    check(auto_edit.emphasis_runs("100万円", off) == [("100万円", False)],
          "emphasis無効時に強調されている")

    # ASSに実際に色タグが乗る
    cfg2 = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "business")
    ass = auto_edit.build_ass([TelopEvent(0, 2, "年収1000万円です")], cfg2,
                              1920, 1080, 5.0, font="F")
    check(auto_edit.ass_color("FFE14D") in ass, "強調色がASSに出ていない")


def test_tv_touches():
    """テレビ寄りの仕上げ: 出入りのフェード・読了+余裕の表示時間・サイドテロップ。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")

    # 出入りのフェードが入る (瞬時に出ると素人っぽくなる)
    ass = auto_edit.build_ass([TelopEvent(0, 3, "テスト")], cfg, 1920, 1080, 5.0,
                              font="F")
    fin = int(cfg["telop"]["fade_in"] * 1000)
    fout = int(cfg["telop"]["fade_out"] * 1000)
    check(f"\\fad({fin},{fout})" in ass, f"フェードが入っていない: {fin}/{fout}")
    off = copy.deepcopy(cfg)
    off["telop"].update({"fade_in": 0, "fade_out": 0})
    check("\\fad(" not in auto_edit.build_ass([TelopEvent(0, 3, "テスト")], off,
                                              1920, 1080, 5.0, font="F"),
          "フェード0でもタグが出ている")

    # 表示時間 = 読み終わる時間 + 余裕
    keeps = [(0.0, 60.0)]
    text = "あ" * 10
    out = auto_edit.remap_events([TelopEvent(0.0, 0.5, text)], keeps,
                                 min_duration=0.5, max_duration=6.5,
                                 reading_speed=5.0, duration_padding=0.5)
    check(abs(out[0].end - (10 / 5.0 + 0.5)) < 1e-6,
          f"読了+余裕の表示時間になっていない: {out[0].end}")

    # サイドテロップ: 1行目が色付き見出し、2行目が本題
    side = copy.deepcopy(cfg)
    side["side"]["text"] = "特集\nエンジニアの転職"
    ass = auto_edit.build_ass([TelopEvent(0, 3, "本文")], side, 1920, 1080, 9.0,
                              font="F")
    rows = [l for l in ass.splitlines() if ",Side,," in l]
    check(len(rows) == 2, f"サイドテロップが2行出ていない: {len(rows)}")
    check("特集" in rows[0] and "エンジニアの転職" in rows[1], "サイドの行順が想定外")
    check(auto_edit.ass_color(side["side"]["accent_color"],
                              side["side"]["bg_opacity"]) in rows[0],
          "1行目の背景がアクセント色になっていない")
    check("0:00:09.00" in rows[0], "サイドテロップが全編表示になっていない")
    # 空なら出さない
    check(",Side,," not in auto_edit.build_ass([TelopEvent(0, 3, "本文")], cfg,
                                               1920, 1080, 9.0, font="F"),
          "side.text が空なのに表示されている")


def test_line_budget():
    """max_chars_per_line: auto は画面幅から1行の文字数を決める。"""
    # talk は「文字を大きく」を優先して固定値、business は幅いっぱいの auto
    talk = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    check(isinstance(talk["telop"]["max_chars_per_line"], int),
          "talk が固定文字数になっていない")
    check(talk["telop"]["max_chars_per_line"] <= 16,
          "talk の1行が長すぎて文字を大きくできない")

    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "business")
    st = cfg["style"]
    check(str(cfg["telop"]["max_chars_per_line"]).lower() == "auto",
          "business が auto になっていない")
    budget = auto_edit.line_budget(cfg, 1920, 1080)

    # 上限ぴったりの1行が、左右マージンの内側にちょうど収まる
    fs = (st.get("font_size_ratio_wrapped") or st["font_size_ratio"]) * 1080
    got = auto_edit.estimate_text_width("あ" * int(budget), fs, st["tracking_em"] * fs)
    avail = 1920 * (1 - 2 * st["safe_margin_ratio"])
    check(got <= avail + 1, f"上限まで入れると幅を超える: {got:.0f} > {avail:.0f}")
    check(got > avail * 0.9, f"幅を使い切れていない: {got:.0f} / {avail:.0f}")

    # 少しはみ出す程度の文は、折り返さず1行のまま(文字が縮んで収まる)
    text = "あ" * (int(budget) - 1)
    check(len(auto_edit.wrap_lines(text, budget)) == 1,
          "1行に収まるはずの文が折り返された")
    check(len(auto_edit.wrap_lines("あ" * (int(budget) + 6), budget)) == 2,
          "長すぎる文が2行になっていない")

    # 解像度が変わっても文字数の上限は同じ (比率で決めているため)
    check(abs(auto_edit.line_budget(cfg, 1280, 720) - budget) < 0.5,
          "解像度で1行の文字数が変わってしまう")

    # 数値指定ならその値がそのまま使われる
    fixed = copy.deepcopy(cfg)
    fixed["telop"]["max_chars_per_line"] = 12
    check(auto_edit.line_budget(fixed, 1920, 1080) == 12.0,
          "数値指定が尊重されていない")


def test_line_spacing():
    """行間はフォント任せにせず line_height_em どおりに自前で置く。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    ass = auto_edit.build_ass([TelopEvent(0, 2, "一行目\\N二行目")], cfg,
                              1920, 1080, 5.0, font="F")
    ys = [float(m) for m in re.findall(r"Dialogue: 3,[^,]+,[^,]+,Telop,[^}]*"
                                       r"\\pos\(\d+,(-?[0-9.]+)\)", ass)]
    check(len(ys) == 2, f"行ごとにDialogueが出ていない: {ys}")
    got = abs(ys[1] - ys[0])
    want = fs_of(ass) * cfg["style"]["line_height_em"]
    check(abs(got - want) < 1.5, f"行間が設定と合わない: {got:.1f} (期待 {want:.1f})")

    # 座布団なしのときは最終行の下端が position_ratio に固定される
    one = auto_edit.build_ass([TelopEvent(0, 2, "一行だけ")], cfg, 1920, 1080, 5.0,
                              font="F")
    y_one = float(re.search(r"Dialogue: 3,[^,]+,[^,]+,Telop,[^}]*"
                            r"\\pos\(\d+,(-?[0-9.]+)\)", one).group(1))
    check(abs(y_one - max(ys)) < 1.5,
          f"1行と2行で最終行の位置がずれる: {y_one} vs {max(ys)}")


def test_speaker_band():
    """「名前: 発言」で話者を判定し、座布団の色を話者ごとに変える。"""
    speakers = {"株本": "FFD34D", "ゲスト": "7FD4FF"}
    check(auto_edit.split_speaker("株本: そうですね", speakers) == ("株本", "そうですね"),
          "話者の切り出しが想定外")
    check(auto_edit.split_speaker("株本：全角コロン", speakers)[0] == "株本",
          "全角コロンを扱えていない")
    # 知らない名前は発言として扱う(誤爆防止)
    check(auto_edit.split_speaker("結論: これです", speakers) == ("", "結論: これです"),
          "未登録の名前を話者にしてしまっている")

    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG, "talk")
    cfg["band"]["enabled"] = True   # 座布団ありの動作を見る
    cfg["speakers"] = speakers
    ass = auto_edit.build_ass([TelopEvent(0, 2, "はい", "株本")], cfg,
                              1920, 1080, 5.0, font="F")
    check(f"\\1c{auto_edit.ass_color('FFD34D')}" in ass,
          "話者色が座布団に反映されていない")
    other = auto_edit.build_ass([TelopEvent(0, 2, "はい", "ゲスト")], cfg,
                                1920, 1080, 5.0, font="F")
    check(f"\\1c{auto_edit.ass_color('7FD4FF')}" in other,
          "話者ごとに座布団の色が変わっていない")


def test_no_break_inside_number():
    """「1000万円」「Live2D」が改行で割れない。"""
    cases = [("上位3割の人だけが年収1000万円を超えているそうです", 18, "1000万円"),
             ("Live2Dモデルの量産計画が明らかになりました", 15, "Live2D")]
    for text, limit, token in cases:
        lines = auto_edit.wrap_lines(text, limit)
        check(any(token in line for line in lines),
              f"{token} が改行で割れた: {lines}")
        check(all(auto_edit.display_width(line) <= limit for line in lines),
              f"最大幅を超えた: {lines}")


def test_frame_padding():
    """keep_padding_frames はfpsで秒に換算される(発話間2〜3フレーム)。"""
    # 30fpsで2フレーム = 0.0667秒。前後に付くので発話間は約4フレーム分あく
    pad = 2 / 30.0
    keeps = auto_edit.build_keep_segments([(2.0, 4.0)], 6.0, pad, 0.0, 0.1)
    check(abs(keeps[0][1] - (2.0 + pad)) < 1e-9 and abs(keeps[1][0] - (4.0 - pad)) < 1e-9,
          f"フレーム換算の余白が想定外: {keeps}")


def test_font_helpers():
    """フォント自動選択: 候補がPCに無ければ先頭候補にフォールバックする。"""
    cfg = auto_edit.load_config(auto_edit.DEFAULT_CONFIG)
    cfg["style"]["font_candidates"] = ["架空フォントA", "架空フォントB"]
    check(auto_edit.resolve_font(cfg, quiet=True) == "架空フォントA",
          "存在しない候補のフォールバックが想定外")
    cfg["style"]["font"] = "明示指定フォント"
    check(auto_edit.resolve_font(cfg, quiet=True) == "明示指定フォント",
          "明示指定のフォントが尊重されていない")

    # 実在するフォントファイルからファミリ名を読める (name テーブル解析)
    out = subprocess.run(["fc-list", "--format=%{file}\\n"],
                         capture_output=True, text=True)
    files = [Path(p) for p in out.stdout.splitlines()
             if p.lower().endswith((".ttf", ".otf"))]
    if files:
        got = [auto_edit.font_family_from_file(p) for p in files[:8]]
        check(any(got), f"フォントファイルからファミリ名を読めない: {files[:8]}")
    check(auto_edit.font_family_from_file(Path("/etc/hostname")) == "",
          "フォントでないファイルで空文字を返さない")
    check(auto_edit.font_names_from_file(Path("/etc/hostname")) == ("", ""),
          "フォントでないファイルで空のペアを返さない")

    # 同梱フォントは「総称名」ではなく「ウェイト固有名」で指定する。
    # 総称名のままだとシステムの同系統フォントに置き換わって細く描画される。
    fonts_dir = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    drop = auto_edit.dropin_fonts(fonts_dir)
    for alias, resolved in drop.items():
        check(resolved.lower() in drop,
              f"解決先 {resolved} が引けない")
        path = auto_edit.dropin_font_files(fonts_dir).get(alias)
        if path:
            specific, generic = auto_edit.font_names_from_file(path)
            check(resolved == (specific or generic),
                  f"{alias} の解決先が固有名でない: {resolved}")
            if generic and specific and generic != specific:
                check(drop.get(generic.lower()) == specific,
                      f"総称名 {generic} が固有名 {specific} に解決されない")

    # ウェイトを読めるか (太字の二重掛け判定に使う)
    for path in sorted(fonts_dir.glob("*.ttf"))[:3]:
        check(auto_edit.font_weight_from_file(path) > 0,
              f"ウェイトを読めない: {path.name}")


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
    test_single_line_split()
    test_srt_roundtrip()
    test_ass_build()
    test_ass_gradient_band()
    test_reading_speed()
    test_presets()
    test_talk_preset()
    test_scenes()
    test_wrapped_font_size()
    test_round_rect()
    test_emphasis()
    test_tv_touches()
    test_line_budget()
    test_line_spacing()
    test_speaker_band()
    test_no_break_inside_number()
    test_frame_padding()
    test_font_helpers()
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
