#!/usr/bin/env python3
"""動画のテロップ・カット・演出パターンを全フレーム解析するスクリプト。

YouTube編集自動化プロジェクトの「解析」側。参考動画1本を最初から最後まで
1フレームずつ処理し、以下を抽出する:

  - カット割り(場面転換)の位置と頻度
  - テロップ帯(画面下部/上部)の出現・消滅イベントと表示時間
  - 各テロップ区間の代表フレーム画像(あとで目視・OCRで様式分類する)
  - ズーム/フラッシュ等の急激な画面変化の候補位置

使い方:
    python3 scripts/analyze_video.py <video.mp4> --out output/analysis_<名前>

出力:
    events.json        全イベント(カット/テロップ開始・終了)のタイムライン
    metrics.npz        全フレームの生メトリクス(再解析用)
    frames/            テロップ区間ごとの代表フレーム(フル画面 + 帯クロップ)
    montage_*.jpg      代表フレームを3x3タイルにした一覧画像(目視分類用)
    summary.txt        統計サマリ

GPU不要。720p・20分の動画でCPU数分〜十数分程度。
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

# 解析用の縮小幅(速度と精度のバランス。全フレームこのサイズで処理する)
PROC_W = 480

# テロップ帯の位置(画面に対する比率)。年収チャンネル等の一般的な配置を想定。
# 解析対象で位置が違う場合はここを調整する。
BANDS = {
    "bottom": (0.68, 0.99),   # メインテロップ(発言テロップ)帯
    "top": (0.00, 0.22),      # 上部帯(コーナー名・ツッコミ等)
}

CUT_THRESH = 28.0        # 場面転換とみなす全画面平均差分(0-255)
FLASH_THRESH = 60.0      # フラッシュ/ズーム演出候補の輝度ジャンプ
TELOP_DIFF_THRESH = 9.0  # テロップ帯の内容が変わったとみなす差分
TELOP_MIN_FRAMES = 8     # これ未満の帯変化はノイズとして無視(約0.27秒@30fps)
TEXT_EDGE_MIN = 0.012    # 帯に「文字がある」とみなすエッジ密度の下限


def band_slice(h, band):
    y0, y1 = BANDS[band]
    return slice(int(h * y0), int(h * y1))


def text_score(gray_band):
    """帯領域に縁取り文字らしき高コントラスト構造がどれだけあるか(0-1)。"""
    edges = cv2.Canny(gray_band, 80, 200)
    return float(np.count_nonzero(edges)) / edges.size


def analyze(path, out_dir):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        sys.exit(f"動画を開けません: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"入力: {path} {src_w}x{src_h} {fps:.2f}fps 約{n_frames}フレーム")

    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    prev_gray = None
    prev_bands = {}
    # 帯ごとの現在の状態: 表示中かどうか、開始フレーム、安定カウンタ
    band_state = {b: {"on": False, "start": 0, "pending": None, "count": 0}
                  for b in BANDS}
    events = []
    metrics = {"global_diff": [], "luma": []}
    for b in BANDS:
        metrics[f"{b}_diff"] = []
        metrics[f"{b}_text"] = []

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        h = int(frame.shape[0] * PROC_W / frame.shape[1])
        small = cv2.resize(frame, (PROC_W, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        g_diff = 0.0
        if prev_gray is not None:
            g_diff = float(np.mean(cv2.absdiff(gray, prev_gray)))
        metrics["global_diff"].append(g_diff)
        metrics["luma"].append(float(gray.mean()))

        if g_diff > CUT_THRESH:
            events.append({"type": "cut", "frame": idx, "t": idx / fps,
                           "strength": round(g_diff, 1)})

        for b in BANDS:
            sl = band_slice(h, b)
            band_gray = gray[sl]
            ts = text_score(band_gray)
            b_diff = 0.0
            if b in prev_bands:
                b_diff = float(np.mean(cv2.absdiff(band_gray, prev_bands[b])))
            prev_bands[b] = band_gray
            metrics[f"{b}_diff"].append(b_diff)
            metrics[f"{b}_text"].append(ts)

            st = band_state[b]
            has_text = ts > TEXT_EDGE_MIN
            changed = b_diff > TELOP_DIFF_THRESH
            # 状態遷移: 文字あり かつ (新規 or 内容変化) → 新テロップ候補
            if has_text and (not st["on"] or changed):
                if st["pending"] is None:
                    st["pending"] = idx
                    st["count"] = 0
                st["count"] += 1
                if st["count"] >= TELOP_MIN_FRAMES:
                    if st["on"]:
                        events.append({"type": f"telop_end", "band": b,
                                       "frame": st["pending"],
                                       "t": st["pending"] / fps})
                    events.append({"type": "telop_start", "band": b,
                                   "frame": st["pending"],
                                   "t": st["pending"] / fps})
                    st["on"] = True
                    st["start"] = st["pending"]
                    st["pending"] = None
            elif not has_text and st["on"]:
                if st["pending"] is None:
                    st["pending"] = idx
                    st["count"] = 0
                st["count"] += 1
                if st["count"] >= TELOP_MIN_FRAMES:
                    events.append({"type": "telop_end", "band": b,
                                   "frame": st["pending"],
                                   "t": st["pending"] / fps})
                    st["on"] = False
                    st["pending"] = None
            else:
                st["pending"] = None
                st["count"] = 0

        prev_gray = gray
        idx += 1
        if idx % 3000 == 0:
            print(f"  {idx}/{n_frames} フレーム処理済み ({idx/fps/60:.1f}分)")

    cap.release()
    total = idx

    # ---- テロップ区間を組み立てて代表フレームを保存 ----
    segments = build_segments(events, total, fps)
    save_representatives(path, segments, frames_dir, fps)
    montages = build_montages(frames_dir, out_dir)

    np.savez_compressed(os.path.join(out_dir, "metrics.npz"),
                        **{k: np.array(v, dtype=np.float32)
                           for k, v in metrics.items()})
    with open(os.path.join(out_dir, "events.json"), "w", encoding="utf-8") as f:
        json.dump({"fps": fps, "frames": total, "src": os.path.basename(path),
                   "events": events, "segments": segments}, f,
                  ensure_ascii=False, indent=1)

    write_summary(out_dir, events, segments, total, fps, montages)
    print(f"完了: {out_dir}")


def build_segments(events, total, fps):
    """telop_start/endイベント列を帯ごとの区間リストに変換する。"""
    segs = []
    open_seg = {}
    for e in events:
        if e["type"] == "telop_start":
            open_seg[e["band"]] = e["frame"]
        elif e["type"] == "telop_end" and e.get("band") in open_seg:
            s = open_seg.pop(e["band"])
            segs.append({"band": e["band"], "start": s, "end": e["frame"],
                         "t0": round(s / fps, 2), "t1": round(e["frame"] / fps, 2),
                         "dur": round((e["frame"] - s) / fps, 2)})
    for b, s in open_seg.items():
        segs.append({"band": b, "start": s, "end": total,
                     "t0": round(s / fps, 2), "t1": round(total / fps, 2),
                     "dur": round((total - s) / fps, 2)})
    segs.sort(key=lambda x: x["start"])
    return segs


def save_representatives(path, segments, frames_dir, fps):
    """各テロップ区間の中間フレームをフル解像度で保存する。"""
    cap = cv2.VideoCapture(path)
    for i, seg in enumerate(segments):
        mid = (seg["start"] + seg["end"]) // 2
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ok, frame = cap.read()
        if not ok:
            continue
        name = f"seg{i:04d}_{seg['band']}_{seg['t0']:.0f}s"
        cv2.imwrite(os.path.join(frames_dir, name + ".jpg"), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
        seg["image"] = name + ".jpg"
    cap.release()


def build_montages(frames_dir, out_dir, tile=3, cell_w=640):
    """代表フレームを3x3のタイル画像にまとめる(目視分類を効率化)。"""
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
    montages = []
    for m_idx in range(0, len(files), tile * tile):
        chunk = files[m_idx:m_idx + tile * tile]
        cells = []
        for f in chunk:
            img = cv2.imread(os.path.join(frames_dir, f))
            ch = int(img.shape[0] * cell_w / img.shape[1])
            img = cv2.resize(img, (cell_w, ch))
            cv2.putText(img, f, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 255), 2)
            cells.append(img)
        while len(cells) < tile * tile:
            cells.append(np.zeros_like(cells[0]))
        rows = [np.hstack(cells[r * tile:(r + 1) * tile]) for r in range(tile)]
        out = np.vstack(rows)
        name = f"montage_{m_idx // (tile*tile):03d}.jpg"
        cv2.imwrite(os.path.join(out_dir, name), out,
                    [cv2.IMWRITE_JPEG_QUALITY, 82])
        montages.append(name)
    return montages


def write_summary(out_dir, events, segments, total, fps, montages):
    cuts = [e for e in events if e["type"] == "cut"]
    dur_min = total / fps / 60
    lines = [
        f"総フレーム数: {total} ({dur_min:.1f}分, {fps:.2f}fps)",
        f"カット数: {len(cuts)} (平均 {dur_min*60/max(len(cuts),1):.1f}秒に1回)",
        f"テロップ区間数: {len(segments)}",
    ]
    for b in BANDS:
        bs = [s for s in segments if s["band"] == b]
        if bs:
            durs = [s["dur"] for s in bs]
            on_time = sum(durs)
            lines.append(
                f"  {b}帯: {len(bs)}区間 / 表示中 {on_time/60:.1f}分 "
                f"({on_time/(total/fps)*100:.0f}%) / "
                f"平均{np.mean(durs):.1f}秒 中央値{np.median(durs):.1f}秒")
    lines.append(f"モンタージュ画像: {len(montages)}枚")
    text = "\n".join(lines)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(
        "output", "analysis_" +
        os.path.splitext(os.path.basename(args.video))[0])
    analyze(args.video, out)
