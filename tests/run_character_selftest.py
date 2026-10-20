#!/usr/bin/env python3
"""立ち絵(口パク・まばたき・表情)・話者ごとの字幕・look(見た目の共通設定)・かんたん台本の検証。

音声エンジン不要(dummy の仮音声)・ネット不要。次を確認する:
  - 口の開き: 無音では閉じ、声の山と谷で開閉する(声の大きさが違っても同じ動き)
  - 立ち絵フォルダの読み取り(<表情>.png / _open / _half / _blink)と仮の立ち絵の書き出し
  - look の下敷き(台本が優先・credits は足し算・映像の無い場面に default_visual)
  - かんたん台本 → 台本YAML(話者・表情・場面と台詞の [ ]・ナレーション扱い・書き間違いのエラー)
  - 台本の誤り(無い表情・無いキャラ・立ち絵フォルダが無い)と、下書きでの省略
  - タイムライン: 出演(場面ごと・なし)、表情の切り替えと持続、口パクは本人の台詞の間だけ、
    まばたき、話し始めの跳ね、暗転で背景が透けないこと
  - 制作: 立ち絵が映る場面/映らない場面、話者ごとの字幕の縁の色、vlab script / vlab make の通し

usage: python tests/run_character_selftest.py
"""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from videolab.produce.characters import CharacterStage, mouth_levels, scan_face_dir  # noqa: E402
from videolab.produce.episode import EpisodeError, load_episode  # noqa: E402
from videolab.produce.script_text import ScriptError, convert, parse_script  # noqa: E402

BG = (90, 60, 30)          # 誰も出ない場面の背景(茶色の単色画像)
ZUNDA_OUTLINE = "#2f8f3a"  # ずんだもんの台詞の字幕の縁(緑)

LOOK = f"""
default_speaker: ナレーター
voices:
  ナレーター: {{engine: dummy, speaker: 11}}
  ずんだもん: {{engine: dummy, speaker: 3}}
characters:
  ずんだもん: {{dir: chars/zunda, x: 0.8}}
cast: [ずんだもん]
default_visual: {{type: color, color: "#203040"}}
telop:
  size: 0.05
  speaker_styles:
    ずんだもん: {{outline: "{ZUNDA_OUTLINE}"}}
credits: ["立ち絵: テスト用の仮の立ち絵"]
scenes: [{{id: look_no_scenes}}]
"""

EPISODE = """
look: look.yaml
title: 立ち絵テスト
resolution: [960, 540]
telop: {size: 0.07}
credits: ["BGM: テスト", "立ち絵: テスト用の仮の立ち絵"]
characters:
  ずんだもん: {x: 0.2}
scenes:
  - id: a
    lines:
      - {speaker: ずんだもん, text: "今日はとても良い天気なのだ。お散歩に行くのだ。", face: happy}
      - {speaker: ナレーター, text: "そう言って、彼は家を出ました。しかし空はすぐに曇ってきます。"}
  - id: b
    cast: []
    visuals: [{type: image, path: assets/bg.png, camera: static}]
    lines:
      - {speaker: ナレーター, text: "誰もいない公園に、冷たい雨が降り始めました。"}
  - id: c
    lines:
      - {speaker: ナレーター, text: "雨に気づいた彼は、とても悲しい顔になりました。", faces: {ずんだもん: sad}}
      - {speaker: ずんだもん, text: "傘を持ってくればよかったのだ。", se: assets/don.wav}
"""

STORY = """\
look: look.yaml
# 雨の日の話
// この行はメモなので動画に入らない
## 朝 [タイトル: 月曜日] [背景: assets/bg.png]
ナレーター: これは、ある日の物語です。
ずんだもん(happy): 今日も余裕なのだ！ [強調: 余裕]
ずんだもん（sad）：つらいのだ… [間: 0.8]
話者名の無い行はナレーションになる。
注意: この行は話者名ではありません [字幕: 注意!]
ナレーター: 彼はうつむきました。 [表情: ずんだもん=sad] [効果音: assets/don.wav]
## まとめ [出演: なし] [間: 1.5] [素材: city night]
最後の一言です。
"""


def _frames_at(video: Path, times, w: int, h: int):
    out = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, check=True)
        out.append(np.frombuffer(r.stdout, np.uint8)[: w * h * 3].reshape(h, w, 3))
    return out


