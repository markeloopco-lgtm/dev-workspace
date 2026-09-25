"""解析レポート(report.html): 解析フォルダ → ブラウザで開く1枚のHTML。

外部CDN・ネット接続なしで開けるよう、CSS・SVGグラフ・JavaScriptはすべて埋め込む。
キーフレーム画像だけは相対パス(keyframes/shot_0001.jpg)で参照するので、
report.html は解析フォルダに置いたまま開く(フォルダごとなら移動してよい)。

  1. 見出し        : 動画名・解像度・fps・長さ
  2. 要点カード    : ショット長・カット頻度・カメラワーク・テロップ・音量・話速を一言で
  3. タイムライン  : 明るさ/彩度/動き/切替/ズーム/パン/テロップ/音量を同じ時間軸で縦に並べる
                     (マウスを乗せると値、クリックでそのショットの行へ移動)
  4. 分布          : ショット長ヒストグラム・カメラワーク/イージング/遷移の内訳
  5. 色            : 全体の配色・テロップの色
  6. ショット一覧  : 代表フレーム・カメラ・配色・そのショットの間に流れるナレーション

どの入力ファイルが欠けても(音声なし・VLM注釈なし等)、その部分を省いて出力する。
"""

import csv
import html
import json
import math
import re
from pathlib import Path

import numpy as np

MAX_POINTS = 2000       # タイムライン1段あたりの最大点数(ファイルサイズ対策)
VB_W = 1000             # タイムラインSVGの横座標(viewBox)。実際の幅には伸縮する
YOUTUBE_LUFS = -14.0

NOTICE = ("このレポート(台詞・字幕の文字を含む)と keyframes/・filmstrips/・contact_sheet.jpg・"
          "transcript.json は参考動画の複製です。個人の分析用に留め、公開・再配布・制作素材への"
          "流用はしないでください（著作権法30条の4の情報解析の範囲で利用）。人に見せる場合は "
          "vlab purge の後に vlab report で作り直した、数値だけのレポートにしてください。")

CAMERA_JA = {
    "static": "静止", "static_action": "静止(画面内に動き)", "zoom_in": "ズームイン",
    "zoom_out": "ズームアウト", "pan_left": "左パン", "pan_right": "右パン",
    "tilt_up": "上ティルト", "tilt_down": "下ティルト", "rotate": "回転",
    "orbit": "回り込み", "unknown": "不明",
}
EASING_JA = {
    "linear": "等速", "ease_in_out": "ゆっくり→速く→ゆっくり",
    "ease_in": "だんだん速く", "ease_out": "だんだん遅く",
}
TRANS_JA = {"start": "冒頭", "cut": "カット", "dissolve": "ディゾルブ",
            "fade_black": "暗転", "flash": "フラッシュ"}
# 遷移の種類 → 色(カテゴリ色は固定順)。形も変えて色だけに頼らない
TRANS_COLOR = {"cut": "var(--k1)", "dissolve": "var(--k2)", "fade_black": "var(--k3)",
               "flash": "var(--k4)"}
SPEECH_SRC_JA = {"transcript": "字幕/文字起こしから", "energy_vad": "音量からの推定・精度低め"}


# ---------------------------------------------------------------- 読み込み

def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_frames(path: Path):
    """frames.csv → {列名: float配列}。空欄はNaN。"""
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return None
    head, body = rows[0], rows[1:]

    def num(s):
        try:
            return float(s) if s != "" else math.nan
        except ValueError:
            return math.nan

    return {name: np.array([num(r[j]) if j < len(r) else math.nan for r in body])
            for j, name in enumerate(head)}


def _segments(items) -> list:
    """[{start,end,text}] を [(start, end, text)] に(壊れた行は捨てる)。"""
    if isinstance(items, dict):
        items = items.get("segments")
    out = []
    for s in items if isinstance(items, list) else []:
        try:
            a, b = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((a, b, str(s.get("text") or "").strip()))
    return sorted(out)


# ---------------------------------------------------------------- 書式

def _esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _fin(x):
    """有限の数値ならfloat、それ以外(None・NaN・±inf・文字列)はNone。"""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _get(d, path: str):
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _tc(t: float, fps: float) -> str:
    """秒 → mm:ss.ff(ffは秒内のフレーム番号)。1時間以上は h:mm:ss.ff。"""
    t = max(0.0, t) + 1e-6
    sec = int(t)
    ff = min(int((t - sec) * fps), max(0, int(math.ceil(fps)) - 1))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{ff:02d}" if h else f"{m:02d}:{s:02d}.{ff:02d}"


def _mmss(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _dur_ja(t: float) -> str:
    return f"{t:.1f}秒" if t < 60 else f"{int(t // 60)}分{int(round(t % 60)):02d}秒"


def _label(table: dict, key) -> str:
    return table.get(key, key or "—") if key is not None else "—"


def _clean(o):
    """JSON埋め込み用: NaN/±inf を null に、numpy型を素のPython型に。"""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o


def _rounded(arr, nd: int) -> list:
    return [round(float(v), nd) if math.isfinite(v) else None for v in arr]


# ---------------------------------------------------------------- SVG部品

def _buckets(n: int):
    """n点を最大MAX_POINTS区間に分ける。返り値: (代表index, 区間境界 or None)。"""
    if n <= MAX_POINTS:
        return np.arange(n), None
    edges = np.linspace(0, n, MAX_POINTS + 1).astype(int)
    return (edges[:-1] + edges[1:]) // 2, edges


def _reduce(v: np.ndarray, idx: np.ndarray, edges, how: str) -> np.ndarray:
    """区間ごとに間引く。max=瞬間的な山を残す / mean=平均 / pick=代表点の値。"""
    if edges is None or how == "pick":
        return v[idx].astype(float)
    fin = np.isfinite(v)
    if how == "max":
        r = np.maximum.reduceat(np.where(fin, v, -np.inf), edges[:-1])
        r[~np.isfinite(r)] = np.nan
        return r
    s = np.add.reduceat(np.where(fin, v, 0.0), edges[:-1])
    c = np.add.reduceat(fin.astype(float), edges[:-1])
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0, s / np.maximum(c, 1), np.nan)


def _scale_y(v, lo, hi, h, pad=3.0):
    y = h - pad - (np.asarray(v, float) - lo) / ((hi - lo) or 1.0) * (h - 2 * pad)
    return np.clip(y, 0, h)


def _runs(x: np.ndarray, y: np.ndarray, breaks=None) -> list:
    """NaN と breaks(True=直前と繋げない)で折れ線を区切る → [(xs, ys), ...]。"""
    out, cur = [], []
    for i in range(len(x)):
        if not math.isfinite(y[i]):
            if cur:
                out.append(cur)
            cur = []
            continue
        if breaks is not None and breaks[i] and cur:
            out.append(cur)
            cur = []
        cur.append((x[i], y[i]))
    if cur:
        out.append(cur)
    return out


def _line_d(runs: list) -> str:
    parts = []
    for r in runs:
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in r[1:])
        parts.append(f"M{r[0][0]:.1f},{r[0][1]:.1f}" + (f"L{pts}" if pts else "h0.1"))
    return "".join(parts)


def _area_d(runs: list, base: float) -> str:
    parts = []
    for r in runs:
        if len(r) < 2:
            continue
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in r)
        parts.append(f"M{r[0][0]:.1f},{base:.1f}L{pts} {r[-1][0]:.1f},{base:.1f}Z")
    return "".join(parts)


def _hline(y: float, cls: str = "gl") -> str:
    return f'<path class="{cls}" d="M0,{y:.1f}H{VB_W}"/>'


def _strip_svg(h: int, inner: str) -> str:
    return (f'<svg class="strip" viewBox="0 0 {VB_W} {h}" preserveAspectRatio="none" '
            f'height="{h}" data-h="{h}" aria-hidden="true">{inner}</svg>')


def _series(x, v, lo, hi, h, color, area=True, base=None, breaks=None) -> str:
    y = np.where(np.isfinite(v), _scale_y(np.nan_to_num(v), lo, hi, h), np.nan)
    runs = _runs(x, y, breaks)
    out = ""
    if area:
        b = float(_scale_y(lo if base is None else base, lo, hi, h))
        out += f'<path class="ar" style="fill:{color}" d="{_area_d(runs, b)}"/>'
    return out + f'<path class="ln" style="stroke:{color}" d="{_line_d(runs)}"/>'


