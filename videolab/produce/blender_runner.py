"""Blender(無料の3DCGソフト)で宇宙シーンを描画し、連番PNGを FrameSource として返す。

  find_blender()  … 環境変数 BLENDER → PATH の blender →
                    Windows: C:/Program Files/Blender Foundation/Blender */blender.exe(一番新しい版)
                    macOS  : /Applications/Blender.app
  render_space()  … spec.json を書いて
                      blender -b --factory-startup -P blender_space.py -- --spec spec.json --out <フォルダ>
                    を実行し、renders/cache/<指定内容のハッシュ>/frame_0001.png… を作って
                    ImageSequenceSource を返す。同じ指定なら2回目以降は描画せずに再利用する
                    (途中で止めても、次回は描き終わったフレームの続きから描く)

Blenderは 4.2 LTS 以降(5.x 含む)に対応。GPU(RTX 3050 4GB)ならEEVEEで1080p 1フレーム数秒程度。
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .sources import CameraMove, ImageSequenceSource
from .space import (BLENDER_BG_SHARE, CAMERA_VFOV_DEG, PRESETS, camera_track, compare_layout,
                    resolve_params, sun_vector)

SCRIPT = Path(__file__).with_name("blender_space.py")
FLIGHT_VFOV_DEG = 62.0          # space2d.WarpStars と同じ
DEFAULT_SAMPLES = {"eevee": 24, "cycles": 32}

INSTALL_HINT = (
    "Blenderが見つかりません(engine=blender/eevee/cycles の指定には必要です)。\n"
    "  1. https://www.blender.org/download/ から Blender 4.2 LTS 以降をインストール\n"
    "     (PowerShellなら  winget install --id BlenderFoundation.Blender -e )\n"
    "  2. 標準の場所(C:/Program Files/Blender Foundation/)に入れれば自動で見つかります。\n"
    "     別の場所なら環境変数 BLENDER に blender.exe のフルパスを設定してください。\n"
    "  Blenderを使わない場合は engine: 2d を指定すると、numpy版の描画で作れます。"
)


def _version_key(path: str):
    m = re.search(r"Blender\s*(\d+)\.(\d+)(?:\.(\d+))?", path, re.IGNORECASE)
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def find_blender():
    """Blender本体(またはそれを起動するランチャー)のパス。見つからなければ None。"""
    # エクスプローラーの「パスのコピー」で付く "" や前後の空白を取り除く
    env = (os.environ.get("BLENDER") or "").strip().strip('"').strip("'").strip()
    if env:
        p = Path(env)
        if p.is_dir():          # インストール先フォルダを指定された場合
            for name in ("blender.exe", "blender", "Contents/MacOS/Blender"):
                if (p / name).is_file():
                    return str(p / name)
        elif p.is_file():
            return str(p)
        w = shutil.which(env)
        if w:
            return w
        print(f"  [注意] 環境変数 BLENDER={env!r} は blender.exe として使えません"
              "(blender.exe までのフルパスを設定してください)。ほかの場所を探します",
              file=sys.stderr, flush=True)
    w = shutil.which("blender")
    if w:
        return w
    if os.name == "nt":
        cands = []
        for root in (os.environ.get("ProgramFiles", "C:/Program Files"), "C:/Program Files"):
            cands += glob.glob(str(Path(root) / "Blender Foundation" / "Blender *" / "blender.exe"))
        cands = sorted(set(cands), key=_version_key)
        if cands:
            return cands[-1]
    if sys.platform == "darwin":
        mac = "/Applications/Blender.app/Contents/MacOS/Blender"
        if Path(mac).exists():
            return mac
    return None


def build_spec(template: str, params: dict, n_frames: int, w: int, h: int, fps: float,
               cam: CameraMove, seed: int = 0, engine: str = "eevee", samples: int = None) -> dict:
    """blender_space.py に渡す指定(JSON化できる dict)。見た目に効く値はすべてここに入れる。"""
    p = dict(resolve_params(template, params))
    p.pop("_resolved", None)
    used = []
    if template == "planet":
        used = [p["preset"]]
    elif template == "planet_compare":
        used = list(p["presets"])
    presets = {k: PRESETS[k] for k in used}
    sun_view = (0.0, 0.0, 1.0)
    if template in ("planet", "planet_compare"):
        sun_view = sun_vector(p["sun_angle"], p["sun_elevation"], 0.0)
    spec = {
        "template": template, "params": p, "presets": presets,
        "w": int(w), "h": int(h), "fps": float(fps), "n_frames": int(n_frames), "seed": int(seed),
        "engine": engine, "samples": int(samples or DEFAULT_SAMPLES.get(engine, 24)),
        "vfov_deg": CAMERA_VFOV_DEG, "flight_vfov_deg": FLIGHT_VFOV_DEG,
        "bg_share": BLENDER_BG_SHARE, "sun_view": [round(v, 7) for v in sun_view],
        "camera_track": camera_track(cam, n_frames),
    }
    if template == "planet_compare":
        spec["layout"] = [list(x) for x in compare_layout(p, w, h)]
    return spec


def _file_sig(path) -> str:
    try:
        st = Path(path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return "missing"


def spec_hash(spec: dict) -> str:
    """指定内容 + 描画スクリプト本体 + 地表画像の更新日時 からキャッシュのキーを作る。"""
    h = hashlib.sha1()
    h.update(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    h.update(SCRIPT.read_bytes())
    p = spec["params"]
    for t in [p.get("texture")] + list(p.get("textures") or []):
        if t:
            h.update(_file_sig(t).encode())
    return h.hexdigest()[:16]


FRAME_GLOB = "frame_*.jpg"
_USED_KEYS = set()          # この実行で使ったキャッシュ(掃除の対象から外す)


def _frame_ok(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(-2, 2)
            return fh.read() == b"\xff\xd9"       # JPEGの終端マーカー = 書き出し完了
    except OSError:
        return False


def _is_done(folder: Path, n_frames: int) -> bool:
    try:
        d = json.loads((folder / "done.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    files = sorted(folder.glob(FRAME_GLOB))
    return (int(d.get("frames", -1)) == n_frames and len(files) >= n_frames
            and all(_frame_ok(f) for f in files))


def prune_cache(cache_dir: Path, max_gb: float = 30.0, max_age_days: float = 30.0,
                keep: set = None) -> int:
    """Blender描画キャッシュ(renders/cache/<16桁>/)のうち、古いもの・容量超過分を消す。

    音声のキャッシュ(renders/cache/tts)には触らない。今回使ったものは消さない。
    返り値: 削除したバイト数。
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return 0
    keep = set(keep if keep is not None else _USED_KEYS)
    items = []
    for d in cache_dir.iterdir():
        if not (d.is_dir() and re.fullmatch(r"[0-9a-f]{16}", d.name) and (d / "spec.json").exists()):
            continue
        stamp = d / "done.json" if (d / "done.json").exists() else d / "spec.json"
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        items.append((stamp.stat().st_mtime, size, d))
    items.sort()                           # 古い順
    total = sum(sz for _, sz, _ in items)
    now = time.time()
    freed = 0
    for mtime, size, d in items:
        if d.name in keep:
            continue
        if now - mtime > max_age_days * 86400 or total > max_gb * 1e9:
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            freed += size
    return freed


