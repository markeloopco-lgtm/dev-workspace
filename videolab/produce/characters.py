"""立ち絵(キャラクター画像)の表示: 口パク・まばたき・表情の切り替え・話し始めの小さな跳ね。

ずんだもん・ゆっくり系の解説/物語動画のように、話者の立ち絵を画面の下に置き、声に合わせて
口を開閉する。画像は各キャラクターの利用ガイドラインに沿って配布されている立ち絵素材を
assets/chars/<名前>/ に置いて使う(このツールはキャラクターの絵を作らない)。
素材が届くまでは scripts/make_placeholder_character.py の仮の立ち絵(単純な図形)で流れを確かめられる。

台本(書式は episodes/README.md):
  characters:
    ずんだもん: {dir: assets/chars/zundamon, x: 0.84, height: 0.72, bottom: 1.03}
  scenes:
    - cast: [ずんだもん]                                   # この場面に出すキャラ(省略時はその場面で話すキャラ)
      lines:
        - {speaker: ずんだもん, text: "……", face: happy}   # 表情を切り替える(次に変えるまで続く)

dir の中のファイル名の決まり(表情ごと):
  <表情>.png        口を閉じた絵(必須)。例 normal.png / happy.png / sad.png
  <表情>_open.png   口を開けた絵(任意。無ければ口パクしない)
  <表情>_half.png   口が半開きの絵(任意)
  <表情>_blink.png  目を閉じた絵(任意。無ければまばたきしない)
"""

import math
from bisect import bisect_right
from pathlib import Path

import cv2
import numpy as np

from .overlay import load_rgba
from .telop import blend

VARIANTS = ("open", "half", "blink")
DEFAULT_LAYOUT = {
    "x": 0.84,              # 立ち絵の中心の横位置(画面比)
    "height": 0.72,         # 立ち絵の高さ(画面の高さ比)
    "bottom": 1.03,         # 立ち絵の下端の位置(画面比。1より大きいと少し下にはみ出す)
    "flip": False,          # 左右反転
    "hop": 0.012,           # 話し始めの小さな跳ね(画面の高さ比。0で無し)
    "breathe": 0.003,       # 待機中のゆっくりした上下(画面の高さ比。0で無し)
    "silent_dim": 0.0,      # 話していない時に暗くする度合い(0〜1)
    "blink_every": [2.5, 5.5],   # まばたきの間隔(秒の範囲)
}
HOP_SEC = 0.22
BLINK_SEC = 0.12
# 表情の日本語の別名(台本に「ずんだもん(笑顔): …」と書ける)。ファイル名そのものの表情が優先
FACE_ALIASES = {
    "normal": ("普通", "ふつう", "通常", "いつもの"),
    "happy": ("笑顔", "えがお", "嬉しい", "うれしい", "喜び", "にこにこ"),
    "sad": ("悲しい", "かなしい", "悲しみ", "泣き", "しょんぼり"),
    "surprised": ("驚き", "おどろき", "びっくり"),
    "angry": ("怒り", "いかり", "怒る", "ぷんぷん"),
}


def resolve_face(face, faces) -> str | None:
    """台本に書いた表情の名前を、立ち絵フォルダにある表情の名前にする(無ければ None)。"""
    face = str(face).strip()
    if face in faces:
        return face
    for name, aliases in FACE_ALIASES.items():
        if (face in aliases or face.lower() == name) and name in faces:
            return name
    return None


