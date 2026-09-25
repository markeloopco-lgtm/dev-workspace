"""Gemini API(無料枠)で映像の「意味」を分析する。任意機能。

  annotate : 解析済みフォルダの各ショット代表フレームを分類・言語化する(vlm.json)
  watch    : YouTubeのURLをそのままGeminiに渡して構成を分析する(ダウンロード不要)

APIキーは環境変数 GEMINI_API_KEY(または GOOGLE_API_KEY)か、作業フォルダの .env に書く。
モデル名は GEMINI_MODEL で変更できる(既定: watch は最新Flashの別名 gemini-flash-latest、
annotate は回数の多い軽量版 gemini-flash-lite-latest)。無料枠の対象モデルや回数制限は
プロジェクトごとに Google AI Studio に表示される(固定の数値を当てにしない)。

注意: 無料枠に送った内容は Google の製品改善に使われ、人が読むこともある(Gemini API 規約)。
  watch はURLを渡すだけ(Google自身の機能)だが、annotate は参考動画のフレーム画像を送信する。
  そのため annotate は明示的に選んだ時だけ動かす。
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_BATCH_MODEL = "gemini-flash-lite-latest"

CATEGORIES = ["3DCG宇宙", "3DCG地球・自然", "3DCG人物・キャラクター", "3DCGその他",
              "実写(ストック素材)", "実写(取材・撮影)", "宇宙機関などの記録映像・画像",
              "イラスト・2Dアニメ", "図解・グラフ・地図", "文字・タイトルカード", "その他"]

ANNOTATE_WARNING = """\
【確認】annotate は参考動画の代表フレーム画像を Gemini API に送信します。
  無料枠では、送った内容が Google の製品改善に使われ、人が読むこともあります。
  他人の動画のフレームを第三者に送ることになる点を理解した上で実行してください。
  (送らずに済ませるなら、Claude Code に contact_sheet.jpg を見せて分類を頼む方法もあります)"""

ANNOTATE_PROMPT = """あなたは映像編集の専門家です。YouTubeの科学解説動画の各ショットの代表フレームを{n}枚渡します。
画像は渡した順に shot番号 {indices} に対応します。各画像について日本語で分析し、
次のJSONだけを出力してください(前置き・コードブロック不要):
{{"shots":[{{"index":ショット番号,"category":"{cats}のどれか",
"subject":"何が映っているか(20字以内)",
"composition":"構図(例: 中央に惑星・画面右1/3に人物・俯瞰)",
"lighting":"光と色の特徴(例: 逆光で縁が光る・暖色の太陽光)",
"on_screen_text":"画面内の文字(テロップ・図の文字)。無ければ空文字",
"quality_points":"見栄えを良くしている工夫(被写界深度・パーティクル・グロー等)を1文で",
"ai_generated_suspect":true または false (画像生成AI特有の破綻・質感が見られるか),
"ai_reason":"そう判断した理由(falseなら空文字)"}}]}}"""

WATCH_PROMPT = """この動画(日本語の科学解説動画)を映像編集の観点で分析してください。
目的は、同じ品質の「オリジナル」動画を作るための作風研究です(キャラクターや台本の複製はしません)。
次のJSONだけを出力してください(前置き・コードブロック不要):
{"summary":"動画全体の構成と作風の要約(200字程度)",
 "structure":[{"start":"mm:ss","end":"mm:ss","section":"つかみ/導入/本題/山場/まとめ/エンディング等",
               "what_happens":"その区間で起きること","hook_technique":"視聴維持の工夫(あれば)"}],
 "visual_style":{"cg_ratio":"3DCGの割合の目安","other_sources":"実写・図解・イラストなどの使い方",
                 "source_by_section":"区間ごとの映像の出どころ(3DCG/ストック実写/取材映像/宇宙機関の記録/図解/AI生成の疑い)",
                 "camera_work":"カメラワークの傾向","color_grading":"色味の傾向","transitions":"場面転換の傾向"},
 "telop_style":{"font":"書体の印象","colors":"文字色・縁取り","position":"位置","frequency":"頻度",
                "emphasis":"強調の仕方(色変え・拡大など)"},
 "audio_style":{"narration":"ナレーションの声質・速さ・間","dialogue":"掛け合いの有無と役割",
                "bgm":"BGMの傾向","se":"効果音の使い方"},
 "pacing":"テンポ(カットの細かさ、情報量)","reproducible_techniques":["無料ツールで再現できる工夫を箇条書き"]}"""


def load_env(path: Path = Path(".env")) -> None:
    """作業フォルダの .env(KEY=VALUE形式)を環境変数に読み込む(既存の値は上書きしない)。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _key() -> str:
    load_env()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Gemini APIキーがありません。Google AI Studio(https://aistudio.google.com/)で"
                           "無料のキーを作り、.env に GEMINI_API_KEY=... と書いてください")
    return key


def _model(model: str = None, default: str = DEFAULT_MODEL) -> str:
    load_env()
    return model or os.environ.get("GEMINI_MODEL") or default


