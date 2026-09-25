"""重ね物: 背景の上に載せるキャラクター・図の画像(overlay)と、強調テキスト(callout)。

VAIENCE型の動画では、体験役のキャラクターや矢印・図解が背景CGの上に重なり、
After Effectsで「ポン」と出る強調文字が入る。ここではその代わりを軽量に行う。

overlay(映像素材ごと):
  visuals:
    - type: space
      template: planet
      overlays:
        - {path: assets/chars/explorer.png, x: 0.72, y: 0.58, height: 0.55, anim: float, enter: slide_right}
callout(台詞ごと):
  lines:
    - {speaker: 教授, text: "その力は8倍じゃ。", callout: "潮を起こす力 8倍"}
"""

import math
from pathlib import Path

import cv2
import numpy as np

from .sources import FrameSource
from .telop import TextRenderer, blend

ANIMS = ("none", "float", "shake", "breathe")
ENTERS = ("none", "fade", "slide_left", "slide_right", "slide_up", "pop")


def load_rgba(path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"重ね画像を読めません: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA).astype(np.float32)


def _ease_out(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


class OverlayLayer:
    """画像1枚の重ね物。位置は画像の中心(画面比)、height は画面の高さ比。"""

    def __init__(self, spec: dict, w: int, h: int, fps: float, seed: int = 0):
        self.spec = spec
        self.w, self.h, self.fps = w, h, fps
        img = load_rgba(spec["path"])
        th = max(2, int(float(spec.get("height", 0.5)) * h))
        tw = max(2, int(img.shape[1] * th / img.shape[0]))
        self.img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        self.x, self.y = float(spec.get("x", 0.5)), float(spec.get("y", 0.55))
        self.anim = spec.get("anim", "float")
        self.enter = spec.get("enter", "fade")
        if self.anim not in ANIMS:
            raise ValueError(f"overlay.anim は {', '.join(ANIMS)} のどれか: {self.anim}")
        if self.enter not in ENTERS:
            raise ValueError(f"overlay.enter は {', '.join(ENTERS)} のどれか: {self.enter}")
        self.enter_s = float(spec.get("enter_duration", 0.45))
        self.opacity = float(spec.get("opacity", 1.0))
        self.flip = bool(spec.get("flip", False))
        if self.flip:
            self.img = self.img[:, ::-1].copy()
        self.rng = np.random.default_rng(seed)
        self._shake = self.rng.normal(0, 1, (4096, 2))

    def draw(self, frame: np.ndarray, i: int) -> None:
        t = i / self.fps
        u = _ease_out(t / self.enter_s) if self.enter != "none" else 1.0
        dx = dy = 0.0
        alpha = self.opacity
        scale = 1.0
        if self.enter == "fade":
            alpha *= u
        elif self.enter == "slide_left":
            dx = 0.35 * (1 - u)
        elif self.enter == "slide_right":
            dx = -0.35 * (1 - u)
        elif self.enter == "slide_up":
            dy = 0.3 * (1 - u)
        elif self.enter == "pop":
            scale *= 0.6 + 0.4 * u + 0.08 * math.sin(math.pi * u)
            alpha *= min(1.0, 3 * u)
        if self.anim == "float":
            dy += 0.012 * math.sin(2 * math.pi * t / 3.2)
        elif self.anim == "shake":
            j = self._shake[i % len(self._shake)]
            dx += 0.004 * j[0]
            dy += 0.004 * j[1]
        elif self.anim == "breathe":
            scale *= 1.0 + 0.015 * math.sin(2 * math.pi * t / 3.0)
        img = self.img
        if abs(scale - 1.0) > 1e-3:
            nh, nw = max(2, int(img.shape[0] * scale)), max(2, int(img.shape[1] * scale))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        cx, cy = (self.x + dx) * self.w, (self.y + dy) * self.h
        blend(frame, img, int(cx - img.shape[1] / 2), int(cy - img.shape[0] / 2), alpha)


class OverlaySource(FrameSource):
    """背景ソースの各フレームに重ね物を描き足すラッパー。"""

    def __init__(self, base: FrameSource, layers: list):
        self.base = base
        self.layers = layers
        self.n_frames = base.n_frames

    def frame(self, i):
        img = self.base.frame(i).copy()
        for layer in self.layers:
            layer.draw(img, i)
        return img

    def close(self):
        self.base.close()


DEFAULT_CALLOUT = {"size": 0.11, "color": "#ffe14d", "outline": "#1a1a1a", "outline_width": 0.12,
                   "y": 0.42, "max_chars": 16, "band": None, "speaker_colors": {}}


class CalloutRenderer:
    """強調テキストを「ポン」と拡大しながら出し、最後に薄く消す。"""

    def __init__(self, w: int, h: int, style: dict, font_path: str):
        self.tr = TextRenderer(w, h, {**DEFAULT_CALLOUT, **(style or {})}, font_path)

    def draw(self, frame: np.ndarray, text: str, t_in: float, dur: float) -> None:
        arr, x, y = self.tr.render(text)
        pop = _ease_out(t_in / 0.18)
        scale = 0.7 + 0.3 * pop + 0.06 * math.sin(math.pi * min(1.0, t_in / 0.18))
        alpha = min(1.0, t_in / 0.08, max(0.0, (dur - t_in) / 0.25))
        if abs(scale - 1.0) > 1e-3:
            nh, nw = max(2, int(arr.shape[0] * scale)), max(2, int(arr.shape[1] * scale))
            cx, cy = x + arr.shape[1] / 2, y + arr.shape[0] / 2
            arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_LINEAR)
            x, y = int(cx - nw / 2), int(cy - nh / 2)
        blend(frame, arr, x, y, alpha)


def wrap_with_overlays(src: FrameSource, visual: dict, w: int, h: int, fps: float,
                       seed: int = 0) -> FrameSource:
    specs = visual.get("overlays") or []
    if not specs:
        return src
    layers = [OverlayLayer(sp, w, h, fps, seed + k) for k, sp in enumerate(specs)]
    return OverlaySource(src, layers)


def validate_overlay(spec: dict, where: str) -> None:
    if not spec.get("path"):
        raise ValueError(f"{where}: overlay には path が必要です")
    if not Path(spec["path"]).exists():
        raise ValueError(f"{where}: 重ね画像がありません: {spec['path']}")
