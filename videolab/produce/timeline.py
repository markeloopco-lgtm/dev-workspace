"""台本 + スタイルプロファイル → タイムライン(台詞・ショット・テロップ・音の配置)。

VAIENCEの求人に書かれた編集工程(ナレーションに合わせたカット編集 → テロップ →
グラフィック → 色と音の調整)を自動化する考え方で、先にナレーションを合成して
尺を確定させ、その尺に対してプロファイルのテンポでカットを割る。
"""

import math
from dataclasses import dataclass, field

import numpy as np

from .sources import CameraMove
from .telop import split_telop
from .tts import count_chars


@dataclass
class LineClip:
    scene_id: str
    speaker: str
    text: str
    start: float
    dur: float
    audio: np.ndarray
    sr: int


@dataclass
class ShotPlan:
    index: int
    scene_id: str
    start: int                 # フレーム(含む)
    end: int                   # フレーム(含まない)
    visual: dict
    camera: CameraMove
    trans_in: str = "cut"      # start / cut / dissolve / fade_black
    trans_len: int = 0         # フレーム数
    pad_in: int = 0            # ディゾルブのため前に余分に描くフレーム数
    pad_out: int = 0
    seed: int = 0


@dataclass
class Plan:
    fps: float
    w: int
    h: int
    total_frames: int
    lines: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    telops: list = field(default_factory=list)    # {start, end, text, speaker, color}
    titles: list = field(default_factory=list)    # {start, end, text}
    callouts: list = field(default_factory=list)  # {start, end, text} 強調テキスト
    bgm: list = field(default_factory=list)       # {file, start, end, volume_db}
    se: list = field(default_factory=list)        # {file, t, volume_db}
    scenes: list = field(default_factory=list)    # {id, start, end}

    @property
    def duration(self) -> float:
        return self.total_frames / self.fps


def _get(style: dict, path: str, default):
    cur = style
    for p in path.split("."):
        if not isinstance(cur, dict) or cur.get(p) is None:
            return default
        cur = cur[p]
    return cur


# 同じ素材を続けて使う時の構図の変え方 (寄りの倍率, 中身のx移動, y移動)。
# 1回目は素のまま、2回目以降は大きく寄せてずらし、別カットと分かる画角差にする
FRAMINGS = [(1.0, 0.0, 0.0), (1.4, 0.12, -0.06), (1.25, -0.1, 0.05), (1.55, 0.06, 0.08)]


class DeficitPicker:
    """構成比(mix)に沿って決定的に選ぶ: 「期待回数 − 実回数」が最大のものを選ぶ。"""

    def __init__(self, mix: dict):
        tot = sum(mix.values()) or 1.0
        self.mix = {k: v / tot for k, v in mix.items() if v > 0}
        self.counts = {k: 0 for k in self.mix}
        self.n = 0

    def pick(self, allowed=None, avoid=None) -> str:
        self.n += 1
        keys = [k for k in self.mix if (allowed is None or k in allowed) and k != avoid] or \
            [k for k in self.mix if allowed is None or k in allowed] or list(self.mix)
        k = max(keys, key=lambda k: (self.mix[k] * self.n - self.counts[k], self.mix[k]))
        self.counts[k] += 1
        return k


def _camera_for(kind: str, dur: float, style: dict, easing: str, override=None) -> CameraMove:
    if override:
        if isinstance(override, str):
            override = {"kind": override}
        kind = override.get("kind", kind)
        easing = override.get("easing", easing)
        amount = override.get("amount")
    else:
        amount = None
    if kind in ("static_action", "unknown", "rotate"):
        kind = "static"
    if amount is None:
        if kind.startswith("zoom"):
            amount = float(np.clip(_get(style, "camera.zoom_speed_median", 0.025) * dur, 0.03, 0.3))
        elif kind.startswith(("pan", "tilt")):
            amount = float(np.clip(_get(style, "camera.pan_speed_median", 0.02) * dur, 0.02, 0.25))
        else:
            amount = 0.0
    return CameraMove(kind=kind, amount=float(amount), easing=easing)


def synth_lines(ep: dict, narrator, style: dict) -> dict:
    """全台詞を合成する。speed: auto の話者は目標話速に合わせて速度を補正する。"""
    target_cps = _get(style, "audio.chars_per_sec", 7.5)
    speeds = {}
    by_speaker = {}
    for sc in ep["scenes"]:
        for ln in sc["lines"]:
            by_speaker.setdefault(ln["speaker"], []).append(ln)
    for spk, lines in by_speaker.items():
        cfg = narrator.voice_cfg(spk)
        sp = cfg.get("speed", "auto")
        if sp == "auto" and cfg.get("engine") != "dummy":
            chars = sum(count_chars(ln["text"]) for ln in lines)
            dur = 0.0
            for ln in lines:
                y, sr = narrator.synth(spk, ln["text"], 1.0)
                dur += len(y) / sr
            factor = (target_cps / (chars / dur)) if chars and dur else 1.0
            speeds[spk] = float(np.clip(factor, 0.7, 1.45))
        else:
            speeds[spk] = 1.0 if sp == "auto" else float(sp)
    out = {}
    for spk, lines in by_speaker.items():
        for ln in lines:
            out[id(ln)] = narrator.synth(spk, ln["text"], speeds[spk])
    return out, speeds