def resize_rgba(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """透明な部分の色が縁ににじまないよう、色に透明度を掛けてから縮小し、元に戻す。"""
    a = img[..., 3:4] / 255.0
    pm = cv2.resize(np.concatenate([img[..., :3] * a, img[..., 3:4]], axis=2), (w, h),
                    interpolation=cv2.INTER_AREA)
    a2 = pm[..., 3:4] / 255.0
    rgb = np.where(a2 > 1e-4, pm[..., :3] / np.maximum(a2, 1e-4), 0.0)
    return np.concatenate([np.clip(rgb, 0, 255), pm[..., 3:4]], axis=2).astype(np.float32)


def scan_face_dir(d: Path) -> dict:
    """フォルダの <表情>.png / <表情>_open.png などを表情ごとにまとめる(口を閉じた絵が無い表情は除く)。"""
    faces = {}
    for p in sorted(Path(d).glob("*.png")):
        stem = p.stem
        for v in VARIANTS:
            if stem.endswith("_" + v):
                faces.setdefault(stem[: -len(v) - 1], {})[v] = str(p)
                break
        else:
            faces.setdefault(stem, {})["base"] = str(p)
    return {k: v for k, v in faces.items() if "base" in v}


def mouth_levels(y: np.ndarray, sr: int, fps: float) -> np.ndarray:
    """台詞の音声から、フレームごとの口の開き(0=閉じ・1=半開き・2=開き)を決める。

    台詞の中の静かな所と大きい所の差で正規化するので、声の大きさや音声エンジンが違っても
    音節ごとに開閉する。1コマだけの開閉は目立つので前後とならす。
    """
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    hop = sr / fps
    n = max(1, int(math.ceil(len(y) / hop)))
    edges = np.minimum(len(y), (np.arange(n + 1) * hop).astype(np.int64))
    sq = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    cnt = np.maximum(1, edges[1:] - edges[:-1])
    rms = np.sqrt((sq[edges[1:]] - sq[edges[:-1]]) / cnt)
    if rms.max() <= 0:
        return np.zeros(n, np.uint8)
    hi, lo = np.percentile(rms, 95), np.percentile(rms, 10)
    v = rms / hi if hi - lo < 0.05 * hi else (rms - lo) / (hi - lo)
    lv = np.where(v >= 0.55, 2, np.where(v >= 0.25, 1, 0)).astype(np.uint8)
    lv[20 * np.log10(rms / rms.max() + 1e-9) < -35] = 0     # 声が出ていない所は閉じる
    if n >= 3:
        lv[1:-1] = np.median(np.stack([lv[:-2], lv[1:-1], lv[2:]]), axis=0).astype(np.uint8)
    return lv


class CharacterStage:
    """台本の characters を、タイムライン(plan)に合わせて毎フレーム描く。"""

    def __init__(self, plan, w: int, h: int, fps: float, seed: int = 0):
        self.w, self.h, self.fps = w, h, fps
        self.specs = plan.characters
        self.n = n = plan.total_frames
        self._cache = {}
        self.visible = {nm: np.zeros(n, bool) for nm in self.specs}
        for c in plan.cast:
            a, b = int(round(c["start"] * fps)), int(round(c["end"] * fps))
            self.visible[c["name"]][max(0, a):min(n, b)] = True
        self.mouth = {nm: np.zeros(n, np.uint8) for nm in self.specs}
        self.talk = {nm: np.zeros(n, bool) for nm in self.specs}
        self.starts = {nm: [] for nm in self.specs}
        for ln in plan.lines:
            if ln.speaker not in self.specs:
                continue
            f0 = int(round(ln.start * fps))
            lv = mouth_levels(ln.audio, ln.sr, fps)
            f1 = min(n, f0 + len(lv))
            if f1 <= f0:
                continue
            self.mouth[ln.speaker][f0:f1] = lv[:f1 - f0]
            self.talk[ln.speaker][f0:f1] = True
            self.starts[ln.speaker].append(f0)
        self.face_t = {nm: [0] for nm in self.specs}
        self.face_v = {nm: [spec["default"]] for nm, spec in self.specs.items()}
        for e in sorted(plan.faces, key=lambda e: e["t"]):
            self.face_t[e["name"]].append(int(round(e["t"] * fps)))
            self.face_v[e["name"]].append(e["face"])
        rng = np.random.default_rng(seed)
        self.blink = {}
        bl = max(2, int(round(BLINK_SEC * fps)))
        for nm, spec in self.specs.items():
            b = np.zeros(n, bool)
            lo, hi = spec["blink_every"]
            t = rng.uniform(0.6, 1.8)
            while t * fps < n:
                a = int(t * fps)
                b[a:a + bl] = True
                t += rng.uniform(float(lo), float(hi))
            self.blink[nm] = b
        self.phase = {nm: rng.uniform(0, 2 * math.pi) for nm in self.specs}

    def face_at(self, name: str, f: int) -> str:
        return self.face_v[name][bisect_right(self.face_t[name], f) - 1]

    def variant_at(self, name: str, f: int) -> str:
        """そのフレームに使う絵: base(口閉じ) / half / open / blink。"""
        faces = self.specs[name]["faces"][self.face_at(name, f)]
        lv = int(self.mouth[name][f])
        if lv >= 2 and "open" in faces:
            return "open"
        if lv >= 1 and ("half" in faces or "open" in faces):
            return "half" if "half" in faces else "open"
        if lv == 0 and self.blink[name][f] and "blink" in faces:
            return "blink"     # 口が動いている間はまばたきより口パクを優先
        return "base"

    def _image(self, name: str, face: str, variant: str, dim: float) -> np.ndarray:
        key = (name, face, variant, dim)
        if key not in self._cache:
            spec = self.specs[name]
            img = load_rgba(spec["faces"][face].get(variant) or spec["faces"][face]["base"])
            th = max(2, int(float(spec["height"]) * self.h))
            tw = max(2, int(img.shape[1] * th / img.shape[0]))
            img = resize_rgba(img, tw, th)
            if spec.get("flip"):
                img = img[:, ::-1].copy()
            if dim > 0:
                img[..., :3] *= (1.0 - dim)
            self._cache[key] = img
        return self._cache[key]

    def draw(self, frame: np.ndarray, f: int, alpha: float = 1.0) -> None:
        """立ち絵を frame に重ねる。alpha は暗転(場面転換)に合わせた明るさ(0〜1)。"""
        if alpha <= 0 or f >= self.n:
            return
        for name, spec in self.specs.items():
            if not self.visible[name][f]:
                continue
            face = self.face_at(name, f)
            talking = bool(self.talk[name][f])
            dim = round(float(spec["silent_dim"]), 3) if not talking else 0.0
            img = self._image(name, face, self.variant_at(name, f), dim)
            dy = float(spec["breathe"]) * self.h * math.sin(2 * math.pi * f / (self.fps * 3.2) + self.phase[name])
            k = bisect_right(self.starts[name], f) - 1
            if k >= 0 and float(spec["hop"]) > 0:
                u = (f - self.starts[name][k]) / (HOP_SEC * self.fps)
                if 0 <= u < 1:
                    dy -= float(spec["hop"]) * self.h * math.sin(math.pi * u)
            x = int(round(float(spec["x"]) * self.w - img.shape[1] / 2))
            y = int(round(float(spec["bottom"]) * self.h - img.shape[0] + dy))
            if alpha < 1:
                # 暗転中は背景がもう暗くなっているので、立ち絵の色だけ暗くして重ねる
                # (透明度を下げると背景が透けて見えてしまう)
                img = img.copy()
                img[..., :3] *= alpha
            blend(frame, img, x, y)
