"""タイムライン → 映像レンダリング(ショット合成・トランジション・テロップ) → ffmpegでmp4化。

フレームは全てPython(numpy/OpenCV)で合成して生のRGBをffmpegに流し込む。
ffmpegの複雑なフィルタ指定(Windowsでのパスのエスケープ問題)を避けるため。
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from .. import ffmpeg_util as ff
from . import sources as src_mod
from .episode import load_episode
from .mixer import mix
from .telop import DEFAULT_TELOP, DEFAULT_TITLE, TextRenderer, blend, find_font, write_srt
from .timeline import build_plan
from .tts import Narrator


def _space_factory():
    try:
        from .space import make_space_source          # noqa: F401
    except ImportError:
        from .space2d import make_space_source        # noqa: F401
    return make_space_source


def make_source(shot, w: int, h: int, fps: float, engine: str, cache_dir: Path):
    from .overlay import wrap_with_overlays
    return wrap_with_overlays(_base_source(shot, w, h, fps, engine, cache_dir), shot.visual,
                              w, h, fps, seed=shot.seed)


def _base_source(shot, w: int, h: int, fps: float, engine: str, cache_dir: Path):
    v = shot.visual
    n = (shot.end - shot.start) + shot.pad_in + shot.pad_out
    cam = shot.camera
    t = v["type"]
    if t == "image":
        return src_mod.ImageSource(v["path"], n, w, h, cam)
    if t == "video":
        return src_mod.VideoSource(v["path"], n, w, h, float(v.get("start", 0.0)), cam, fps)
    if t == "color":
        return src_mod.ColorSource(n, w, h, v.get("color", "#101018"), v.get("color2"),
                                   label=v.get("_todo"))
    if t == "space":
        make_space_source = _space_factory()
        return make_space_source(v.get("template", "planet"), dict(v.get("params") or {}), n, w, h,
                                 fps, cam, engine=v.get("engine", engine),
                                 seed=int(v.get("seed", shot.seed)), cache_dir=cache_dir)
    raise ValueError(f"未知の映像タイプ: {t}")


def encoder_args(encoder: str, draft: bool) -> list:
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5" if not draft else "p2", "-cq",
                "19" if not draft else "26", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium" if not draft else "veryfast",
            "-crf", "18" if not draft else "24", "-pix_fmt", "yuv420p"]


def render_video(plan, audio_wav: Path, out_path: Path, engine: str = "auto",
                 encoder: str = "x264", draft: bool = False, telop_style: dict = None,
                 title_style: dict = None, cache_dir: Path = Path("renders/cache"),
                 quiet: bool = False, callout_style: dict = None) -> Path:
    w, h, fps = plan.w, plan.h, plan.fps
    font = find_font((telop_style or {}).get("font", "auto"))
    telop = TextRenderer(w, h, {**DEFAULT_TELOP, **(telop_style or {})}, font)
    title = TextRenderer(w, h, {**DEFAULT_TELOP, **DEFAULT_TITLE, **(title_style or {})}, font)
    from .overlay import CalloutRenderer
    callout = CalloutRenderer(w, h, callout_style, font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff.find_ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-", "-i", str(audio_wav),
           *encoder_args(encoder, draft), "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
           "-t", f"{plan.duration:.3f}", "-movflags", "+faststart", str(out_path)]
    errf = tempfile.TemporaryFile()      # stderrをパイプにすると詰まることがあるので一時ファイルへ
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
    broken = False
    shots = plan.shots
    live = {}
    t0 = time.time()
    telops = sorted(plan.telops, key=lambda e: e["start"])
    try:
        for f in range(plan.total_frames):
            tsec = f / fps
            frame = _compose_frame(f, shots, live, w, h, fps, engine, cache_dir)
            for ev in telops:
                if ev["start"] <= tsec < ev["end"]:
                    arr, x, y = telop.render(ev["text"], ev.get("color"))
                    blend(frame, arr, x, y)
                elif ev["start"] > tsec:
                    break
            for ev in plan.callouts:
                if ev["start"] <= tsec < ev["end"]:
                    callout.draw(frame, ev["text"], tsec - ev["start"], ev["end"] - ev["start"])
            for ev in plan.titles:
                if ev["start"] <= tsec < ev["end"]:
                    dur = ev["end"] - ev["start"]
                    u = (tsec - ev["start"]) / dur
                    alpha = min(1.0, u / 0.12, (1 - u) / 0.2)
                    arr, x, y = title.render(ev["text"])
                    blend(frame, arr, x, y, max(0.0, alpha))
            if frame.dtype != np.uint8 or frame.shape != (h, w, 3):
                raise RuntimeError(f"内部エラー: フレームの形式が不正です {frame.dtype} {frame.shape}")
            try:
                proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            except OSError:      # ffmpegが先に終了した(BrokenPipe / WindowsではEINVAL)
                broken = True
                break
            if not quiet and (f % 60 == 0 or f == plan.total_frames - 1):
                el = time.time() - t0
                eta = el / (f + 1) * (plan.total_frames - f - 1)
                sys.stderr.write(f"\r  映像レンダリング {f + 1}/{plan.total_frames}  残り約{eta:5.0f}秒 ")
                sys.stderr.flush()
    finally:
        for s in list(live.values()):
            s.close()
        try:
            proc.stdin.close()
        except Exception:
            pass
        rc = proc.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
        errf.close()
    if not quiet:
        sys.stderr.write("\n")
    if rc != 0 or broken:
        hint = ""
        if encoder == "nvenc":
            hint = "\n  NVENCが使えない可能性: GPUドライバを更新するか --encoder x264 で再実行してください"
        elif "Permission denied" in err:
            hint = "\n  出力ファイルが動画プレーヤー等で開かれていないか確認してください"
        raise RuntimeError(f"エンコードに失敗しました:\n{err[-1500:]}{hint}")
    return out_path


def _shot_frame(shot, f, live, w, h, fps, engine, cache_dir):
    if shot.index not in live:
        live[shot.index] = make_source(shot, w, h, fps, engine, cache_dir)
    i = f - (shot.start - shot.pad_in)
    src = live[shot.index]
    i = max(0, min(src.n_frames - 1, i))
    return src.frame(i)


def _compose_frame(f, shots, live, w, h, fps, engine, cache_dir):
    # 使い終わったソースを閉じる
    for idx in [k for k in live if shots[k].end + shots[k].pad_out <= f]:
        live.pop(idx).close()
    cur = None
    for s in shots:
        if s.start <= f < s.end:
            cur = s
            break
    if cur is None:
        cur = shots[-1]
    nxt = shots[cur.index + 1] if cur.index + 1 < len(shots) else None
    prv = shots[cur.index - 1] if cur.index > 0 else None
    # ディゾルブ: 境界Bの前後 [B - pad_in, B + pad_out) で前後ショットを混ぜる
    if nxt is not None and nxt.trans_in == "dissolve" and f >= nxt.start - nxt.pad_in:
        return _mix2(cur, nxt, f, nxt, live, w, h, fps, engine, cache_dir)
    if cur.trans_in == "dissolve" and prv is not None and f < cur.start + prv.pad_out:
        return _mix2(prv, cur, f, cur, live, w, h, fps, engine, cache_dir)
    img = _shot_frame(cur, f, live, w, h, fps, engine, cache_dir)
    # 暗転: 境界の前半でフェードアウト、後半でフェードイン
    if nxt is not None and nxt.trans_in == "fade_black":
        half = nxt.trans_len // 2
        if f >= nxt.start - half:
            k = (nxt.start - f) / max(1, half)
            return (img.astype(np.float32) * max(0.0, k - 1 / max(1, half))).astype(np.uint8)
    if cur.trans_in == "fade_black":
        half = cur.trans_len - cur.trans_len // 2
        if f < cur.start + half:
            k = (f - cur.start + 1) / max(1, half)
            return (img.astype(np.float32) * min(1.0, k)).astype(np.uint8)
    return img.copy()   # テロップを上書きするので必ず複製(ソース側のキャッシュを汚さない)


def _mix2(a, b, f, boundary_shot, live, w, h, fps, engine, cache_dir):
    B = boundary_shot.start
    lo = B - boundary_shot.pad_in
    L = boundary_shot.pad_in + a.pad_out
    alpha = (f - lo + 0.5) / max(1, L)
    ia = _shot_frame(a, f, live, w, h, fps, engine, cache_dir).astype(np.float32)
    ib = _shot_frame(b, f, live, w, h, fps, engine, cache_dir).astype(np.float32)
    return (ia * (1 - alpha) + ib * alpha).astype(np.uint8)


def produce_episode(episode_path, style_path=None, out_path=None, tts: str = None,
                    engine: str = "auto", encoder: str = "x264", draft: bool = False,
                    quiet: bool = False) -> dict:
    """台本YAMLから完成動画を作る。返り値: 出力ファイル類のパスと統計。"""
    from ..profile import load_profile

    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    # 下書き(--draft)では未配置の素材を「素材TODO」の仮カードで代用して最後まで通す
    ep = load_episode(episode_path, allow_missing=draft)
    for m in ep.get("_missing", []):
        say(f"      素材TODO(未配置・仮カードで代用): {m}")
    style_path = style_path or ep.get("style") or "configs/style_profile.yaml"
    style = load_profile(style_path)
    fmt = style.get("format", {})
    # 出力解像度は台本の resolution > 1920x1080。参考動画の解析解像度(取得は720p上限)は使わない
    w, h = ep.get("resolution") or [1920, 1080]
    fps = float(ep.get("fps") or fmt.get("fps") or 30.0)
    w, h = ff.even(w), ff.even(h)
    if draft:
        w, h = ff.even(w / 2), ff.even(h / 2)
    out_path = Path(out_path or ep.get("output") or Path("renders") / f"{ep['_name']}.mp4")
    ep["_telop_style"] = {**DEFAULT_TELOP, **(ep.get("telop") or {})}
    ep["_title_style"] = {**DEFAULT_TITLE, **(ep.get("title_style") or {})}
    if (ep.get("telop") or {}).get("y") is None and style.get("telop", {}).get("y_median"):
        ep["_telop_style"]["y"] = float(style["telop"]["y_median"])

    cache = Path("renders/cache")
    narrator = Narrator(ep.get("voices"), cache / "tts", force_engine=tts,
                        target_cps=style.get("audio", {}).get("chars_per_sec"))
    say(f"[1/4] ナレーション合成 ({len([l for s in ep['scenes'] for l in s['lines']])}台詞)")
    plan = build_plan(ep, style, narrator, int(w), int(h), fps)
    say(f"      尺 {plan.duration:.1f}秒 / {len(plan.shots)}ショット / テロップ{len(plan.telops)}枚")
    say("[2/4] 音声ミックス(BGMダッキング・ラウドネス正規化)")
    wav = out_path.with_suffix(".mix.wav")
    mix_stats = mix(plan, style, wav)
    say(f"      ナレーション {mix_stats['speech_lufs']} LUFS → 完成 {mix_stats['final_lufs']} LUFS")
    say("[3/4] 映像レンダリング")
    try:
        render_video(plan, wav, out_path, engine=engine, encoder=encoder, draft=draft,
                     telop_style=ep["_telop_style"], title_style=ep["_title_style"],
                     cache_dir=cache, quiet=quiet, callout_style=ep.get("callout_style"))
    except BaseException:
        wav.unlink(missing_ok=True)
        raise
    say("[4/4] 字幕・クレジット・設計図を書き出し")
    srt = Path(str(out_path.with_suffix("")) + ".ja.srt")
    write_srt([{"start": ln.start, "end": ln.start + ln.dur, "text": ln.text}
               for ln in plan.lines], srt)
    credits = sorted({narrator.credit(ln.speaker) for ln in plan.lines})
    credits += list(ep.get("credits") or [])
    cred_path = out_path.with_suffix(".credits.txt")
    cred_path.write_text("【クレジット（概要欄に記載）】\n" + "\n".join(credits) + "\n",
                         encoding="utf-8")
    plan_path = out_path.with_suffix(".plan.json")
    plan_path.write_text(json.dumps({
        "episode": str(episode_path), "style": str(style_path), "duration": plan.duration,
        "resolution": [plan.w, plan.h], "fps": plan.fps, "speeds": plan.speeds,
        "mix": mix_stats, "scenes": plan.scenes,
        "shots": [{"index": s.index, "scene": s.scene_id, "start": round(s.start / fps, 3),
                   "end": round(s.end / fps, 3), "visual": s.visual, "camera": vars(s.camera),
                   "transition_in": s.trans_in, "transition_len": round(s.trans_len / fps, 3)}
                  for s in plan.shots],
        "telops": plan.telops, "callouts": plan.callouts,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    wav.unlink(missing_ok=True)
    say(f"完成: {out_path}")
    return {"video": out_path, "srt": srt, "credits": cred_path, "plan": plan_path,
            "mix": mix_stats, "style": str(style_path), "missing": ep.get("_missing", [])}
