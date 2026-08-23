#!/usr/bin/env python3
"""翻訳切り抜き制作パイプライン。

使い方:
    python tools/make_clip.py 001            # clips/001_* を処理(全工程)
    python tools/make_clip.py 001 --skip-fetch   # ダウンロード済み素材で字幕生成〜焼き込みのみ

工程:
  1. fetch-subs : yt-dlp で元動画の自動字幕(VTT)を取得
  2. match      : translation.yaml のアンカー英文を字幕から探し、日本語字幕にタイミングを付与
  3. fetch-video: 切り抜き区間だけ動画をダウンロード
  4. burn       : ffmpeg で字幕を焼き込み → out/ に完成mp4と投稿用メタ(upload.md)を出力

translation.yaml に units が無い(または一致しない)場合は、区間の書き起こし
(transcript.txt)を出力して停止するので、それをClaudeに渡して全訳を作る。
"""

import argparse
import difflib
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML がありません。`pip install pyyaml` を実行してください。")

# ---------------------------------------------------------------- 共通ユーティリティ


def die(msg: str):
    sys.exit(f"[エラー] {msg}")


def info(msg: str):
    print(f"[make_clip] {msg}")


def run(cmd, **kw):
    info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def require_tool(name: str):
    if shutil.which(name) is None:
        die(f"{name} が見つかりません。README.md のセットアップ手順を確認してください。")