def _key(kind: str, color: str) -> str:
    """凡例の小さな見本(線/帯/縦線)。"""
    if kind == "line":
        g = f'<path d="M1,5H13" style="stroke:{color}" class="kl"/>'
    elif kind == "band":
        g = f'<rect x="1" y="1" width="12" height="8" rx="2" style="fill:{color}" opacity=".45"/>'
    elif kind == "tick":
        g = f'<path d="M7,0V10" style="stroke:{color}" class="kl"/>'
    else:  # short tick at top
        g = f'<path d="M7,0V5" style="stroke:{color};stroke-width:3" class="kl"/>'
    return f'<svg class="key" viewBox="0 0 14 10" width="14" height="10" aria-hidden="true">{g}</svg>'


def _legend(items: list) -> str:
    return '<div class="lg">' + "".join(
        f'<span>{_key(k, c)}{_esc(t)}</span>' for k, c, t in items) + "</div>"


def _nice_step(span: float, target: int = 10) -> float:
    for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800):
        if span / s <= target:
            return s
    return 3600.0


# ---------------------------------------------------------------- 1. 見出し

def _header(name: str, meta: dict, prof: dict, T: float, out_dir: Path, has_audio) -> str:
    v = (meta or {}).get("video") or {}
    fmt = (prof or {}).get("format") or {}
    w, h = v.get("width") or fmt.get("width"), v.get("height") or fmt.get("height")
    fps = _fin(v.get("fps")) or _fin(fmt.get("fps"))
    full = _fin(v.get("duration")) or _fin(fmt.get("duration")) or T
    facts = []
    if w and h:
        facts.append(f"{w}×{h}")
    if fps:
        facts.append(f"{fps:g} fps")
    facts.append(f"長さ {_mmss(full)}（{full:.1f}秒）")
    n = (meta or {}).get("n_frames_analyzed")
    if n:
        step = meta.get("step", 1) or 1
        facts.append(f"{n:,}フレームを解析" + ("（全フレーム）" if step == 1 else f"（{step}フレームおき）"))
    if full and T < full - 1.0:
        facts.append(f"先頭{_dur_ja(T)}のみ解析")
    if has_audio is not None:
        facts.append("音声あり" if has_audio else "音声なし")
    files = [(f, lab) for f, lab in (("contact_sheet.jpg", "全ショット一覧画像"),
                                      ("frames.csv", "フレーム毎の数値(CSV)"),
                                      ("shots.csv", "ショット毎の数値(CSV)"),
                                      ("profile.json", "スタイルプロファイル"))
             if (out_dir / f).exists()]
    links = " ".join(f'<a href="{_esc(f)}">{_esc(lab)}</a>' for f, lab in files)
    return (
        '<header class="hd"><div class="eyebrow">動画スタイル解析レポート</div>'
        f'<h1>{_esc(name)}</h1><div class="facts">'
        + "".join(f"<span>{_esc(f)}</span>" for f in facts) + "</div>"
        + (f'<div class="files">関連ファイル: {links}</div>' if links else "")
        + '<p class="lead">参考動画を1フレームずつ計測し、編集テンポ・カメラワーク・色・テロップ・音を'
          '数値にしたものです。まず「要点」で作風をつかみ、「タイムライン」で時間ごとの変化を、'
          '「ショット一覧」でカットとナレーションの対応を確認してください。</p></header>')


# ---------------------------------------------------------------- 2. 要点カード

def _card(label: str, value: str, unit: str, expl: str) -> str:
    u = f'<span class="unit">{_esc(unit)}</span>' if unit and value != "—" else ""
    return (f'<div class="card"><div class="c-lab">{_esc(label)}</div>'
            f'<div class="c-val">{_esc(value)}{u}</div><div class="c-exp">{_esc(expl)}</div></div>')


def _cards(prof: dict, aud: dict, T: float) -> str:
    ed, cam = prof.get("editing") or {}, prof.get("camera") or {}
    col, tel = prof.get("color") or {}, prof.get("telop") or {}
    au = {**(prof.get("audio") or {}), **(aud or {})}
    dur = _fin(_get(prof, "format.duration")) or T
    cards = []

    n = ed.get("n_shots")
    cards.append(_card("ショット数", "—" if n is None else str(n), "カット",
                       f"{_dur_ja(dur)}の動画を{n}カットで構成（黒画面を除く）" if n else "測定できず"))
    med, mean = _fin(ed.get("shot_len_median")), _fin(ed.get("shot_len_mean"))
    cards.append(_card("ショットの長さ(中央値)", "—" if med is None else f"{med:.1f}", "秒",
                       f"約{med:.1f}秒ごとに画が切り替わる" + (f"（平均{mean:.1f}秒）" if mean else "")
                       if med else "測定できず"))
    f30, rest = _fin(ed.get("cuts_per_min_first30s")), _fin(ed.get("cuts_per_min_rest"))
    if f30 is not None and rest is not None:
        head, tail = ("冒頭30秒", "以降") if dur > 60 else ("前半", "後半")
        cards.append(_card("カットの頻度", f"{f30:.0f} → {rest:.0f}", "回/分",
                           f"{head}は1分あたり{f30:.0f}回、{tail}は{rest:.0f}回切り替わる"))
    mr = _fin(cam.get("moving_ratio"))
    cards.append(_card("カメラが動くショット", "—" if mr is None else f"{mr * 100:.0f}", "%",
                       f"10ショット中 約{round(mr * 10)}本でズームやパンが入る" if mr is not None
                       else "測定できず"))
    mix = [(k, v) for k, v in (cam.get("move_mix") or {}).items() if _fin(v)]
    if mix:
        rest_txt = "・".join(f"{_label(CAMERA_JA, k)} {v * 100:.0f}%" for k, v in mix[1:3])
        cards.append(_card("いちばん多いカメラワーク", _label(CAMERA_JA, mix[0][0]),
                           f" {mix[0][1] * 100:.0f}%",
                           (f"次いで {rest_txt}" if rest_txt else "ほぼこれだけ") + "（再生時間の割合）"))
    fr, y = _fin(tel.get("frame_ratio")), _fin(tel.get("y_median"))
    pos = "" if y is None else "（主に画面" + ("上部" if y < 0.33 else "中央" if y < 0.66 else "下部") + "）"
    cards.append(_card("テロップ表示率", "—" if fr is None else f"{fr * 100:.0f}", "%",
                       f"再生時間の{fr * 100:.0f}%でテロップ・字幕が出ている{pos}" if fr is not None
                       else "測定できず"))
    dr = _fin(col.get("dark_ratio"))
    if dr is not None:
        cards.append(_card("暗い画面の割合", f"{dr * 100:.0f}", "%",
                           f"再生時間の{dr * 100:.0f}%が暗い画面（宇宙の背景など）"))

    if au.get("has_audio") is False or ("has_audio" not in au and au.get("lufs_integrated") is None):
        msg = "この動画には音声トラックがない" if au.get("has_audio") is False else "音声の解析結果(audio.json)がない"
        cards.append(_card("音声", "なし", "", msg))
        return '<div class="cards">' + "".join(cards) + "</div>"
    lufs = _fin(au.get("lufs_integrated"))
    if lufs is None:
        lexp = "測定できず"
    elif lufs <= -60:
        lexp = "ほぼ無音（音声トラックが空か極端に小さい）"
    elif abs(lufs - YOUTUBE_LUFS) < 1:
        lexp = "YouTubeの基準(約-14)とほぼ同じ音量"
    elif lufs < YOUTUBE_LUFS:
        lexp = f"YouTubeの基準(約-14)より{YOUTUBE_LUFS - lufs:.0f}dB小さい"
    else:
        lexp = f"YouTubeの基準(約-14)より{lufs - YOUTUBE_LUFS:.0f}dB大きい（再生時に下げられる）"
    cards.append(_card("音量(ラウドネス)", "—" if lufs is None else f"{lufs:.1f}", "LUFS", lexp))
    sr = _fin(au.get("speech_ratio"))
    src = SPEECH_SRC_JA.get(au.get("speech_source"), "")
    if sr is not None:
        cards.append(_card("ナレーションの割合", f"{sr * 100:.0f}", "%",
                           f"再生時間の{sr * 100:.0f}%で声が鳴っている" + (f"（{src}）" if src else "")))
    cps = _fin(au.get("chars_per_sec"))
    cards.append(_card("話す速さ", "—" if cps is None else f"{cps:.1f}", "文字/秒",
                       f"ナレーションは1秒に約{cps:.1f}文字を読む" if cps is not None
                       else "字幕・文字起こしが無いので測れない"))
    fq = _fin(au.get("first_question_s"))
    if fq is not None:
        cards.append(_card("最初の問いかけ", f"{fq:.1f}", "秒",
                           f"開始{fq:.1f}秒で最初の「？」の台詞が来る（つかみの速さ）"))
    nv, tpm = _fin(au.get("n_voices")), _fin(au.get("turns_per_min"))
    if nv:
        cards.append(_card("声の掛け合い", f"{nv:.0f}", "人",
                           f"声が1分に約{tpm:.0f}回入れ替わる（声の高さからの推定・目安）"
                           if nv > 1 and tpm is not None else "ほぼ1人の声で進む（声の高さからの推定）"))
    bgm = _fin(au.get("bgm_rel_db"))
    if bgm is None:
        cards.append(_card("BGMの音量", "—", "", "発話の「間」が少なく判定できない"))
    elif bgm <= -59:
        cards.append(_card("BGMの音量", "なし", "", "喋っていない「間」はほぼ無音 = BGMなし"))
    else:
        rel = "小さい（BGMは控えめ）" if bgm < 0 else "大きい（BGMが目立つ）"
        cards.append(_card("BGMの音量", f"{bgm:+.0f}", "dB",
                           f"喋っていない「間」の音は発話より{abs(bgm):.0f}dB{rel}"))
    return '<div class="cards">' + "".join(cards) + "</div>"


