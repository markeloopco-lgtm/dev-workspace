"""エピソード台本YAMLの読み込みと検証。書式は episodes/README.md と episodes/sample_*.yaml を参照。"""

from pathlib import Path

import yaml

VISUAL_TYPES = ("space", "image", "video", "color", "stock")   # stock = フリー素材の検索語(videolab/stock.py)
FORBIDDEN_DIRS = ("refs", "analysis")   # 参考動画とその解析物は制作素材に使わない
_REPO = Path(__file__).resolve().parents[2]


class EpisodeError(ValueError):
    pass


def _resolve(path_str: str, base: Path) -> Path:
    """台本のあるフォルダ → 作業フォルダ の順に探し、実体のパス(リンクを辿った先)を返す。"""
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    for root in (base, Path.cwd()):
        cand = root / p
        if cand.exists():
            return cand.resolve()
    return (Path.cwd() / p).resolve()


def _guard(path: Path, what: str, base: Path) -> None:
    """プロジェクトの refs/ analysis/ の中のファイルなら拒否する。

    実体のパスで判定するのでリンク経由でもすり抜けない。判定するのはプロジェクト
    (リポジトリ・作業フォルダ・台本のフォルダ)直下の refs/ analysis/ だけなので、
    たまたま上の階層に analysis という名前のフォルダがあっても誤って拒否しない。
    """
    real = Path(path).resolve()
    roots = {_REPO, Path.cwd().resolve(), Path(base).resolve(), Path(base).resolve().parent}
    for root in roots:
        try:
            rel = real.relative_to(root)
        except ValueError:
            continue
        head = rel.parts[0].lower() if rel.parts else ""
        if head in FORBIDDEN_DIRS:
            raise EpisodeError(
                f"{what} に {head}/ 内のファイルが指定されています: {path}\n"
                "  参考動画(refs/)と解析物(analysis/)は他人の著作物の複製です。制作素材には使えません。"
                "自作・フリー素材・生成素材を assets/ に置いて使ってください。")


def _file(path_str: str, base: Path, where: str) -> str:
    p = _resolve(str(path_str), base)
    _guard(p, where, base)
    if not p.exists():
        raise EpisodeError(f"{where}: ファイルがありません: {p}")
    return str(p)


def load_episode(path, allow_missing: bool = False) -> dict:
    """台本を読み、パスを解決・検査する。

    allow_missing=True(下書き)なら、まだ用意していない画像・動画は「素材TODO」の仮カードに
    置き換え、ep["_missing"] に一覧を入れる。
    """
    path = Path(path)
    try:
        ep = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        raise EpisodeError(
            f"台本YAMLの書き方に誤りがあります: {path}\n{e}\n"
            "  (字下げは半角スペースでそろえる。: や # を含む台詞は \"...\" で囲む)") from e
    if not isinstance(ep, dict) or not ep.get("scenes"):
        raise EpisodeError(f"scenes がありません: {path}")
    base = path.parent
    ep.setdefault("title", path.stem)
    ep["voices"] = ep.get("voices") or {}
    ep["_path"] = str(path)
    ep["_name"] = path.stem
    ep["_missing"] = []
    for key in ("output", "style"):
        if ep.get(key):
            ep[key] = str(_resolve(ep[key], base))
    bgms = ep.get("bgm") or []
    for i, bg in enumerate(bgms):
        if isinstance(bg, str):
            bgms[i] = bg = {"file": bg}
        if "file" not in bg:
            raise EpisodeError(f"bgm[{i}] に file がありません")
        bg["file"] = _file(bg["file"], base, f"bgm[{i}]")
    ep["bgm"] = bgms
    voices = ep["voices"]
    ids = set()
    for si, sc in enumerate(ep["scenes"]):
        sc.setdefault("id", f"scene{si + 1}")
        if sc["id"] in ids:
            raise EpisodeError(f"シーンIDが重複しています: {sc['id']}")
        ids.add(sc["id"])
        lines = sc.get("lines") or []
        if isinstance(lines, str):
            lines = [{"text": lines}]
        norm = []
        for li, ln in enumerate(lines):
            if isinstance(ln, str):
                ln = {"text": ln}
            if not ln.get("text"):
                raise EpisodeError(f"{sc['id']} の lines[{li}] に text がありません")
            ln["text"] = str(ln["text"])
            ln.setdefault("speaker", sc.get("speaker") or ep.get("default_speaker") or
                          next(iter(voices), "ナレーター"))
            if voices and "default" not in voices and ln["speaker"] not in voices:
                raise EpisodeError(
                    f"{sc['id']}.lines[{li}]: 話者 '{ln['speaker']}' が voices にありません"
                    f"（定義済み: {', '.join(voices)}）。voices に追加するか名前を直してください")
            if ln.get("visual"):
                ln["visual"] = _norm_visual(ln["visual"], base, f"{sc['id']}.lines[{li}].visual",
                                            ep, allow_missing)
            norm.append(ln)
        sc["lines"] = norm
        vis = sc.get("visuals") or ([sc["visual"]] if sc.get("visual") else [])
        sc["visuals"] = [_norm_visual(v, base, f"{sc['id']}.visuals[{vi}]", ep, allow_missing)
                         for vi, v in enumerate(vis)]
        if not sc["visuals"] and not any(ln.get("visual") for ln in norm):
            sc["visuals"] = [{"type": "space", "template": "starfield", "params": {}}]
        if not norm and not sc.get("duration"):
            raise EpisodeError(f"{sc['id']}: lines も duration も無いシーンは作れません")
        ses = sc.get("se") or []
        for ei, se in enumerate(ses):
            if isinstance(se, str):
                ses[ei] = se = {"file": se}
            se["file"] = _file(se["file"], base, f"{sc['id']}.se[{ei}]")
            se["at"] = float(se.get("at", 0.0))
        sc["se"] = ses
    return ep


