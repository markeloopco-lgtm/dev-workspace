"""解析の一括実行: 動画1本 → analysis/<名前>/ 以下に全成果物を書き出す。

  meta.json        動画情報・遷移(カット/ディゾルブ/暗転/フラッシュ)一覧・テロップ色
  frames.csv       フレーム毎の計測値(Excelで開ける)
  shots.json/.csv  ショット毎の集計(尺・カメラワーク・イージング・配色・テロップ率)
  audio.json       ラウドネス・話速・間・BGM音量差など
  transcript.json  文字起こし/字幕(ある場合)
  profile.json     スタイルプロファイル(数値のみ)
  keyframes/       ショット代表フレーム(※参考動画の複製。共有・コミットしない)
  contact_sheet.jpg 全ショット一覧画像(同上)
  report.html      ブラウザで見る解析レポート
"""

import json
import math
from pathlib import Path

import cv2
import numpy as np

from . import analyze, audio, ffmpeg_util as ff, profile as prof_mod


def find_subtitles(video_path: Path):
    """動画と同じ場所にある字幕(yt-dlpの <名前>.ja.vtt など)を探す。"""
    stem = video_path.with_suffix("")
    for pat in (".ja.vtt", ".ja.srt", ".ja-orig.vtt", ".vtt", ".srt"):
        cand = Path(str(stem) + pat)
        if cand.exists():
            return cand
    for cand in sorted(video_path.parent.glob(video_path.stem + ".*")):
        if cand.suffix in (".vtt", ".srt"):
            return cand
    return None


