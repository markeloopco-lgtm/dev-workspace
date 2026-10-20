"""エピソード台本YAMLの読み込みと検証。書式は episodes/README.md と episodes/sample_*.yaml を参照。"""

from pathlib import Path

import yaml

VISUAL_TYPES = ("space", "image", "video", "color", "stock")   # stock = フリー素材の検索語(videolab/stock.py)
FORBIDDEN_DIRS = ("refs", "analysis")   # 参考動画とその解析物は制作素材に使わない
_REPO = Path(__file__).resolve().parents[2]


class EpisodeError(ValueError):
    pass


def _resolve(path_str: str, base: Path) -> Path:
    """台本のあるフォルダ → 作業フォルダ → リポジトリ直下 の順に探し、実体のパス(リンクを辿った先)を返す。"""
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    for root in (base, Path.cwd(), _REPO):
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


def _norm_se(items, base: Path, where: str, ep: dict, allow_missing: bool) -> list:
    """効果音(文字列 / {file, at, volume_db} / そのリスト)をそろえる。下書きでは未配置の音を飛ばして進む。"""
    if not items:
        return []
    if isinstance(items, (str, dict)):
        items = [items]
    out = []
    for i, se in enumerate(items):
        se = {"file": se} if isinstance(se, str) else dict(se or {})
        if not se.get("file"):
            raise EpisodeError(f"{where}[{i}] に file がありません")
        p = _resolve(str(se["file"]), base)
        _guard(p, f"{where}[{i}]", base)
        if not p.exists():
            if not allow_missing:
                raise EpisodeError(f"{where}[{i}]: ファイルがありません: {p}")
            ep["_missing"].append(str(p))
            continue
        se["file"] = str(p)
        se["at"] = float(se.get("at", 0.0))
        out.append(se)
    return out


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_look(ep: dict, base: Path) -> dict:
    """look: に書いた「見た目の共通設定」(声・立ち絵・字幕・タイトル・BGMなど)を下敷きにする。

    台本に同じ項目があれば台本が優先(辞書は項目ごとに上書き)。credits は両方を足す。
    チャンネルの作風を1つのファイルにまとめ、台本は中身(scenes)に集中するため。
    """
    if not ep.get("look"):
        return ep
    lp = _resolve(str(ep["look"]), base)
    _guard(lp, "look", base)
    if not lp.exists():
        raise EpisodeError(f"look のファイルがありません: {lp}")
    try:
        look = yaml.safe_load(lp.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        raise EpisodeError(f"look のYAMLの書き方に誤りがあります: {lp}\n{e}") from e
    if not isinstance(look, dict):
        raise EpisodeError(f"look は「項目: 値」の形で書きます: {lp}")
    look.pop("scenes", None)
    own = {k: v for k, v in ep.items() if k != "look"}
    merged = _deep_merge(look, own)
    credits = list(look.get("credits") or []) + [c for c in (own.get("credits") or [])
                                                  if c not in (look.get("credits") or [])]
    if credits:
        merged["credits"] = credits
    merged["_look"] = str(lp)
    return merged


def _norm_characters(ep: dict, base: Path, allow_missing: bool) -> None:
    """characters(立ち絵)を読み、表情ごとの画像を集めて ep["characters"] に入れ直す。"""
    from .characters import DEFAULT_LAYOUT, VARIANTS, resolve_face, scan_face_dir
    raw = ep.get("characters") or {}
    if not isinstance(raw, dict):
        raise EpisodeError("characters は「名前: 設定」の形で書きます(episodes/README.md)")
    out, skipped = {}, []
    for name, c in raw.items():
        c = dict(c or {})
        where = f"characters.{name}"
        faces = {}
        if c.get("dir"):
            d = _resolve(str(c["dir"]), base)
            _guard(d, where, base)
            if d.is_dir():
                faces = scan_face_dir(d)
            elif not allow_missing:
                raise EpisodeError(
                    f"{where}: 立ち絵のフォルダがありません: {d}\n"
                    "  利用規約に沿って配布されている立ち絵素材を置いてください(docs/09)。"
                    "仮の立ち絵は scripts/make_placeholder_character.py で作れます(--draft なら省略して進めます)")
        for fname, fv in (c.get("faces") or {}).items():
            fv = {"base": fv} if isinstance(fv, str) else dict(fv or {})
            faces[str(fname)] = {k: _file(p, base, f"{where}.faces.{fname}.{k}")
                                 for k, p in fv.items() if k in ("base",) + VARIANTS}
        faces = {k: v for k, v in faces.items() if "base" in v}
        if not faces:
            if not allow_missing:
                raise EpisodeError(f"{where}: 立ち絵の画像がありません(<表情>.png を置くか faces: で指定。docs/09)")
            ep["_missing"].append(f"立ち絵: {name}")
            skipped.append(name)
            continue
        want = str(c.get("default") or ("normal" if "normal" in faces else sorted(faces)[0]))
        default = resolve_face(want, faces)
        if default is None:
            raise EpisodeError(f"{where}.default: 表情 '{want}' がありません(ある表情: {', '.join(sorted(faces))})")
        layout = {**DEFAULT_LAYOUT, **{k: c[k] for k in DEFAULT_LAYOUT if k in c}}
        out[name] = {"name": name, "faces": faces, "default": default, **layout}
    ep["characters"] = out
    ep["_skipped_characters"] = skipped
    if ep.get("cast") is not None:
        ep["cast"] = _check_cast(ep["cast"], out, set(skipped), "cast")


def _check_cast(cast, chars: dict, skipped: set, where: str) -> list:
    cast = cast if isinstance(cast, list) else [cast]
    bad = [str(c) for c in cast if c not in chars and c not in skipped]
    if bad:
        raise EpisodeError(f"{where}: characters に無い名前があります: {', '.join(bad)}"
                           f"（定義済み: {', '.join(chars) or 'なし'}）")
    return [c for c in cast if c in chars]


def _check_faces(ln: dict, chars: dict, skipped: set, where: str) -> None:
    """台詞の face(話者の表情) / faces(他のキャラの表情) が実在する表情か確かめ、日本語の別名
    (笑顔 → happy など)をファイルの表情の名前に直す。"""
    from .characters import resolve_face

    def fix(name, face):
        if name in skipped:
            return str(face)
        if name not in chars:
            raise EpisodeError(f"{where}: 表情を変えるキャラ '{name}' が characters にありません")
        got = resolve_face(face, chars[name]["faces"])
        if got is None:
            raise EpisodeError(f"{where}: '{name}' に表情 '{face}' がありません"
                               f"（ある表情: {', '.join(sorted(chars[name]['faces']))}）")
        return got

    if ln.get("face"):
        ln["face"] = fix(ln["speaker"], ln["face"])
    if ln.get("faces"):
        if not isinstance(ln["faces"], dict):
            raise EpisodeError(f"{where}: faces は {{キャラ名: 表情}} の形で書きます")
        ln["faces"] = {name: fix(name, face) for name, face in ln["faces"].items()}


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
    ep = _apply_look(ep, base)
    ep.setdefault("title", path.stem)
    ep["voices"] = ep.get("voices") or {}
    ep["_path"] = str(path)
    ep["_name"] = path.stem
    ep["_missing"] = []
    for key in ("output", "style"):
        if ep.get(key):
            ep[key] = str(_resolve(ep[key], base))
    bgms = []
    for i, bg in enumerate(ep.get("bgm") or []):
        bg = {"file": bg} if isinstance(bg, str) else dict(bg or {})
        if "file" not in bg:
            raise EpisodeError(f"bgm[{i}] に file がありません")
        p = _resolve(str(bg["file"]), base)
        if allow_missing and not p.exists():
            ep["_missing"].append(str(p))      # 下書きはBGM無しで進む
            continue
        bg["file"] = _file(bg["file"], base, f"bgm[{i}]")
        bgms.append(bg)
    ep["bgm"] = bgms
    _norm_characters(ep, base, allow_missing)
    chars = ep["characters"]
    skipped = set(ep.get("_skipped_characters", []))
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
            _check_faces(ln, chars, skipped, f"{sc['id']}.lines[{li}]")
            if ln.get("se"):    # 台詞の頭で鳴らす効果音(at は台詞の頭からの秒数)
                ln["se"] = _norm_se(ln["se"], base, f"{sc['id']}.lines[{li}].se", ep, allow_missing)
            norm.append(ln)
        sc["lines"] = norm
        if sc.get("cast") is not None:
            sc["cast"] = _check_cast(sc["cast"], chars, skipped, f"{sc['id']}.cast")
        vis = sc.get("visuals") or ([sc["visual"]] if sc.get("visual") else [])
        sc["visuals"] = [_norm_visual(v, base, f"{sc['id']}.visuals[{vi}]", ep, allow_missing)
                         for vi, v in enumerate(vis)]
        if not sc["visuals"] and not any(ln.get("visual") for ln in norm):
            # 映像の指定が無い場面: look/台本の default_visual(例: 物語動画の背景)、無ければ星空
            dv = ep.get("default_visual")
            sc["visuals"] = ([_norm_visual(dv, base, f"{sc['id']}.default_visual", ep, allow_missing)] if dv
                             else [{"type": "space", "template": "starfield", "params": {}}])
        if not norm and not sc.get("duration"):
            raise EpisodeError(f"{sc['id']}: lines も duration も無いシーンは作れません")
        sc["se"] = _norm_se(sc.get("se"), base, f"{sc['id']}.se", ep, allow_missing)
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
