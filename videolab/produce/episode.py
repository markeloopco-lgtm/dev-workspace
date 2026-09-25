"""エピソード台本YAMLの読み込みと検証。書式は episodes/README.md と episodes/sample_*.yaml を参照。"""

from pathlib import Path

import yaml

VISUAL_TYPES = ("space", "image", "video", "color")
FORBIDDEN_DIRS = ("refs", "analysis")   # 参考動画とその解析物は制作素材に使わない


class EpisodeError(ValueError):
    pass


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    for root in (base, Path.cwd()):
        cand = (root / p)
        if cand.exists():
            return cand.resolve()
    return (Path.cwd() / p).resolve()


def _guard(path: Path, what: str):
    parts = {x.lower() for x in path.parts}
    for d in FORBIDDEN_DIRS:
        if d in parts:
            raise EpisodeError(
                f"{what} に {d}/ 内のファイルが指定されています: {path}\n"
                "  参考動画(refs/)と解析物(analysis/)は他人の著作物の複製です。制作素材には使えません。"
                "自作・フリー素材・生成素材を assets/ に置いて使ってください。")


def load_episode(path) -> dict:
    path = Path(path)
    ep = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    if not ep.get("scenes"):
        raise EpisodeError(f"scenes がありません: {path}")
    ep.setdefault("title", path.stem)
    ep.setdefault("voices", {})
    ep["_path"] = str(path)
    ep["_name"] = path.stem
    for key in ("output", "style"):
        if ep.get(key):
            ep[key] = str(_resolve(ep[key], base))
    for i, bg in enumerate(ep.get("bgm") or []):
        if "file" not in bg:
            raise EpisodeError(f"bgm[{i}] に file がありません")
        bg["file"] = str(_resolve(bg["file"], base))
        _guard(Path(bg["file"]), f"bgm[{i}]")
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
            ln.setdefault("speaker", sc.get("speaker") or ep.get("default_speaker") or
                          next(iter(ep["voices"]), "ナレーター"))
            if ln.get("visual"):
                ln["visual"] = _norm_visual(ln["visual"], base, f"{sc['id']}.lines[{li}].visual")
            norm.append(ln)
        sc["lines"] = norm
        vis = sc.get("visuals") or ([sc["visual"]] if sc.get("visual") else [])
        sc["visuals"] = [_norm_visual(v, base, f"{sc['id']}.visuals[{vi}]")
                         for vi, v in enumerate(vis)]
        if not sc["visuals"] and not any(ln.get("visual") for ln in norm):
            sc["visuals"] = [{"type": "space", "template": "starfield", "params": {}}]
        if not norm and not sc.get("duration"):
            raise EpisodeError(f"{sc['id']}: lines も duration も無いシーンは作れません")
        for ei, se in enumerate(sc.get("se") or []):
            if isinstance(se, str):
                sc["se"][ei] = se = {"file": se}
            se["file"] = str(_resolve(se["file"], base))
            _guard(Path(se["file"]), f"{sc['id']}.se[{ei}]")
            se.setdefault("at", 0.0)
    return ep


def _norm_visual(v, base: Path, where: str) -> dict:
    if isinstance(v, str):
        v = {"type": "image", "path": v} if Path(v).suffix else {"type": "space", "template": v}
    v = dict(v)
    t = v.get("type", "space" if "template" in v else "image")
    if t not in VISUAL_TYPES:
        raise EpisodeError(f"{where}: 未知の type '{t}' (使えるもの: {', '.join(VISUAL_TYPES)})")
    v["type"] = t
    if t in ("image", "video"):
        if not v.get("path"):
            raise EpisodeError(f"{where}: type={t} には path が必要です")
        p = _resolve(v["path"], base)
        _guard(p, where)
        if not p.exists():
            raise EpisodeError(f"{where}: ファイルがありません: {p}")
        v["path"] = str(p)
    ovs = []
    for oi, ov in enumerate(v.get("overlays") or []):
        ov = {"path": ov} if isinstance(ov, str) else dict(ov)
        if not ov.get("path"):
            raise EpisodeError(f"{where}.overlays[{oi}]: path が必要です")
        op = _resolve(ov["path"], base)
        _guard(op, f"{where}.overlays[{oi}]")
        if not op.exists():
            raise EpisodeError(f"{where}.overlays[{oi}]: ファイルがありません: {op}")
        ov["path"] = str(op)
        ovs.append(ov)
    if ovs:
        v["overlays"] = ovs
    if t == "space":
        v.setdefault("template", "planet")
        v.setdefault("params", {})
        if v["params"].get("texture"):
            tp = _resolve(v["params"]["texture"], base)
            _guard(tp, where)
            v["params"]["texture"] = str(tp)
    return v