def contact_sheet(shots: list, out_dir: Path, cols: int = 6, tile_w: int = 320) -> Path:
    tiles = []
    for s in shots:
        kf = s.get("keyframe")
        img = ff.imread(out_dir / kf) if kf else None
        if img is None:
            continue
        th = int(tile_w * img.shape[0] / img.shape[1])
        img = cv2.resize(img, (tile_w, th), interpolation=cv2.INTER_AREA)
        label = f"#{s['index']} {s['start']:.1f}s {s['duration']:.1f}s {s.get('camera', '')}"
        cv2.rectangle(img, (0, 0), (tile_w, 18), (0, 0, 0), -1)
        cv2.putText(img, label, (4, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1,
                    cv2.LINE_AA)
        tiles.append(img)
    if not tiles:
        return None
    th = tiles[0].shape[0]
    rows = math.ceil(len(tiles) / cols)
    sheet = np.zeros((rows * th, cols * tile_w, 3), np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        t = cv2.resize(t, (tile_w, th))
        sheet[r * th:(r + 1) * th, c * tile_w:(c + 1) * tile_w] = t
    path = out_dir / "contact_sheet.jpg"
    ff.imwrite(path, sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


def run_analysis(video, out_dir=None, subs=None, whisper: str = None, step: int = 1,
                 max_seconds: float = None, report: bool = True, quiet: bool = False) -> Path:
    video = Path(video)
    out_dir = Path(out_dir) if out_dir else Path("analysis") / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))

    say(f"[1/4] 映像をフレーム単位で解析: {video.name}")
    info, fa, arrays, transitions, shots = analyze.analyze_frames(
        video, step=step, max_seconds=max_seconds, quiet=quiet)
    shot_dicts, meta = analyze.write_outputs(out_dir, info, fa, arrays, transitions, shots)
    say(f"      {len(shot_dicts)}ショット / {len(meta['transitions'])}遷移を検出")

    say("[2/4] 音声を解析")
    transcript = None
    subs = Path(subs) if subs else find_subtitles(video)
    if subs and subs.exists():
        transcript = audio.parse_subtitles(subs)
        say(f"      字幕を使用: {subs.name} ({len(transcript)}行)")
    elif whisper:
        say(f"      faster-whisper({whisper})で文字起こし中(CPUだと動画の長さの0.3〜1倍程度かかります)")
        transcript = audio.transcribe_whisper(video, whisper, max_seconds=max_seconds)
    if max_seconds and transcript:
        transcript = [s for s in transcript if s["start"] < max_seconds]
    if transcript:
        (out_dir / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    a_info = info
    if max_seconds:
        a_info = ff.VideoInfo(**{**info.to_dict(), "duration": min(info.duration, max_seconds)})
    aud = audio.analyze_audio(video, a_info, transcript) if not max_seconds else \
        _audio_trimmed(video, a_info, transcript, max_seconds)
    (out_dir / "audio.json").write_text(json.dumps(aud, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    say("[3/4] スタイルプロファイルを作成")
    p = prof_mod.build_profile(video.stem, meta, shot_dicts, arrays, aud, _load_vlm(out_dir))
    prof_mod.save_profile(p, out_dir / "profile.json")
    contact_sheet(shot_dicts, out_dir)

    if report:
        say("[4/4] レポートを作成")
        from . import report as report_mod
        path = report_mod.write_report(out_dir)
        say(f"      → {path}")
    return out_dir


def _audio_trimmed(video, info, transcript, max_seconds):
    """--max-seconds 指定時は先頭だけを一時ファイルに切り出して音声解析する。"""
    import tempfile

    if not info.has_audio:
        return audio.analyze_audio(video, info, transcript)   # {"has_audio": False}

    with tempfile.TemporaryDirectory() as td:
        clip = Path(td) / "head.mka"
        ff.run([ff.find_ffmpeg(), "-v", "error", "-y", "-i", str(video), "-t", f"{max_seconds}",
                "-vn", "-c:a", "copy", str(clip)])
        info2 = ff.VideoInfo(**{**info.to_dict(), "path": str(clip)})
        return audio.analyze_audio(clip, info2, transcript)


def _load_vlm(out_dir: Path):
    f = Path(out_dir) / "vlm.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def load_frames_csv(out_dir: Path) -> dict:
    """frames.csv を数値配列の辞書に読み戻す(空欄はNaN)。"""
    import csv

    with open(Path(out_dir) / "frames.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cols = rows[0].keys() if rows else []
    return {c: np.array([float(r[c]) if r[c] != "" else np.nan for r in rows]) for c in cols}


def rebuild_profile(out_dir) -> Path:
    """解析済みフォルダのファイルから profile.json を作り直す(Gemini分類を足した後など)。"""
    out_dir = Path(out_dir)
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    shots = json.loads((out_dir / "shots.json").read_text(encoding="utf-8"))
    aud_f = out_dir / "audio.json"
    aud = json.loads(aud_f.read_text(encoding="utf-8")) if aud_f.exists() else {"has_audio": False}
    name = Path(meta["video"]["path"]).stem
    p = prof_mod.build_profile(name, meta, shots, load_frames_csv(out_dir), aud, _load_vlm(out_dir))
    prof_mod.save_profile(p, out_dir / "profile.json")
    return out_dir / "profile.json"


PURGE_TARGETS = ("keyframes", "filmstrips", "contact_sheet.jpg", "transcript.json", "report.html",
                 "vlm.json", "gemini_watch.json", "gemini_watch.md")


def purge(out_dir, video=None) -> list:
    """参考動画の複製にあたるもの(キーフレーム・一覧画像・文字起こし・レポート)と
    元動画・字幕を削除し、数値データ(profile.json, frames/shots/audio)だけを残す。"""
    import glob
    import shutil

    out_dir = Path(out_dir)
    removed = []
    # 旧版で analysis/ 直下に保存したコマ送り画像も消す
    for p in out_dir.parent.glob(glob.escape(out_dir.name) + "_filmstrip_*"):
        p.unlink()
        removed.append(p)
    for name in PURGE_TARGETS:
        p = out_dir / name
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(p)
        elif p.exists():
            p.unlink()
            removed.append(p)
    aud_f = out_dir / "audio.json"
    if aud_f.exists():    # 発話区間の「文字」だけ消す(時刻は数値なので残す)
        aud = json.loads(aud_f.read_text(encoding="utf-8"))
        for sgm in aud.get("segments", []):
            sgm["text"] = ""
        aud_f.write_text(json.dumps(aud, ensure_ascii=False, indent=1), encoding="utf-8")
    if video:
        video = Path(video)
        siblings = [q for q in video.parent.iterdir() if q.name.startswith(video.stem + ".")] \
            if video.parent.exists() else []     # glob は [ ] を特殊文字と解釈するので使わない
        for p in [video, *siblings]:
            if p.exists() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".vtt",
                                                     ".srt", ".json", ".part", ".jpg", ".webp"):
                p.unlink()
                removed.append(p)
    return removed