# ---------------------------------------------------------------- 3. タイムライン

def _timeline(frames, shots, meta, aud, speech, fps, T):
    """時間軸を共有した計測値の帯グラフ群 + ホバー用データ。"""
    X = lambda t: np.asarray(t, float) / T * VB_W  # noqa: E731
    rows = []           # (ラベル, 補足, 凡例html, 高さ, 中身html)
    data = {"T": round(T, 3), "fps": fps}

    # 代表フレームの帯
    thumbs = []
    for s in shots:
        kf = s.get("keyframe")
        if not kf or s.get("start") is None:
            continue
        left, width = s["start"] / T * 100, max(s["duration"] / T * 100, 0.05)
        thumbs.append(f'<a href="#shot-{int(s["index"])}" style="left:{left:.3f}%;width:{width:.3f}%">'
                      f'<img src="{_esc(kf)}" alt="ショット{int(s["index"])}" loading="lazy"></a>')
    if thumbs:
        rows.append(("代表フレーム", "クリックで一覧へ", "", 44,
                     '<div class="thumbs"><div class="thumbs-in">' + "".join(thumbs) + "</div></div>"))

    if frames is not None and "t" in frames and len(frames["t"]):
        t_all = frames["t"]
        idx, edges = _buckets(len(t_all))
        tb = t_all[idx]
        x = X(tb)
        col = lambda k: frames.get(k, np.full(len(t_all), np.nan))  # noqa: E731
        shot_b = _reduce(col("shot"), idx, edges, "pick")
        brk = np.r_[False, shot_b[1:] != shot_b[:-1]]
        luma = _reduce(col("luma"), idx, edges, "mean")
        sat = _reduce(col("sat"), idx, edges, "mean")
        # 動き量(画面幅の%/秒)。切替直後の2フレームはオプティカルフローが跳ねるので除外
        jump = np.r_[True, col("shot")[1:] != col("shot")[:-1]]
        jump |= np.r_[False, jump[:-1]]
        motion = _reduce(np.where(jump, np.nan, col("motion")) * fps * 100, idx, edges, "max")
        cut = _reduce(col("cut_score"), idx, edges, "max")
        zoom = _reduce((np.exp(col("cam_zoom")) - 1) * 100, idx, edges, "pick")
        panx = _reduce(col("cam_x") * 100, idx, edges, "pick")
        pany = _reduce(col("cam_y") * 100, idx, edges, "pick")
        data.update(t=_rounded(tb, 3), luma=_rounded(luma, 3), sat=_rounded(sat, 3),
                    motion=_rounded(motion, 2), cut=_rounded(cut, 3), zoom=_rounded(zoom, 2),
                    panx=_rounded(panx, 2), pany=_rounded(pany, 2))
        h = 56
        for lab, arr in (("明るさ", luma), ("彩度(色の濃さ)", sat)):
            rows.append((lab, "0（暗い/灰色）〜 1", "", h,
                         _strip_svg(h, _hline(_scale_y(0.5, 0, 1, h)) + _hline(h - 0.5, "bl")
                                    + _series(x, arr, 0, 1, h, "var(--k1)"))))
        fin_m = motion[np.isfinite(motion)]
        mhi = max(float(np.percentile(fin_m, 99.5)) * 1.1 if len(fin_m) else 0.0, 1.0)
        rows.append(("動き量", f"画面幅の%/秒（{mhi:.1f}以上は頭打ち）", "", h,
                     _strip_svg(h, _hline(h - 0.5, "bl") + _series(x, motion, 0, mhi, h, "var(--k1)"))))

        # 切替スコア + 遷移マーカー
        h = 64
        fin_c = cut[np.isfinite(cut)]
        chi = max(float(fin_c.max()) if len(fin_c) else 0.0, 0.3)
        marks = []
        trans = (meta or {}).get("transitions") or []
        for tr in trans:
            t0, kind = _fin(tr.get("t")), tr.get("kind")
            if t0 is None:
                continue
            c = TRANS_COLOR.get(kind, "var(--muted)")
            ln = (tr.get("length") or 0) / fps
            xs = float(X(t0))
            if kind == "dissolve" and ln:
                marks.append(f'<rect x="{float(X(t0 - ln / 2)):.2f}" y="0" width="{float(X(ln)):.2f}" '
                             f'height="{h}" style="fill:{c}" opacity=".3"/>')
            elif kind == "fade_black" and ln:
                marks.append(f'<rect x="{float(X(t0 - ln)):.2f}" y="0" width="{float(X(ln)):.2f}" '
                             f'height="{h}" style="fill:{c}" opacity=".3"/>')
            y1 = h * 0.45 if kind == "flash" else h
            marks.append(f'<path class="mk{" fl" if kind == "flash" else ""}" style="stroke:{c}" '
                         f'd="M{xs:.2f},0V{y1:.1f}"/>')
        data["trans"] = [[_fin(tr.get("t")), _label(TRANS_JA, tr.get("kind"))] for tr in trans
                         if _fin(tr.get("t")) is not None]
        counts = {k: sum(1 for tr in trans if tr.get("kind") == k) for k in TRANS_COLOR}
        lg = _legend([("line", "var(--muted)", "変化量")] + [
            ("tick" if k in ("cut",) else "top" if k == "flash" else "band", TRANS_COLOR[k],
             f"{TRANS_JA[k]} {counts[k]}") for k in TRANS_COLOR if counts[k]])
        rows.append(("切り替わり", "変化量と検出した遷移", lg, h,
                     _strip_svg(h, _hline(h - 0.5, "bl") + "".join(marks)
                                + _series(x, cut, 0, chi, h, "var(--muted)", area=False))))

        # カメラ(ショット内の累積。ショットが変わると0に戻る)
        h = 56
        fin_z = np.abs(zoom[np.isfinite(zoom)])
        zr = max(float(fin_z.max()) if len(fin_z) else 0.0, 1.0)
        rows.append(("ズーム", f"ショット開始からの拡大率（±{zr:.0f}%）", "", h,
                     _strip_svg(h, _hline(float(_scale_y(0, -zr, zr, h)), "bl")
                                + _series(x, zoom, -zr, zr, h, "var(--k1)", base=0, breaks=brk))))
        fin_p = np.abs(np.r_[panx[np.isfinite(panx)], pany[np.isfinite(pany)]])
        pr = max(float(fin_p.max()) if len(fin_p) else 0.0, 1.0)
        rows.append(("パン/ティルト", f"画面の中身の移動（±{pr:.0f}%、+は右/下へ）",
                     _legend([("line", "var(--k1)", "横"), ("line", "var(--k2)", "縦")]), h,
                     _strip_svg(h, _hline(float(_scale_y(0, -pr, pr, h)), "bl")
                                + _series(x, panx, -pr, pr, h, "var(--k1)", area=False, breaks=brk)
                                + _series(x, pany, -pr, pr, h, "var(--k2)", area=False, breaks=brk))))

        # テロップ(5Hz標本のみ値がある)
        txt = col("text")
        ok = np.isfinite(txt)
        ts, tv = t_all[ok], txt[ok]
        runs, start = [], None
        for i in range(len(ts)):
            if tv[i] > 0.5 and start is None:
                start = ts[i]
            if start is not None and (tv[i] <= 0.5 or i == len(ts) - 1):
                end = ts[i] if tv[i] <= 0.5 else ts[i] + 1.0 / ((meta or {}).get("sample_hz") or 5.0)
                runs.append((float(start), float(end)))
                start = None
        data["telop"] = [[round(a, 2), round(b, 2)] for a, b in runs]
        h = 22
        rects = "".join(f'<rect x="{float(X(a)):.2f}" y="4" width="{float(X(b - a)):.2f}" height="{h - 8}"/>'
                        for a, b in runs)
        rows.append(("テロップ", "文字が出ている区間" if len(ts) else "測定なし", "", h,
                     _strip_svg(h, _hline(h / 2, "gl") + f'<g class="tp">{rects}</g>')))

    # 音量 + 発話区間
    if aud and aud.get("has_audio"):
        h = 64
        curve = [(float(a), float(b)) for a, b in (aud.get("loudness_curve") or [])
                 if _fin(a) is not None and _fin(b) is not None]
        vals = np.array([max(b, -70.0) for _, b in curve]) if curve else np.zeros(0)
        lo = max(-60.0, math.floor(float(np.percentile(vals, 2)) / 5) * 5) if len(vals) else -60.0
        hi = min(0.0, math.ceil(float(vals.max()) / 5) * 5) if len(vals) else -40.0
        if hi - lo < 20:
            lo, hi = min(lo, hi - 20), max(hi, lo + 20)
        bands = "".join(f'<rect x="{float(X(a)):.2f}" y="0" width="{float(X(b - a)):.2f}" height="{h}"/>'
                        for a, b, _ in speech)
        ev = "".join(f'<path class="mk" style="stroke:var(--k2)" d="M{float(X(t)):.2f},0V10"/>'
                     for t in aud.get("loud_events") or [] if _fin(t) is not None)
        line = ""
        if curve:
            line = _series(X([a for a, _ in curve]), vals, lo, hi, h, "var(--k1)", area=False)
        items = [("line", "var(--k1)", "音量")]
        if speech:
            items.append(("band", "var(--k3)", "発話"))
        if ev:
            items.append(("tick", "var(--k2)", "強調音"))
        silent = len(vals) and float(vals.max()) <= -60
        rows.append(("音量", "ほぼ無音" if silent else f"短期ラウドネス {lo:.0f}〜{hi:.0f} LUFS",
                     _legend(items), h,
                     _strip_svg(h, f'<g class="sp">{bands}</g>' + _hline(h - 0.5, "bl") + line + ev)))
        data["loud"] = [[round(a, 1), round(max(b, -70.0), 1)] for a, b in curve]
    elif speech:
        h = 22
        bands = "".join(f'<rect x="{float(X(a)):.2f}" y="3" width="{float(X(b - a)):.2f}" height="{h - 6}"/>'
                        for a, b, _ in speech)
        rows.append(("ナレーション", "字幕/文字起こしの区間", "", h,
                     _strip_svg(h, _hline(h / 2, "gl") + f'<g class="sp solid">{bands}</g>')))
    data["speech"] = [[round(a, 2), round(b, 2), t] for a, b, t in speech]
    data["shots"] = [[int(s["index"]), s["start"], s["end"], _label(CAMERA_JA, s.get("camera")),
                      s.get("kind")] for s in shots if s.get("start") is not None]

    if not rows:
        return '<p class="empty">フレーム毎の計測値(frames.csv)が無いため表示できません。</p>', data

    # 背景(ショットの縞・黒画面・目盛り線)と時間軸
    step = _nice_step(T)
    ticks = np.arange(0, T + 1e-9, step)
    bg = [f'<rect class="{"sb" if s.get("kind") == "black" else "so"}" x="{float(X(s["start"])):.2f}" '
          f'y="0" width="{float(X(s["duration"])):.2f}" height="1"/>'
          for s in shots if s.get("start") is not None and (s.get("kind") == "black" or s["index"] % 2)]
    bg += [f'<path class="gl" d="M{float(X(t)):.2f},0V1"/>' for t in ticks[1:]]
    axis = "".join(f'<span style="left:{t / T * 100:.3f}%{";transform:translateX(-100%)" if t / T > 0.97 else ""}">'
                   f'{_mmss(t) if step >= 1 else f"{t:.1f}"}</span>' for t in ticks)
    # 拡大ボタン: 1画面に60点以上残る範囲だけ出す(JSが無効なら隠れたまま)
    n_pts = max(len(data.get("t") or []), 1)
    spans = [sp for sp in (300, 60, 20, 5) if 60 * T / n_pts <= sp < 0.8 * T]
    ctl = ""
    if spans:
        ctl = ('<div class="tl-ctl" hidden><span>表示範囲</span><button type="button" data-span="0" '
               'aria-pressed="true">全体</button>' + "".join(
                   f'<button type="button" data-span="{sp}">{_mmss(sp) if sp >= 60 else f"{sp}秒"}</button>'
                   for sp in spans) + '<input type="range" min="0" max="1000" value="0" '
               'aria-label="表示位置" disabled></div>')
    body = []
    for lab, sub, lg, h, inner in rows:
        body.append(f'<div class="tl-lab" style="min-height:{h}px"><b>{_esc(lab)}</b>'
                    f'<small>{_esc(sub)}</small>{lg}</div><div class="tl-plot">{inner}</div>')
    body.append(f'<div class="tl-lab"></div><div class="tl-axis">{axis}</div>')
    return (ctl + '<div class="tl" id="tl" tabindex="0" aria-label="タイムライン。左右キーでショット移動、Enterで一覧へ">'
            f'<div class="tl-bg"><svg viewBox="0 0 {VB_W} 1" preserveAspectRatio="none" data-h="1" aria-hidden="true">'
            + "".join(bg) + "</svg></div>" + "".join(body)
            + '<div class="tl-over"><div class="tl-cross"></div></div></div>'), data


