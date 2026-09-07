# タナカ LINE構築データの所在調査（2026-09-07）

## 結論

**「タナカのLINE構築時に入力したプロンプト」の記録は、このリポジトリ・Claude Codeのクラウドセッション・Notion・Google Drive・Gmail のいずれにも残っていない。**

Claude Code の会話ログ（入力したプロンプト）は、その会話を実行したPCのローカルにしか保存されない。
リポジトリや Notion に書き出さない限り、別のセッションからは読めない。

LINE構築は手元のPC（Mac/Windows）の Claude Code CLI で行ったと推定されるので、
そのPC上の `~/.claude/projects/` にログが残っている可能性が高い。
抽出手順は末尾の「プロンプト記録の復元手順」を参照。

## 調べた場所と結果

| 場所 | 調べ方 | 結果 |
|---|---|---|
| GitHub `dev-workspace` 全ブランチ（17本） | ファイル一覧・全文検索（tanaka / タナカ / 田中 / LINE公式 / Lステップ / Messaging API / リッチメニュー） | 該当なし。LINEに触れているのは「ミツプロ リード獲得提案」「メディフリ YouTube企画」の中の言及のみ |
| GitHub PR / Issue | 一覧取得 | 0件 |
| Claude Code セッション一覧（42件） | タイトル確認 | LINE構築に該当するタイトルなし。クラウド側で LINE を構築したセッションは存在しない |
| Notion | 「タナカ」「LINE」「集客力診断」「line-shindan」「Messaging API」で検索 | 構築記録はなし。ただし LINE診断の**存在を示す痕跡**はあり（下記） |
| Google Drive | 「タナカ」「LINE」「診断」「shindan」で検索 | タナカ向けはなし。別クライアント向けの LINE ファネル設計書が2件（下記） |
| Gmail | 「line-shindan」「集客力診断」「企業価値診断」「タナカ LINE」 | 0件 |

## 見つかった関連情報

### Notion（MomentumMarketing / タナカ）

タナカ配下は X ポスト生成・記事作成システムが中心。LINE構築の手順やプロンプトは記録されていないが、
構築済みの LINE 診断が本番稼働していることが分かる。

- 稼働中のLINE診断（Cloud Run 上のアプリ）
  - `https://line-shindan-617542607371.asia-northeast1.run.app/v`（2026-08-25 頃から記事下書きに記載）
  - `/r/czrdpk`、`/r/46yu4w` などリファラ付きリンク（2026-09-02〜03）
  - `https://lin.ee/TNcJQRY`（LINE追加型。「従来の line-shindan リンクとは別ファネル」と記載、2026-09-05）
- 診断の中身（記事内の説明から）
  - 「無料AI集客力診断」: LINE上で9問、数分。創業4社・売却3社・支援300社の基準で作成
  - 「AI企業価値診断」: LINEで【企業価値】と送ると企業価値の目安と価値を下げている要因が分かる
  - 「集客資産スコア診断」（2026-09-04 記事）
- 関連ページ
  - [タナカ（ハブ）](https://app.notion.com/p/3900a6e9b67680e79542d577f54b39b4)
  - [🖋 記事作成システム](https://app.notion.com/p/3a50a6e9b676816c8c56dd63fa930558) → ① 記事下書きDB に診断リンク入りの記事が多数
  - [記事追撃⑤AI企業価値診断の導線](https://app.notion.com/p/3c40a6e9b6768103a9cffcd8ed24fcc1)（ポストストックDB、2026-08-22）
  - [X記事生成マスタープロンプト（統合版）](https://app.notion.com/p/3c40a6e9b676813e9feefa410cc9bfe2)（LINE登録→商談 の KPI 定義）

時期の推定: 記事に診断リンクが入り始めたのが 2026-08-22〜25 なので、LINE構築はその直前（2026-08 中旬〜下旬）と考えられる。

### Google Drive（別クライアント向け。タナカではない）

- `ポラリスFC_診断ファネル_構築指示書.md`（2026-07-01）
- `メディフリ_LINEファネル設計指示書_v1`（2026-07-06、最終更新 2026-08-15）
- `メディフリ_LINEファネル設計書.md`（2026-07-02）

タナカのLINE診断と同型（診断ファネル）なので、構築の流れを思い出す参考にはなる。

### このリポジトリ内

- `claude/tax-channel-lead-strategy-1h9p82` の `proposals/mitsupro-lead-gen/` に「LINE公式構築（リッチメニュー・自動応答）」の提案が含まれるが、提案書であって構築記録ではない。

## プロンプト記録の復元手順

LINE構築を行ったPCで、以下を実行する。`scripts/extract_claude_prompts.py` が
Claude Code のローカルログ（`~/.claude/projects/**/*.jsonl`）から、自分が入力したプロンプトだけを時系列で Markdown に書き出す。

```bash
# 1. このリポジトリを取得（済みなら pull）
git clone https://github.com/markeloopco-lgtm/dev-workspace.git
cd dev-workspace
git checkout claude/tanaka-line-data-check-e7pw77

# 2. LINE / タナカ / 診断 のどれかを含むセッションだけを抜き出す
python3 scripts/extract_claude_prompts.py --grep LINE --grep タナカ --grep 診断 -o docs/tanaka-line/prompts.md

# 期間で絞る場合（構築時期の推定は 2026-08 中旬〜下旬）
python3 scripts/extract_claude_prompts.py --since 2026-08-10 --until 2026-08-31 -o docs/tanaka-line/prompts.md
```

Windows の場合は `python` で実行する。ログの場所は `%USERPROFILE%\.claude\projects` で、スクリプトは自動で見つける。

出力例:

```markdown
## 2026-08-20 10:00  タナカ LINE診断ボット構築
- セッションID: `...`
- 入力数: 2

### 1. 2026-08-20 10:00
LINE公式で9問の集客力診断を作りたい

### 2. 2026-08-20 10:06
Cloud Runにデプロイして
```

出来上がった `prompts.md` をこのフォルダにコミットすれば、次回以降はクラウドセッションからも参照できる。

### ログが見つからない場合

- Claude Code のログは「その会話を動かしたPC」にしか残らない。別PCや claude.ai のチャットで構築した場合はそちらを確認する。
- claude.ai のブラウザチャットで構築した場合は、claude.ai の会話履歴（左メニュー）から該当会話を開いて手動でコピーする。
- Cloud Run の `line-shindan` サービスはデプロイ元のソース（GitHub 連携またはローカルの `gcloud run deploy`）を持つ。Google Cloud コンソールの Cloud Run → リビジョン → ソースから、デプロイ元のディレクトリやリポジトリを確認できる。