def _green_px(img: np.ndarray) -> int:
    r, g, b = (img[..., i].astype(int) for i in range(3))
    return int(((g > r + 50) & (g > b + 30)).sum())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="char_selftest_"))
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)
        return cond

    # 1) 口の開き(合成した「声」: 0.3秒の無音 → 1秒間に4音節 → 0.3秒の無音)
    sr, fps = 24000, 30
    t = np.arange(int(1.6 * sr)) / sr
    speech = (t >= 0.3) & (t < 1.3)
    env = np.where(speech, 0.15 + 0.85 * np.sin(np.pi * 4 * (t - 0.3)) ** 2, 0.0)
    y = (0.2 * env * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    lv = mouth_levels(y, sr, fps)
    check(len(lv) == 48, f"口の開きのフレーム数 {len(lv)}")
    check(not lv[:8].any() and not lv[-8:].any(), f"無音で口が開いている {lv[:8]} {lv[-8:]}")
    inner = lv[9:39]
    opens = int(((inner[1:] == 2) & (inner[:-1] < 2)).sum()) + int(inner[0] == 2)
    check(3 <= opens <= 5 and (inner < 2).any(), f"音節ごとの開閉 {opens}回 {inner.tolist()}")
    check(np.array_equal(mouth_levels(y * 0.05, sr, fps), lv), "声の大きさで口の動きが変わる")
    check(not mouth_levels(np.zeros(4800, np.float32), sr, fps).any(), "無音の台詞で口が開く")
    check(len(mouth_levels(np.zeros(0, np.float32), sr, fps)) == 1, "空の音声で落ちる")

    # 2) 立ち絵フォルダ: 仮の立ち絵の書き出しと読み取り
    zdir = tmp / "chars" / "zunda"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_placeholder_character.py"), str(zdir)],
                       capture_output=True, text=True)
    check(r.returncode == 0, f"仮の立ち絵の書き出し: {r.stderr}")
    faces = scan_face_dir(zdir)
    check(sorted(faces) == ["angry", "happy", "normal", "sad", "surprised"], f"表情の一覧 {sorted(faces)}")
    check(all(set(v) == {"base", "open", "half", "blink"} for v in faces.values()), f"表情ごとの絵 {faces}")
    odir = tmp / "chars" / "other"
    odir.mkdir(parents=True)
    for nm in ("normal", "normal_open", "smile", "wink_blink"):
        Image.new("RGBA", (40, 60), (200, 100, 100, 255)).save(odir / f"{nm}.png")
    of = scan_face_dir(odir)
    check(sorted(of) == ["normal", "smile"] and set(of["normal"]) == {"base", "open"},
          f"口閉じの絵が無い表情を除く {of}")

    # 3) look の下敷き
    (tmp / "assets").mkdir()
    Image.new("RGB", (64, 36), BG).save(tmp / "assets" / "bg.png")
    import soundfile as sf
    tt = np.arange(4800) / 24000
    sf.write(str(tmp / "assets" / "don.wav"), (0.3 * np.sin(2 * np.pi * 440 * tt)).astype(np.float32), 24000)
    (tmp / "look.yaml").write_text(LOOK, encoding="utf-8")
    ep_path = tmp / "ep.yaml"
    ep_path.write_text(EPISODE, encoding="utf-8")
    ep = load_episode(ep_path)
    zs = ep["characters"].get("ずんだもん", {})
    check(zs.get("x") == 0.2 and zs.get("height") == 0.72 and zs.get("default") == "normal",
          f"characters の重ね合わせ(台本の x が優先・ほかは既定) {zs}")
    check(ep["telop"].get("size") == 0.07 and ep["telop"].get("speaker_styles", {}).get("ずんだもん")
          == {"outline": ZUNDA_OUTLINE}, f"telop の重ね合わせ {ep['telop']}")
    check(ep["credits"] == ["立ち絵: テスト用の仮の立ち絵", "BGM: テスト"], f"credits の足し算 {ep['credits']}")
    check([s["id"] for s in ep["scenes"]] == ["a", "b", "c"], "look の scenes が混ざった")
    va, vb = ep["scenes"][0]["visuals"], ep["scenes"][1]["visuals"]
    check(va == [{"type": "color", "color": "#203040"}], f"映像の無い場面に default_visual {va}")
    check(vb[0]["type"] == "image" and vb[0]["path"].endswith("bg.png"), f"場面の映像が優先 {vb}")
    check(ep.get("cast") == ["ずんだもん"] and ep["scenes"][1]["cast"] == [], "cast の読み取り")

    # 4) 台本の誤り
    def broken(edit, draft=False):
        d = yaml.safe_load(EPISODE)
        edit(d)
        p = tmp / "broken.yaml"
        p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
        try:
            return load_episode(p, allow_missing=draft), ""
        except EpisodeError as e:
            return None, str(e)

    _, msg = broken(lambda d: d["scenes"][0]["lines"][0].update(face="smug"))
    check("smug" in msg and "happy" in msg, f"無い表情のエラー(ある表情を案内): {msg}")
    aep, msg = broken(lambda d: d["scenes"][0]["lines"][0].update(face="笑顔"))
    check(aep is not None and aep["scenes"][0]["lines"][0]["face"] == "happy", f"日本語の表情名(笑顔→happy): {msg}")
    aep, msg = broken(lambda d: d["scenes"][2]["lines"][0].update(faces={"ずんだもん": "しょんぼり"}))
    check(aep is not None and aep["scenes"][2]["lines"][0]["faces"] == {"ずんだもん": "sad"},
          f"日本語の表情名(しょんぼり→sad): {msg}")
    _, msg = broken(lambda d: d["scenes"][2]["lines"][1].update(se="assets/none.wav"))
    check("none.wav" in msg, f"無い効果音のエラー: {msg}")
    sep, msg = broken(lambda d: d["scenes"][2]["lines"][1].update(se="assets/none.wav"), draft=True)
    check(sep is not None and any(m.endswith("none.wav") for m in sep["_missing"])
          and not sep["scenes"][2]["lines"][1]["se"], f"下書きでは無い効果音を飛ばす: {msg}")
    _, msg = broken(lambda d: d["scenes"][1].update(cast=["めたん"]))
    check("めたん" in msg, f"無いキャラの cast のエラー: {msg}")
    _, msg = broken(lambda d: d["scenes"][2]["lines"][0].update(faces={"めたん": "sad"}))
    check("めたん" in msg, f"無いキャラの表情のエラー: {msg}")
    _, msg = broken(lambda d: d["characters"]["ずんだもん"].update(default="sleepy"))
    check("sleepy" in msg, f"無い既定の表情のエラー: {msg}")
    _, msg = broken(lambda d: d["characters"]["ずんだもん"].update(dir="chars/none"))
    check("make_placeholder_character" in msg, f"立ち絵フォルダが無い時のエラー(仮の立ち絵を案内): {msg}")
    dep, msg = broken(lambda d: d["characters"]["ずんだもん"].update(dir="chars/none"), draft=True)
    check(dep is not None and "立ち絵: ずんだもん" in dep["_missing"] and not dep["characters"],
          f"下書きでは立ち絵を省略して進む: {msg or dep['_missing']}")

    # 5) かんたん台本
    sp = parse_script(STORY, {"ナレーター", "ずんだもん"}, "ナレーター")
    check(sp.get("look") == "look.yaml" and sp.get("title") == "雨の日の話", f"look とタイトル {sp.get('look')}")
    s1, s2 = sp["scenes"]
    check(s1.get("title") == "月曜日" and s1.get("memo") == "朝"
          and s1.get("visuals") == [{"type": "image", "path": "assets/bg.png", "camera": "static"}],
          f"場面の [ ] {s1}")
    want = [
        {"speaker": "ナレーター", "text": "これは、ある日の物語です。"},
        {"speaker": "ずんだもん", "text": "今日も余裕なのだ！", "face": "happy", "callout": "余裕"},
        {"speaker": "ずんだもん", "text": "つらいのだ…", "face": "sad", "gap": 0.8},
        {"text": "話者名の無い行はナレーションになる。", "speaker": "ナレーター"},
        {"text": "注意: この行は話者名ではありません", "speaker": "ナレーター", "telop": "注意!"},
        {"speaker": "ナレーター", "text": "彼はうつむきました。", "faces": {"ずんだもん": "sad"},
         "se": [{"file": "assets/don.wav"}]},
    ]
    for i, (got, exp) in enumerate(zip(s1["lines"], want)):
        check(got == exp, f"台詞{i + 1}: {got} (期待 {exp})")
    check(len(s1["lines"]) == len(want), f"台詞の数 {len(s1['lines'])}")
    check(s2.get("cast") == [] and s2.get("pause_after") == 1.5
          and s2.get("visuals") == [{"type": "stock", "query": "city night"}]
          and s2["lines"] == [{"text": "最後の一言です。", "speaker": "ナレーター"}], f"まとめの場面 {s2}")
    for bad, word in (("## a\n台詞です [音量: 3]", "音量"), ("## a\nずんだもん(happy):", "台詞がありません"),
                      ("## a\n台詞です [間: すこし]", "秒数"), ("## a\n台詞 [表情: sad]", "名前=表情"),
                      ("// メモだけ\n", "台詞が1つもありません")):
        try:
            parse_script(bad, {"ずんだもん"})
            check(False, f"エラーにならない: {bad!r}")
        except ScriptError as e:
            check(word in str(e), f"エラーの文言 {bad!r}: {e}")
    # 台本ファイルの変換: 作業フォルダが別でも、台本の隣の look を見つける
    (tmp / "story.txt").write_text(STORY, encoding="utf-8")
    work = tmp / "work"
    work.mkdir()
    cwd = os.getcwd()
    os.chdir(work)
    try:
        conv = convert(tmp / "story.txt", tmp / "out" / "story.yaml")
    finally:
        os.chdir(cwd)
    text = conv.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    check(text.startswith("# story.txt から自動変換") and Path(raw.get("look", "")).name == "look.yaml"
          and Path(raw["look"]).is_absolute(), f"変換したYAML(look のパス) {raw.get('look')}")
    cep = load_episode(conv, allow_missing=True)
    check(cep["scenes"][0]["visuals"][0]["path"].endswith("bg.png") and "stock: city night" in cep["_missing"]
          and cep["scenes"][1]["cast"] == [], f"変換した台本の読み込み {cep['_missing']}")

    # 6) タイムラインと立ち絵の動き
    from videolab.produce.compose import _fade_level
    from videolab.produce.telop import DEFAULT_TELOP, DEFAULT_TITLE
    from videolab.produce.timeline import build_plan
    from videolab.produce.tts import Narrator
    from videolab.profile import load_profile
    ep = load_episode(ep_path)
    ep["_telop_style"] = {**DEFAULT_TELOP, **(ep.get("telop") or {})}
    ep["_title_style"] = dict(DEFAULT_TITLE)
    W, H, FPS = 480, 270, 30.0
    plan = build_plan(ep, load_profile(ROOT / "configs" / "style_profile.yaml"),
                      Narrator(ep["voices"], tmp / "tts", force_engine="dummy"), W, H, FPS)
    scenes = {s["id"]: s for s in plan.scenes}
    end_all = round(plan.total_frames / FPS, 3)
    check([(c["name"], c["start"], c["end"]) for c in plan.cast]
          == [("ずんだもん", 0.0, scenes["a"]["end"]), ("ずんだもん", scenes["c"]["start"], end_all)],
          f"出演区間(b は cast: [] で出さない・頭と終わりは動画の端まで) {plan.cast}")
    by_scene = {}
    for ln in plan.lines:
        by_scene.setdefault(ln.scene_id, []).append(ln)
    a0, a1 = by_scene["a"]
    (b0,) = by_scene["b"]
    c0, c1 = by_scene["c"]
    check([(round(e["t"], 2), e["face"]) for e in plan.faces]
          == [(round(a0.start, 2), "happy"), (round(c0.start, 2), "sad")], f"表情の切り替え {plan.faces}")
    check([(round(e["t"], 3), Path(e["file"]).name) for e in plan.se] == [(round(c1.start, 3), "don.wav")],
          f"台詞の効果音は台詞の頭で鳴る {plan.se}")
    zt = [ev for ev in plan.telops if ev["speaker"] == "ずんだもん"]
    nt = [ev for ev in plan.telops if ev["speaker"] == "ナレーター"]
    check(zt and all(ev.get("style") == {"outline": ZUNDA_OUTLINE} for ev in zt)
          and nt and not any(ev.get("style") for ev in nt), "話者ごとの字幕の見た目")

    st = CharacterStage(plan, W, H, FPS)

    def fr(ln, u):
        return int((ln.start + ln.dur * u) * FPS)

    name = "ずんだもん"
    check(st.visible[name][fr(a0, 0.5)] and not st.visible[name][fr(b0, 0.5)] and st.visible[name][fr(c1, 0.5)],
          "場面ごとの表示/非表示")
    check(st.face_at(name, fr(a0, 0.5)) == "happy" and st.face_at(name, fr(a1, 0.5)) == "happy"
          and st.face_at(name, fr(c0, 0.5)) == "sad" and st.face_at(name, fr(c1, 0.5)) == "sad"
          and st.face_at(name, 0) == "normal", "表情は次に変えるまで続く")
    za = st.mouth[name][fr(a0, 0.05):fr(a0, 0.95)]
    check((za == 2).any() and (za < 2).any() and int((np.diff(za.astype(int)) != 0).sum()) >= 4,
          f"ずんだもんの台詞で口パク {za.tolist()}")
    na = st.mouth[name][fr(a1, 0.0):fr(a1, 1.0)]
    check(not na.any() and not st.talk[name][fr(a1, 0.5)], "ナレーターの台詞で口が動く")
    var = [st.variant_at(name, f) for f in range(plan.total_frames)]
    check("blink" in var and all(st.mouth[name][f] == 0 for f, v in enumerate(var) if v == "blink"),
          "まばたきは口を閉じている時だけ")
    check("open" in var and "half" in var, "開き/半開きの絵を使っていない")

    def top_row(f, alpha=1.0, bg=0):
        img = np.full((H, W, 3), bg, np.uint8)
        st.draw(img, f, alpha)
        rows = np.where((img != bg).any(axis=(1, 2)))[0]
        return img, (int(rows[0]) if len(rows) else None)

    f0 = int(round(a0.start * FPS))
    _, top0 = top_row(f0)
    _, top3 = top_row(f0 + 3)
    check(top0 is not None and top3 is not None and top3 <= top0 - 3, f"話し始めの跳ね {top0} → {top3}")
    img_b, _ = top_row(fr(b0, 0.5))
    check(not img_b.any(), "出演なしの場面で立ち絵が描かれた")
    img_a, _ = top_row(fr(a1, 0.5))
    cx = int(0.2 * W)
    check(img_a[:, cx - 40:cx + 40].any() and not img_a[:, W // 2:].any(), "立ち絵の横位置(x: 0.2)")
    img0, _ = top_row(fr(a1, 0.5), alpha=0.0)
    check(not img0.any(), "暗転の底(明るさ0)で立ち絵が見える")
    dark0, _ = top_row(fr(a1, 0.5), alpha=0.5, bg=0)
    dark1, _ = top_row(fr(a1, 0.5), alpha=0.5, bg=100)
    hy = int((1.03 - 0.72 * (1 - 260 / 900)) * H)   # 仮の立ち絵の頭の中ほど
    px0, px1 = dark0[hy, cx].astype(int), dark1[hy, cx].astype(int)
    full = img_a[hy, cx].astype(int)
    check(np.abs(px0 - px1).max() <= 2 and np.abs(px0 - full // 2).max() <= 3,
          f"暗転中に背景が透ける/暗くならない {px0} {px1} {full}")
    lv_ = SimpleNamespace
    shots = [lv_(index=0, start=0, end=30, trans_in="cut", trans_len=0),
             lv_(index=1, start=30, end=60, trans_in="fade_black", trans_len=10)]
    check([round(_fade_level(f, shots), 2) for f in (10, 25, 29, 30, 34, 40)] == [1.0, 0.8, 0.0, 0.2, 1.0, 1.0],
          "暗転の明るさ")
    plan.characters[name]["silent_dim"] = 0.5
    dim = CharacterStage(plan, W, H, FPS)
    m_talk, m_quiet = [], []
    for f, store in ((fr(a0, 0.5), m_talk), (fr(a1, 0.5), m_quiet)):
        img = np.zeros((H, W, 3), np.uint8)
        dim.draw(img, f)
        store.append(img[img.any(axis=2)].mean())
    check(m_quiet[0] < 0.7 * m_talk[0], f"話していない時の暗さ(silent_dim) {m_quiet} {m_talk}")

    # 7) 制作(日本語フォントが無い環境では省略)
    from videolab.produce.telop import find_font
    produced = ""
    os.chdir(tmp)   # renders/cache を一時フォルダに作らせる
    try:
        try:
            find_font()
        except FileNotFoundError:
            produced = "（日本語フォントが無いため制作の通し確認は省略）"
        if not produced:
            import json

            from videolab.produce.compose import produce_episode
            res = produce_episode(ep_path, ROOT / "configs" / "style_profile.yaml", tmp / "out.mp4",
                                  tts="dummy", draft=True, quiet=True)
            pj = json.loads(Path(res["plan"]).read_text(encoding="utf-8"))
            w, h = pj["resolution"]
            check((w, h) == (480, 270), f"下書きの解像度 {w}x{h}")
            check(pj.get("characters", {}).get(name, {}).get("faces") == sorted(faces)
                  and len(pj.get("cast", [])) == 2, f"plan.json の立ち絵情報 {pj.get('characters')}")
            ztel = next(ev for ev in pj["telops"] if ev["speaker"] == name)
            sb = next(s for s in pj["scenes"] if s["id"] == "b")
            ntel = next(ev for ev in pj["telops"] if ev["speaker"] == "ナレーター" and ev["start"] >= sb["start"])
            fz, fn = _frames_at(res["video"], [(ztel["start"] + ztel["end"]) / 2,
                                               (ntel["start"] + ntel["end"]) / 2], w, h)
            box = (slice(int(0.40 * h), int(0.75 * h)), slice(int(0.12 * w), int(0.28 * w)))
            far_z = (np.abs(fz[box].astype(int) - np.array([0x20, 0x30, 0x40])).max(axis=2) > 40).mean()
            far_n = (np.abs(fn[box].astype(int) - np.array(BG)).max(axis=2) > 40).mean()
            check(far_z > 0.5, f"立ち絵が映っていない(場面a) {far_z:.2f}")
            check(far_n < 0.02, f"出演なしの場面に何か映っている(場面b) {far_n:.2f}")
            band = slice(int(0.75 * h), h)
            gz, gn = _green_px(fz[band]), _green_px(fn[band])
            check(gz > 30 and gn < 5, f"ずんだもんの字幕の縁が緑でない/ナレーターが緑 {gz} {gn}")
            cred = Path(res["credits"]).read_text(encoding="utf-8")
            check("立ち絵: テスト用の仮の立ち絵" in cred and "BGM: テスト" in cred, f"credits.txt {cred}")

            # vlab script / vlab make(素材APIのキーは無い扱いにして、ネットに出ない)
            import vlab
            from videolab import stock
            stock.STOCK_DIR = tmp / "stock"
            stock._load_keys = lambda: None
            stock._key = lambda provider: ""
            buf, ebuf = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(ebuf):
                rc = vlab.main(["script", str(tmp / "story.txt"), "-o", str(tmp / "conv.yaml")])
            check(rc == 0 and (tmp / "conv.yaml").exists(), f"vlab script: {ebuf.getvalue()}")
            with redirect_stdout(buf), redirect_stderr(ebuf):
                rc = vlab.main(["make", str(tmp / "story.txt"), "--out-episode", str(tmp / "made.yaml"),
                                "--out", str(tmp / "made_final.mp4"), "--tts", "dummy"])
            check(rc == 1 and "APIキー" in ebuf.getvalue() and not (tmp / "made_final.mp4").exists(),
                  f"素材のキーが無い本番は止まる: rc={rc} {ebuf.getvalue()[-200:]}")
            ebuf.truncate(0)
            with redirect_stdout(buf), redirect_stderr(ebuf):
                rc = vlab.main(["make", str(tmp / "story.txt"), "--out-episode", str(tmp / "made.yaml"),
                                "--out", str(tmp / "made.mp4"), "--tts", "dummy", "--draft"])
            check(rc == 0 and (tmp / "made.mp4").exists() and (tmp / "made.mp4").stat().st_size > 10_000,
                  f"vlab make --draft: rc={rc} {ebuf.getvalue()[-300:]}")
            check("stock: city night" in buf.getvalue(), "下書きで素材TODOを表示していない")
            produced = "・台本から制作(立ち絵の有無・字幕の色を画面で確認)・vlab script / make"
    finally:
        os.chdir(cwd)

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] 口パク・立ち絵フォルダ・look・かんたん台本・台本の誤り・出演/表情/まばたき/跳ね/暗転{produced}を確認 ({tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