# ---------------------------------------------------------------- 4. 分布

def _histogram(lens: np.ndarray, median) -> str:
    if len(lens) == 0:
        return '<p class="empty">ショットがありません</p>'
    W, H, L, B, TP, R = 820, 240, 34, 30, 24, 8
    hi = max(1.0, math.ceil(float(np.percentile(lens, 99)) / 0.5) * 0.5)
    edges = np.arange(0, hi + 1e-9, 0.5)
    counts = [int(((lens >= a) & (lens < b)).sum()) for a, b in zip(edges[:-1], edges[1:])]
    over = int((lens >= hi).sum())
    labels = [f"{a:g}〜{b:g}秒" for a, b in zip(edges[:-1], edges[1:])]
    if over:
        counts.append(over)
        labels.append(f"{hi:g}秒以上")
    ymax = max(counts) or 1
    ystep = next(s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 10 ** 9) if ymax / s <= 5)
    ytop = math.ceil(ymax / ystep) * ystep
    pw, ph = W - L - R, H - B - TP
    slot = pw / len(counts)
    bw = min(24.0, slot * 0.72)
    out = [f'<svg class="hist" viewBox="0 0 {W} {H}" role="img" aria-label="ショット長の分布">']
    for yv in range(0, ytop + 1, ystep):
        y = TP + ph - yv / ytop * ph
        out.append(f'<path class="{"bl" if yv == 0 else "gl"}" d="M{L},{y:.1f}H{W - R}"/>'
                   f'<text class="ax" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{yv}</text>')
    every = max(1, math.ceil(len(counts) / 12))
    for i, (c, lab) in enumerate(zip(counts, labels)):
        x0 = L + i * slot
        bx = x0 + (slot - bw) / 2
        bh = c / ytop * ph
        base = TP + ph
        if c:
            r = min(4.0, bw / 2, bh)
            top = base - bh
            out.append(f'<path class="bar" d="M{bx:.1f},{base:.1f}V{top + r:.1f}Q{bx:.1f},{top:.1f} '
                       f'{bx + r:.1f},{top:.1f}H{bx + bw - r:.1f}Q{bx + bw:.1f},{top:.1f} '
                       f'{bx + bw:.1f},{top + r:.1f}V{base:.1f}Z"/>')
        out.append(f'<rect class="hit" x="{x0:.1f}" y="{TP}" width="{slot:.1f}" height="{ph}" '
                   f'tabindex="0" data-tip="{_esc(f"{lab}: {c}ショット")}"/>')
        if i % every == 0:
            tl = "≥" + f"{hi:g}" if (over and i == len(counts) - 1) else f"{edges[i]:g}"
            out.append(f'<text class="ax" x="{x0:.1f}" y="{H - B + 16}" text-anchor="middle">{tl}</text>')
    if _fin(median) is not None and median < hi:
        mx = L + median / 0.5 * slot
        out.append(f'<path class="med" d="M{mx:.1f},{TP - 6}V{TP + ph}"/>'
                   f'<text class="ax strong" x="{mx + 4:.1f}" y="{TP - 10}">中央値 {median:.1f}秒</text>')
    out.append(f'<text class="ax" x="{W - R}" y="{H - 2}" text-anchor="end">ショットの長さ(秒)</text></svg>')
    table = "".join(f"<tr><td>{_esc(lab)}</td><td>{c}</td></tr>" for lab, c in zip(labels, counts))
    return ("".join(out) + '<details class="raw"><summary>数値で見る</summary><table>'
            f"<tr><th>長さ</th><th>ショット数</th></tr>{table}</table></details>")