def parse_ts(value) -> float:
    """'1:23:45.6' / '12:34' / 90 / '90' → 秒(float)"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def fmt_ts(sec: float) -> str:
    h, rem = divmod(max(sec, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def fmt_ass(sec: float) -> str:
    h, rem = divmod(max(sec, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


# ---------------------------------------------------------------- VTT 解析

TAG_RE = re.compile(r"<[^>]+>")
CUE_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})"
)


def _to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(path: Path):
    """YouTube自動字幕VTTを (start, end, text) のリストに。

    YouTubeの自動字幕は「前のキューの行を再掲しつつ1行ずつ進む」ロール形式なので、
    直前キューに含まれていた行は捨てて、新出行だけをそのキューの発話として扱う。
    """
    raw_cues = []  # [start, end, [lines]]
    cur = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CUE_TIME_RE.search(raw)
        if m:
            if cur:
                raw_cues.append(cur)
            g = m.groups()
            cur = [_to_sec(*g[:4]), _to_sec(*g[4:]), []]
            continue
        if cur is None:
            continue
        text = TAG_RE.sub("", raw).strip()
        if text:
            cur[2].append(text)
    if cur:
        raw_cues.append(cur)

    cleaned = []
    prev_lines: list = []
    for start, end, lines in raw_cues:
        new_lines = [l for l in lines if l not in prev_lines]
        text = " ".join(new_lines).strip()
        if text:
            cleaned.append((start, end, text))
        prev_lines = lines
    return cleaned


def normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9' ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def find_anchor(cues, anchor: str, start_idx: int):
    """anchor に最も近いキューを start_idx 以降から探す。(index, score) を返す。"""
    target = normalize(anchor)
    best = (None, 0.0)
    for i in range(start_idx, len(cues)):
        window = normalize(cues[i][2])
        if target in window:
            return i, 1.0
        nxt = normalize(cues[i + 1][2]) if i + 1 < len(cues) else ""
        joined = (window + " " + nxt).strip()
        # 2キューまたぎの一致。ただし次キュー単独で一致するならそちらを採用させる
        if target in joined and target not in nxt:
            return i, 1.0
        score = difflib.SequenceMatcher(None, target, joined).ratio()
        if score > best[1]:
            best = (i, score)
    return best


# ---------------------------------------------------------------- 設定読み込み


def load_clip_dir(root: Path, key: str) -> Path:
    clips = root / "clips"
    hits = sorted(p for p in clips.iterdir() if p.is_dir() and p.name.startswith(key))
    if not hits:
        die(f"clips/{key}* が見つかりません")
    if len(hits) > 1:
        die(f"clips/{key}* が複数あります: {[p.name for p in hits]}")
    return hits[0]


def load_yaml(path: Path):
    if not path.exists():
        die(f"{path} がありません")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------- 各工程


def stage_fetch_subs(clip_dir: Path, cfg: dict):
    url = cfg["source"]["url"]
    require_tool("yt-dlp")
    probe = run(
        ["yt-dlp", "--skip-download", "--no-warnings",
         "--print", "uploader", "--print", "title", url],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        die(f"動画情報の取得に失敗:\n{probe.stderr.strip()[-500:]}")
    uploader, title = (probe.stdout.strip().splitlines() + ["", ""])[:2]
    info(f"チャンネル: {uploader} / タイトル: {title}")
    expected = cfg["source"].get("expected_channel")
    if expected and expected.lower() not in uploader.lower():
        die(
            f"アップロード元 '{uploader}' が期待値 '{expected}' と一致しません。"
            " 公式ソースか確認してから clip.yaml の expected_channel を更新してください。"
        )
    (clip_dir / "source_title.txt").write_text(f"{title}\n{uploader}\n", encoding="utf-8")

    if not list(clip_dir.glob("source*.vtt")):
        r = run(
            ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
             "--sub-langs", "en.*,en", "-o", "source.%(ext)s", url],
            cwd=clip_dir,
        )
        if r.returncode != 0 or not list(clip_dir.glob("source*.vtt")):
            die("英語字幕(VTT)を取得できませんでした。動画に字幕が無い可能性があります。")
    info("字幕VTT取得済み")


def stage_match(clip_dir: Path, cfg: dict, trans: dict):
    vtts = sorted(clip_dir.glob("source*.vtt"))
    cues = parse_vtt(vtts[0])
    if not cues:
        die("VTTから字幕を読み取れませんでした")

    units = trans.get("units") or []
    win = cfg.get("window") or {}
    w_start, w_end = parse_ts(win.get("start")), parse_ts(win.get("end"))
    lead, tail = float(win.get("lead", 4)), float(win.get("tail", 4))

    matched, idx = [], 0
    for u in units:
        i, score = find_anchor(cues, u["match"], idx)
        if i is None or score < 0.55:
            info(f"  [未一致] {u['match'][:60]} (score={score:.2f})")
            matched.append({**u, "start": None, "end": None, "cue": None})
            continue
        start, end, text = cues[i]
        end = end + float(u.get("extend", 0))
        matched.append({**u, "start": start, "end": end, "cue": text})
        info(f"  [{fmt_ts(start)}] {u['jp'][:30]}…  ←  {text[:50]}")
        idx = i

    hit = [m for m in matched if m["start"] is not None]
    if hit and w_start is None:
        w_start = max(0.0, hit[0]["start"] - lead)
        w_end = hit[-1]["end"] + tail
        info(f"切り抜き区間(自動提案): {fmt_ts(w_start)} 〜 {fmt_ts(w_end)}")

    # 区間内の書き起こしを常に出力(翻訳の追加・検品用)
    t_lo = w_start if w_start is not None else 0.0
    t_hi = w_end if w_end is not None else cues[-1][1]
    lines = [
        f"# transcript: {cfg['source']['url']}",
        f"# 区間: {fmt_ts(t_lo)} 〜 {fmt_ts(t_hi)}",
        "# この内容をClaudeに貼り付けて全訳のtranslation.yamlを作成/更新する",
        "",
    ]
    lines += [f"[{fmt_ts(s)} - {fmt_ts(e)}] {t}" for s, e, t in cues if e >= t_lo and s <= t_hi]
    (clip_dir / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    info(f"書き起こしを出力: {clip_dir / 'transcript.txt'}")

    if not hit:
        info("一致した翻訳ユニットがありません。transcript.txt をClaudeに渡して翻訳を作成してください。")
        return None
    return {"window": (w_start, w_end), "units": hit}


# ---- .ass 生成

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: JP_neuro,{font},78,&H00C88AFF,&H00FFFFFF,&H00201020,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,70,1
Style: JP_evil,{font},78,&H005C5CFF,&H00FFFFFF,&H00100C20,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,70,1
Style: JP_vedal,{font},78,&H007FE07F,&H00FFFFFF,&H00102010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,70,1
Style: JP_other,{font},78,&H00FFFFFF,&H00FFFFFF,&H00151515,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,70,1
Style: EN_orig,{font},44,&H00C8C8C8,&H00FFFFFF,&H00151515,&H80000000,0,0,0,0,100,100,0,0,1,3,0,2,60,60,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def stage_ass(clip_dir: Path, cfg: dict, result: dict) -> Path:
    w_start, _ = result["window"]
    font = (cfg.get("style") or {}).get("font", "Noto Sans CJK JP")
    ev = []
    units = result["units"]
    for n, u in enumerate(units):
        s = u["start"] - w_start
        e = u["end"] - w_start
        # 次のユニットと重なる場合は詰める
        if n + 1 < len(units):
            e = min(e, units[n + 1]["start"] - w_start - 0.05)
        if e <= s:
            e = s + 1.5
        style = f"JP_{u.get('speaker', 'other')}"
        if style not in ("JP_neuro", "JP_evil", "JP_vedal"):
            style = "JP_other"
        jp = str(u["jp"]).replace("\n", r"\N")
        ev.append(f"Dialogue: 0,{fmt_ass(s)},{fmt_ass(e)},{style},,0,0,0,,{jp}")
        if u.get("en_show", True) and u.get("cue"):
            en = str(u["cue"]).replace("\n", " ")
            ev.append(f"Dialogue: 0,{fmt_ass(s)},{fmt_ass(e)},EN_orig,,0,0,0,,{en}")
    out = clip_dir / "subs.ass"
    out.write_text(ASS_HEADER.format(font=font) + "\n".join(ev) + "\n", encoding="utf-8")
    info(f"字幕ファイル生成: {out}")
    return out


def stage_fetch_video(clip_dir: Path, cfg: dict, result: dict):
    existing = [p for p in clip_dir.glob("cut.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if existing:
        info(f"切り出し済み動画を再利用: {existing[0].name}")
        return existing[0]
    require_tool("yt-dlp")
    require_tool("ffmpeg")
    w_start, w_end = result["window"]
    section = f"*{fmt_ts(w_start)}-{fmt_ts(w_end)}"
    r = run(
        ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--download-sections", section, "--force-keyframes-at-cuts",
         "--merge-output-format", "mp4", "-o", "cut.%(ext)s", cfg["source"]["url"]],
        cwd=clip_dir,
    )
    files = [p for p in clip_dir.glob("cut.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if r.returncode != 0 or not files:
        die("動画区間のダウンロードに失敗しました")
    return files[0]


def stage_burn(clip_dir: Path, cfg: dict, video: Path, ass: Path) -> Path:
    require_tool("ffmpeg")
    out_dir = clip_dir / "out"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{clip_dir.name}.mp4"
    r = run(
        ["ffmpeg", "-y", "-i", video.name, "-vf", f"ass={ass.name}",
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "192k", str(out)],
        cwd=clip_dir,
    )
    if r.returncode != 0 or not out.exists():
        die("字幕焼き込みに失敗しました")
    info(f"完成: {out}")
    return out


def stage_upload_md(clip_dir: Path, cfg: dict):
    up = cfg.get("upload") or {}
    src = cfg["source"]["url"]
    title_file = clip_dir / "source_title.txt"
    src_title = title_file.read_text(encoding="utf-8").splitlines()[0] if title_file.exists() else ""
    body = [
        f"# 投稿メタ: {clip_dir.name}",
        "",
        "## タイトル",
        up.get("title", "(未設定)"),
        "",
        "## 説明欄",
        "```",
        f"元動画 / Source: {src}",
        f"{src_title}",
        "",
        up.get("description", "").strip(),
        "",
        "本動画は Neuro-sama 公式のクリップ方針 (https://vedal.ai/advice/) に基づく翻訳切り抜きです。",
        "This is a Japanese-translated clip based on Vedal's clip policy.",
        "```",
        "",
        "## タグ",
        ", ".join(up.get("tags", [])),
        "",
        "## サムネ文字",
        up.get("thumbnail_text", "(未設定)"),
        "",
    ]
    out = clip_dir / "out" / "upload.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(body), encoding="utf-8")
    info(f"投稿用メタ情報: {out}")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("clip", help="clips/ 配下のフォルダ名(前方一致。例: 001)")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="ダウンロードを行わず、既存の source*.vtt / cut.* を使う")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    clip_dir = load_clip_dir(root, args.clip)
    info(f"対象: {clip_dir.name}")
    cfg = load_yaml(clip_dir / "clip.yaml")
    trans = load_yaml(clip_dir / "translation.yaml")

    if not args.skip_fetch:
        stage_fetch_subs(clip_dir, cfg)
    result = stage_match(clip_dir, cfg, trans)
    if result is None:
        sys.exit(0)
    ass = stage_ass(clip_dir, cfg, result)
    if args.skip_fetch:
        vids = [p for p in clip_dir.glob("cut.*") if p.suffix in (".mp4", ".mkv", ".webm")]
        if not vids:
            die("--skip-fetch 指定ですが cut.mp4 がありません")
        video = vids[0]
    else:
        video = stage_fetch_video(clip_dir, cfg, result)
    stage_burn(clip_dir, cfg, video, ass)
    stage_upload_md(clip_dir, cfg)
    info("全工程完了。out/ を確認して transcript.txt で訳漏れがないかチェックしてください。")


if __name__ == "__main__":
    main()
