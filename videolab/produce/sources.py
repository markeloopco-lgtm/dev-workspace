"""映像ソース(1ショット分のフレームを返す部品)とカメラワーク。

全ソース共通の約束(FrameSource):
  - n_frames 枚のフレームを frame(i) で返す。i は 0 <= i < n_frames
  - 返り値は出力解像度の RGB uint8 (H, W, 3)
  - 同じ i には同じ画を返す(決定的)。順番に呼ぶのが最速

カメラワークの向きは解析側(analyze.classify_camera)と同じ定義:
  zoom_in  = 画面の中身が大きくなる / pan_right = 中身が左へ流れる(カメラが右へ)
"""

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .. import ffmpeg_util as ff

EASINGS = {
    "linear": lambda u: u,
    "ease_in_out": lambda u: u * u * (3 - 2 * u),
    "ease_in": lambda u: u * u,
    "ease_out": lambda u: 1 - (1 - u) ** 2,
}

CAMERA_KINDS = ("static", "zoom_in", "zoom_out", "pan_left", "pan_right", "tilt_up",
                "tilt_down", "orbit")


@dataclass
class CameraMove:
    kind: str = "static"
    amount: float = 0.08      # zoom: 倍率変化(0.08 = 8%) / pan・tilt: 画面幅(高さ)に対する移動量
    easing: str = "linear"
    # 構図(ショット全体にかかる固定の寄り・ずらし)。同じ素材を続けて使う時に
    # 「引き → 寄り」のように画角を大きく変えて、別カットとして見せるために使う
    frame_scale: float = 1.0
    frame_x: float = 0.0
    frame_y: float = 0.0

    def __post_init__(self):
        if self.kind not in CAMERA_KINDS:
            raise ValueError(f"未知のカメラワーク: {self.kind} (使えるもの: {', '.join(CAMERA_KINDS)})")
        if self.easing not in EASINGS:
            raise ValueError(f"未知のイージング: {self.easing}")

    def at(self, u: float):
        """進行度u(0〜1)での (拡大率, 中身のx移動, 中身のy移動)。移動は画面比。"""
        s, x, y = self._move(u)
        return s * self.frame_scale, x + self.frame_x, y + self.frame_y

    def _move(self, u: float):
        e = EASINGS[self.easing](min(1.0, max(0.0, u)))
        a = self.amount
        if self.kind == "zoom_in":
            return 1.0 + a * e, 0.0, 0.0
        if self.kind == "zoom_out":
            return (1.0 + a) - a * e, 0.0, 0.0
        if self.kind == "pan_right":
            return 1.0, a / 2 - a * e, 0.0
        if self.kind == "pan_left":
            return 1.0, -a / 2 + a * e, 0.0
        if self.kind == "tilt_down":
            return 1.0, 0.0, a / 2 - a * e
        if self.kind == "tilt_up":
            return 1.0, 0.0, -a / 2 + a * e
        return 1.0, 0.0, 0.0     # static / orbit(2D画像では静止扱い)

    def max_scale_needed(self) -> float:
        """画面端が見切れないために必要な原画の余白(倍率)。"""
        s0, x0, y0 = self.at(0.0)
        s1, x1, y1 = self.at(1.0)
        pan = max(abs(x0), abs(x1), abs(y0), abs(y1))
        return (1.0 + 2 * pan) / min(s0, s1)


class FrameSource:
    n_frames: int = 0

    def frame(self, i: int) -> np.ndarray:
        raise NotImplementedError

    def close(self):
        pass