def _mix_bars(mix: dict, labels: dict, colors: dict = None, suffix: dict = None) -> str:
    rows = []
    for k, v in mix.items():
        v = _fin(v)
        if v is None:
            continue
        color = (colors or {}).get(k, "var(--k1)")
        lab = _label(labels, k) + ((suffix or {}).get(k, ""))
        rows.append(f'<div class="mix" tabindex="0" data-tip="{_esc(f"{lab}: {v * 100:.1f}%")}">'
                    f'<span class="m-lab">{_esc(lab)}</span><span class="m-track"><span class="m-bar" '
                    f'style="width:{v * 100:.1f}%;background:{color}"></span></span>'
                    f'<span class="m-val">{v * 100:.0f}%</span></div>')
    return "".join(rows) or '<p class="empty">データなし</p>'


def _distributions(prof: dict, shots: list, meta: dict, vlm_by: dict) -> str:
    real = [s for s in shots if s.get("kind") != "black" and _fin(s.get("duration"))]
    lens = np.array([s["duration"] for s in real])
    trans = [tr.get("kind") for tr in (meta or {}).get("transitions") or []]
    tcount = {k: trans.count(k) for k in TRANS_COLOR if trans.count(k)}
    tmix = {k: c / len(trans) for k, c in tcount.items()} if trans else {}
    cam = prof.get("camera") or {}
    boxes = [
        ("ショットの長さの分布", "1本のカットが何秒続くか（0.5秒刻み）",
         _histogram(lens, _get(prof, "editing.shot_len_median")), "wide"),
        ("カメラワークの内訳", "再生時間に占める割合", _mix_bars(cam.get("move_mix") or {}, CAMERA_JA), ""),
        ("動きの付け方(イージング)", "カメラが動くショットでの速度変化",
         _mix_bars(cam.get("easing_mix") or {}, EASING_JA), ""),
        ("画の切り替え方", "検出した遷移の回数の割合",
         _mix_bars(tmix, TRANS_JA, TRANS_COLOR, {k: f" {c}回" for k, c in tcount.items()}), ""),
    ]
    have = {int(s["index"]) for s in shots if _fin(s.get("index")) is not None}
    cats = [str(v.get("category")) for i, v in sorted(vlm_by.items()) if v.get("category") and i in have]
    if cats:
        cmix = {k: cats.count(k) / len(cats) for k in sorted(set(cats), key=lambda k: (-cats.count(k), k))}
        boxes.append(("画面の種類(AIの見立て)", "VLMがショットに付けた分類の割合",
                      _mix_bars(cmix, {}), ""))
    return '<div class="grid2">' + "".join(
        f'<div class="box {w}"><h3>{_esc(t)}</h3><p class="sub">{_esc(s)}</p>{b}</div>'
        for t, s, b, w in boxes) + "</div>"


# ---------------------------------------------------------------- 5. 色

def _palette(pal: list, note: str) -> str:
    pal = [(str(c), _fin(w) or 0.0) for c, w in pal or [] if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c))]
    if not pal:
        return '<p class="empty">データなし</p>'
    tot = sum(w for _, w in pal) or 1.0
    strip = "".join(f'<span style="flex:{w / tot:.4f};background:{c}" tabindex="0" '
                    f'data-tip="{c} {w / tot * 100:.0f}%"></span>' for c, w in pal)
    chips = "".join(f'<div class="chip"><span class="sw" style="background:{c}"></span>'
                    f'<code>{c}</code><small>{w / tot * 100:.0f}%</small></div>' for c, w in pal)
    return f'<div class="pal-strip">{strip}</div><div class="chips">{chips}</div><p class="sub">{_esc(note)}</p>'


def _colors(prof: dict) -> str:
    col, tel = prof.get("color") or {}, prof.get("telop") or {}
    stats = [("平均の明るさ", col.get("luma_mean"), "0=真っ黒 / 1=真っ白"),
             ("平均の彩度", col.get("sat_mean"), "0=灰色 / 1=原色"),
             ("コントラスト", col.get("contrast_mean"), "明暗の差(標準偏差)")]
    st = "".join(f'<div class="kv"><b>{"—" if _fin(v) is None else f"{v:.2f}"}</b>'
                 f'<span>{_esc(k)}</span><small>{_esc(n)}</small></div>' for k, v, n in stats)
    return ('<div class="grid2">'
            f'<div class="box"><h3>全体の配色</h3><p class="sub">各ショットの代表フレームの主要色を、'
            f'長さで重み付けしてまとめたもの</p>{_palette(col.get("palette"), "色見本の幅 = 画面に占める割合")}'
            f'<div class="kvs">{st}</div></div>'
            f'<div class="box"><h3>テロップの色</h3><p class="sub">画面下半分の文字領域から集めた色'
            f'（文字色・縁取り・背景が混ざる）</p>'
            f'{_palette(tel.get("colors_observed"), "文字と縁取りの組み合わせの参考に")}</div></div>')


# ---------------------------------------------------------------- 6. ショット一覧

