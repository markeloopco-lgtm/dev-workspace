# テロップ用フォントの置き場

このフォルダに `.ttf` / `.otf` を置くと、**PCにインストールしなくても**
テロップに使われます（`scripts/auto_edit.py` が自動で見つけて ffmpeg に渡します）。

置いたあと、こう打つと認識されているか確認できます:

```powershell
python scripts/auto_edit.py fonts
```

## おすすめ: Noto Sans JP（無料・商用可）

報道番組で使われる「太ゴB101」「ゴシックMB101」系の代替として、実務者が
第一に挙げる無料フォントです（ライセンス: SIL Open Font License）。

1. https://fonts.google.com/noto/specimen/Noto+Sans+JP を開く
2. 右上の「Get font」→「Download all」でZIPをダウンロード
3. ZIPの中の `static/NotoSansJP-Black.ttf` を**このフォルダにコピー**
   （太めが好みでなければ `NotoSansJP-Bold.ttf` でも可）

これだけで次回のレンダリングから反映されます。設定を書き換える必要はありません
（`configs/auto_edit.yaml` の `font: auto` が自動で拾います）。

## 置かない場合

Windows 10/11 に最初から入っている **BIZ UDPGothic → メイリオ → 游ゴシック**
の順に自動で使われます。そのままでも読めるテロップになりますが、
Noto Sans JP Black のほうがニュース番組らしい太さになります。

## 注意

- フォントファイル自体はこのリポジトリにコミットしていません
  （再配布はライセンス条件の確認が必要なため）。各自でダウンロードしてください
- 有料フォントを入れる場合は、動画配信に使ってよいライセンスか必ず確認してください
