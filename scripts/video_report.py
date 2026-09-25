#!/usr/bin/env python3
"""analyze_video.py の解析結果を書き出す。

report.md(日本語レポート) / recipe.json(再現用の数値) / analysis.json(全データ) /
frames.csv(1行=1フレーム) / sheets/(目視確認用コンタクトシート) / timeline_*.png
"""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from video_io import imread, imwrite_jpg

# グラフの色(dataviz既定パレットの先頭3色 = 全組み合わせで色覚多様性チェック合格済み)
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
MUTED, GRID, AXIS = "#898781", "#e1e0d9", "#c3c2b7"
INK, INK2, SURF = "#0b0b0b", "#52514e", "#fcfcfb"

KIND_JA = {
    "cut": "カット", "jump": "ジャンプカット(同一構図)", "zoom_in": "ズームカット(寄り)",
    "zoom_out": "ズームカット(引き)", "dissolve": "ディゾルブ(クロスフェード)",
    "fade_black": "黒フェード", "fade_white": "白フェード", "flash": "白フラッシュ",
    "zoom_transition": "ズームトランジション", "slide": "スライド", "wipe_or_other": "ワイプ等のアニメ",
    "effect": "画面効果(カットなし)",
}
ANIM_JA = {
    "cut": "パッと表示(アニメなし)", "fade": "フェードイン", "pop": "ポップ(拡大して出る)",
    "pop_overshoot": "ポップ(大きく出て少し戻る)", "slide_from_left": "左からスライド",
    "slide_from_right": "右からスライド", "slide_from_top": "上からスライド",
    "slide_from_bottom": "下からスライド", "wipe_left_to_right": "左から右へワイプ(文字送り)",
    "animated": "その他のアニメ", "unknown": "不明",
}


def tc(sec):
    m, s = divmod(max(0.0, float(sec)), 60)
    return f"{int(m):02d}:{s:04.1f}"


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return round(float(o), 5) if math.isfinite(o) else None
    return o