def _shot_table(shots: list, fps: float, lines: list, vlm_by: dict) -> str:
    if not shots:
        return '<p class="empty">ショット情報(shots.json)がありません。</p>'
    has_vlm = bool(vlm_by)
    head = ["代表フレーム", "ショット（開始 分:秒.コマ）", "カメラワーク", "画の数値", "配色"] + (
        ["AIの見立て"] if has_vlm else []) + ["この間のナレーション"]
    out = ['<div class="tbl-wrap"><table class="shots"><thead><tr>'
           + "".join(f"<th>{_esc(h)}</th>" for h in head) + "</tr></thead><tbody>"]
    for s in shots:
        i = int(s.get("index", 0))
        st, en = _fin(s.get("start")) or 0.0, _fin(s.get("end")) or 0.0
        dur = _fin(s.get("duration")) or (en - st)
        black = s.get("kind") == "black"
        kf = s.get("keyframe")
        img = (f'<a href="{_esc(kf)}" target="_blank"><img src="{_esc(kf)}" alt="ショット{i}" '
               f'loading="lazy"></a>' if kf else '<span class="noimg">画像なし</span>')
        tin, tl = s.get("transition_in"), _fin(s.get("transition_len"))
        head_html = (f'<b class="no">#{i}</b><a class="tc" href="#tl" data-t="{st:.3f}">{_tc(st, fps)}</a>'
                     f'<small>長さ {dur:.2f}秒</small><span class="tag">{_esc(_label(TRANS_JA, tin))}'
                     + (f" {tl:.2f}秒" if tl and tin in ("dissolve", "fade_black") else "") + "</span>"
                     + ('<span class="tag dark">黒画面</span>' if black else ""))
        z, px, py = _fin(s.get("zoom_total")), _fin(s.get("pan_x_total")), _fin(s.get("pan_y_total"))
        det = [] if z is None else [f"拡大 ×{z:.3f}"]
        if px is not None and py is not None:
            det.append(f"移動 横{px * 100:+.1f}% / 縦{py * 100:+.1f}%")
        if s.get("easing"):
            det.append(_label(EASING_JA, s["easing"]))
        cam_html = f"<b>{_esc(_label(CAMERA_JA, s.get('camera')))}</b>" + "".join(
            f"<small>{_esc(d)}</small>" for d in det)
        mo, luma, sat = _fin(s.get("motion")), _fin(s.get("luma")), _fin(s.get("sat"))
        tr_, ty = _fin(s.get("text_ratio")), _fin(s.get("telop_y"))
        tel = "—" if tr_ is None else f"{tr_ * 100:.0f}%"
        if ty is not None and tr_:
            tel += "（" + ("上部" if ty < 0.33 else "中央" if ty < 0.66 else "下部") + "）"
        nums = [("動き", "—" if mo is None else f"{mo * fps * 100:.2f} %/秒"),
                ("明るさ", "—" if luma is None else f"{luma:.2f}"),
                ("彩度", "—" if sat is None else f"{sat:.2f}"), ("テロップ", tel)]
        num_html = '<dl class="nums">' + "".join(f"<dt>{k}</dt><dd>{_esc(v)}</dd>" for k, v in nums) + "</dl>"
        pal = [(c, _fin(w) or 0.0) for c, w in s.get("palette") or []
               if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c))]
        pal_html = '<div class="mini">' + "".join(
            f'<span style="flex:{w:.3f};background:{c}" data-tip="{c} {w * 100:.0f}%"></span>'
            for c, w in pal) + "</div>" if pal else "—"
        cells = [img, head_html, cam_html, num_html, pal_html]
        if has_vlm:
            v = vlm_by.get(i) or {}
            cells.append((f'<span class="tag">{_esc(v["category"])}</span>' if v.get("category") else "")
                         + (f'<p class="desc">{_esc(v["description"])}</p>' if v.get("description") else "")
                         or '<span class="none">—</span>')
        said = [(a, t) for a, b, t in lines if t and a < en and b > st]
        cells.append("".join(f'<p class="say"><time>{_tc(a, fps)}</time>{_esc(t)}</p>'
                             for a, t in said) or '<span class="none">—</span>')
        out.append(f'<tr id="shot-{i}"{" class=black" if black else ""}>'
                   + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


# ---------------------------------------------------------------- 組み立て

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--k1:#2a78d6;
--k2:#eb6834;--k3:#1baf7a;--k4:#eda100;--shade:rgba(11,11,11,.035);--shade-b:rgba(11,11,11,.16);
--hi:rgba(42,120,214,.14);--lab:170px;--gap:14px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--k1:#3987e5;--k2:#d95926;--k3:#199e70;
--k4:#c98500;--shade:rgba(255,255,255,.035);--shade-b:rgba(0,0,0,.55);--hi:rgba(57,135,229,.2)}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.6 system-ui,-apple-system,
"Segoe UI","Hiragino Sans","Yu Gothic UI",Meiryo,"Noto Sans CJK JP",sans-serif}
a{color:var(--k1)}
.wrap{max-width:1320px;margin:0 auto;padding:0 24px 48px}
nav.toc{position:sticky;top:0;z-index:20;background:var(--page);border-bottom:1px solid var(--border);
display:flex;gap:18px;padding:10px 24px;font-size:13px;overflow-x:auto;white-space:nowrap}
nav.toc a{color:var(--ink2);text-decoration:none}nav.toc a:hover{color:var(--ink)}
.hd{padding:28px 0 8px}.eyebrow{font-size:12px;color:var(--ink2);letter-spacing:.08em}
h1{font-size:26px;margin:4px 0 10px;line-height:1.3;word-break:break-all}
.facts{display:flex;flex-wrap:wrap;gap:8px}.facts span{background:var(--surface);
border:1px solid var(--border);border-radius:999px;padding:2px 12px;font-size:13px;color:var(--ink2)}
.files{margin-top:10px;font-size:13px;color:var(--ink2)}.files a{margin-right:10px}
.lead{color:var(--ink2);max-width:70em;margin:14px 0 0}
section{margin-top:34px}h2{font-size:19px;margin:0 0 4px}section>.sub{margin:0 0 14px}
h3{font-size:15px;margin:0}.sub{color:var(--ink2);font-size:12.5px;margin:2px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card,.box,.tl-card{background:var(--surface);border:1px solid var(--border);border-radius:12px}
.card{padding:14px 16px}.c-lab{font-size:12.5px;color:var(--ink2)}
.c-val{font-size:26px;font-weight:600;margin:2px 0 4px;line-height:1.25}
.unit{font-size:13px;font-weight:500;color:var(--ink2);margin-left:4px}
.c-exp{font-size:12.5px;color:var(--ink2);line-height:1.5}
.tl-card{padding:16px 16px 10px}
.tl{position:relative;display:grid;grid-template-columns:var(--lab) 1fr;column-gap:var(--gap);
row-gap:6px;outline:none}
.tl:focus-visible{box-shadow:0 0 0 2px var(--k1);border-radius:6px}
.tl-bg,.tl-over{position:absolute;top:0;bottom:22px;left:calc(var(--lab) + var(--gap));right:0}
.tl-bg svg{width:100%;height:100%;display:block}.tl-over{pointer-events:none;z-index:3}
.so{fill:var(--shade)}.sb{fill:var(--shade-b)}
.tl-lab{display:flex;flex-direction:column;justify-content:center;font-size:13px;line-height:1.35}
.tl-lab small{color:var(--ink2);font-size:11px}.tl-plot{position:relative;z-index:1;min-width:0;overflow:hidden}
.strip{display:block;width:100%;overflow:hidden}
.ln{fill:none;stroke-width:2;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.ar{stroke:none;opacity:.12}
.gl,.bl,.mk,.med{fill:none;vector-effect:non-scaling-stroke}.gl{stroke:var(--grid);stroke-width:1}
.bl{stroke:var(--axis);stroke-width:1}.mk{stroke-width:2}.mk.fl{stroke-width:3}
.tp rect{fill:var(--k1);opacity:.85}.sp rect{fill:var(--k3);opacity:.2}.sp.solid rect{opacity:.75}
.thumbs{position:relative;height:44px;overflow:hidden;border-radius:4px}
.thumbs-in{position:absolute;top:0;bottom:0;left:0;width:100%}
.tl-ctl{display:flex;align-items:center;gap:6px;margin:0 0 12px calc(var(--lab) + var(--gap));
font-size:12.5px;color:var(--ink2)}.tl-ctl[hidden]{display:none}
.tl-ctl button{font:inherit;color:var(--ink);background:var(--page);border:1px solid var(--border);
border-radius:999px;padding:1px 12px;cursor:pointer}.tl-ctl button[aria-pressed=true]{background:var(--ink);
color:var(--page)}.tl-ctl input{flex:1;max-width:420px;margin-left:8px}
.thumbs a{position:absolute;top:0;bottom:0;overflow:hidden;border-right:2px solid var(--surface)}
.thumbs img{width:100%;height:100%;object-fit:cover;display:block}
.tl-axis{position:relative;height:16px;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.tl-axis span{position:absolute;top:0;transform:translateX(-50%)}
.tl-axis span:first-child{transform:none}
.tl-cross{position:absolute;top:0;bottom:0;width:1px;background:var(--ink);opacity:.55;display:none}
.lg{display:flex;flex-wrap:wrap;gap:2px 10px;margin-top:3px;font-size:11px;color:var(--ink2)}
.lg span{display:inline-flex;align-items:center;gap:4px}
.key{flex:none}.kl{fill:none;stroke-width:2;stroke-linecap:round}
#tip{position:fixed;z-index:50;pointer-events:none;display:none;background:var(--surface);
color:var(--ink);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;
box-shadow:0 6px 24px rgba(0,0,0,.18);max-width:340px;line-height:1.45}
#tip .th{font-weight:600;margin-bottom:4px}#tip .row{display:flex;gap:8px;align-items:baseline}
#tip .row b{font-variant-numeric:tabular-nums;min-width:74px}#tip .row span{color:var(--ink2)}
#tip .say{margin-top:5px;color:var(--ink);border-top:1px solid var(--border);padding-top:5px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}
.box{padding:16px}.box.wide{grid-column:span 2}
.hist{width:100%;height:auto;display:block}.hist text{font-size:11px}
.ax{fill:var(--muted);font-variant-numeric:tabular-nums}.ax.strong{fill:var(--ink2)}
.bar{fill:var(--k1)}.hit{fill:transparent;outline:none}.hit:hover,.hit:focus{fill:var(--hi)}
.med{stroke:var(--ink2);stroke-width:1}
.mix{display:grid;grid-template-columns:minmax(120px,40%) 1fr 44px;align-items:center;gap:10px;
padding:4px 0;font-size:13px;outline:none}.mix:hover,.mix:focus{background:var(--hi);border-radius:4px}
.m-track{height:12px;background:var(--shade);border-radius:0 4px 4px 0}
.m-bar{display:block;height:100%;border-radius:0 4px 4px 0;min-width:2px}
.m-val{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink2)}
details.raw{margin-top:8px;font-size:12.5px;color:var(--ink2)}
details.raw table{border-collapse:collapse;margin-top:6px}
details.raw td,details.raw th{padding:2px 12px 2px 0;text-align:left;font-variant-numeric:tabular-nums}
.pal-strip{display:flex;gap:2px;height:40px;border-radius:6px;overflow:hidden;box-shadow:0 0 0 1px var(--border)}
.pal-strip span{min-width:3px}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.chip{display:flex;flex-direction:column;align-items:flex-start;font-size:12px;width:78px}
.chip .sw{width:78px;height:34px;border-radius:6px;border:1px solid var(--border)}
.chip code{margin-top:3px;font-size:11.5px}.chip small{color:var(--ink2)}
.kvs{display:flex;gap:22px;margin-top:16px;flex-wrap:wrap}.kv{display:flex;flex-direction:column}
.kv b{font-size:20px;font-weight:600}.kv span{font-size:12.5px}.kv small{font-size:11px;color:var(--ink2)}
.tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
table.shots{border-collapse:collapse;width:100%;font-size:13px}
.shots th{background:var(--surface);text-align:left;font-weight:600;font-size:12px;
color:var(--ink2);padding:10px 10px;border-bottom:1px solid var(--axis);white-space:nowrap}
.shots td{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:top;
font-variant-numeric:tabular-nums}
.shots td small{display:block;color:var(--ink2);font-size:11.5px}
.shots td:first-child{width:172px}.shots img{width:160px;height:90px;object-fit:cover;border-radius:4px;
display:block;background:#000}
.shots td:last-child{min-width:240px}
.shots td:nth-child(2){min-width:118px}.shots td:nth-child(3){min-width:160px}
.shots td:nth-child(3) small{white-space:nowrap}
.shots .no{display:block;font-size:15px}.shots .tc{display:block}.shots td .tag{margin-top:4px}
.nums{display:grid;grid-template-columns:4.6em auto;gap:1px 10px;margin:0;font-size:12.5px;white-space:nowrap}
.nums dt{color:var(--ink2)}.nums dd{margin:0}
.shots tr.black td{opacity:.65}
.shots tr:target,.shots tr.flash{background:var(--hi)}
.shots tr.flash{animation:fl 2.4s ease-out}@keyframes fl{0%{background:var(--hi)}100%{background:transparent}}
.tag{display:inline-block;font-size:11.5px;padding:0 7px;border-radius:999px;border:1px solid var(--border);
background:var(--page);white-space:nowrap}.tag.dark{background:#111;color:#eee;margin-left:4px}
.tc{font-weight:600;text-decoration:none}
.mini{display:flex;gap:2px;width:96px;height:16px;border-radius:3px;overflow:hidden;box-shadow:0 0 0 1px var(--border)}
.mini span{min-width:2px}
.desc{margin:4px 0 0;font-size:12.5px;color:var(--ink2);max-width:28em}
.say{margin:0 0 4px;max-width:34em}.say time{color:var(--ink2);font-size:11px;margin-right:6px}
.none,.noimg{color:var(--muted)}.empty{color:var(--ink2)}
.vlm-sum{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;
margin-bottom:12px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);font-size:12.5px;
color:var(--ink2)}footer .notice{color:var(--ink);font-weight:500}
@media (max-width:760px){:root{--lab:92px;--gap:8px}.wrap{padding:0 16px 40px}
.grid2{grid-template-columns:1fr}.box.wide{grid-column:auto}nav.toc{padding:10px 16px}h1{font-size:21px}}
"""

JS = r"""
(function(){
var el=document.getElementById('tl-data'); var D=el?JSON.parse(el.textContent):null;
var tip=document.getElementById('tip');
function tc(t){t=Math.max(0,t)+1e-6;var s=Math.floor(t),f=Math.min(Math.floor((t-s)*D.fps),Math.ceil(D.fps)-1);
 var m=Math.floor(s/60);s%=60;return (m<10?'0':'')+m+':'+(s<10?'0':'')+s+'.'+(f<10?'0':'')+f;}
function near(a,x,key){var lo=0,hi=a.length-1;if(hi<0)return -1;while(lo<hi){var md=(lo+hi)>>1;
 if((key?a[md][0]:a[md])<x)lo=md+1;else hi=md;} if(lo>0&&Math.abs((key?a[lo-1][0]:a[lo-1])-x)<
 Math.abs((key?a[lo][0]:a[lo])-x))lo--;return lo;}
function inside(list,t){for(var i=0;i<list.length;i++){if(list[i][0]<=t&&t<list[i][1])return list[i];}return null;}
function shotAt(t){for(var i=0;i<D.shots.length;i++){if(D.shots[i][1]<=t&&t<D.shots[i][2])return D.shots[i];}
 var b=null,bd=1e9;D.shots.forEach(function(x){
 var d=Math.min(Math.abs(x[1]-t),Math.abs(x[2]-t));if(d<bd){bd=d;b=x;}});return b;}
function place(x,y){tip.style.display='block';var w=tip.offsetWidth,h=tip.offsetHeight;
 var L=x+16;if(L+w>innerWidth-8)L=x-w-16;var T=y+14;if(T+h>innerHeight-8)T=Math.max(8,y-h-14);
 tip.style.left=Math.max(8,L)+'px';tip.style.top=T+'px';}
function row(v,lab,color){var r=document.createElement('div');r.className='row';var b=document.createElement('b');
 b.textContent=v;var s=document.createElement('span');s.textContent=lab;if(color){var k=document.createElement('i');
 k.style.cssText='display:inline-block;width:10px;height:2px;margin-right:4px;vertical-align:middle;background:'+color;
 s.prepend(k);}r.append(b,s);return r;}
function f(v,n,u){return v==null?'—':(u==='+'?(v>0?'+':'')+v.toFixed(n)+'%':v.toFixed(n)+(u||''));}
function fill(t){tip.textContent='';var h=document.createElement('div');h.className='th';var s=shotAt(t);
 h.textContent=tc(t)+(s?'　ショット #'+s[0]+'（'+s[3]+'）':'');tip.append(h);
 if(D.t&&D.t.length){var i=near(D.t,t);
  tip.append(row(f(D.luma[i],3),'明るさ'),row(f(D.sat[i],3),'彩度'),row(f(D.motion[i],2,' %/秒'),'動き量'),
  row(f(D.cut[i],3),'変化量'),row(f(D.zoom[i],1,'+'),'ズーム(ショット内)'),
  row(f(D.panx[i],1,'+')+' / '+f(D.pany[i],1,'+'),'パン 横 / 縦'),
  row(D.telop?(inside(D.telop,t)?'あり':'なし'):'—','テロップ'));}
 if(D.loud&&D.loud.length){var j=near(D.loud,t,1);tip.append(row(f(D.loud[j][1],1,' LUFS'),'音量'));}
 if(D.trans&&D.trans.length){var k=near(D.trans,t,1);if(Math.abs(D.trans[k][0]-t)<0.25)
  tip.append(row(D.trans[k][1],'遷移 '+tc(D.trans[k][0])));}
 var sp=inside(D.speech||[],t);if(sp&&sp[2]){var p=document.createElement('div');p.className='say';
  p.textContent='「'+sp[2]+'」';tip.append(p);}}
var tl=document.getElementById('tl');
if(tl&&D){var over=tl.querySelector('.tl-over'),cross=tl.querySelector('.tl-cross'),cur=-1,V=[0,D.T];
 var ctl=document.querySelector('.tl-ctl'),rng=ctl&&ctl.querySelector('input');
 function xOf(t){var r=over.getBoundingClientRect();return r.left+(t-V[0])/(V[1]-V[0])*r.width;}
 function tAt(cx){var r=over.getBoundingClientRect();var q=(cx-r.left)/r.width;return(q<0||q>1)?null:V[0]+q*(V[1]-V[0]);}
 function mark(t,cx,cy){if(t<V[0]||t>V[1])setView(t-(V[1]-V[0])/2,V[1]-V[0]);cross.style.display='block';
  cross.style.left=((t-V[0])/(V[1]-V[0])*100)+'%';fill(t);place(cx==null?xOf(t):cx,cy);}
 function setView(a,span){span=Math.min(span||D.T,D.T);a=Math.max(0,Math.min(a,D.T-span));V=[a,a+span];
  var x0=a/D.T*1000,w=span/D.T*1000;tl.querySelectorAll('svg[data-h]').forEach(function(s){
   s.setAttribute('viewBox',x0.toFixed(2)+' 0 '+w.toFixed(2)+' '+s.dataset.h);});
  var th=tl.querySelector('.thumbs-in');if(th){th.style.width=(D.T/span*100)+'%';th.style.left=(-a/span*100)+'%';}
  var ax=tl.querySelector('.tl-axis'),st=[0.5,1,2,5,10,15,30,60,120,300,600,900,1800,3600].find(function(x){return span/x<=10;})||3600;
  ax.textContent='';for(var t=Math.ceil(a/st)*st;t<=a+span+1e-9;t+=st){var e=document.createElement('span');
   var q=(t-a)/span;e.style.left=(q*100)+'%';if(q>0.97)e.style.transform='translateX(-100%)';else if(q<0.03)e.style.transform='none';
   var m=Math.floor(Math.round(t)/60),sec=Math.round(t)%60;e.textContent=st<1?t.toFixed(1):m+':'+(sec<10?'0':'')+sec;ax.append(e);}
  if(rng){rng.disabled=span>=D.T;rng.value=span>=D.T?0:Math.round(a/(D.T-span)*1000);}}
 if(ctl){ctl.hidden=false;ctl.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
   ctl.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false');});
   var sp=parseFloat(b.dataset.span)||D.T,c=cross.style.display==='block'?V[0]+parseFloat(cross.style.left)/100*(V[1]-V[0]):(V[0]+V[1])/2;
   setView(c-sp/2,sp);});
  rng.addEventListener('input',function(){var sp=V[1]-V[0];setView(rng.value/1000*(D.T-sp),sp);});
  tl.addEventListener('wheel',function(e){var sp=V[1]-V[0];if(sp>=D.T)return;var d=e.deltaX||(e.shiftKey?e.deltaY:0);
   if(!d)return;e.preventDefault();setView(V[0]+d/over.getBoundingClientRect().width*sp,sp);},{passive:false});}
 function hide(){cross.style.display='none';tip.style.display='none';}
 function go(i){var r=document.getElementById('shot-'+i);if(!r)return;r.scrollIntoView({behavior:'smooth',block:'center'});
  r.classList.remove('flash');void r.offsetWidth;r.classList.add('flash');}
 tl.addEventListener('pointermove',function(e){var t=tAt(e.clientX);if(t==null)return hide();mark(t,e.clientX,e.clientY);});
 tl.addEventListener('pointerleave',hide);
 tl.addEventListener('click',function(e){if(e.target.closest('a'))return;var t=tAt(e.clientX);if(t==null)return;
  var s=shotAt(t);if(s)go(s[0]);});
 tl.addEventListener('keydown',function(e){if(!D.shots.length)return;
  if(e.key==='ArrowRight'||e.key==='ArrowLeft'){cur=Math.max(0,Math.min(D.shots.length-1,cur+(e.key==='ArrowRight'?1:-1)));
   var s=D.shots[cur],t=(s[1]+s[2])/2;mark(t,null,over.getBoundingClientRect().top+20);e.preventDefault();}
  else if(e.key==='Enter'&&cur>=0){go(D.shots[cur][0]);}else if(e.key==='Escape')hide();});
 tl.addEventListener('blur',hide);
 document.querySelectorAll('a.tc').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();
  tl.scrollIntoView({behavior:'smooth',block:'center'});var t=parseFloat(a.dataset.t)+0.01;
  setTimeout(function(){mark(t,null,over.getBoundingClientRect().top+20);},450);});});}
