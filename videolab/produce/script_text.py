"""かんたん台本(ふつうの文章)→ 台本YAML(episodes/*.yaml)への変換。

見た目(声・立ち絵・字幕・タイトル・BGM)は look ファイルにまとめておき、台本には中身だけを書く。
同じ look を使えば、どんな台本からでも同じ作風の動画になる。

書き方(docs/09):
  look: configs/look_zunda_story.yaml        ← 最初の場面より前に1回(vlab の --look でも指定できる)
  # 動画のタイトル
  ## 場面の名前 [背景: assets/images/office.png] [出演: ずんだもん] [タイトル: 3日後…]
  ナレーター: これは、ある男の物語です。
  ずんだもん(happy): 今日も余裕なのだ！ [強調: 余裕]
  話者名の無い行はナレーション(look の default_speaker。無ければ「ナレーター」)
  // で始まる行はメモ(動画に入らない)

場面の [ ] に書けるもの:
  背景: 画像  / 動画: 動画ファイル / 素材: フリー素材の検索語(docs/08) / 色: #102030
  タイトル: 場面の頭に出す文字 / 出演: 立ち絵を出すキャラ(カンマ区切り。なし で出さない) / 間: 場面の後の秒数
  効果音: 場面の頭で鳴らす音 / BGM: この場面から流すBGM
台詞の後ろの [ ] に書けるもの:
  強調: 画面中央に「ポン」と出す文字 / 字幕: 字幕だけ別の文にする / 間: 次の台詞までの秒数
  表情: 話していないキャラの表情も変える(例 [表情: ずんだもん=sad]。カンマで複数)
  効果音: 台詞の頭で鳴らす音(例 [効果音: assets/se/shock.wav])
"""

import re
from pathlib import Path

import yaml

OPT = re.compile(r"[\[［]\s*([^\]］:：]+?)\s*[:：]\s*([^\]］]*?)\s*[\]］]")
LINE = re.compile(r"^(?P<name>[^:：()（）\[\]\s][^:：()（）\[\]]*?)\s*(?:[(（]\s*(?P<face>[^)）]+?)\s*[)）])?\s*[:：]\s*(?P<text>.+)$")
SCENE_KEYS = {"背景": "bg", "動画": "video", "素材": "stock", "色": "color", "タイトル": "title",
              "出演": "cast", "間": "pause", "効果音": "se", "bgm": "bgm"}
LINE_KEYS = {"強調": "callout", "字幕": "telop", "間": "gap", "表情": "faces", "効果音": "se"}
LOOK_LINE = re.compile(r"^look\s*[:：]\s*(.+)$", re.I)
_REPO = Path(__file__).resolve().parents[2]
AUTO_MARK = "から自動変換"


class ScriptError(ValueError):
    pass


def _opts(text: str, keys: dict, where: str):
    """末尾などの [キー: 値] を取り出し、(残りの文, [(キー, 値)...]) を返す。"""
    found = []
    for m in OPT.finditer(text):
        k = keys.get(m.group(1).strip().lower()) or keys.get(m.group(1).strip())
        if k is None:
            raise ScriptError(f"{where}: [{m.group(1)}: …] は使えません(使えるもの: {', '.join(keys)})")
        found.append((k, m.group(2).strip()))
    return OPT.sub("", text).strip(), found


def _seconds(v: str, where: str) -> float:
    try:
        return float(v)
    except ValueError:
        raise ScriptError(f"{where}: [間: …] は秒数(数字)で書きます: {v}") from None


def _faces(v: str, where: str) -> dict:
    """「ずんだもん=sad, めたん=happy」→ {名前: 表情}"""
    out = {}
    for part in re.split(r"[,、，]", v):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)\s*[=＝]\s*(.+)$", part)
        if not m:
            raise ScriptError(f"{where}: [表情: …] は「名前=表情」で書きます(例 [表情: ずんだもん=sad]): {part}")
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def _head_look(text: str):
    """最初の場面・台詞より前にある look: の行を読む(parse_script と同じ決まり)。"""
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("//"):
            continue
        m = LOOK_LINE.match(line)
        if m:
            return m.group(1).strip()
        if line.startswith("#") and not line.startswith("##"):
            continue
        return None
    return None


def find_look(look, script_dir: Path = None) -> Path:
    """look のファイルを 作業フォルダ → 台本のフォルダ → リポジトリ直下 の順に探す。"""
    p = Path(str(look))
    if p.is_absolute():
        if not p.exists():
            raise ScriptError(f"look のファイルがありません: {p}")
        return p
    for root in [Path.cwd()] + ([Path(script_dir)] if script_dir else []) + [_REPO]:
        if (root / p).exists():
            return (root / p).resolve()
    raise ScriptError(f"look のファイルがありません: {look}(作業フォルダ・台本のフォルダ・リポジトリ直下を探しました)")


def _fix_paths(ep: dict, script_dir: Path) -> None:
    """素材のパスを、書き出したYAMLをどこに置いても読める形(リポジトリ直下から/絶対パス)にする。

    作業フォルダ → 台本のフォルダ → リポジトリ直下 の順に探す。見つからないものはそのまま
    (--draft なら「素材TODO」の仮カードになる)。
    """
    def fix(p: str) -> str:
        q = Path(p)
        if q.is_absolute():
            return p
        for root in (Path.cwd(), script_dir, _REPO):
            if (root / q).exists():
                r = (root / q).resolve()
                try:
                    return r.relative_to(_REPO).as_posix()
                except ValueError:
                    return r.as_posix()
        return p

    for sc in ep["scenes"]:
        for v in sc.get("visuals") or []:
            if v.get("path"):
                v["path"] = fix(v["path"])
        for se in sc.get("se") or []:
            se["file"] = fix(se["file"])
        for ln in sc["lines"]:
            for se in ln.get("se") or []:
                se["file"] = fix(se["file"])
    for bg in ep.get("bgm") or []:
        bg["file"] = fix(bg["file"])


