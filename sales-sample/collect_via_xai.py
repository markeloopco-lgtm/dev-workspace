# -*- coding: utf-8 -*-
"""
中小企業決裁者Xアカウント 自動収集パイプライン（xAI Grok API / X Search 利用）

前提:
  - 環境変数 XAI_API_KEY にxAIのAPIキーが設定されていること
  - api.x.ai への通信が許可されていること（環境のネットワークポリシー）

使い方:
  python collect_via_xai.py --dry-run          # API を叩かず設定と処理系だけ検証
  python collect_via_xai.py --limit 3          # 3タスクだけ試す（少額でテスト）
  python collect_via_xai.py                    # 全タスク実行

出力:
  sme_list_progress.json に追記（重複は自動スキップ）
  中小企業決裁者リスト_vN_<日付>.xlsx を生成
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
PROGRESS = os.path.join(BASE, "sme_list_progress.json")
API_URL = "https://api.x.ai/v1/chat/completions"
MODEL = os.environ.get("XAI_MODEL", "grok-4.3")

# 目標: 20業種 × 各50件。地域分割で件数を稼ぐ。
INDUSTRIES = [
    ("IT・Web制作・システム開発", "Web制作／システム開発／受託開発／SES／アプリ開発"),
    ("EC・D2C", "EC／D2C／ネットショップ／自社ブランド／Shopify／楽天出店"),
    ("広告・マーケ・デザイン", "広告代理店／マーケティング支援／デザイン事務所／SNS運用代行／映像制作"),
    ("士業（税理士・社労士等）", "税理士／社労士／行政書士／司法書士／中小企業診断士／事務所代表"),
    ("人材・採用支援", "人材紹介／採用支援／人材派遣／採用代行"),
    ("美容室・サロン・エステ", "美容室／サロン経営／エステ／ネイルサロン"),
    ("飲食店", "飲食店／居酒屋／カフェ／ラーメン／焼肉／多店舗展開"),
    ("建設・工務店・リフォーム", "工務店／建設会社／建築／リフォーム／塗装／外構"),
    ("不動産", "不動産／賃貸仲介／売買仲介／管理会社"),
    ("フィットネス・パーソナルジム", "パーソナルジム／フィットネス／スタジオ運営"),
    ("教育・塾・スクール", "学習塾／塾長／スクール運営／英会話／予備校"),
    ("医療（クリニック院長）", "クリニック／医院／院長／開業医"),
    ("歯科", "歯科医院／歯科医師／歯科院長／矯正歯科"),
    ("整骨院・治療院", "整骨院／接骨院／鍼灸院／整体院経営"),
    ("自動車（販売・整備）", "中古車販売／車屋／整備工場／板金塗装"),
    ("製造業（町工場）", "町工場／金属加工／溶接／部品製造／製造業"),
    ("物流・運送", "運送会社／物流／軽貨物／トラック運送"),
    ("介護・福祉", "介護施設／デイサービス／訪問看護／障害福祉"),
    ("農業・食品加工", "農家／農業法人／6次産業化／食品加工／直販"),
    ("小売・店舗", "小売店／雑貨店／アパレルショップ／専門店"),
]

REGIONS = ["東京都", "大阪府", "愛知県", "福岡県", "北海道",
           "神奈川県", "埼玉県", "兵庫県", "宮城県", "広島県"]

SYSTEM_PROMPT = (
    "あなたは日本のX（旧Twitter）から中小企業の経営者アカウントを探す調査アシスタントです。"
    "実在が確認できたアカウントだけを報告し、推測でアカウントIDを作ることは絶対にしないでください。"
    "見つからない場合は空の配列を返してください。必ずJSONのみを出力してください。"
)

USER_TEMPLATE = """Xで、次の条件を全て満たす日本語アカウントを最大{n}件探してください。

条件:
1. bio（プロフィール）に「代表取締役」「社長」「代表」「オーナー」「経営」「院長」「塾長」のいずれかを含む
2. {keywords} に関連する中小企業・個人事業の経営者本人
3. 直近90日以内に投稿がある
4. 主な拠点が{region}

除外:
- 上場企業・大企業の役員
- 企業の公式アカウント（本人アカウントのみ）
- 副業・投資・FX・せどり・情報商材系
- プレゼント企画や相互フォローでフォロワーを増やしているアカウント