function tipFor(e){var n=e.target.closest&&e.target.closest('[data-tip]');if(!n)return null;return n;}
document.addEventListener('pointerover',function(e){var n=tipFor(e);if(!n)return;tip.textContent=n.dataset.tip;
 place(e.clientX,e.clientY);});
document.addEventListener('pointermove',function(e){if(tl&&tl.contains(e.target))return;var n=tipFor(e);
 if(n)place(e.clientX,e.clientY);else if(!(tl&&tl.contains(e.target)))tip.style.display='none';});
document.addEventListener('focusin',function(e){var n=e.target.closest&&e.target.closest('[data-tip]');if(!n)return;
 tip.textContent=n.dataset.tip;var r=n.getBoundingClientRect();place(r.left+r.width/2,r.bottom);});
document.addEventListener('focusout',function(e){if(e.target.closest&&e.target.closest('[data-tip]'))tip.style.display='none';});
})();
"""


def write_report(out_dir: Path) -> Path:
    """解析フォルダ(out_dir)の成果物から out_dir/report.html を書き出してパスを返す。"""
    out_dir = Path(out_dir)
    meta = _load_json(out_dir / "meta.json")
    shots = _load_json(out_dir / "shots.json")
    shots = [s for s in shots if isinstance(s, dict)] if isinstance(shots, list) else []
    # purge 後のフォルダでも壊れた画像を出さない
    shots = [s if not s.get("keyframe") or (out_dir / s["keyframe"]).exists() else {**s, "keyframe": None}
             for s in shots]
    frames = _load_frames(out_dir / "frames.csv")
    prof = _load_json(out_dir / "profile.json")
    aud = _load_json(out_dir / "audio.json")
    aud = aud if isinstance(aud, dict) else None
    if meta is None and not shots and frames is None and prof is None:
        raise FileNotFoundError(f"解析結果が見つかりません(meta.json / shots.json / frames.csv): {out_dir}")

    v = (meta or {}).get("video") or {}
    path = str(v.get("path") or "")
    name = re.split(r"[\\/]", path)[-1] if path else (_get(prof, "source.videos") or [out_dir.name])[0]
    fps = _fin(v.get("fps")) or _fin(_get(prof, "format.fps")) or 30.0
    if frames is not None and "t" in frames and len(frames["t"]):
        T = float(np.nanmax(frames["t"])) + ((meta or {}).get("step") or 1) / fps
    else:
        T = _fin(_get(prof, "format.duration")) or _fin(v.get("duration")) or \
            max([_fin(s.get("end")) or 0.0 for s in shots] + [0.0])
    T = max(T, 1e-3)
    if prof is None and meta and shots and frames is not None:
        # profile.json が無ければその場で作る(表示用。保存はしない)
        try:
            from . import profile as prof_mod
            prof = prof_mod.build_profile(name, meta, shots, frames, aud or {})
        except Exception:
            prof = None
    prof = prof if isinstance(prof, dict) else {}

    transcript = _segments(_load_json(out_dir / "transcript.json"))
    aud_segs = _segments((aud or {}).get("segments"))
    speech = aud_segs or transcript                          # タイムラインの発話帯
    lines = transcript if any(t for *_, t in transcript) else aud_segs   # 一覧の台詞
    vlm = _load_json(out_dir / "vlm.json")
    vlm_by = {}
    if isinstance(vlm, dict):
        for s in vlm.get("shots") or []:
            if isinstance(s, dict) and _fin(s.get("index")) is not None:
                vlm_by[int(s["index"])] = s
    has_audio = aud.get("has_audio") if aud else _get(prof, "audio.has_audio")
    if has_audio is None and _get(prof, "audio.lufs_integrated") is not None:
        has_audio = True

    tl_html, tl_data = _timeline(frames, shots, meta, aud, speech, fps, T)
    vlm_sum = vlm.get("summary") if isinstance(vlm, dict) else None
    data_json = json.dumps(_clean(tl_data), ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")).replace("</", "<\\/")
    sections = [
        ("summary", "要点", "この動画の作風を数字で一言ずつ", _cards(prof, aud, T)),
        ("timeline", "タイムライン", "縦に並んだ帯はすべて同じ時間軸。マウスを乗せると値、"
         "クリックでそのショットの行へ移動（キーボードは←→とEnter）。灰色の縞はショットの区切り",
         f'<div class="tl-card">{tl_html}</div>'),
        ("dist", "分布", "編集のクセを割合で見る", _distributions(prof, shots, meta, vlm_by)),
        ("color", "色", "画づくりの配色", _colors(prof)),
        ("shots", "ショット一覧", "1行 = 1カット。開始時刻を押すとタイムライン上の位置を表示",
         (f'<div class="vlm-sum"><b>AIによる全体の要約</b><p>{_esc(vlm_sum)}</p></div>'
          if vlm_sum else "") + _shot_table(shots, fps, lines, vlm_by)),
    ]
    nav = "".join(f'<a href="#{sid}">{_esc(t)}</a>' for sid, t, _, _ in sections)
    body = "".join(f'<section id="{sid}"><h2>{_esc(t)}</h2><p class="sub">{_esc(sub)}</p>{h}</section>'
                   for sid, t, sub, h in sections)
    try:
        from . import __version__ as ver
    except ImportError:
        ver = ""
    doc = (
        '<!DOCTYPE html>\n<html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(name)} 解析レポート</title><style>{CSS}</style></head><body>"
        f'<nav class="toc"><b>videolab</b>{nav}</nav><div class="wrap">'
        + _header(name, meta, prof, T, out_dir, has_audio) + body
        + f'<footer><p class="notice">{_esc(NOTICE)}</p><p>videolab {_esc(ver)} で生成。'
          "数値で測れない要素（台本の面白さ・CGの作り込み・声の演技）は含まれません。</p></footer>"
        f'</div><div id="tip" role="tooltip"></div>'
        f'<script type="application/json" id="tl-data">{data_json}</script>'
        f"<script>{JS}</script></body></html>\n")
    path = out_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path