def cover_fit(img: np.ndarray, w: int, h: int, margin: float = 1.0) -> np.ndarray:
    """アスペクト比を保って (w*margin, h*margin) を覆うように拡大縮小し中央を切り出す。"""
    tw, th = int(math.ceil(w * margin)), int(math.ceil(h * margin))
    ih, iw = img.shape[:2]
    s = max(tw / iw, th / ih)
    nw, nh = max(tw, int(round(iw * s))), max(th, int(round(ih * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
    r = cv2.resize(img, (nw, nh), interpolation=interp)
    x0, y0 = (nw - tw) // 2, (nh - th) // 2
    return r[y0:y0 + th, x0:x0 + tw]


def apply_camera(base: np.ndarray, cam: CameraMove, u: float, w: int, h: int) -> np.ndarray:
    """base(出力より大きい原画)にカメラワークを適用して (h, w) を切り出す。サブピクセル補間。"""
    bh, bw = base.shape[:2]
    scale, ox, oy = cam.at(u)
    # baseは既に (w*margin) px ある。拡大率1で「原画1px = 画面1px」とし、はみ出した余白(margin)で
    # パン・構図ずらしを吸収する(ここで margin で割ると余白が消え、端に鏡像の帯が出る)
    s = scale
    tx = w / 2 - s * bw / 2 + ox * w
    ty = h / 2 - s * bh / 2 + oy * h
    m = np.array([[s, 0, tx], [0, s, ty]], np.float32)
    return cv2.warpAffine(base, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def to_uint8(img: np.ndarray) -> np.ndarray:
    """16bit・浮動小数の画像を8bitにそろえる(書き出しは rgb24 なので8bit以外は壊れる)。"""
    if img.dtype == np.uint8:
        return img
    if img.dtype == np.uint16:
        return (img >> 8).astype(np.uint8)
    if img.dtype.kind == "f":
        return (np.clip(img, 0.0, 1.0) * 255 + 0.5).astype(np.uint8)
    return np.clip(img, 0, 255).astype(np.uint8)


def load_image_rgb(path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)   # 日本語パス対策(cv2.imreadはWindowsで非ASCII不可)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"画像を読めません: {path}")
    if img.ndim == 3 and img.shape[2] == 4:
        img = to_uint8(img)            # 透過PNG: 黒背景に合成
        a = img[..., 3:4].astype(np.float32) / 255
        img = (img[..., :3].astype(np.float32) * a).astype(np.uint8)
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    # 透過なし: IMREAD_COLOR で読み直す(スマホ写真のEXIF回転を反映し、16bitも8bitにする)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return cv2.cvtColor(to_uint8(img), cv2.COLOR_BGR2RGB)


class ImageSource(FrameSource):
    """静止画にケン・バーンズ(ゆっくりズーム/パン)をかける。"""

    def __init__(self, path, n_frames: int, w: int, h: int, cam: CameraMove = None):
        self.n_frames, self.w, self.h = n_frames, w, h
        self.cam = cam or CameraMove()
        margin = max(1.0, self.cam.max_scale_needed()) * 1.02
        self.base = cover_fit(load_image_rgb(path), w, h, margin)

    def frame(self, i):
        u = i / max(1, self.n_frames - 1)
        return apply_camera(self.base, self.cam, u, self.w, self.h)


class ArraySource(FrameSource):
    """numpy画像1枚(プログラムで描いた画)にカメラワークをかける。"""

    def __init__(self, img: np.ndarray, n_frames: int, w: int, h: int, cam: CameraMove = None):
        self.n_frames, self.w, self.h = n_frames, w, h
        self.cam = cam or CameraMove()
        margin = max(1.0, self.cam.max_scale_needed()) * 1.02
        self.base = cover_fit(img, w, h, margin)

    def frame(self, i):
        u = i / max(1, self.n_frames - 1)
        return apply_camera(self.base, self.cam, u, self.w, self.h)


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


class ColorSource(FrameSource):
    """単色または上下グラデーション(タイトルカード・説明用の下地)。"""

    def __init__(self, n_frames: int, w: int, h: int, color="#101018", color2=None,
                 label: str = None):
        self.n_frames = n_frames
        top = np.array(_hex(color), np.float32)
        bot = np.array(_hex(color2 or color), np.float32)
        g = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
        self.img = np.broadcast_to((top * (1 - g) + bot * g).astype(np.uint8), (h, w, 3)).copy()
        if label:     # 下書きの「素材TODO」仮カード
            from .telop import DEFAULT_TELOP, TextRenderer, blend, find_font
            tr = TextRenderer(w, h, {**DEFAULT_TELOP, "size": 0.06, "y": 0.4,
                                     "color": "#ffd24d"}, find_font())
            arr, x, y = tr.render(label)
            blend(self.img, arr, x, y)

    def frame(self, i):
        return self.img


class VideoSource(FrameSource):
    """動画素材(自前の実写・AI生成動画・フリー素材)。短ければループ、長ければ先頭から使う。

    メモリを食わないよう全フレームは保持せず、ffmpegから順に読む(末尾まで来たら読み直す)。
    """

    def __init__(self, path, n_frames: int, w: int, h: int, start: float = 0.0,
                 cam: CameraMove = None, fps: float = 30.0):
        self.n_frames, self.w, self.h = n_frames, w, h
        self.path, self.start, self.fps = str(path), start, fps
        self.cam = cam or CameraMove()
        margin = max(1.0, self.cam.max_scale_needed()) * 1.02
        self.src_w, self.src_h = ff.even(w * margin), ff.even(h * margin)
        self._proc = None
        self._served = -1
        self._last = None

    def _open(self):
        self.close()
        vf = (f"fps={self.fps},scale={self.src_w}:{self.src_h}:force_original_aspect_ratio=increase,"
              f"crop={self.src_w}:{self.src_h}")
        cmd = [ff.find_ffmpeg(), "-v", "error", "-nostdin"]
        if self.start:
            cmd += ["-ss", f"{self.start:.3f}"]
        cmd += ["-i", self.path, "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _read(self):
        nbytes = self.src_w * self.src_h * 3
        buf = self._proc.stdout.read(nbytes) if self._proc else b""
        if len(buf) < nbytes:
            return None
        return np.frombuffer(buf, np.uint8).reshape(self.src_h, self.src_w, 3)

    def frame(self, i):
        if self._proc is None or i < self._served:
            self._open()
            self._served = -1
        while self._served < i:
            img = self._read()
            if img is None:                      # 素材の末尾 → 先頭からループ
                if self._served < 0:
                    raise ValueError(f"動画をデコードできません: {self.path}")
                self._open()
                img = self._read()
                if img is None:
                    raise ValueError(f"動画をデコードできません: {self.path}")
            self._last = img
            self._served += 1
        u = i / max(1, self.n_frames - 1)
        return apply_camera(self._last, self.cam, u, self.w, self.h)

    def close(self):
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.stdout.close()
                self._proc.wait()
            except Exception:
                pass
            self._proc = None


class ImageSequenceSource(FrameSource):
    """連番画像フォルダ(Blenderの書き出し等)。枚数が足りなければ最後の画で止める。"""

    def __init__(self, folder, n_frames: int, w: int, h: int, pattern: str = "*.png"):
        # 番号は数値として並べる(文字列順だと frame_10000 が frame_1000 の次に来てしまう)
        key = lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]  # noqa: E731
        self.files = sorted(Path(folder).glob(pattern), key=key)
        if not self.files:
            raise ValueError(f"連番画像がありません: {folder}")
        self.n_frames, self.w, self.h = n_frames, w, h
        self._one = None

    def _load(self, f):
        img = cv2.imdecode(np.fromfile(str(f), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if img is not None and img.ndim == 3 and img.shape[2] == 3 and img.dtype == np.uint8:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)     # Blender出力(8bit RGB)は1回の読み込みで済ませる
        else:
            img = load_image_rgb(f)                         # 透過・16bit・グレーは通常の読み込み
        if img.shape[1] != self.w or img.shape[0] != self.h:
            img = cover_fit(img, self.w, self.h)
        return img

    def frame(self, i):
        if len(self.files) == 1:            # 静止画1枚(動かない星空など)は読み込みを1回に
            if self._one is None:
                self._one = self._load(self.files[0])
            return self._one
        return self._load(self.files[min(i, len(self.files) - 1)])
