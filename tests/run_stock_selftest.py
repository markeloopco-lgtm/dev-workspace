#!/usr/bin/env python3
"""videolab/stock.py(フリー動画素材の取得)の検証。ネット不要・APIキー不要。

素材サイトの応答は実際のAPIと同じ形の見本(下の PEXELS / PIXABAY)に差し替え、
ダウンロードはffmpegで作る合成動画に置き換えて、次を確認する:
  - 応答の読み取りと素材の選び方(横長・長さ・1080p優先、空のURLは使わない)
  - 台本の {type: stock} の集計(表記ゆれは同じ検索語、使用回数ぶん取得)
  - 取得済みの再利用(2回目はダウンロードしない)・別の検索語で同じ素材を使わない
  - 読み込み時の差し替え(同じ検索語の2回目は2本目の素材)・クレジット・未取得時のエラーと仮カード
  - 台本 → 動画の制作(音声エンジン不要のdummy声・半分の解像度)

usage: python tests/run_stock_selftest.py
"""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from videolab import stock  # noqa: E402
from videolab.produce.episode import EpisodeError, load_episode  # noqa: E402

PEXELS = {"page": 1, "per_page": 3, "videos": [
    {"id": 101, "width": 3840, "height": 2160, "duration": 12, "url": "https://www.pexels.com/video/101/",
     "user": {"name": "Taro", "url": "https://www.pexels.com/@taro"},
     "video_files": [
         {"id": 1, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "fps": 29.97,
          "link": "https://videos.example/101_360.mp4"},
         {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "fps": 29.97,
          "link": "https://videos.example/101_1080.mp4"},
         {"id": 3, "quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "fps": 29.97,
          "link": "https://videos.example/101_2160.mp4"}]},
    {"id": 102, "width": 1080, "height": 1920, "duration": 8, "url": "https://www.pexels.com/video/102/",
     "user": {"name": "Ken", "url": "https://www.pexels.com/@ken"},
     "video_files": [{"id": 4, "quality": "hd", "file_type": "video/mp4", "width": 1080, "height": 1920,
                      "fps": 30, "link": "https://videos.example/102.mp4"}]},
    {"id": 103, "width": 1280, "height": 720, "duration": 3, "url": "https://www.pexels.com/video/103/",
     "user": {"name": "Mei", "url": "https://www.pexels.com/@mei"},
     "video_files": [{"id": 5, "quality": "hd", "file_type": "video/mp4", "width": 1280, "height": 720,
                      "fps": 25, "link": "https://videos.example/103.mp4"}]},
]}
PIXABAY = {"total": 2, "totalHits": 2, "hits": [
    {"id": 201, "pageURL": "https://pixabay.com/videos/201/", "type": "film", "tags": "forest, trees",
     "duration": 15, "user_id": 55, "user": "Hanako",
     "videos": {"large": {"url": "https://cdn.example/201_l.mp4", "width": 1920, "height": 1080},
                "medium": {"url": "https://cdn.example/201_m.mp4", "width": 1280, "height": 720},
                "small": {"url": "https://cdn.example/201_s.mp4", "width": 960, "height": 540},
                "tiny": {"url": "https://cdn.example/201_t.mp4", "width": 640, "height": 360}}},
    {"id": 202, "pageURL": "https://pixabay.com/videos/202/", "type": "film", "tags": "sea",
     "duration": 7, "user_id": 66, "user": "Jiro",
     "videos": {"large": {"url": "", "width": 0, "height": 0},
                "medium": {"url": "https://cdn.example/202_m.mp4", "width": 1280, "height": 720}}},
]}

