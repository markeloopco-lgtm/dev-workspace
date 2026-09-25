"""フリー動画素材(Pexels / Pixabay)の検索・ダウンロードとクレジット管理。

台本の visuals に検索語で書いておくと、
    - {type: stock, query: "ocean waves", min_duration: 6}
`python scripts/vlab.py stock 台本.yaml` で素材を assets/video/stock/ に取ってきて、
produce のときに自動で動画素材 {type: video, path: ...} に差し替える。
撮影者と出典は credits.txt(概要欄用)に自動で入る。

APIキー(どちらも無料登録で発行)は作業フォルダの .env に書く:
    PEXELS_API_KEY=...     https://www.pexels.com/api/
    PIXABAY_API_KEY=...    https://pixabay.com/api/docs/
どちらも商用利用可・クレジット表記は任意だが、素材そのままの再配布・販売は不可で、
人物・ロゴ・商標が写った素材の使い方には注意がいる(各サイトのライセンスを必ず確認)。
"""

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STOCK_DIR = REPO / "assets" / "video" / "stock"
PEXELS_SEARCH = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH = "https://pixabay.com/api/videos/"
PROVIDERS = ("pexels", "pixabay")
PROVIDER_NAMES = {"pexels": "Pexels", "pixabay": "Pixabay"}
KEY_ENV = {"pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY"}
USER_AGENT = "videolab-stock/1.0"
MAX_CLIPS_PER_QUERY = 5   # 同じ検索語を何度も使うときに取ってくる本数の上限(変化をつけるため)


@dataclass
class Candidate:
    provider: str
    id: str
    page_url: str
    author: str
    author_url: str
    duration: float
    width: int        # 選んだファイルの解像度
    height: int
    fps: float | None
    url: str          # 選んだファイルのダウンロードURL
    tags: str = ""


def qkey(query: str) -> str:
    """検索語の表記ゆれ(空白の数・大文字小文字)をそろえた索引キー。"""
    return " ".join(str(query).split()).lower()


# ---------------------------------------------------------------- APIの応答を読む

def _pick_file(files: list, target_h: int):
    """1本の素材の複数解像度から、target_h 以上で最小のもの(無ければ最大)を選ぶ。"""
    files = [f for f in files if f.get("url") and (f.get("h") or 0) > 0]
    if not files:
        return None
    enough = [f for f in files if f["h"] >= target_h]
    if enough:
        return min(enough, key=lambda f: (f["h"], f["w"]))
    return max(files, key=lambda f: (f["h"], f["w"]))


def parse_pexels(data: dict, target_h: int = 1080) -> list:
    out = []
    for v in data.get("videos") or []:
        files = [{"w": int(f.get("width") or 0), "h": int(f.get("height") or 0), "fps": f.get("fps"),
                  "url": f.get("link")}
                 for f in v.get("video_files") or []
                 if (f.get("file_type") or "video/mp4") == "video/mp4"]
        best = _pick_file(files, target_h)
        if not best:
            continue
        user = v.get("user") or {}
        out.append(Candidate("pexels", str(v.get("id")), v.get("url") or "", user.get("name") or "",
                             user.get("url") or "", float(v.get("duration") or 0), best["w"], best["h"],
                             float(best["fps"]) if best.get("fps") else None, best["url"]))
    return out


def parse_pixabay(data: dict, target_h: int = 1080) -> list:
    out = []
    for h in data.get("hits") or []:
        files = [{"w": int(f.get("width") or 0), "h": int(f.get("height") or 0), "fps": None,
                  "url": f.get("url")}
                 for f in (h.get("videos") or {}).values() if isinstance(f, dict)]
        best = _pick_file(files, target_h)
        if not best:
            continue
        user, uid = h.get("user") or "", h.get("user_id")
        out.append(Candidate("pixabay", str(h.get("id")), h.get("pageURL") or "", user,
                             f"https://pixabay.com/users/{user}-{uid}/" if user and uid else "",
                             float(h.get("duration") or 0), best["w"], best["h"], None, best["url"],
                             tags=h.get("tags") or ""))
    return out


def rank(cands: list, min_duration: float = 0.0, landscape: bool = True) -> list:
    """向き(横長)・長さ・解像度(720p以上)を満たすものを前に。同条件ならAPIの関連度順のまま。"""
    def ok(c):
        return ((c.width >= c.height) == landscape, c.duration >= min_duration, c.height >= 720)
    return sorted(cands, key=ok, reverse=True)


# ---------------------------------------------------------------- 通信

def _key(provider: str) -> str:
    import os
    return os.environ.get(KEY_ENV[provider], "").strip()


def _load_keys() -> None:
    from videolab.vlm import load_env
    load_env()                    # 作業フォルダの .env
    load_env(REPO / ".env")       # リポジトリ直下の .env (既に読んだ値は上書きしない)