出力は次のJSON形式のみ。説明文は不要です。
{{"accounts":[{{"handle":"@id","name":"表示名","title":"bioに書かれた肩書き","company":"会社名・屋号（不明ならnull）","region":"所在地（不明ならnull）","followers":"フォロワー数（概数、不明ならnull）","last_post":"直近投稿日 YYYY-MM-DD（不明ならnull）"}}]}}
不明な項目は推測せず null にしてください。"""


def call_api(keywords, region, n, api_key, timeout=120):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(
                n=n, keywords=keywords, region=region)},
        ],
        "search_parameters": {
            "mode": "on",
            "sources": [{"type": "x"}],
            "max_search_results": 30,
        },
        "temperature": 0,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def parse_accounts(text):
    """モデル出力からJSONを取り出す。コードフェンスや前後の文があっても耐える。"""
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for a in data.get("accounts", []):
        h = (a.get("handle") or "").strip()
        if not h:
            continue
        if not h.startswith("@"):
            h = "@" + h
        # ハンドルの形式チェック（英数字と_のみ・1〜15文字）
        if not re.fullmatch(r"@[A-Za-z0-9_]{1,15}", h):
            continue
        out.append({
            "handle": h,
            "name": (a.get("name") or "").strip(),
            "title": (a.get("title") or "").strip(),
            "company": (a.get("company") or "").strip(),
            "region": (a.get("region") or "").strip(),
            "followers": (a.get("followers") or "").strip() if a.get("followers") else "",
            "last_post": (a.get("last_post") or "").strip() if a.get("last_post") else "",
        })
    return out


def load_progress():
    with open(PROGRESS, encoding="utf-8") as f:
        return json.load(f)


def save_progress(data):
    with open(PROGRESS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="APIを叩かずタスク一覧と処理系だけ検証する")
    ap.add_argument("--limit", type=int, default=0,
                    help="実行するタスク数の上限（0=全部）")
    ap.add_argument("--per-task", type=int, default=20,
                    help="1タスクあたりの取得目標件数")
    ap.add_argument("--target", type=int, default=1000,
                    help="累計目標件数。到達したら停止")
    args = ap.parse_args()

    data = load_progress()
    seen = {r["handle"].lower() for r in data["rows"]}
    print(f"既存: {len(data['rows'])}件")

    tasks = [(ind, kw, reg) for ind, kw in INDUSTRIES for reg in REGIONS]
    if args.limit:
        tasks = tasks[:args.limit]
    print(f"タスク数: {len(tasks)}（{len(INDUSTRIES)}業種 × {len(REGIONS)}地域）")

    est_calls = len(tasks)
    print(f"概算コスト: X Search {est_calls}回 ≒ ${est_calls * 5 / 1000:.2f} "
          f"＋ トークン代（数ドル程度）")

    if args.dry_run:
        print("\n--dry-run のためAPIは呼びません。先頭3タスクのプロンプト例:")
        for ind, kw, reg in tasks[:3]:
            print(f"  [{ind}] {reg} / キーワード: {kw}")
        # パーサの自己テスト
        sample = '```json\n{"accounts":[{"handle":"testuser","name":"テスト","title":"代表取締役","company":"株式会社テスト","region":"東京都","followers":"1200","last_post":"2026-08-01"}]}\n```'
        parsed = parse_accounts(sample)
        assert len(parsed) == 1 and parsed[0]["handle"] == "@testuser", parsed
        bad = parse_accounts('{"accounts":[{"handle":"不正な ID","name":"x"}]}')
        assert bad == [], bad
        print("\nパーサ自己テスト: OK（コードフェンス除去・@補完・不正ID除外を確認）")
        return 0

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("エラー: 環境変数 XAI_API_KEY が未設定です。", file=sys.stderr)
        return 1

    added_total = 0
    for i, (ind, kw, reg) in enumerate(tasks, 1):
        if len(data["rows"]) >= args.target:
            print(f"目標{args.target}件に到達したため終了します。")
            break
        try:
            text = call_api(kw, reg, args.per_task, api_key)
            accounts = parse_accounts(text)
        except urllib.error.HTTPError as e:
            print(f"[{i}/{len(tasks)}] {ind}/{reg} HTTPエラー {e.code}: "
                  f"{e.read().decode('utf-8', 'ignore')[:200]}", file=sys.stderr)
            time.sleep(5)
            continue
        except Exception as e:
            print(f"[{i}/{len(tasks)}] {ind}/{reg} 失敗: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        added = 0
        for a in accounts:
            if a["handle"].lower() in seen:
                continue
            seen.add(a["handle"].lower())
            data["rows"].append({
                "industry": ind,
                "region": a["region"] or reg,
                "name": a["name"],
                "handle": a["handle"],
                "title": a["title"],
                "company": a["company"],
                "followers": a["followers"],
                "memo": (f"直近投稿 {a['last_post']}" if a["last_post"] else "")
                        + " ／ 収集: xAI X Search（検証レベルC→要目視確認）",
            })
            added += 1
        added_total += added
        print(f"[{i}/{len(tasks)}] {ind}/{reg}: 取得{len(accounts)} 新規{added} "
              f"累計{len(data['rows'])}")
        save_progress(data)
        time.sleep(1)

    print(f"\n完了: 新規{added_total}件 / 累計{len(data['rows'])}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