EPISODE = """
title: 素材テスト
voices:
  ナレーター: {engine: dummy}
scenes:
  - id: a
    lines:
      - {speaker: ナレーター, text: "海の映像から始めます。"}
    visuals:
      - {type: stock, query: "ocean waves", camera: static}
  - id: b
    lines:
      - {speaker: ナレーター, text: "次は森です。"}
      - {speaker: ナレーター, text: "もう一度、海を映します。"}
    visuals:
      - {type: stock, query: "forest", camera: static}
      - {type: stock, query: "Ocean  Waves", camera: static}
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="stock_selftest_"))
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)
        return cond

    # 1) 応答の読み取りと素材の選び方
    px = stock.parse_pexels(PEXELS)
    pb = stock.parse_pixabay(PIXABAY)
    check([(c.id, c.width, c.height) for c in px] == [("101", 1920, 1080), ("102", 1080, 1920), ("103", 1280, 720)],
          f"Pexelsの解像度選択 {[(c.id, c.width, c.height) for c in px]}")
    check(px[0].author == "Taro" and px[0].url.endswith("101_1080.mp4"), f"Pexelsの撮影者/URL {px[0]}")
    check([(c.id, c.height) for c in pb] == [("201", 1080), ("202", 720)], f"Pixabayの解像度選択 {pb}")
    check(pb[0].author_url == "https://pixabay.com/users/Hanako-55/", f"Pixabayの撮影者URL {pb[0].author_url}")
    check([c.id for c in stock.rank(px, min_duration=5)] == ["101", "103", "102"],
          f"並び順(横長>長さ) {[c.id for c in stock.rank(px, min_duration=5)]}")

    # 2) 通信を見本に差し替える(キーの渡し方・検索語も記録して確認)
    calls, downloads = [], []

    def fake_get_json(url, params, headers=None, timeout=30):
        calls.append((url, dict(params), dict(headers or {})))
        return PEXELS if "pexels" in url else PIXABAY

    def fake_download(url, dest, timeout=120):
        downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        hue = (len(downloads) * 67) % 360
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc2=s=640x360:r=30:d=6,hue=h={hue}", "-c:v", "libx264",
                        "-preset", "veryfast", "-pix_fmt", "yuv420p", str(dest)], check=True)

    stock._get_json, stock._download = fake_get_json, fake_download
    stock.STOCK_DIR = tmp / "assets" / "video" / "stock"
    os.environ["PEXELS_API_KEY"], os.environ["PIXABAY_API_KEY"] = "test-pexels", "test-pixabay"

    ep_path = tmp / "stock_ep.yaml"
    ep_path.write_text(EPISODE, encoding="utf-8")

    # 3) 未取得: 通常はエラー(取得コマンドを案内)、下書きは仮カード
    try:
        load_episode(ep_path)
        check(False, "未取得の stock 素材でエラーにならない")
    except EpisodeError as e:
        check("vlab.py stock" in str(e), f"エラーに取得方法の案内がない: {e}")
    draft = load_episode(ep_path, allow_missing=True)
    check(len(draft["_missing"]) == 3 and all(m.startswith("stock: ") for m in draft["_missing"]),
          f"下書きの仮カード {draft['_missing']}")

    # 4) 台本の集計と取得
    import yaml
    reqs = stock.stock_requests(yaml.safe_load(EPISODE))
    check({k: v["uses"] for k, v in reqs.items()} == {"ocean waves": 2, "forest": 1}, f"検索語の集計 {reqs}")
    got = stock.fetch_for_episode(ep_path, log=lambda *a: None)
    check(got == {"ocean waves": 2, "forest": 1}, f"取得本数 {got}")
    px_call = next((c for c in calls if "pexels" in c[0]), None)
    pb_call = next((c for c in calls if "pixabay" in c[0]), None)
    check(px_call and px_call[2].get("Authorization") == "test-pexels" and px_call[1].get("locale") == "ja-JP",
          f"Pexelsへの渡し方 {px_call}")
    check(pb_call and pb_call[1].get("key") == "test-pixabay" and pb_call[1].get("lang") == "ja",
          f"Pixabayへの渡し方 {pb_call}")
    idx = stock.load_index()
    ocean = [e["id"] for e in idx["queries"]["ocean waves"]]
    forest = [e["id"] for e in idx["queries"]["forest"]]
    check(len(set(ocean)) == 2 and not set(ocean) & set(forest), f"素材の重複 ocean={ocean} forest={forest}")
    check("102" not in ocean + forest, "縦長の素材が横長より先に選ばれた")
    n_dl = len(downloads)
    stock.fetch_for_episode(ep_path, log=lambda *a: None)
    check(len(downloads) == n_dl, "取得済みの素材を再ダウンロードした")
    # 気に入らない素材はファイルを消して取り直す → 次の候補になり、消した素材は二度と取らない
    first = stock.clips_for("ocean waves")[0]
    (stock.STOCK_DIR / Path(first["file"]).name).unlink()
    stock.fetch_for_episode(ep_path, log=lambda *a: None)
    idx = stock.load_index()
    now = [e["id"] for e in idx["queries"]["ocean waves"]]
    check(len(now) == 2 and first["id"] not in now and f"{first['provider']}:{first['id']}" in idx["rejected"],
          f"消した素材の扱い: 取り直し後 {now} / 記録 {idx.get('rejected')}")

    # 5) 読み込み時の差し替えとクレジット
    ep = load_episode(ep_path)
    va = ep["scenes"][0]["visuals"][0]
    vb_forest, vb_ocean = ep["scenes"][1]["visuals"]
    check(va["type"] == "video" and Path(va["path"]).exists() and va.get("camera") == "static",
          f"差し替え後の素材 {va}")
    check(va["path"] != vb_ocean["path"], "同じ検索語の2回目に別の素材が割り当てられていない")
    check(vb_forest["path"] not in (va["path"], vb_ocean["path"]), "別の検索語に同じ素材")
    creds = ep.get("credits") or []
    check(len(creds) == 3 and all(("Pexels" in c or "Pixabay" in c) for c in creds), f"クレジット {creds}")

    # 6) コマンド(--search)
    import vlab
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vlab.main(["stock", "ocean", "--search", "--provider", "pexels"])
    check(rc == 0 and "#101" in buf.getvalue() and "Taro" in buf.getvalue(), f"vlab stock --search: {buf.getvalue()}")

    # 7) 台本 → 動画(日本語フォントが無い環境では省略)
    produced = ""
    from videolab.produce.telop import find_font
    cwd = os.getcwd()
    os.chdir(tmp)   # renders/cache を一時フォルダに作らせる
    try:
        find_font()
    except FileNotFoundError:
        produced = "（日本語フォントが無いため制作の通し確認は省略）"
    if not produced:
        from videolab.produce.compose import produce_episode
        try:
            res = produce_episode(ep_path, ROOT / "configs" / "style_profile.yaml", tmp / "out.mp4",
                                  draft=True, quiet=True)
            check(Path(res["video"]).exists() and Path(res["video"]).stat().st_size > 10_000, "動画が出力されない")
            text = Path(res["credits"]).read_text(encoding="utf-8")
            check("Pexels" in text and "Pixabay" in text, f"credits.txt に素材クレジットがない: {text}")
            produced = "・台本から制作"
        finally:
            os.chdir(cwd)
    else:
        os.chdir(cwd)

    if errors:
        print("[FAIL]")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] 応答の読み取り・素材の選び方・取得と再利用・台本への差し替え・クレジット{produced}を確認 ({tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