def look_speakers(look_path) -> tuple:
    """look ファイルから、話者として扱う名前と、話者名の無い行の話者を読む。"""
    if not look_path:
        return set(), None
    lp = Path(look_path)
    if not lp.exists():
        raise ScriptError(f"look のファイルがありません: {lp}")
    look = yaml.safe_load(lp.read_text(encoding="utf-8-sig")) or {}
    names = set((look.get("voices") or {}).keys()) | set((look.get("characters") or {}).keys())
    return names, look.get("default_speaker")


def parse_script(text: str, known_speakers=(), default_speaker: str = None) -> dict:
    """かんたん台本の文字列を台本YAMLの辞書にする。known_speakers に無い「名前:」はナレーション扱い。"""
    known = set(known_speakers)
    ep = {"title": None, "scenes": []}
    look = None
    sc = None
    bgms = []

    def new_scene(name="", opts=(), where=""):
        s = {"id": f"s{len(ep['scenes']) + 1:02d}", "lines": [], "visuals": []}
        if name:
            s["memo"] = name
        for k, v in opts:
            if k == "bg":
                s["visuals"].append({"type": "image", "path": v, "camera": "static"})
            elif k == "video":
                s["visuals"].append({"type": "video", "path": v})
            elif k == "stock":
                s["visuals"].append({"type": "stock", "query": v})
            elif k == "color":
                s["visuals"].append({"type": "color", "color": v})
            elif k == "title":
                s["title"] = v
            elif k == "cast":   # [出演: なし] で立ち絵を出さない場面
                s["cast"] = [] if v in ("なし", "無し", "-", "") else \
                    [c.strip() for c in re.split(r"[,、，]", v) if c.strip()]
            elif k == "pause":
                s["pause_after"] = _seconds(v, where)
            elif k == "se":
                s.setdefault("se", []).append({"file": v, "at": 0.0})
            elif k == "bgm":
                bgms.append({"file": v, "from_scene": s["id"]})
        if not s["visuals"]:
            del s["visuals"]
        ep["scenes"].append(s)
        return s

    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        where = f"{no}行目"
        if not line or line.startswith("//"):
            continue
        if sc is None and not ep["scenes"] and LOOK_LINE.match(line):
            look = LOOK_LINE.match(line).group(1).strip()
            continue
        if line.startswith("##"):
            name, opts = _opts(line.lstrip("#").strip(), SCENE_KEYS, where)
            sc = new_scene(name, opts, where)
            continue
        if line.startswith("#"):
            if ep["title"] is None:
                ep["title"] = line.lstrip("#").strip()
            continue
        if sc is None:
            sc = new_scene()
        body, opts = _opts(line, LINE_KEYS, where)
        if not body:
            raise ScriptError(f"{where}: 台詞がありません: {raw}")
        m = LINE.match(body)
        item = None
        if m and (m.group("name").strip() in known or (not known and len(m.group("name")) <= 10)):
            item = {"speaker": m.group("name").strip(), "text": m.group("text").strip()}
            if m.group("face"):
                item["face"] = m.group("face").strip()
        else:
            empty = re.match(r"^(?P<name>[^:：(（]+?)\s*(?:[(（][^)）]*[)）])?\s*[:：]$", body)
            if empty and empty.group("name").strip() in known:
                raise ScriptError(f"{where}: 台詞がありません: {raw.strip()}")
            item = {"text": body}
            if default_speaker:
                item["speaker"] = default_speaker
        for k, v in opts:
            if k == "gap":
                item[k] = _seconds(v, where)
            elif k == "faces":
                item.setdefault("faces", {}).update(_faces(v, where))
            elif k == "se":
                item.setdefault("se", []).append({"file": v})
            else:
                item[k] = v
        sc["lines"].append(item)
    if not ep["scenes"] or not any(s["lines"] for s in ep["scenes"]):
        raise ScriptError("台詞が1つもありません")
    if ep["title"] is None:
        ep.pop("title")
    if bgms:
        ep["bgm"] = bgms
    if look:
        ep = {"look": look, **ep}
    return ep


def convert(script_path, out_path=None, look: str = None) -> Path:
    """かんたん台本ファイルを episodes/<名前>.yaml に書き出す。"""
    script_path = Path(script_path)
    text = script_path.read_text(encoding="utf-8-sig")
    look = look or _head_look(text)
    lp = find_look(look, script_path.parent) if look else None
    names, default_speaker = look_speakers(lp)
    ep = parse_script(text, names, default_speaker or ("ナレーター" if "ナレーター" in names else None))
    ep.pop("look", None)
    _fix_paths(ep, script_path.parent)
    if lp:
        # 書き出したYAMLをどこに置いても読めるよう、リポジトリ内ならリポジトリ直下からのパスにする
        try:
            ref = lp.resolve().relative_to(_REPO).as_posix()
        except ValueError:
            ref = lp.resolve().as_posix()
        ep = {"look": ref, **ep}
    out = Path(out_path) if out_path else Path("episodes") / f"{script_path.stem}.yaml"
    if out.exists() and AUTO_MARK not in out.read_text(encoding="utf-8-sig").split("\n", 1)[0]:
        # 手で書いた台本YAMLを上書きしない
        raise ScriptError(f"{out} は手で書いた台本のようなので上書きしません。-o(make は --out-episode)で別の名前にしてください")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(ep, allow_unicode=True, sort_keys=False, width=1000)
    out.write_text(f"# {script_path.name} {AUTO_MARK}(直すときは元の台本を直して vlab script で作り直す)\n" + body,
                   encoding="utf-8")
    return out