def write_json(path, obj):
    Path(path).write_text(json.dumps(_clean(obj), ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- JSON / CSV

def event_records(S, fps):
    out = []
    for ev in S.events:
        rec = {k: ev[k] for k in ("id", "start", "end", "instant", "moving_before", "dynamic", "shot",
                                  "n_cells", "cell_bbox") if k in ev}
        rec["t"] = round(ev["start"] / fps, 3)
        if ev.get("overlay"):
            rec["overlay"] = ev["overlay"]
        out.append(rec)
    return out


def style_of_event(result):
    m = {}
    for st in result["telop"]["styles"]:
        for eid in st.get("member_ids", []):
            m[eid] = st["id"]
    return m


def build_recipe(r):
    a = r.get("audio", {})
    gs = a.get("gap_stats") or {}
    bgm = a.get("bgm") or {}
    se = (a.get("se_sync") or {}).get("by_event", {})
    zooms = r["camera"]["zooms"]
    med = lambda xs: round(float(np.median(xs)), 3) if xs else None
    easing = defaultdict(int)
    for z in zooms:
        easing[z["easing"]] += 1
    return {
        "_about": "動画の編集を数値化したもの(自動推定)。report.md と sheets/ の画像で目視確認してから使うこと",
        "source": {k: r["source"].get(k) for k in ("title", "id", "webpage_url") if r["source"].get(k)},
        "format": r["format"],
        "pacing": {k: r["pacing"][k] for k in ("edit_points_per_min", "interval_median_sec", "interval_p10_sec",
                                               "interval_p90_sec", "shot_median_sec", "first_30s_edit_points")},
        "cuts": {
            "by_type": r["cuts"]["by_type"],
            "jump_cuts_partial": r["cuts"]["jump_cuts_partial"],
            "zoom_cut_scale_median": r["cuts"]["zoom_cut_scale_median"],
        },
        "transitions": r["transitions"]["by_type"],
        "camera": {
            "smooth_zooms": len(zooms),
            "smooth_zoom_scale_median": med([z["scale"] for z in zooms]),
            "smooth_zoom_sec_median": med([z["sec"] for z in zooms]),
            "smooth_zoom_easing": dict(easing),
            "pans": len(r["camera"]["pans"]),
            "shakes": len(r["camera"]["shakes"]),
        },
        "telop": {
            "events_per_min": r["telop"]["events_per_min"],
            "styles": [{k: v for k, v in st.items() if k not in ("examples", "member_ids", "in_animation_counts")}
                       for st in r["telop"]["styles"]],
        },
        "persistent_overlays": r["persistent_overlays"],
        "audio": {
            "integrated_lufs": (a.get("loudness") or {}).get("integrated_lufs"),
            "true_peak_dbfs": (a.get("loudness") or {}).get("true_peak_dbfs"),
            "gap_median_sec": gs.get("median"), "gap_p90_sec": gs.get("p90"),
            "bgm_present": bgm.get("present"), "bgm_below_speech_db": bgm.get("below_speech_db"),
            "se_on_telop_rate": (se.get("overlay_in") or {}).get("rate"),
            "se_on_cut_rate": (se.get("cut") or {}).get("rate"),
            "se_chance_rate": (a.get("se_sync") or {}).get("chance_rate"),
            "se_level_vs_speech_db": a.get("se_level_vs_speech_db"),
        },
        "color": r["color"],
    }


def write_frames_csv(path, F, S, result):
    n, fps = F.n, F.fps
    shot_of = np.zeros(n, int)
    for sh in S.shots:
        shot_of[sh["start"]:sh["end"] + 1] = sh["index"]
    labels = defaultdict(list)
    for ci in S.cut_info:
        labels[ci["frame"]].append(ci.get("type", "cut") if ci.get("type") != "cut" else "cut")
    for t in S.transitions:
        for f in range(t["start"], t["end"] + 1):
            labels[f].append(t["kind"])
    st_of = style_of_event(result)
    for ev in S.events:
        ov = ev.get("overlay")
        if ov:
            tag = f"{ov['kind']}_{ov['change']}"
            if ev["id"] in st_of:
                tag += f"[{st_of[ev['id']]}]"
            labels[ev["start"]].append(tag)
    for z in S.motion["zooms"]:
        for f in range(z["start"], z["end"] + 1):
            labels[f].append("zoom")
    for p in S.motion["pans"]:
        for f in range(p["start"], p["end"] + 1):
            labels[f].append("pan")
    for s in S.motion["shakes"]:
        for f in range(s["start"], s["end"] + 1):
            labels[f].append("shake")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["frame", "time_sec", "shot", "luma", "luma_std", "saturation", "hist_diff", "edge_change",
                    "change_frac_1f", "change_frac_02s", "zoom_scale", "rotation_deg", "shift_x", "shift_y",
                    "motion_ok", "motion_residual", "camera_move", "labels"])
        for i in range(n):
            w.writerow([i, f"{i / fps:.3f}", shot_of[i], f"{F.luma[i]:.1f}", f"{F.luma_std[i]:.1f}",
                        f"{F.sat[i]:.1f}", f"{F.hist_d[i]:.4f}", f"{F.ecr[i]:.3f}", f"{S.frac1[i]:.3f}",
                        f"{S.fracK[i]:.3f}", f"{F.m_scale[i]:.5f}", f"{F.m_rot[i]:.3f}", f"{F.m_dx[i]:.5f}",
                        f"{F.m_dy[i]:.5f}", int(F.m_ok[i]), f"{F.mc_ratio[i]:.3f}", int(S.cam[i]),
                        " ".join(labels.get(i, []))])


# ---------------------------------------------------------------- コンタクトシート

def _fit(img, w, h):
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    r = cv2.resize(img, (max(1, int(iw * s)), max(1, int(ih * s))), interpolation=cv2.INTER_AREA)
    out = np.full((h, w, 3), 24, np.uint8)
    y, x = (h - r.shape[0]) // 2, (w - r.shape[1]) // 2
    out[y:y + r.shape[0], x:x + r.shape[1]] = r
    return out


def _labelled(img, lines, strip=34):
    h, w = img.shape[:2]
    out = np.full((h + strip, w, 3), 245, np.uint8)
    out[:h] = img
    for k, text in enumerate(lines[:2]):
        cv2.putText(out, text, (6, h + 14 + 15 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)
    return out


def _pages(tiles, cols, rows, prefix):
    paths = []
    if not tiles:
        return paths
    th, tw = tiles[0].shape[:2]
    per = cols * rows
    for p in range(0, len(tiles), per):
        chunk = tiles[p:p + per]
        nrow = math.ceil(len(chunk) / cols)
        sheet = np.full((nrow * (th + 6) + 6, cols * (tw + 6) + 6, 3), 255, np.uint8)
        for k, t in enumerate(chunk):
            y, x = 6 + (k // cols) * (th + 6), 6 + (k % cols) * (tw + 6)
            sheet[y:y + th, x:x + tw] = t
        path = Path(f"{prefix}_{p // per + 1:02d}.jpg")
        imwrite_jpg(path, sheet, 85)
        paths.append(path)
    return paths


def _read(out_dir, rel):
    if not rel:
        return None
    return imread(out_dir / rel)


def _crop_norm(img, bbox, pad=0.25):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    xa, ya = max(0, int((x0 - pad * bw) * w)), max(0, int((y0 - pad * bh) * h))
    xb, yb = min(w, int((x1 + pad * bw) * w)), min(h, int((y1 + pad * bh) * h))
    return img[ya:yb, xa:xb] if yb > ya and xb > xa else img


def write_sheets(out_dir, result, F, S):
    sd = out_dir / "sheets"
    sd.mkdir(exist_ok=True)
    fps = F.fps
    land = F.size[0] >= F.size[1]
    tw, th = (320, 180) if land else (180, 320)
    cut_by_frame = {ci["frame"]: ci for ci in S.cut_info}
    made = {}

    tiles = []
    for sh in S.shots:
        img = _read(out_dir, sh.get("image"))
        if img is None:
            continue
        how = sh["in"]
        ci = cut_by_frame.get(sh["start"])
        if ci and ci.get("type", "cut") != "cut":
            how = ci["type"] + (f" x{ci['scale']:.2f}" if ci.get("scale") and ci["type"].startswith("zoom") else "")
        tiles.append(_labelled(_fit(img, tw, th), [f"#{sh['index']:03d}  {tc(sh['start'] / fps)}  {sh['sec']:.1f}s",
                                                   f"in: {how}"]))
    made["shots"] = _pages(tiles, 5 if land else 8, 6 if land else 3, sd / "shots")

    st_of = style_of_event(result)
    tiles = []
    for ev in S.events:
        ov = ev.get("overlay")
        if not ov or ov["kind"] not in ("text", "box", "graphic") or not ov.get("image"):
            continue
        img = _read(out_dir, ov["image"])
        if img is None:
            continue
        anim = (ov.get("animation") or {}).get("type", "-")
        frames = (ov.get("animation") or {}).get("frames", 0)
        tiles.append(_labelled(_fit(_crop_norm(img, ov["bbox"]), 420, 110),
                               [f"E{ev['id']:05d} {tc(ev['start'] / fps)} {ov['kind']} {ov['change']} "
                                f"style:{st_of.get(ev['id'], '-')}", f"in: {anim} {frames}f  h={ov['line_h_px1080']}px"]))
    made["telops"] = _pages(tiles, 3, 8, sd / "telops")

    rows = []
    by_id = {ev["id"]: ev for ev in S.events}
    for st in result["telop"]["styles"][:8]:
        crops = []
        for eid in st["examples"][:4]:
            ov = by_id[eid].get("overlay") or {}
            img = _read(out_dir, ov.get("image"))
            if img is not None:
                crops.append(_fit(_crop_norm(img, ov["bbox"], 0.15), 300, 100))
        if not crops:
            continue
        crops += [np.full_like(crops[0], 24)] * (4 - len(crops))
        row = np.hstack([np.pad(c, ((0, 0), (0, 6), (0, 0)), constant_values=255) for c in crops])
        head = np.full((30, row.shape[1], 3), 245, np.uint8)
        text = (f"Style {st['id']}  n={st['count']}  y={st['center'][1]}  h={st['line_h_px1080']}px  "
                f"fill {st['fill']} / outline {st['outline']}  in: {st['in_animation']}")
        cv2.putText(head, text, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
        rows.append(np.vstack([head, row, np.full((8, row.shape[1], 3), 255, np.uint8)]))
    if rows:
        wmax = max(r.shape[1] for r in rows)
        rows = [np.pad(r, ((0, 0), (0, wmax - r.shape[1]), (0, 0)), constant_values=255) for r in rows]
        path = sd / "telop_styles.jpg"
        imwrite_jpg(path, np.vstack(rows), 88)
        made["telop_styles"] = [path]

    tiles = []
    for ci in S.cut_info:
        if not ci.get("pair"):
            continue
        b, a = _read(out_dir, ci["pair"][0]), _read(out_dir, ci["pair"][1])
        if b is None or a is None:
            continue
        pair = np.hstack([_fit(b, tw, th), np.full((th, 4, 3), 255, np.uint8), _fit(a, tw, th)])
        extra = f" x{ci['scale']:.2f} center {ci.get('center')}" if ci["type"].startswith("zoom") else \
            f" same {ci.get('same_framing')}"
        tiles.append(_labelled(pair, [f"{tc(ci['frame'] / fps)}  {ci['type']}{extra}", "before | after"]))
    for ev in S.events:
        ov = ev.get("overlay")
        if ov and ov["kind"] == "jump" and ov.get("image"):
            img = _read(out_dir, ov["image"])
            if img is None:
                continue
            h, w = img.shape[:2]
            x0, y0, x1, y1 = ov["bbox"]
            img = img.copy()
            cv2.rectangle(img, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), (0, 140, 255), 3)
            one = _fit(img, tw, th)
            pair = np.hstack([one, np.full((th, 4, 3), 255, np.uint8), np.full_like(one, 245)])
            tiles.append(_labelled(pair, [f"{tc(ev['start'] / fps)}  jump (partial)", "orange box = jumped region"]))
    made["cuts"] = _pages(tiles, 3 if land else 4, 6, sd / "cuts_zoom_jump")
    return made


# ---------------------------------------------------------------- タイムライン図

def _font():
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Yu Gothic", "Meiryo", "BIZ UDGothic", "MS Gothic", "Noto Sans CJK JP", "Noto Sans JP",
                 "IPAexGothic", "IPAGothic", "Hiragino Sans", "TakaoGothic"):
        if cand in names:
            return cand
    return None


def write_timeline(out_dir, result, F, S, A, seg_sec=180):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    font = _font()
    ja = font is not None
    if ja:
        plt.rcParams["font.family"] = font
    L = (lambda j, e: j) if ja else (lambda j, e: e)
    fps, n = F.fps, F.n
    dur = n / fps
    cuts_t = [ci["frame"] / fps for ci in S.cut_info if not ci.get("type", "cut").startswith("zoom")]
    cuts_t += [ev["start"] / fps for ev in S.events if (ev.get("overlay") or {}).get("kind") == "jump"]
    zoomcut_t = [ci["frame"] / fps for ci in S.cut_info if ci.get("type", "cut").startswith("zoom")]
    trans_t = [t["start"] / fps for t in S.transitions if t["kind"] != "effect"]
    styles = result["telop"]["styles"]
    st_of = style_of_event(result)
    top = [st["id"] for st in styles[:3]]
    tel = defaultdict(list)
    for ev in S.events:
        ov = ev.get("overlay")
        if ov and ov["kind"] in ("text", "box") and ov["change"] != "disappear":
            sid = st_of.get(ev["id"])
            tel[sid if sid in top else "other"].append(ev["start"] / fps)
    # ショット内で累積した拡大率(カットで1.0に戻す)
    ls = np.where(F.m_ok, np.log(np.clip(F.m_scale, 1e-3, None)), 0.0)
    zoom = np.ones(n)
    for sh in S.shots:
        a, b = sh["start"], sh["end"]
        zoom[a:b + 1] = np.exp(np.cumsum(np.concatenate([[0.0], ls[a + 1:b + 1]])))
    t_frames = np.arange(n) / fps
    lc = (A or {}).get("loudness_curve") or {}
    lt, lm = np.array(lc.get("t", [])), np.clip(np.array(lc.get("momentary", [])), -60, 0)
    gaps = (A or {}).get("gaps") or []
    onsets = ((A or {}).get("onsets") or {}).get("strong_times", [])

    seg_sec = min(seg_sec, max(10, math.ceil(dur / 10) * 10))  # 短い動画は全体を1枚に
    paths = []
    for k, t0 in enumerate(np.arange(0, dur, seg_sec)):
        t1 = min(dur, t0 + seg_sec)
        fig, axes = plt.subplots(4, 1, figsize=(16, 7.2), sharex=True, facecolor=SURF,
                                 gridspec_kw=dict(height_ratios=[1.0, 1.0, 1.2, 1.6], hspace=0.45))
        for ax in axes:
            ax.set_facecolor(SURF)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            ax.spines["bottom"].set_color(AXIS)
            ax.spines["bottom"].set_linewidth(1)
            ax.tick_params(colors=INK2, labelsize=9, length=0)
            ax.grid(axis="x", color=GRID, linewidth=1)
            ax.set_axisbelow(True)
        sel = lambda xs: [x for x in xs if t0 <= x < t1]

        ax = axes[0]
        for y, xs, col in ((2, cuts_t, C1), (1, trans_t, C2), (0, zoomcut_t, C3)):
            ax.vlines(sel(xs), y - 0.3, y + 0.3, color=col, linewidth=2)
        ax.set_yticks([0, 1, 2], [L("ズームカット", "zoom cut"), L("トランジション", "transition"), L("カット", "cut")])
        ax.set_ylim(-0.7, 2.7)
        ax.set_title(L("編集点", "Edit points"), loc="left", color=INK, fontsize=11)
        ax.legend(handles=[Line2D([], [], color=C1, lw=2, label=L("カット(ジャンプ含む)", "cut (incl. jump)")),
                           Line2D([], [], color=C2, lw=2, label=L("トランジション", "transition")),
                           Line2D([], [], color=C3, lw=2, label=L("ズームカット", "zoom cut"))],
                  loc="upper right", ncol=3, fontsize=8, frameon=False, labelcolor=INK2)

        ax = axes[1]
        rows = top + ["other"]
        cols = [C1, C2, C3][:len(top)] + [MUTED]
        for y, (sid, col) in enumerate(zip(rows, cols)):
            xs = sel(tel.get(sid, []))
            ax.plot(xs, [y] * len(xs), "o", color=col, markersize=7, markeredgecolor=SURF, markeredgewidth=2)
        ax.set_yticks(range(len(rows)), [L(f"スタイル{s}", f"style {s}") if s != "other" else L("その他", "other")
                                         for s in rows])
        ax.set_ylim(-0.7, len(rows) - 0.3)
        ax.set_title(L("テロップの出現(スタイル別)", "Telop appearances by style"), loc="left", color=INK, fontsize=11)
        if len(rows) >= 2:
            ax.legend(handles=[Line2D([], [], marker="o", ls="", color=c, markersize=7,
                                      label=(L(f"スタイル{s}", f"style {s}") if s != "other" else L("その他", "other")))
                               for s, c in zip(rows, cols)], loc="upper right", ncol=len(rows), fontsize=8,
                      frameon=False, labelcolor=INK2)

        ax = axes[2]
        m = (t_frames >= t0) & (t_frames < t1)
        ax.plot(t_frames[m], zoom[m], color=C1, linewidth=2)
        ax.axhline(1.0, color=AXIS, linewidth=1)
        zmax = max(1.1, float(zoom[m].max()) if m.any() else 1.1)
        zmin = min(0.95, float(zoom[m].min()) if m.any() else 0.95)
        ax.set_ylim(zmin - 0.02, zmax + 0.02)
        ax.grid(axis="y", color=GRID, linewidth=1)
        ax.set_title(L("ショット内のズーム(拡大率、カットで1.0に戻る)", "In-shot zoom (scale, resets at cuts)"),
                     loc="left", color=INK, fontsize=11)

        ax = axes[3]
        for g in gaps:
            if g["end"] >= t0 and g["start"] < t1:
                ax.axvspan(max(t0, g["start"]), min(t1, g["end"]), color=GRID, alpha=0.9, linewidth=0)
        if len(lt):
            mm = (lt >= t0) & (lt < t1)
            ax.plot(lt[mm], lm[mm], color=C1, linewidth=1.5)
        ax.vlines(sel(onsets), -3, 0, color=C2, linewidth=2)
        ax.set_ylim(-60, 0)
        ax.grid(axis="y", color=GRID, linewidth=1)
        ax.set_title(L("音声", "Audio"), loc="left", color=INK, fontsize=11)
        ax.set_ylabel("LUFS", color=INK2, fontsize=9)
        ax.legend(handles=[Line2D([], [], color=C1, lw=2, label=L("音量(瞬時)", "loudness (momentary)")),
                           Line2D([], [], color=C2, lw=2, label=L("強い立ち上がり(効果音候補)", "strong onset (SE?)")),
                           Patch(color=GRID, label=L("声の途切れ(間)", "speech gap"))],
                  loc="lower right", ncol=3, fontsize=8, frameon=False, labelcolor=INK2)
        ticks = np.arange(math.floor(t0 / 10) * 10, t1 + 0.01, 10 if seg_sec <= 180 else 30)
        ax.set_xticks(ticks, [tc(t)[:-2] for t in ticks])
        ax.set_xlim(t0, t0 + seg_sec)
        fig.suptitle(L(f"編集タイムライン {tc(t0)[:-2]}〜{tc(t1)[:-2]}", f"Edit timeline {tc(t0)[:-2]}-{tc(t1)[:-2]}"),
                     x=0.01, ha="left", color=INK, fontsize=13)
        path = out_dir / f"timeline_{k + 1:02d}.png"
        fig.savefig(path, dpi=100, facecolor=SURF, bbox_inches="tight")
        plt.close(fig)
        paths.append(path)
    return paths


# ---------------------------------------------------------------- レポート

def _pct(x):
    return "-" if x is None else f"{100 * x:.0f}%"


def write_report(path, r, made):
    fmt, pace, cuts, cam, tel, aud = r["format"], r["pacing"], r["cuts"], r["camera"], r["telop"], r.get("audio", {})
    src = r["source"]
    L = []
    w = L.append
    w(f"# 編集解析レポート: {src.get('title', Path(src.get('path', '')).name)}")
    w("")
    w("自動解析の推定値です。数値は `sheets/` の画像と見比べて確かめてから使ってください。")
    w("元動画・切り出した画像の著作権は制作者にあります(手元での研究用。再配布・素材流用はしない)。")
    w("")
    w("## 1. 基本情報")
    w("")
    w("| 項目 | 値 |")
    w("|---|---|")
    w(f"| 解像度 | {fmt['width']}×{fmt['height']} ({'横' if fmt['orientation'] == 'landscape' else '縦'}動画) |")
    w(f"| フレームレート | {fmt['fps']} fps |")
    w(f"| 長さ | {tc(fmt['duration_sec'])} ({fmt['frames']}フレーム) |")
    if src.get("chapters"):
        w(f"| チャプター | {len(src['chapters'])}個 |")
    w("")

    w("## 2. カットのテンポ")
    w("")
    w(f"- 編集点(カット・トランジション・ジャンプカット): **{pace['edit_points']}回 / 1分あたり{pace['edit_points_per_min']}回**")
    w(f"- 編集点の間隔: 中央値 **{pace['interval_median_sec']}秒** (短い方10% {pace['interval_p10_sec']}秒 / "
      f"長い方10% {pace['interval_p90_sec']}秒)")
    w(f"- 冒頭30秒の編集点: {pace['first_30s_edit_points']}回")
    w(f"- 1分ごとの編集点数: {', '.join(str(x) for x in pace['per_minute'])}")
    w(f"- 画面(ショット)が切り替わった回数: {pace['shots'] - 1}回 (ショットの長さ 中央値{pace['shot_median_sec']}秒)")
    w("")
    w("| カットの種類 | 回数 |")
    w("|---|---|")
    for k, v in sorted(cuts["by_type"].items(), key=lambda kv: -kv[1]):
        w(f"| {KIND_JA.get(k, k)} | {v} |")
    if cuts["jump_cuts_partial"]:
        w(f"| ジャンプカット(人物・アバター部分だけ飛ぶ) | {cuts['jump_cuts_partial']} |")
    if cuts["zoom_cut_scale_median"]:
        w("")
        w(f"ズームカットの倍率: 中央値 **{cuts['zoom_cut_scale_median']}倍** "
          f"(全{len(cuts['zoom_cut_scales'])}回: {', '.join(f'{s:.2f}' for s in cuts['zoom_cut_scales'][:12])})")
    w("")

    w("## 3. トランジション")
    w("")
    items = r["transitions"]["items"]
    if items:
        w("| 時刻 | 種類 | 長さ |")
        w("|---|---|---|")
        for t in items[:40]:
            extra = ""
            if t["kind"].startswith("fade"):
                extra = f" (アウト{t.get('out_frames')}f / イン{t.get('in_frames')}f)"
            w(f"| {tc(t['sec'])} | {KIND_JA.get(t['kind'], t['kind'])} | {t['frames']}フレーム{extra} |")
        if len(items) > 40:
            w(f"| … | 他{len(items) - 40}件 | |")
    else:
        w("トランジションなし(すべてハードカット)。")
    w("")

    w("## 4. ズーム・カメラワーク")
    w("")
    zs = cam["zooms"]
    if zs:
        w(f"ショット内のズーム: **{len(zs)}回**")
        w("")
        w("| 時刻 | 倍率 | 秒数 | 加減速 |")
        w("|---|---|---|---|")
        for z in zs[:30]:
            w(f"| {tc(z['t'])} | {z['scale']:.2f}倍 | {z['sec']}秒 | {z['easing']} |")
    else:
        w("ショット内のズームなし。")
    w("")
    if cam["pans"]:
        w(f"パン/スクロール: {len(cam['pans'])}回 " +
          ", ".join(f"{tc(p['t'])}({p['sec']}秒)" for p in cam["pans"][:10]))
        w("")
    if cam["shakes"]:
        w(f"シェイク(画面揺れ): {len(cam['shakes'])}回 " +
          ", ".join(f"{tc(s['t'])}({s['sec']}秒)" for s in cam["shakes"][:10]))
        w("")

    w("## 5. テロップ・オーバーレイ")
    w("")
    w(f"テロップの出現・切り替え: **{tel['events']}回 (1分あたり{tel['events_per_min']}回)** / "
      f"図形・画像の出現: {tel['graphics']}回")
    oc = tel.get("other_changes") or {}
    if any(oc.values()):
        w("")
        w(f"(テロップ以外の部分変化: 画面内の小さな文字(UI・チャット出力等) {oc.get('ui_text', 0)}回 / "
          f"動き(口パク・瞬き等) {oc.get('motion', 0)}回 / ごく小さな変化 {oc.get('minor', 0)}回)")
    w("")
    if tel["styles"]:
        w("| スタイル | 回数 | 位置 | 文字の高さ(1080p換算) | 行数 | 塗り | 縁 | 縁の太さ | 座布団 | 出方 | 表示時間 |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for st in tel["styles"][:10]:
            anim = ANIM_JA.get(st["in_animation"], st["in_animation"])
            if st.get("in_frames_median"):
                anim += f" {st['in_frames_median']:.0f}f"
            others = {k: v for k, v in st.get("in_animation_counts", {}).items() if k != st["in_animation"]}
            if others:
                anim += " (他: " + ", ".join(f"{ANIM_JA.get(k, k)}×{v}" for k, v in others.items()) + ")"
            w(f"| {st['id']} | {st['count']} | {st['position']} (y={st['center'][1]}) | {st['line_h_px1080']}px | "
              f"{int(round(st['lines_median'] or 1))} | {st['fill'] or '-'} | {st['outline'] or '-'} | "
              f"{st['outline_px1080'] or '-'}px | {st['box_color'] if st['box'] else 'なし'} | {anim} | "
              f"{st['display_sec_median'] or '-'}秒 |")
        w("")
        w("フォントは自動判定できないため `sheets/telop_styles.jpg` で目視確認する。")
    else:
        w("テロップは検出されませんでした。")
    w("")
    po = r["persistent_overlays"]
    if po:
        w("ずっと同じ場所に出ている要素(ロゴ・常設の見出し等):")
        w("")
        for o in po[:8]:
            w(f"- {o['position']} bbox={o['bbox']} 色{o['mean_color']} (画面の{100 * o['area_frac']:.1f}%)")
        w("")

    w("## 6. 音声")
    w("")
    if aud.get("present"):
        ld = aud.get("loudness") or {}
        w(f"- ラウドネス: **{ld.get('integrated_lufs')} LUFS** (YouTubeの基準は-14) / "
          f"トゥルーピーク {ld.get('true_peak_dbfs')} dBFS / LRA {ld.get('lra_lu')} LU")
        gs = aud.get("gap_stats")
        if gs:
            med = gs["median"]
            level = ("かなり詰めている(強めのジェットカット)" if med <= 0.2 else "詰め気味" if med <= 0.35 else
                     "自然な間" if med <= 0.6 else "ゆったり")
            w(f"- 声の途切れ(間): 中央値 **{med}秒** → {level} (1分あたり{gs['per_min']}回, 0.3秒以下が{_pct(gs['share_le_0_3s'])})")
        if aud.get("speech_ratio") is not None:
            w(f"- 声が鳴っている割合: {_pct(aud['speech_ratio'])}")
        bgm = aud.get("bgm")
        if bgm:
            if bgm["present"]:
                w(f"- BGM: **あり** (声より約{bgm['below_speech_db']} dB小さい / 間の部分で{bgm['level_dbfs']} dBFS / "
                  f"{'音楽的' if bgm.get('tonal') else '雑音的'})")
            else:
                w(f"- BGM: なし(間の部分は{bgm['level_dbfs']} dBFSでほぼ無音)")
        se = aud.get("se_sync") or {}
        ch = se.get("chance_rate")
        for key, ja in (("overlay_in", "テロップ・図形の出現"), ("cut", "カット"), ("zoom_cut", "ズームカット"),
                        ("transition", "トランジション")):
            s = (se.get("by_event") or {}).get(key)
            if s:
                verdict = "効果音を合わせている" if s["rate"] >= max(0.3, 2 * (ch or 0) + 0.1) else "はっきりした同期なし"
                if s["events"] < 5:
                    verdict += "(件数が少なく参考値)"
                w(f"- {ja}と同時に強い音: {s['with_se']}/{s['events']}回 ({_pct(s['rate'])}, 偶然なら{_pct(ch)}) → {verdict}")
        if aud.get("se_level_vs_speech_db") is not None:
            w(f"- 効果音候補の大きさ: 声より{aud['se_level_vs_speech_db']:+.0f} dB")
    else:
        w("音声なし(または解析をスキップ)。")
    w("")

    w("## 7. 色")
    w("")
    c = r["color"]
    w(f"平均の明るさ {c['luma_mean']}/255、彩度 {c['saturation_mean']}/255、コントラスト(輝度の標準偏差) {c['contrast_mean']}")
    w("")

    w("## 8. 目視確認用ファイル")
    w("")
    labels = {"shots": "ショット一覧", "telops": "テロップ一覧(出現時の切り抜き)", "telop_styles": "テロップのスタイル見本",
              "cuts": "ズームカット・ジャンプカットの前後比較", "timeline": "時間軸のタイムライン図"}
    for key, ja in labels.items():
        ps = made.get(key) or []
        if ps:
            w(f"- {ja}: " + ", ".join(f"`{Path(p).relative_to(Path(path).parent).as_posix()}`" for p in ps))
    w("- 全フレームの数値: `frames.csv` / 検出結果の全データ: `analysis.json` / 再現用の数値: `recipe.json`")
    w("")
    w("## 注意")
    w("")
    w("- カットと同時に切り替わったテロップは「部分変化」として数えられない(テロップ回数は少なめに出る)")
    w("- ズームカットの倍率・中心、テロップの色・縁の太さは前後フレームの差分からの推定")
    w("- 効果音は「強い音の立ち上がり」を数えているだけで、声の破裂音なども混ざる(偶然一致率と比べて判断)")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def write_all(out_dir, result, F, S, A):
    out_dir = Path(out_dir)
    fps = F.fps
    full = dict(result)
    full["shots"] = [{**sh, "t": round(sh["start"] / fps, 3)} for sh in S.shots]
    full["cut_detail"] = [{**ci, "t": round(ci["frame"] / fps, 3)} for ci in S.cut_info]
    full["events"] = event_records(S, fps)
    full["audio_detail"] = A
    write_json(out_dir / "analysis.json", full)
    write_json(out_dir / "recipe.json", build_recipe(result))
    write_frames_csv(out_dir / "frames.csv", F, S, result)
    made = write_sheets(out_dir, result, F, S)
    made["timeline"] = write_timeline(out_dir, result, F, S, A)
    write_report(out_dir / "report.md", result, made)
    return made