def _placeholder(v: dict, missing: str) -> dict:
    return {"type": "color", "color": "#2b2f36", "color2": "#1b1e23",
            "_todo": f"素材TODO: {Path(missing).name}"}


def _stock_to_video(v: dict, where: str, ep: dict, allow_missing: bool) -> dict:
    """{type: stock, query: 検索語} を取得済みのフリー素材 {type: video, path: ...} に差し替える。

    同じ検索語を何度も使うと、取得済みの素材を順番に割り当てる(vlab stock が使用回数ぶん取ってくる)。
    撮影者と出典は credits に足す(credits.txt → 概要欄)。
    """
    from videolab import stock
    q = str(v.get("query") or "").strip()
    if not q:
        raise EpisodeError(f"{where}: type=stock には query(検索語)が必要です")
    uses = ep.setdefault("_stock_uses", {})
    nth = uses.get(stock.qkey(q), 0)
    uses[stock.qkey(q)] = nth + 1
    hit = stock.resolve(q, nth)
    if hit is None:
        if not allow_missing:
            raise EpisodeError(f"{where}: フリー素材がまだありません(検索語: {q})\n"
                               f"  python scripts/vlab.py stock {ep['_path']} で取得してください"
                               "(--draft なら仮カードで代用できます)")
        ep["_missing"].append(f"stock: {q}")
        return _placeholder(v, q)
    out = {k: val for k, val in v.items() if k not in ("query", "provider", "min_duration")}
    out.update(type="video", path=hit["path"])
    out.setdefault("start", 0.0)
    credits = ep.get("credits") or []
    line = stock.credit_line(hit)
    if line not in credits:
        credits.append(line)
    ep["credits"] = credits
    return out


def _norm_visual(v, base: Path, where: str, ep: dict, allow_missing: bool) -> dict:
    if isinstance(v, str):
        v = {"type": "image", "path": v} if Path(v).suffix else {"type": "space", "template": v}
    v = dict(v)
    t = v.get("type", "space" if "template" in v else "image")
    if t not in VISUAL_TYPES:
        raise EpisodeError(f"{where}: 未知の type '{t}' (使えるもの: {', '.join(VISUAL_TYPES)})")
    v["type"] = t
    if t == "stock":
        v = _stock_to_video(v, where, ep, allow_missing)
        if v.get("_todo"):
            return v
        t = "video"
    if t in ("image", "video"):
        if not v.get("path"):
            raise EpisodeError(f"{where}: type={t} には path が必要です")
        p = _resolve(v["path"], base)
        _guard(p, where, base)
        if not p.exists():
            if not allow_missing:
                raise EpisodeError(f"{where}: ファイルがありません: {p}\n"
                                   "  (まだ用意していない素材は --draft なら仮カードで代用できます)")
            ep["_missing"].append(str(p))
            return _placeholder(v, str(p))
        v["path"] = str(p)
    ovs = []
    for oi, ov in enumerate(v.get("overlays") or []):
        ov = {"path": ov} if isinstance(ov, str) else dict(ov)
        if not ov.get("path"):
            raise EpisodeError(f"{where}.overlays[{oi}]: path が必要です")
        op = _resolve(ov["path"], base)
        _guard(op, f"{where}.overlays[{oi}]", base)
        if not op.exists():
            if not allow_missing:
                raise EpisodeError(f"{where}.overlays[{oi}]: ファイルがありません: {op}")
            ep["_missing"].append(str(op))
            continue
        ov["path"] = str(op)
        ovs.append(ov)
    if ovs:
        v["overlays"] = ovs
    else:
        v.pop("overlays", None)
    if t == "space":
        v.setdefault("template", "planet")
        params = v["params"] = dict(v.get("params") or {})
        for key in ("texture", "textures"):      # 惑星テクスチャ(1枚 / 比較用に2枚)
            val = params.get(key)
            if not val:
                continue
            items = val if isinstance(val, (list, tuple)) else [val]
            out = []
            for it in items:
                if it in (None, ""):
                    out.append(it)
                    continue
                out.append(_file(it, base, f"{where}.params.{key}"))
            params[key] = out if isinstance(val, (list, tuple)) else out[0]
    return v