def _get_json(url: str, params: dict, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise RuntimeError(f"素材サイトに拒否されました(HTTP {e.code})。APIキーが正しいか .env を確認してください") from e
        if e.code == 429:
            raise RuntimeError("素材サイトの回数制限に達しました。しばらく待ってからやり直してください") from e
        raise RuntimeError(f"素材サイトのエラー(HTTP {e.code}): {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"素材サイトに接続できません: {e.reason}") from e


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"素材をダウンロードできません: {url} ({e})") from e
    tmp.replace(dest)


def search(query: str, provider: str = "auto", n: int = 10, landscape: bool = True,
           target_h: int = 1080) -> list:
    """検索語で素材を探す。provider=auto はキーのあるサイトを全部使う(Pexels → Pixabay の順)。"""
    _load_keys()
    names = PROVIDERS if provider == "auto" else (provider,)
    names = [p for p in names if _key(p)]
    if not names:
        need = " / ".join(KEY_ENV[p] for p in (PROVIDERS if provider == "auto" else (provider,)))
        raise RuntimeError(f"APIキーがありません。作業フォルダの .env に {need} を書いてください"
                           "(無料登録で発行。docs/08)")
    out = []
    for p in names:
        if p == "pexels":
            data = _get_json(PEXELS_SEARCH, {"query": query, "per_page": min(80, n), "size": "medium",
                                             "orientation": "landscape" if landscape else "portrait",
                                             "locale": "ja-JP"}, {"Authorization": _key(p)})
            out += parse_pexels(data, target_h)
        else:
            data = _get_json(PIXABAY_SEARCH, {"key": _key(p), "q": query[:100], "per_page": max(3, min(200, n)),
                                              "lang": "ja", "safesearch": "true"})
            out += parse_pixabay(data, target_h)
    return out


# ---------------------------------------------------------------- 取得済み素材の索引

def _index_path() -> Path:
    return STOCK_DIR / "index.json"


def load_index() -> dict:
    p = _index_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 1, "queries": {}}


def save_index(idx: dict) -> None:
    STOCK_DIR.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


def _abs(file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else REPO / p


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def clips_for(query: str, idx: dict | None = None) -> list:
    """その検索語で取得済み(ファイルが実在する)の素材。"""
    idx = idx or load_index()
    return [e for e in idx["queries"].get(qkey(query), []) if _abs(e["file"]).exists()]


def resolve(query: str, nth: int = 0) -> dict | None:
    """制作時の差し替え先。同じ検索語の n 回目の使用には n 本目の素材を割り当てる(足りなければ循環)。"""
    clips = clips_for(query)
    if not clips:
        return None
    e = clips[nth % len(clips)]
    return {**e, "path": str(_abs(e["file"]).resolve())}


def credit_line(e: dict) -> str:
    who = e.get("author") or "撮影者不明"
    return f"動画素材: {who} / {PROVIDER_NAMES.get(e.get('provider'), e.get('provider'))} ({e.get('page_url', '')})"


def fetch_query(query: str, count: int = 1, provider: str = "auto", min_duration: float = 5.0,
                landscape: bool = True, target_h: int = 1080, log=print) -> list:
    """検索語の素材を count 本そろえる(取得済みは再利用。別の検索語で使った素材は避ける)。

    assets/video/stock/ から消したファイルは「気に入らなかった素材」として記録し、二度と取らない
    (消してから取り直すと次の候補になる)。
    """
    idx = load_index()
    key = qkey(query)
    have = clips_for(query, idx)
    rejected = idx.setdefault("rejected", [])
    gone = [e for e in idx["queries"].get(key, []) if not _abs(e["file"]).exists()]
    for e in gone:
        tag = f"{e['provider']}:{e['id']}"
        if tag not in rejected:
            rejected.append(tag)
    need = count - len(have)
    if need <= 0:
        if gone:
            idx["queries"][key] = have
            save_index(idx)
        return have
    used = {(e["provider"], str(e["id"])) for es in idx["queries"].values() for e in es}
    used |= {tuple(t.split(":", 1)) for t in rejected}
    cands = rank(search(query, provider, n=max(10, need * 4), landscape=landscape, target_h=target_h),
                 min_duration, landscape)
    for c in cands:
        if need <= 0:
            break
        if (c.provider, c.id) in used:
            continue
        dest = STOCK_DIR / f"{c.provider}_{c.id}_{c.height}p.mp4"
        if not dest.exists():
            log(f"  取得: {query} ← {PROVIDER_NAMES[c.provider]} #{c.id} {c.width}x{c.height} "
                f"{c.duration:.0f}秒 ({c.author})")
            _download(c.url, dest)
        entry = {**asdict(c), "file": _rel(dest), "query": query,
                 "downloaded": datetime.now().strftime("%Y-%m-%d %H:%M")}
        entry.pop("url", None)
        have.append(entry)
        used.add((c.provider, c.id))
        need -= 1
    idx["queries"][key] = have
    save_index(idx)
    if need > 0:
        log(f"  注意: 「{query}」は {len(have)}/{count} 本しか見つかりません(検索語を変えると増えることがある)")
    return have


# ---------------------------------------------------------------- 台本との連携

def stock_requests(ep: dict) -> dict:
    """台本(読み込み前のYAML)から type: stock の検索語ごとの使用回数と最短の長さを集める。"""
    reqs = {}

    def visit(v):
        if not (isinstance(v, dict) and v.get("type") == "stock"):
            return
        q = str(v.get("query") or "").strip()
        if not q:
            return
        r = reqs.setdefault(qkey(q), {"query": q, "uses": 0, "min_duration": 0.0,
                                      "provider": v.get("provider") or "auto"})
        r["uses"] += 1
        r["min_duration"] = max(r["min_duration"], float(v.get("min_duration") or 0))

    for sc in ep.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        for v in sc.get("visuals") or ([sc["visual"]] if sc.get("visual") else []):
            visit(v)
        for ln in sc.get("lines") or []:
            if isinstance(ln, dict):
                visit(ln.get("visual"))
    return reqs


def fetch_for_episode(path, provider: str = "auto", min_duration: float = 5.0, log=print) -> dict:
    """台本の stock 素材を全部そろえる。戻り値は {検索語: 取得済み本数}。"""
    import yaml
    ep = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    reqs = stock_requests(ep)
    if not reqs:
        log("この台本には {type: stock, query: ...} の素材がありません")
        return {}
    got = {}
    for r in reqs.values():
        n = min(r["uses"], MAX_CLIPS_PER_QUERY)
        clips = fetch_query(r["query"], n, provider if provider != "auto" else r["provider"],
                            max(min_duration, r["min_duration"]), log=log)
        got[r["query"]] = len(clips)
    return got