def run_blender(blender: str, spec_path: Path, out_dir: Path, n_frames: int, log_path: Path,
                quiet: bool = False, extra_args: list = None):
    """Blenderを実行する。進み具合を表示し、失敗したらログの末尾つきで例外を出す。"""
    cmd = [str(blender), "-b", "--factory-startup", "-P", str(SCRIPT), "--",
           "--spec", str(spec_path), "--out", str(out_dir)] + list(extra_args or [])
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    t0 = time.time()
    last_print = 0.0
    tail = []
    engine = None
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                log.write(line + "\n")
                tail = (tail + [line])[-40:]
                if line.startswith("VL_ENGINE"):
                    engine = line.split(maxsplit=1)[-1]
                    say(f"      Blender描画エンジン: {engine}")
                elif line.startswith("VL_PROGRESS"):
                    _, f, n, per = line.split()[:4]
                    now = time.time()
                    if now - last_print > 5 or int(f) == int(n):
                        rest = float(per) * (int(n) - int(f))
                        say(f"      Blender: {f}/{n} フレーム(1枚 {float(per):.1f}秒, 残り約{rest / 60:.1f}分)")
                        last_print = now
            proc.wait()
        except BaseException:
            proc.kill()
            raise
    if proc.returncode != 0 or not _is_done(out_dir, n_frames):
        msg = "\n".join(tail[-25:])
        raise RuntimeError(
            f"Blenderでの描画に失敗しました(終了コード {proc.returncode})。ログ: {log_path}\n"
            f"--- ログの末尾 ---\n{msg}\n"
            "Blenderの版が古い(4.2未満)場合は更新してください。急ぐ場合は engine: 2d で作れます。")
    say(f"      Blender描画 完了({time.time() - t0:.0f}秒)")
    return engine


def render_space(template: str, params: dict, n_frames: int, w: int, h: int, fps: float,
                 cam: CameraMove = None, seed: int = 0, cache_dir: Path = Path("renders/cache"),
                 engine: str = "eevee", blender: str = None, samples: int = None,
                 quiet: bool = False) -> ImageSequenceSource:
    """Blenderで描画(またはキャッシュを再利用)して ImageSequenceSource を返す。"""
    blender = blender or find_blender()
    if blender is None:
        raise FileNotFoundError(INSTALL_HINT)
    cam = cam or CameraMove()
    spec = build_spec(template, params, n_frames, w, h, fps, cam, seed, engine, samples)
    track = spec["camera_track"]
    if (template == "starfield" and spec["params"].get("speed", 0) <= 0
            and all(r == track[0] for r in track)):
        # 動かない星空は全フレーム同じ画 → 1枚だけ描いて使い回す(ショットの長さにも依存しない)
        spec["n_frames"], spec["camera_track"] = 1, track[:1]
    render_n = spec["n_frames"]
    key = spec_hash(spec)
    _USED_KEYS.add(key)
    folder = Path(cache_dir) / key
    if not _is_done(folder, render_n):
        folder.mkdir(parents=True, exist_ok=True)
        spec_path = folder / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        if not quiet:
            print(f"      Blenderで描画: {template} {w}x{h} {render_n}フレーム → {folder}", flush=True)
        run_blender(blender, spec_path, folder, render_n, folder / "blender.log", quiet)
    else:
        try:
            os.utime(folder / "done.json", None)     # 最近使った印(古いキャッシュの掃除用)
        except OSError:
            pass
        if not quiet:
            print(f"      Blender描画のキャッシュを使用: {folder}", flush=True)
    return ImageSequenceSource(folder, n_frames, w, h, pattern=FRAME_GLOB)
