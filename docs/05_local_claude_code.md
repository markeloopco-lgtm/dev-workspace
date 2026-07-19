# 05. ローカルPCへの移行（Claude Codeで続きを進める）

ここまでの成果物をあなたのWindows PCに持ち込み、以降のセットアップ作業を
ローカルのClaude Codeに実行させるための手順。所要15分程度。
（コマンドは2026-07-19時点の公式ドキュメントで検証済み）

## Step 1: Claude Codeのインストール

PowerShellを開いて（スタートメニュー→「PowerShell」）:

```powershell
irm https://claude.ai/install.ps1 | iex
```

確認:

```powershell
claude --version
```

バージョン番号が出ればOK。要件はWindows 10 1809以降・RAM 4GB以上（Node.js不要）。

## Step 2: Git for Windowsのインストール（推奨）

https://git-scm.com/downloads/win からインストール（設定は全部デフォルトでOK）。
これでClaude CodeがBashツールを使えるようになり、リポジトリのシェルスクリプトも動く。

## Step 3: このリポジトリをクローン

```powershell
cd $HOME\Documents
git clone -b claude/workspace-check-or7j3j https://github.com/markeloopco-lgtm/dev-workspace.git
cd dev-workspace
```

初回はブラウザが開いてGitHubログインを求められる（Git Credential Manager）。

## Step 4: Claude Codeを起動してログイン

```powershell
claude
```

初回起動時にブラウザが開くので**Claudeアカウント（Pro/Max）でログイン**。
APIキーは不要で、サブスクリプションの利用枠が使われる。

## Step 5: 最初のプロンプト（コピペ用）

起動したClaude Codeにこれを貼る:

```
CLAUDE.mdを読んで現状を把握してください。
私は非エンジニアなので、1ステップずつ確認を取りながら進めてください。
今日はStyle-Bert-VITS2のセットアップから始めましょう。
```

ローカルのClaudeは `CLAUDE.md`（引き継ぎ書）を自動で読み込むため、
このセッションの文脈・決定事項・残タスクを把握した状態で始まる。

## ローカルのClaudeができること／あなたがやること

| 作業 | 担当 |
|---|---|
| Style-Bert-VITS2の導入・起動 | Claude |
| AITuberKitの導入・env設定・モデル配置 | Claude |
| PSDの正規化・検品・マッピング追記 | Claude |
| トラブルシュート全般 | Claude |
| Kaggle/Google/GitHubのアカウント作成・APIキー発行 | **あなた**（Claudeが画面ごとに誘導） |
| Cubism EditorのGUI操作（1体目のリグ） | **あなた**（Claudeがdocs/03に沿って1手順ずつ誘導） |
| OBSのGUI設定 | **あなた**（同上） |

## 補足

- ターミナルが苦手なら**デスクトップアプリ**（https://claude.com/download）でも同じことができ、
  フォルダを開いて使う
- 調子が悪い時は `claude doctor` で診断
- ローカルでの変更もこのリポジトリにコミット＆プッシュしておけば、
  どの環境のClaudeからでも続きができる
