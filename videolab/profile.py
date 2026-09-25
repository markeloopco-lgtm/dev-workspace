"""スタイルプロファイル: 解析結果を「作風の数値仕様」に要約し、集約・比較する。

プロファイルは数値だけ(画像・音声・台詞を含まない)なので、参考動画の統計として
リポジトリに置いてよい。制作側(produce)はこの値を目標に編集テンポ・カメラワーク・
テロップ・音量を決め、compare は自作動画を同じ物差しで採点する。
"""

import json
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

PROFILE_VERSION = 1


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def _mix(values, weights=None) -> dict:
    c = Counter()
    for i, v in enumerate(values):
        if v is None:
            continue
        c[v] += weights[i] if weights is not None else 1.0
    tot = sum(c.values())
    return {k: round(v / tot, 3) for k, v in sorted(c.items(), key=lambda kv: -kv[1])} if tot else {}


def _merge_palette(weighted_colors: list, k: int = 8) -> list:
    """[(hex, weight)] を色空間でk個にまとめる。"""
    import cv2

    if not weighted_colors:
        return []
    cols = np.array([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h, _ in weighted_colors], np.float32)
    w = np.array([wt for _, wt in weighted_colors], np.float32)
    # 重みを反映するため重複サンプリング
    reps = np.maximum(1, np.round(w / w.max() * 20)).astype(int)
    data = np.repeat(cols, reps, axis=0)
    k = min(k, len(np.unique(data, axis=0)))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1.0)
    cv2.setRNGSeed(0)
    _, lab, cen = cv2.kmeans(data, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    cnt = np.bincount(lab.ravel(), minlength=k) / len(lab)
    order = np.argsort(-cnt)
    return [["#%02x%02x%02x" % tuple(int(v) for v in cen[i]), round(float(cnt[i]), 3)]
            for i in order]


def build_profile(name: str, meta: dict, shots: list, frames: dict, audio: dict,
                  vlm: dict = None) -> dict:
    """1本の解析結果からプロファイルを作る。frames は analyze の arrays(dict of ndarray)。

    vlm(Gemini分類 vlm.json)があれば映像の出どころの構成比(visual.source_mix)も入れる。
    """
    v = meta["video"]
    fps = v["fps"] or 30.0
    duration = meta["n_frames_analyzed"] * meta.get("step", 1) / fps
    real = [s for s in shots if s.get("kind") != "black"]
    lens = np.array([s["duration"] for s in real]) if real else np.zeros(0)
    trans = [t for t in meta["transitions"] if t["kind"] != "flash"]
    cut_times = np.array([t["t"] for t in trans])
    first = 30.0 if duration > 60 else duration / 2
    n_first = int((cut_times < first).sum())
    rest_minutes = max((duration - first) / 60.0, 1e-6)

    tmix = _mix([s["transition_in"] for s in real if s["transition_in"] != "start"])
    diss = [s["transition_len"] for s in real if s["transition_in"] == "dissolve"]
    weights = [s["duration"] for s in real]
    cams = [s.get("camera") for s in real]
    moving = [s for s in real if s.get("camera") not in ("static", "static_action", "unknown", None)]
    zoom_moves = [s["zoom_speed"] for s in moving if s["camera"] in ("zoom_in", "zoom_out")]
    pan_moves = [s["pan_speed"] for s in moving if s["camera"].startswith(("pan", "tilt"))]
    known = [s for s in real if s.get("camera") not in ("unknown", None)]
    luma = frames["luma"]
    telop_shots = [s for s in real if s.get("text_ratio") is not None]
    telop_y = [s["telop_y"] for s in real if s.get("telop_y") is not None]
    text_col = frames.get("text")
    pal = []
    for s in real:
        for hx, share in s.get("palette", []):
            pal.append((hx, share * s["duration"]))

    prof = {
        "version": PROFILE_VERSION,
        "source": {"videos": [name], "n": 1},
        "format": {"width": v["width"], "height": v["height"], "fps": _r(fps, 3),
                   "duration": _r(duration, 2)},
        "editing": {
            "n_shots": len(real),
            "shot_len_mean": _r(lens.mean(), 3) if len(lens) else None,
            "shot_len_median": _r(np.median(lens), 3) if len(lens) else None,
            "shot_len_p10": _r(np.percentile(lens, 10), 3) if len(lens) else None,
            "shot_len_p90": _r(np.percentile(lens, 90), 3) if len(lens) else None,
            "cuts_per_min": _r(len(trans) / max(duration / 60, 1e-6), 2),
            "cuts_per_min_first30s": _r(n_first / (first / 60.0), 2) if first > 0 else None,
            "cuts_per_min_rest": _r((len(trans) - n_first) / rest_minutes, 2),
            "transition_mix": tmix,
            "dissolve_len_mean": _r(np.mean(diss), 3) if diss else None,
            "flash_per_min": _r(sum(1 for t in meta["transitions"] if t["kind"] == "flash")
                                / max(duration / 60, 1e-6), 2),
        },
        "camera": {
            "move_mix": _mix(cams, weights),
            "moving_ratio": _r(len(moving) / len(known), 3) if known else None,
            "zoom_speed_median": _r(np.median(zoom_moves), 4) if zoom_moves else None,
            "pan_speed_median": _r(np.median(pan_moves), 4) if pan_moves else None,
            "easing_mix": _mix([s.get("easing") for s in moving]),
            "motion_median": _r(np.median(frames["motion"]), 5),
        },
        "color": {
            "luma_mean": _r(luma.mean()), "contrast_mean": _r(frames["contrast"].mean()),
            "sat_mean": _r(frames["sat"].mean()),
            "colorfulness_mean": _r(frames["colorfulness"].mean(), 2),
            "dark_ratio": _r((luma < 0.2).mean(), 3),
            "palette": _merge_palette(pal),
        },
        "telop": {
            "shot_ratio": _r(np.mean([s["text_ratio"] > 0.5 for s in telop_shots]), 3)
            if telop_shots else None,
            "frame_ratio": _r(np.nanmean(text_col), 3) if text_col is not None and
            np.isfinite(text_col).any() else None,
            "y_median": _r(np.median(telop_y), 3) if telop_y else None,
            "colors_observed": meta.get("telop_colors", []),
        },
        "audio": {k: audio.get(k) for k in (
            "lufs_integrated", "true_peak", "lra", "speech_ratio", "chars_per_sec",
            "gap_median", "bgm_rel_db", "onsets_per_min", "loud_events_per_min",
            "speech_source")} if audio.get("has_audio") else {"has_audio": False},
        "dialogue": {k: audio.get(k) for k in (
            "n_voices", "turns_per_min", "main_voice_share", "turn_len_median")},
        "structure": {"first_question_s": audio.get("first_question_s")},
    }
    if vlm and vlm.get("category_mix"):
        prof["visual"] = {"source_mix": vlm["category_mix"],
                          "ai_suspect_ratio": vlm.get("ai_suspect_ratio")}
    return prof


# ---------------------------------------------------------------- 集約

def _walk(d, prefix=""):
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and not k.endswith("_mix"):
            yield from _walk(v, p)
        else:
            yield p, v


def get_path(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(d: dict, path: str, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def aggregate(profiles: list) -> dict:
    """複数動画のプロファイルを1つの目標スタイルにまとめる(数値は中央値、構成比は平均)。"""
    if not profiles:
        raise ValueError("プロファイルが1つもありません")
    out = {"version": PROFILE_VERSION,
           "source": {"videos": [v for p in profiles for v in p["source"]["videos"]],
                      "n": sum(p["source"]["n"] for p in profiles)}}
    paths = {}
    for p in profiles:
        for path, val in _walk({k: v for k, v in p.items() if k not in ("version", "source")}):
            paths.setdefault(path, []).append(val)
    for path, vals in paths.items():
        vals = [v for v in vals if v is not None]
        if not vals:
            _set_path(out, path, None)
        elif path in ("format.width", "format.height", "format.fps"):
            # 解像度・fpsは中央値だと 1600x900 や 45fps のような実在しない値になるので最頻値
            _set_path(out, path, Counter(vals).most_common(1)[0][0])
        elif path.endswith("_mix"):
            keys = {k for v in vals for k in v}
            mix = {k: float(np.mean([v.get(k, 0.0) for v in vals])) for k in keys}
            tot = sum(mix.values()) or 1.0
            _set_path(out, path, {k: round(x / tot, 3) for k, x in
                                  sorted(mix.items(), key=lambda kv: -kv[1])})
        elif path.endswith("palette") or path.endswith("colors_observed"):
            merged = [(c, w) for v in vals for c, w in v]
            _set_path(out, path, _merge_palette(merged) if merged else [])
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            _set_path(out, path, round(float(np.median(vals)), 4))
        else:
            _set_path(out, path, Counter(map(str, vals)).most_common(1)[0][0])
    return out


# ---------------------------------------------------------------- 比較

# (パス, 表示名, 比較方法, 許容幅, 高すぎる時の助言, 低すぎる時の助言)
CHECKS = [
    ("editing.shot_len_median", "ショット長(中央値, 秒)", "rel", 0.25,
     "カットが長い。ナレーション1文ごとに映像を切り替えるか、素材を増やす",
     "カットが細かすぎる。1文を2カット以上に割らない"),
    ("editing.cuts_per_min_first30s", "冒頭30秒のカット数/分", "rel", 0.35,
     "冒頭が慌ただしい", "冒頭(つかみ)のテンポが遅い。最初の30秒はカットを増やす"),
    ("editing.cuts_per_min_rest", "本編のカット数/分", "rel", 0.3,
     "本編のカットが多すぎる", "本編のカットが少ない"),
    ("editing.transition_mix.cut", "ハードカットの割合", "abs", 0.15,
     "ディゾルブ等の柔らかい切替が少ない", "ディゾルブ/暗転が多すぎる"),
    ("camera.moving_ratio", "カメラが動くショットの割合", "abs", 0.15,
     "カメラを動かしすぎ。静止カットも混ぜる",
     "静止画のまま映す時間が長い。ゆっくりズーム/パンを入れる"),
    ("camera.zoom_speed_median", "ズーム速度(倍率変化/秒)", "rel", 0.5,
     "ズームが速すぎる", "ズームが遅すぎる(変化が分からない)"),
    ("camera.pan_speed_median", "パン速度(画面幅/秒)", "rel", 0.5,
     "パンが速すぎる", "パンが遅すぎる"),
    ("color.luma_mean", "平均の明るさ", "abs", 0.06, "画が明るすぎる", "画が暗すぎる"),
    ("color.contrast_mean", "コントラスト", "abs", 0.04, "コントラストが強すぎる",
     "眠い画(コントラスト不足)"),
    ("color.sat_mean", "平均の彩度", "abs", 0.06, "色が派手すぎる", "色が地味すぎる"),
    ("color.dark_ratio", "暗い画面の割合", "abs", 0.15, "暗い(宇宙系)カットが多すぎる",
     "暗い(宇宙系)カットが少ない"),
    ("telop.frame_ratio", "テロップ表示率", "abs", 0.15, "テロップが多すぎる",
     "テロップが少ない。ナレーションの要点を字幕で出す"),
    ("telop.y_median", "テロップの縦位置(0=上,1=下)", "abs", 0.06, "テロップが下すぎる",
     "テロップが上すぎる"),
    ("audio.lufs_integrated", "ラウドネス(LUFS)", "abs", 1.5, "音量が大きすぎる",
     "音量が小さすぎる"),
    ("audio.chars_per_sec", "話速(文字/秒)", "rel", 0.12, "早口。TTSの速度を下げる",
     "ゆっくりすぎる。TTSの速度を上げる"),
    ("audio.gap_median", "文と文の間(秒)", "abs", 0.15, "間が長い", "間が詰まりすぎ"),
    ("audio.bgm_rel_db", "BGMの音量(発話比 dB)", "abs", 4.0, "BGMが大きい",
     "BGMが小さい/無い"),
    ("audio.speech_ratio", "ナレーションが鳴っている割合", "abs", 0.1,
     "喋りっぱなし。見せ場で間を取る", "無言の時間が長い"),
    ("dialogue.turns_per_min", "話者交代の回数/分(推定)", "rel", 0.4,
     "掛け合いが忙しすぎる。1人の説明を長めに", "一人語りが長い。専門家役との掛け合いを増やす"),
    ("structure.first_question_s", "最初の問いかけまでの秒数", "abs", 5.0,
     "問いかけが遅い。冒頭で「もしも〜したら？」をすぐ出す", "（早いのは問題なし）"),
]


def _metric(prof: dict, path: str):
    """比較用の値。構成比(*_mix)に項目が無いのは「0%」であって「測れていない」ではない。"""
    v = get_path(prof, path)
    if v is None and "." in path:
        parent_path = path.rsplit(".", 1)[0]
        parent = get_path(prof, parent_path)
        if parent_path.endswith("_mix") and isinstance(parent, dict) and parent:
            return 0.0
    return v


def _has_move(prof: dict, prefixes: tuple) -> bool:
    mix = get_path(prof, "camera.move_mix") or {}
    return any(k.startswith(prefixes) and v > 0 for k, v in mix.items())


def compare(target: dict, cand: dict) -> dict:
    rows = []
    for path, label, kind, tol, hi_msg, lo_msg in CHECKS:
        tv, cv = _metric(target, path), _metric(cand, path)
        if tv is not None and cv is None and get_path(cand, "camera.move_mix"):
            # 目標にはズーム/パンがあるのに、自作側に1回も無い → 測れないのではなく「無い」
            missing = {"camera.zoom_speed_median": ("zoom", "ズームが1回も無い。ゆっくり寄る/引くカットを入れる"),
                       "camera.pan_speed_median": (("pan", "tilt"), "パンが1回も無い。横に流すカットを入れる")}
            if path in missing and not _has_move(cand, missing[path][0] if isinstance(
                    missing[path][0], tuple) else (missing[path][0],)):
                rows.append({"metric": path, "label": label, "target": tv, "actual": None,
                             "status": "ng", "advice": missing[path][1]})
                continue
        if tv is None or cv is None:
            rows.append({"metric": path, "label": label, "target": tv, "actual": cv,
                         "status": "skip", "advice": "どちらかの値が測れていない"})
            continue
        diff = cv - tv
        limit = tol * abs(tv) if kind == "rel" else tol
        ok = abs(diff) <= limit or (path == "structure.first_question_s" and diff < 0)
        warn = not ok and abs(diff) <= 2 * limit
        rows.append({
            "metric": path, "label": label, "target": tv, "actual": cv,
            "diff": round(diff, 4), "tolerance": round(limit, 4),
            "status": "ok" if ok else ("warn" if warn else "ng"),
            "advice": "" if ok else (hi_msg if diff > 0 else lo_msg),
        })
    tp = get_path(cand, "audio.true_peak")
    if tp is not None:
        rows.append({"metric": "audio.true_peak", "label": "トゥルーピーク(dBFS)", "target": -1.0,
                     "actual": tp, "diff": round(tp + 1.0, 2), "tolerance": 0.0,
                     "status": "ok" if tp <= -1.0 else "ng",
                     "advice": "" if tp <= -1.0 else "音割れの恐れ。リミッターで-1dBFS以下に"})
    scored = [r for r in rows if r["status"] != "skip"]
    score = sum({"ok": 1.0, "warn": 0.5, "ng": 0.0}[r["status"]] for r in scored)
    return {"score": round(100 * score / len(scored), 1) if scored else None,
            "n_checked": len(scored), "rows": rows}


def compare_markdown(result: dict, target_name: str, cand_name: str) -> str:
    mark = {"ok": "OK", "warn": "△", "ng": "NG", "skip": "-"}
    lines = ["# スタイル差分レポート", "",
             f"- 目標: `{target_name}`", f"- 対象: `{cand_name}`",
             f"- **一致度スコア: {result['score']} / 100**（{result['n_checked']}項目。OK=1, △=0.5, NG=0）",
             "", "| 判定 | 項目 | 目標 | 実測 | 助言 |", "|---|---|---|---|---|"]

    def fmt(x):
        return "-" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))

    order = {"ng": 0, "warn": 1, "ok": 2, "skip": 3}
    for r in sorted(result["rows"], key=lambda r: order[r["status"]]):
        lines.append(f"| {mark[r['status']]} | {r['label']} | {fmt(r['target'])} | "
                     f"{fmt(r['actual'])} | {r['advice']} |")
    lines += ["", "数値で測れない要素（台本の面白さ・CGの作り込み・声の演技）は"
              "このスコアに含まれません。人の目と耳で確認してください。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 入出力

def load_profile(path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / "profile.json"
    if not path.exists():
        raise FileNotFoundError(f"スタイルプロファイルがありません: {path}")
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"スタイルプロファイルの書き方に誤りがあります: {path}\n{e}") from e


def save_profile(prof: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in (".yaml", ".yml"):
        header = ("# スタイルプロファイル (videolab aggregate で生成。数値のみ・映像/音声は含まない)\n"
                  "# 値を手で調整してもよい。produce はこの値を目標に編集する。\n")
        path.write_text(header + yaml.safe_dump(prof, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    else:
        path.write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
