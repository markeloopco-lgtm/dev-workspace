"""指定区間の連続フレームを1枚の一覧画像(フィルムストリップ)にする。目視のコマ送り研究用。

  各コマに フレーム番号・時刻・前コマとの変化量 を書き込む。
  --diff を付けると前コマとの差分(動いた場所)を赤で重ねる。アニメーションの
  タメ・ツメ(加減速)や、テロップが何フレームで出るかを数えるのに使う。
"""

import math
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg_util as ff


def parse_time(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    parts = [float(p) for p in str(s).split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def filmstrip(video, start, end, out_path=None, every: int = 1, cols: int = 6,
              tile_w: int = 320, diff: bool = False, max_tiles: int = 120) -> list:
    video = Path(video)
    info = ff.probe(video)
    t0, t1 = parse_time(start), parse_time(end)
    if t1 <= t0:
        raise ValueError("end は start より後にしてください")
    th = ff.even(tile_w * info.height / info.width)
    tiles, prev = [], None
    first_frame = int(round(t0 * info.fps))
    for k, fr in enumerate(ff.iter_frames(video, tile_w, th, start=t0, duration=t1 - t0)):
        if k % every:
            prev = fr
            continue
        img = fr.copy()
        change = 0.0
        if prev is not None:
            d = np.abs(img.astype(np.int16) - prev.astype(np.int16)).max(axis=2)
            change = float(d.mean() / 255)
            if diff:
                mask = d > 24
                img[mask] = (0.4 * img[mask] + 0.6 * np.array([255, 40, 40])).astype(np.uint8)
        prev = fr
        fidx = first_frame + k
        label = f"f{fidx}  {fidx / info.fps:7.2f}s  d={change:.3f}"
        cv2.rectangle(img, (0, 0), (tile_w, 16), (0, 0, 0), -1)
        cv2.putText(img, label, (3, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1,
                    cv2.LINE_AA)
        tiles.append(img)
        if len(tiles) >= max_tiles:
            break
    if not tiles:
        raise ValueError("指定区間からフレームを取り出せませんでした")
    # 解析フォルダの中に置く(vlab purge で一緒に消えるように)
    out_path = Path(out_path or Path("analysis") / video.stem / "filmstrips" / f"{t0:.1f}-{t1:.1f}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    per_sheet = cols * 8
    outs = []
    for s in range(0, len(tiles), per_sheet):
        chunk = tiles[s:s + per_sheet]
        rows = math.ceil(len(chunk) / cols)
        sheet = np.zeros((rows * th, cols * tile_w, 3), np.uint8)
        for i, t in enumerate(chunk):
            r, c = divmod(i, cols)
            sheet[r * th:(r + 1) * th, c * tile_w:(c + 1) * tile_w] = t
        p = out_path if s == 0 else out_path.with_name(f"{out_path.stem}_{s // per_sheet + 1}{out_path.suffix}")
        ok, buf = cv2.imencode(p.suffix or ".png", cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
        buf.tofile(str(p))
        outs.append(p)
    return outs
