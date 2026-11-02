#!/usr/bin/env python3
"""カット位置から、キラキラ・白発光をどこに置くかを決めて effects.json を書く。

    python video_edit/make_fx_plan.py --name <プロジェクト名>
    python video_edit/make_fx_plan.py --name <名前> --from-analysis analysis/otehon

お手本の「何秒に1回出るか」を真似る。絶対時刻をそのまま写すと尺の違う動画では
破綻するので、密度だけを引き継いでカット頭に割り当てる。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from vedit_common import PROJECTS_DIR, load_json, log, parse_rate, save_json, setup_console

# お手本（縦型トーク動画）の実測値を既定値にしてある
DEFAULT_SPARKLE_EVERY = 10.6   # 秒に1回
DEFAULT_GLOW_EVERY = 12.3


def density_from_analysis(adir: Path) -> tuple[float | None, float | None]:
    """video_style の解析結果から「何秒に1回か」を読む。"""
    ev = adir / "events.json"
    summ = adir / "summary.json"
    if not ev.exists() or not summ.exists():
        return None, None
    e = json.loads(ev.read_text(encoding="utf-8"))
    s = json.loads(summ.read_text(encoding="utf-8"))
    sec = float(s.get("source", {}).get("analyzed_sec") or 0)
    if sec <= 0:
        return None, None
    bursts = len(e.get("color_bursts") or [])
    flashes = sum(1 for sp in (e.get("specials") or []) if sp.get("kind") == "flash")
    return (sec / bursts if bursts else None), (sec / flashes if flashes else None)


def cut_heads(edit: dict) -> list[float]:
    """編集後のタイムラインでの、各カットの頭（秒）。"""
    fps = float(parse_rate(edit["output"]["fps"]))
    heads, prev_end = [], None
    for sg in edit["segments"]:
        if prev_end is None or sg["f0"] != prev_end:   # 本当のつなぎ目だけ
            heads.append(sg["o0"] / fps)
        prev_end = sg["f1"]
    return heads


def pick(heads: list[float], every: float, out_sec: float, offset: int = 0) -> list[float]:
    """カット頭から、おおよそ every 秒おきになるように選ぶ。"""
    if every <= 0 or not heads:
        return []
    picked, last = [], -1e9
    for i, t in enumerate(heads):
        if i < offset:
            continue
        if t - last >= every and t < out_sec:
            picked.append(round(t, 3))
            last = t
    return picked


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="演出(キラキラ・白発光)の配置を決める")
    ap.add_argument("--name", required=True)
    ap.add_argument("--from-analysis", type=Path,
                    help="video_style の解析フォルダ。出現密度をここから読む")
    ap.add_argument("--sparkle-every", type=float, help=f"キラキラを何秒に1回（既定 {DEFAULT_SPARKLE_EVERY}）")
    ap.add_argument("--glow-every", type=float, help=f"白発光を何秒に1回（既定 {DEFAULT_GLOW_EVERY}）")
    ap.add_argument("--sparkle-size", type=float, default=0.62, help="キラキラの大きさ(画面幅比)")
    ap.add_argument("--glow-size", type=float, default=0.45)
    ap.add_argument("--sparkle-pos", default="0.50,0.52", help="キラキラの位置 x,y（画面比）")
    ap.add_argument("--glow-pos", default="0.50,0.45")
    ap.add_argument("--opacity", type=float, default=0.9)
    ap.add_argument("--assets", default="assets", help="素材フォルダ（プロジェクトからの相対）")
    args = ap.parse_args(argv)

    proj = PROJECTS_DIR / args.name
    edit = load_json(proj / "edit.json")
    out_sec = float(edit["out_sec"])

    sp_every = args.sparkle_every
    gl_every = args.glow_every
    if args.from_analysis:
        a, b = density_from_analysis(args.from_analysis)
        sp_every = sp_every or a
        gl_every = gl_every or b
        log(f"[密度] 解析から読み取り: キラキラ {a and round(a, 1)}秒に1回 / "
            f"白発光 {b and round(b, 1)}秒に1回")
    sp_every = sp_every or DEFAULT_SPARKLE_EVERY
    gl_every = gl_every or DEFAULT_GLOW_EVERY

    heads = cut_heads(edit)
    sx, sy = (float(v) for v in args.sparkle_pos.split(","))
    gx, gy = (float(v) for v in args.glow_pos.split(","))
    events = []
    for t in pick(heads, sp_every, out_sec):
        events.append({"type": "sparkle", "at": t, "x": sx, "y": sy,
                       "size": args.sparkle_size, "opacity": args.opacity})
    for t in pick(heads, gl_every, out_sec, offset=1):   # キラキラと頭を揃えすぎない
        events.append({"type": "glow", "at": t, "x": gx, "y": gy,
                       "size": args.glow_size, "opacity": args.opacity})
    events.sort(key=lambda e: e["at"])

    plan = {
        "_説明": "at=編集後の動画での秒。x,y,size は画面に対する比。render.py が読む",
        "assets": {"sparkle": f"{args.assets}/sparkle_pink.mov",
                   "glow": f"{args.assets}/glow_white.mov"},
        "events": events,
    }
    save_json(proj / "effects.json", plan)
    n_sp = sum(1 for e in events if e["type"] == "sparkle")
    n_gl = len(events) - n_sp
    log(f"[完了] キラキラ {n_sp}箇所 / 白発光 {n_gl}箇所 → {proj / 'effects.json'}")
    log(f"  素材は python video_edit/make_assets.py -o {proj / args.assets} で作る")
    return plan


if __name__ == "__main__":
    main()