def build_plan(ep: dict, style: dict, narrator, w: int, h: int, fps: float) -> Plan:
    audio_of, speeds = synth_lines(ep, narrator, style)
    gap_default = float(_get(style, "audio.gap_median", 0.35))
    L_rest = float(_get(style, "editing.shot_len_median", 3.0))
    c_first = _get(style, "editing.cuts_per_min_first30s", None)
    c_rest = _get(style, "editing.cuts_per_min_rest", None)
    L_first = L_rest * (c_rest / c_first) if c_first and c_rest else L_rest
    L_first = float(np.clip(L_first, 0.6, L_rest * 1.5))
    p10 = float(_get(style, "editing.shot_len_p10", 1.0))
    diss_f = max(2, int(round(float(_get(style, "editing.dissolve_len_mean", 0.5)) * fps)))
    fade_f = max(4, int(round(0.6 * fps)))
    easing_mix = _get(style, "camera.easing_mix", {"linear": 1.0}) or {"linear": 1.0}
    easing_pick = DeficitPicker(easing_mix)
    cam_mix = dict(_get(style, "camera.move_mix", {"static": 1.0}) or {"static": 1.0})
    cam_mix.pop("unknown", None)
    cam_mix.pop("rotate", None)
    cam_pick = DeficitPicker(cam_mix or {"static": 1.0})
    trans_mix = dict(_get(style, "editing.transition_mix", {"cut": 1.0}) or {"cut": 1.0})
    trans_pick = DeficitPicker(trans_mix)

    plan = Plan(fps=fps, w=w, h=h, total_frames=0)
    t = 0.3
    scene_bounds = []
    for sc in ep["scenes"]:
        s0 = t
        for i, ln in enumerate(sc["lines"]):
            y, sr = audio_of[id(ln)]
            dur = len(y) / sr
            plan.lines.append(LineClip(sc["id"], ln["speaker"], ln["text"], t, dur, y, sr))
            ln["_start"], ln["_dur"] = t, dur
            t += dur
            if i < len(sc["lines"]) - 1:
                t += float(ln.get("gap", gap_default))
        if not sc["lines"]:
            t += float(sc["duration"])
        t += float(sc.get("pause_after", max(gap_default, 0.6)))
        if sc.get("min_duration"):
            t = max(t, s0 + float(sc["min_duration"]))
        scene_bounds.append((sc, s0, t))
        plan.scenes.append({"id": sc["id"], "start": round(s0, 3), "end": round(t, 3)})
    total = t + 0.8
    plan.total_frames = int(math.ceil(total * fps))

    # ---- ショット割り
    shot_times = []   # (start_s, end_s, visual, scene, is_scene_start, cam_override)
    for si, (sc, s0, s1) in enumerate(scene_bounds):
        if si == len(scene_bounds) - 1:
            s1 = total
        segs = [(s0, sc["visuals"])]
        for ln in sc["lines"]:
            if ln.get("visual"):
                st = s0 if ln is sc["lines"][0] else ln["_start"]
                if segs and abs(segs[-1][0] - st) < 1e-6:
                    segs[-1] = (st, [ln["visual"]])
                else:
                    segs.append((st, [ln["visual"]]))
        segs = [(a, v) for a, v in segs if v]
        line_starts = [ln["_start"] for ln in sc["lines"]]
        for k, (a, vis) in enumerate(segs):
            b = segs[k + 1][0] if k + 1 < len(segs) else s1
            D = b - a
            L = L_first if a < 30.0 else L_rest
            n = max(1, int(round(D / L)))
            if len(vis) > n and D / len(vis) >= max(0.8, 0.8 * p10):
                n = len(vis)
            cuts = [a + D * j / n for j in range(1, n)]
            snapped = []
            for c in cuts:
                near = [ls for ls in line_starts if a + 0.5 < ls < b - 0.5 and abs(ls - c) <= 0.35 * L]
                c2 = min(near, key=lambda ls: abs(ls - c)) if near else c
                if (not snapped or c2 - snapped[-1] >= 0.5) and c2 - a >= 0.5 and b - c2 >= 0.5:
                    snapped.append(c2)
            edges = [a] + snapped + [b]
            for j in range(len(edges) - 1):
                v = vis[j % len(vis)]
                reuse = j // len(vis)      # 同じ素材の何回目の使用か → 構図を変える
                shot_times.append((edges[j], edges[j + 1], v, sc, j == 0 and k == 0,
                                   v.get("camera") if j < len(vis) else None,
                                   FRAMINGS[reuse % len(FRAMINGS)]))

    prev_cam = None
    for i, (a, b, v, sc, scene_start, cam_override, framing) in enumerate(shot_times):
        sf, ef = int(round(a * fps)), int(round(b * fps))
        if i == 0:
            sf = 0
        if i == len(shot_times) - 1:
            ef = plan.total_frames
        if ef <= sf:
            continue
        dur = (ef - sf) / fps
        if v["type"] == "color":      # 無地の下地は動かしても見えないので静止
            cam = _camera_for("static", dur, style, "linear", None)
        else:
            kind = cam_pick.pick(avoid=prev_cam if prev_cam != "static" else None)
            cam = _camera_for(kind, dur, style, easing_pick.pick(), cam_override)
            cam.frame_scale, cam.frame_x, cam.frame_y = framing
            prev_cam = cam.kind
        if i == 0:
            tr, tl = "start", 0
        else:
            tr = trans_pick.pick(allowed=None if scene_start else ("cut", "dissolve"))
            if tr == "fade_black" and not scene_start:
                tr = "cut"
            tl = diss_f if tr == "dissolve" else (fade_f if tr == "fade_black" else 0)
            if tr not in ("cut", "dissolve", "fade_black"):
                tr, tl = "cut", 0
        plan.shots.append(ShotPlan(len(plan.shots), sc["id"], sf, ef, v, cam, tr, tl,
                                   seed=len(plan.shots) * 7919 + 17))
    # 端点をつなげて隙間・重なりを無くす
    for a_, b_ in zip(plan.shots, plan.shots[1:]):
        b_.start = a_.end
    for prev, cur in zip(plan.shots, plan.shots[1:]):
        if cur.trans_in == "dissolve":
            half = min(cur.trans_len // 2, (prev.end - prev.start) // 2, (cur.end - cur.start) // 2)
            cur.trans_len = max(2, 2 * half)
            cur.pad_in = cur.trans_len // 2
            prev.pad_out = cur.trans_len - cur.trans_len // 2
        elif cur.trans_in == "fade_black":
            cur.trans_len = min(cur.trans_len, prev.end - prev.start, cur.end - cur.start)

    # ---- テロップ・タイトル・効果音・BGM
    tstyle = ep.get("_telop_style", {})
    max_chars = int(tstyle.get("max_chars", 22))
    colors = tstyle.get("speaker_colors", {}) or {}
    next_start = [ln.start for ln in plan.lines[1:]] + [total]
    for ln, nxt in zip(plan.lines, next_start):
        src_line = _find_line(ep, ln)
        if src_line is not None and src_line.get("callout"):
            plan.callouts.append({"start": round(ln.start + 0.15, 3),
                                  "end": round(min(nxt, ln.start + ln.dur + 0.4), 3),
                                  "text": str(src_line["callout"])})
        if src_line is not None and src_line.get("telop") is False:
            continue
        text = src_line.get("telop") if src_line and isinstance(src_line.get("telop"), str) else ln.text
        chunks = split_telop(text, max_chars)
        weights = [max(1, count_chars(c)) for c in chunks]
        tot = sum(weights)
        acc = 0
        end_all = nxt if nxt - (ln.start + ln.dur) < 0.5 else ln.start + ln.dur + 0.2
        for c, wgt in zip(chunks, weights):
            cs = ln.start + ln.dur * acc / tot
            acc += wgt
            ce = ln.start + ln.dur * acc / tot if acc < tot else end_all
            plan.telops.append({"start": round(cs, 3), "end": round(ce, 3), "text": c,
                                "speaker": ln.speaker, "color": colors.get(ln.speaker)})
    tdur = float(ep.get("_title_style", {}).get("duration", 2.2))
    for sc, s0, s1 in scene_bounds:
        if sc.get("title"):
            plan.titles.append({"start": s0, "end": min(s1, s0 + tdur), "text": sc["title"]})
        for se in sc.get("se") or []:
            plan.se.append({"file": se["file"], "t": s0 + float(se.get("at", 0.0)),
                            "volume_db": se.get("volume_db")})
    starts = {sc["id"]: s0 for sc, s0, _ in scene_bounds}
    ends = {sc["id"]: s1 for sc, _, s1 in scene_bounds}
    for bg in ep.get("bgm") or []:
        a = starts.get(bg.get("from_scene"), 0.0)
        b = ends.get(bg.get("to_scene"), total)
        plan.bgm.append({"file": bg["file"], "start": a, "end": b,
                         "volume_db": bg.get("volume_db")})
    plan.speeds = speeds
    return plan


def _find_line(ep: dict, clip: LineClip):
    for sc in ep["scenes"]:
        if sc["id"] != clip.scene_id:
            continue
        for ln in sc["lines"]:
            if abs(ln.get("_start", -1) - clip.start) < 1e-6:
                return ln
    return None