def generate(parts: list, model: str = None, retries: int = 5, timeout: float = 300) -> str:
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    url = API.format(model=_model(model))
    data = json.dumps(body).encode("utf-8")
    delay = 5.0
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": _key()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode("utf-8"))
            cands = resp.get("candidates") or []
            if not cands:
                raise RuntimeError(f"Geminiが応答を返しませんでした: {json.dumps(resp)[:500]}")
            return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:800]
            if e.code in (429, 500, 503) and attempt < retries - 1:
                print(f"  Gemini {e.code}(混雑・回数制限)。{delay:.0f}秒待って再試行します", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            hint = ""
            if e.code == 404:
                hint = "\n  モデル名が無効です。GEMINI_MODEL を AI Studio に表示されている名前にしてください"
            elif e.code == 429:
                hint = "\n  無料枠の回数制限です。時間をおくか、GEMINI_MODEL=gemini-flash-lite-latest を試してください"
            raise RuntimeError(f"Gemini APIエラー {e.code}: {msg}{hint}") from e
    raise RuntimeError("Gemini APIの再試行回数を超えました")


def parse_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return json.loads(text[start:])


def annotate_shots(analysis_dir, model: str = None, batch: int = 10, max_shots: int = 400) -> Path:
    analysis_dir = Path(analysis_dir)
    shots = json.loads((analysis_dir / "shots.json").read_text(encoding="utf-8"))
    shots = [s for s in shots if s.get("keyframe")][:max_shots]
    model = _model(model, DEFAULT_BATCH_MODEL)
    out = {"model": model, "shots": []}
    for i in range(0, len(shots), batch):
        chunk = shots[i:i + batch]
        idx = [s["index"] for s in chunk]
        parts = [{"text": ANNOTATE_PROMPT.format(n=len(chunk), indices=idx,
                                                 cats="/".join(CATEGORIES))}]
        for s in chunk:
            b64 = base64.b64encode((analysis_dir / s["keyframe"]).read_bytes()).decode("ascii")
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})
        print(f"  Gemini: ショット {idx[0]}〜{idx[-1]} を分析中", flush=True)
        res = parse_json(generate(parts, model))
        out["shots"].extend(res.get("shots", []) if isinstance(res, dict) else res)
        time.sleep(1.0)
    cats, ai = {}, 0.0
    dur = {s["index"]: s["duration"] for s in shots}
    for a in out["shots"]:
        c = a.get("category", "その他")
        d = dur.get(a.get("index"), 0.0)
        cats[c] = cats.get(c, 0.0) + d
        if a.get("ai_generated_suspect") is True:
            ai += d
    tot = sum(cats.values()) or 1.0
    out["category_mix"] = {k: round(v / tot, 3) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}
    out["ai_suspect_ratio"] = round(ai / tot, 3)
    path = analysis_dir / "vlm.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def youtube_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-40:]


def watch_youtube(url: str, out_dir: Path = None, model: str = None, start: str = None,
                  end: str = None, fps: float = None) -> Path:
    """公開YouTube動画をURLのままGeminiに分析させる(無料枠は1日あたりの動画時間に上限あり)。"""
    vid = youtube_id(url)
    out_dir = Path(out_dir or Path("analysis") / f"watch_{vid}")
    out_dir.mkdir(parents=True, exist_ok=True)
    part = {"fileData": {"fileUri": url}}
    meta = {}
    if start:
        meta["startOffset"] = start if start.endswith("s") else f"{start}s"
    if end:
        meta["endOffset"] = end if end.endswith("s") else f"{end}s"
    if fps:
        meta["fps"] = fps
    if meta:
        part["videoMetadata"] = meta
    text = generate([part, {"text": WATCH_PROMPT}], model, timeout=600)
    try:
        data = parse_json(text)
    except Exception:
        data = {"raw": text}
    data["_url"] = url
    data["_model"] = _model(model)
    path = out_dir / "gemini_watch.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "gemini_watch.md").write_text(watch_markdown(data), encoding="utf-8")
    return path


def watch_markdown(d: dict) -> str:
    if "raw" in d:
        return f"# Gemini分析 ({d.get('_url')})\n\n{d['raw']}\n"
    lines = [f"# Gemini分析: {d.get('_url')}", "", f"モデル: `{d.get('_model')}`", "",
             "## 要約", "", d.get("summary", ""), "", "## 構成", "",
             "| 区間 | セクション | 内容 | 視聴維持の工夫 |", "|---|---|---|---|"]
    for s in d.get("structure", []):
        lines.append(f"| {s.get('start', '')}–{s.get('end', '')} | {s.get('section', '')} | "
                     f"{s.get('what_happens', '')} | {s.get('hook_technique', '')} |")
    for key, title in (("visual_style", "映像"), ("telop_style", "テロップ"), ("audio_style", "音")):
        lines += ["", f"## {title}", ""]
        for k, v in (d.get(key) or {}).items():
            lines.append(f"- **{k}**: {v}")
    lines += ["", "## テンポ", "", str(d.get("pacing", "")), "", "## 再現できる工夫", ""]
    lines += [f"- {x}" for x in d.get("reproducible_techniques", [])]
    return "\n".join(lines) + "\n"
