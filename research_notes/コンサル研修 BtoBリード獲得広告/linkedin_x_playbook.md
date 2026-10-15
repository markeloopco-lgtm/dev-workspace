# LinkedIn広告・X広告によるBtoBリード獲得 運用ノウハウ（日本市場／コンサル・企業研修・士業／月10万〜50万円）

> 調査日: 2026-10-15。調査方法についての注意: 今回の環境では WebFetch がほぼ全ドメインで失敗し（DNS解決エラー／プロキシ拒否: linkedin.com, business.x.com, renue.co.jp, start-link.jp, metadata.io, socialmediatoday.com など）、**本文を直接開いて確認できたページはない**。以下の引用はすべて WebSearch の検索結果要約（抜粋引用を含む）にもとづく。数値は引用元の記載どおりだが、前後の文脈や算出方法は未確認のものが多い。重要な数値は、出稿前に Campaign Manager／X広告マネージャーの推定値で必ず確認すること。
> 表記: 「非日本データ」＝米欧・グローバル・APAC全体の数値。日本の相場の直接の根拠ではない。

---

## Q1. 日本のLinkedIn: 会員数・属性・日本語ターゲティングの規模、最低オーディエンス・最低日予算

### Takeaway
日本のLinkedInの登録者は、公式発表で300万人（2022年8月）。第三者の推計では500万人超（2025年12月）から約621万人（2026年6月、広告リーチベース）。ただし「認知はあるが利用頻度は低い」層が多く、日本語で配信し、さらに人事や経営層に絞ると、LinkedIn自身が推奨するオーディエンス規模（Sponsored Contentで30万人以上）に届かないことが多い。月10万円台なら、最低日予算（約10ドル＝約1,500円/日）で1〜2キャンペーンを回すのが限界になる。

### Cited Findings
**会員数（数値ごとに出所が違う。必ず年と出所を併記すること）**
- LinkedInは**日本の登録メンバーが300万人を突破**したと発表（2022年8月、公式発表の報道）— [Impress Watch](https://www.watch.impress.co.jp/docs/news/1433636.html)
- Business Insider Japanは、国内は「400万人達成が見えている」が、FacebookやXと比べると成長途上だと報じた（記事日付は検索結果からは未確認）— [Business Insider Japan](https://www.businessinsider.jp/article/281034/)
- 2025年12月時点で国内ユーザーは**500万人超**、主な利用層は25〜34歳のビジネスパーソンや経営者・役員・部長職以上とする記述がある（マーケ会社ブログで、一次情報ではない）— [jinrai.co.jp](https://jinrai.co.jp/blog/2026/05/10/linkedin-btob-ai-strategy/)
- NapoleonCatの推計（**広告APIのリーチから集計した値で、LinkedIn公式ではない**）: 日本のLinkedInユーザーは**2026年6月に621万人（人口の5%）**。2026年2月は561万人、3月は601万人。最大の年齢層は25〜34歳で280万人 — [NapoleonCat 2026/06](https://stats.napoleoncat.com/linkedin-users-in-japan/2026/06/)
- 日本語版は2011年に開始し、同年10月に日本法人リンクトイン・ジャパンを設立 — [GameBusiness.jp](https://www.gamebusiness.jp/article/2013/01/15/7375.html)
- hotice／RASA JAPANの調査（2025年10月公表、n=256）: LinkedInの**認知度は約6割だが利用頻度は低め**。一方、活用している人の約9割が成果を実感したと回答 — [Web担当者Forum](https://webtan.impress.co.jp/n/2025/10/10/50187)

**属性（外資系・IT・人事・経営層の比率）**
- 外資系・IT・人事・経営層の**比率を示す公式データや信頼できる調査は見つからなかった**（検索要約でも「該当する記述なし」）— [Impress Watch](https://www.watch.impress.co.jp/docs/news/1433636.html)（会員数のみ）
- 古い論評として「なぜ日本人はLinkedInを使わないのか」（2015年）がある。転職文化の違いなどが論点 — [THE BRIDGE (2015)](https://thebridge.jp/2015/01/why-japanese-dont-use-linkedin)

**日本語対応・日本企業事例**
- MarkeZineが「LinkedInの広告機能が日本語に対応」と報じている（日付と範囲は検索結果からは未確認）— [MarkeZine](https://markezine.jp/news/detail/16312)
- LinkedInは2016年11月に日本向けサイト「LinkedInマーケティングソリューションズ」を開設（2016年のアドタイ記事。古い情報）— [AdverTimes](https://www.advertimes.com/?p=240007)
- LinkedIn公式の日本企業事例（**富士電機**、2022年）: リード獲得フォームでホワイトペーパーのダウンロードを促し、類似オーディエンスでリーチを広げ、リターゲティングも併用 — [LinkedIn Marketing Solutions 富士電機事例](https://business.linkedin.com/ja-jp/marketing-solutions/cx/22/11/fuji-electric)
- 公式の日本語トレーニング「広告を始める」がある — [LinkedIn Marketing Labs (ja)](https://training.marketing.linkedin.com/path/start-advertising-ja)

**最低オーディエンス規模（公式ヘルプ。検索結果に原文の引用あり）**
- 広告セットに必要な最小オーディエンスは**300メンバー**。成果を出す目安として**5万人以上**を推奨。**Sponsored ContentとSponsored Messagingは30万人以上**、テキスト広告は6万〜40万人を推奨。同じカテゴリ内で複数の条件を選ぶと対象は増える（OR条件）— [LinkedIn Help a423690](https://www.linkedin.com/help/lms/answer/a423690)
- ABM（会社名リスト）では300人の下限に引っかかるのが実務上の課題になっている — [Clay community](https://community.clay.com/x/general/kxegkn1ykbjv/how-to-overcome-linkedin-ads-300-member-minimum-au)／[ZenABM](https://zenabm.com/blog/linkedin-ads-minimum-audience-size)

**最低日予算**
- 公式APIは、ターゲティングごとに**最低・推奨入札額と最低日予算（`dailyBudgetLimits.min`）を通貨別に返す**。円の下限値は固定の表ではなく、アカウントごとにAPIや管理画面で確認する — [Microsoft Learn: Ad Budget Pricing API](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ad-budget-pricing?view=li-lms-2023-10)
- 第三者の情報: 新規キャンペーンは**1日10ドル、または通算100ドル**以上の予算が必要。CPC/CPMの最低入札は2ドル — [WebFX](https://www.webfx.com/blog/marketing/linkedin-advertising-cost/)／[Zernio docs](https://docs.zernio.com/platforms/linkedin-ads/create-ads)
- 日本語の解説では「最低予算はキャンペーンあたり**1日約1,500円（10ドル）**から」— [koukoku.jp](https://www.koukoku.jp/service/suketto/marketer/sns/%e3%80%902025%e5%b9%b4%e6%9c%80%e6%96%b0%e3%80%91linkedin%e5%ba%83%e5%91%8a%e3%81%a8%e3%81%af%ef%bc%9f%e7%a8%ae%e9%a1%9e%e3%82%84%e7%89%b9%e5%be%b4%e3%80%81%e3%83%a1%e3%83%aa%e3%83%83%e3%83%88/)
- ある運用者は、入札で競り負けないために1日7.5〜15ドルの支出を推奨。総予算300ドル未満だと月の途中で使い切る可能性も指摘している（個人の助言）— [Online Optimism](https://www.onlineoptimism.com/?p=5893)

### Inferences
- 日本の会員数は約500万〜620万人（推計）。ただし実際にアクティブな人は少ないとみられる（認知6割・利用頻度低め）。そこへ「日本・日本語・人事／経営層・従業員〇名以上」と条件を重ねると、多くの場合**数万人規模**に縮む。公式推奨の30万人を満たすのは難しく、5万人前後を現実的な下限と考えるのが妥当。
- 月10万円は1日約3,300円。最低日予算（約1,500円）なら**同時に回せるのは2キャンペーンまで**で、A/Bテストの余地はほぼない。月30万〜50万円（1日1万〜1.6万円）あれば、フォーマット比較やオーディエンス分割ができる。
- 「日本のLinkedInユーザー＝外資系・IT・グローバル人材・転職関心層に偏っている」という見方は業界でよく言われる。ただし**裏づける比率データは今回見つからなかった**。実際の規模はCampaign Managerのオーディエンス推定で確認すること。

### Gaps
- LinkedIn公式による日本の最新会員数（400万人・500万人突破の正式発表）や、MAU・DAUは見つからなかった。
- 日本ユーザーの業種・職種・役職の構成比（外資系比率、人事職の人数など）について、公開データは見つからなかった。
- 円建ての最低日予算・最低入札額の公式値は未確認（WebFetchが失敗し、公式ヘルプを直接開けなかった）。
- 「プロフィール言語」によるターゲティング（日本語プロフィールのユーザーに絞る機能）は、背景知識としては存在を把握しているが、今回は出典で確認できていない。

---

## Q2. LinkedInのリード獲得向け広告フォーマット（リード獲得フォーム／ドキュメント広告／Thought Leader Ads／会話広告・メッセージ広告／イベント広告）。コンサル・研修・士業に合うのはどれか

### Takeaway
少額のBtoBリード獲得で中心にすべきは「**リード獲得フォーム（ネイティブフォーム）＋ドキュメント広告（資料の途中までを見せ、続きはフォーム入力で読める形）**」。「**Thought Leader Ads（代表者やコンサルタント個人の投稿を広告にする）**」は、コンサル・研修・士業のように「人で選ばれる」商材と相性がよい。ただし公開されている成果データは主にエンゲージメントで、CPL（1件あたり獲得単価）のデータはない。会話広告・メッセージ広告は、EEAとスイスでは受信を許可（オプトイン）したメンバーにしか届かない。日本に固有の制限は今回確認できなかった。

### Cited Findings
**リード獲得フォーム（Lead Gen Forms）**
- LinkedInの2017年発表: リード獲得フォームによって、マーケターの**平均リード単価が20%以上下がった**（LinkedInの自社発表）。選択式の質問や、100文字までの自由回答も設定できる。当初はスポンサードコンテンツ向けで、のちにスポンサードInMailやダイナミック広告にも拡大 — [LinkedIn News 2017](https://news.linkedin.com/2017/11/linkedin-doubles-down-on-lead-generation-tools-for-marketers)／[MediaPost](https://www.mediapost.com/publications/article/310209/linkedin-advertisers-can-generate-leads-from-spons.html)
- 【非日本データ】フォーム送信率の目安は情報源によって幅がある: 6〜10%（SaaS系の代理店）、10〜15%（2026年B2B SaaSベンチマーク）、15〜25%（ベンダー。LPは2〜5%）。外部LPと比べて2〜3倍よく転換する（SalesMind 2025）。入力項目が1つ増えるごとに完了率は約5%下がる — [SaaS Hero](https://www.saashero.net/strategy/linkedin-advertising-conversion-rate-optimization/)／[SalesMind 2025](https://sales-mind.ai/blog/2025-sales-funnel-benchmarks-linkedin)／[NAV43](https://nav43.com/blog/2025-linkedin-ads-benchmarks-every-saas-tech-marketer-needs)
- 【非日本データ】ネイティブフォームのCPLは、LP経由より一般に25〜35%低い（ベンダーの主張）— [Cleverly (2025-08)](https://www.cleverly.co/blog/linkedin-lead-generation-cost)

**ドキュメント広告（Document Ads）**
- 2022年10月に、リード獲得用途の形式として開始 — [Social Samosa](https://www.socialsamosa.com/2022/10/linkedin-launches-document-ads-for-lead-generation)
- 仕様: PDF/DOC/DOCX/PPT/PPTX、最大100MB、**理想は10ページ以下**（上限300ページ）。資料を**全部無料で見せて認知を取る**使い方と、**途中までにしてフォーム入力で続きを読ませる（ゲート）**使い方を選べる — [FoxData (ja)](https://foxdata.com/jp/marketing-academy/understanding-linkedin-ad-formats/)
- 【非日本データ】フィード上で中身を確認してから本人がフォームに進むため、ミスマッチが減りリードの質が上がる。中間ファネル向き。ある情報源は「広告疲れまで最大11週間もつ」としている。ただしドキュメント広告に絞ったCPLベンチマークは見当たらない — [rocketagency](https://rocketagency.com.au/blog/category/digital-marketing-tactics)／[SaaS Hero](https://www.saashero.net/?p=11290)
- 日本語の解説では、ホワイトペーパー配布にはドキュメント広告が推奨されている — [jinrai.co.jp](https://jinrai.co.jp/?p=4671)

**Thought Leader Ads（個人の投稿の広告化）**
- **任意のメンバー（社員以外も含む）の投稿を広告化できるように拡大**。投稿者には「承認／拒否」の通知が届く。拡大時期は**情報源で食い違う**: 2024年3月（全世界で提供）とする報道と、2025年3月とする要約がある — [CMSWire](https://www.cmswire.com/the-wire/companies-can-now-promote-member-content-with-linkedin-thought-leader-ads)／[Mobile Marketing Magazine](https://mobilemarketingmagazine.com/linkedin-thought-ads/)。食い違い元: [mi-3 (2024-03-18)](https://mi-3.com.au/18-03-2024/linkedins-thought-leader-ads-help-advertisers-amplify-b2b-influence) は2024年3月の記事
- **Thought Leader Event Ads**（2025年）: LinkedInイベントページへのリンクを含む個人投稿を広告化できる。ただし目的は**ブランド認知とエンゲージメントに限られる**（リード獲得目的は不可）— [MediaPost](https://www.mediapost.com/publications/article/408197)／[Lindsey Gamble](https://lindseygamble.com/blog/linkedin-expands-thought-leader-ads-to-live-events-to-drive-registrations)
- 【非日本データ・LinkedInの主張】意思決定者の71%は、従来の広告より思想的リーダーシップの方が効果的と感じている。B2Bマーケターは単一画像広告の1.6倍のエンゲージメントを得た — [mi-3](https://mi-3.com.au/18-03-2024/linkedins-thought-leader-ads-help-advertisers-amplify-b2b-influence)
- APACの例: Mastercardが試した際、ある役員は初日に500件以上の通知を受けた（日本の事例ではない）— [mi-3](https://mi-3.com.au/18-03-2024/linkedins-thought-leader-ads-help-advertisers-amplify-b2b-influence)
- Thought Leader AdsのCPLや転換率について、信頼できるデータは見つからない — [SaaS Hero](https://www.saashero.net/?p=11290)

**会話広告／メッセージ広告（Sponsored Messaging, 旧Sponsored InMail）**
- Sponsored Messaging（会話広告・メッセージ広告）は、**EEAとスイスでは受信を許可（オプトイン）したメンバーにだけ配信**される。日本はこの制限の記載に含まれていない — [LinkedIn Help 104116](https://www.linkedin.com/help/lms/answer/104116)
- 会話広告は2020年に、既存のメッセージ広告（旧Sponsored InMail）の発展形として登場 — [LinkedIn Marketing Blog 2020](https://business.linkedin.com/marketing-solutions/blog/linkedin-news/2020/linkedin-introduces-conversation-ads)

### Inferences
- **コンサル・研修・士業との相性（推論）**
  - ①**ドキュメント広告＋リード獲得フォーム**: 「人事向け研修設計チェックリスト」「〇〇法改正対応ガイド」など、10ページ前後の実務資料を途中まで見せてゲートする。コンサル・研修・士業の得意分野で、最初に試すべき形式。
  - ②**Thought Leader Ads**: 代表やトップ講師の実名投稿を広告化する。人で選ばれる商材なので相性はよい。ただしリード獲得目的に直接使えないケースもあるため、**認知・エンゲージメント用（リターゲティング用の母集団づくり）**と割り切り、①でリターゲティングする2段構えが妥当。
  - ③**イベント（ウェビナー）集客**: リード獲得フォームでウェビナー申込を取るのが基本。Thought Leader Event Adsは認知目的に限られる。
  - ④**会話広告**: 日本で配信できる可能性はある。ただし少額では検証が難しく、優先度は低い。
- 少額では「1キャンペーン＝1オファー＝1フォーム」に絞る。フォームの項目は氏名・メール・会社・役職（LinkedInのプロフィールから自動入力される）にとどめ、自由記述は入れない（項目が1つ増えるごとに約5%落ちる、という非日本データにもとづく）。

### Gaps
- 日本のアカウントで、Thought Leader Ads・ドキュメント広告・会話広告の**提供状況と日本語UIでの名称**を、公式ヘルプで確認できていない（WebFetch失敗）。
- 日本でのフォーマット別CPL・CVRの実データは見つからなかった。
- Thought Leader Adsとリード獲得目的・リード獲得フォームを組み合わせられるかについて、2026年10月時点の最新仕様は未確認。

---

## Q3. LinkedInのターゲティング（業種・職務・役職/シニオリティ・会社規模・会社名リスト/ABM・Matched Audiences・Predictive Audiences・オーディエンス拡大）と推奨規模

### Takeaway
業種・職務・シニオリティ・会社規模・会社名・スキル・グループで細かく絞れるのがLinkedInの強みで、会社名やリストのアップロード（Matched Audiences）も使える。ただし日本では母数が小さい。**オーディエンス拡大（Audience Expansion）とLinkedIn Audience Networkはオフで始める**のが実務家の一致した見方。

### Cited Findings
- ターゲティング軸: 職種、役職、シニオリティ、業界、企業規模、特定の企業名、スキル、グループ。顧客リストや企業リストのアップロードにも対応 — [media-radar](https://media-radar.jp/contents/meditsubu/columns5-linkedinads)／[koukoku.jp](https://www.koukoku.jp/service/suketto/marketer/sns/%e3%80%902025%e5%b9%b4%e6%9c%80%e6%96%b0%e3%80%91linkedin%e5%ba%83%e5%91%8a%e3%81%a8%e3%81%af%ef%bc%9f%e7%a8%ae%e9%a1%9e%e3%82%84%e7%89%b9%e5%be%b4%e3%80%81%e3%83%a1%e3%83%aa%e3%83%83%e3%83%88/)
- 「会社名」「統計にもとづく収益・成長率」といった、ビジネスに特化したターゲティングが特徴 — [start-link](https://start-link.jp/hubspot-ai/btob-marketing/web-ads-abm/linkedin-ads-btob-guide)
- 推奨規模: 最小300人、**推奨5万人以上**、Sponsored Content／Messagingは**30万人以上** — [LinkedIn Help a423690](https://www.linkedin.com/help/lms/answer/a423690)
- **Audience Expansion**: 狭いB2Bオーディエンスを関係の薄いユーザーまで広げてしまうため、**オフにする**のが推奨 — [B2Linked: 5 pitfalls](https://b2linked.com/blog-page/5-pitfalls-that-are-killing-your-linkedin-ads-performance)
- **LinkedIn Audience Network（LAN）**: まずオフで始め、試すならブロックリスト／許可リストを強めにかけて**別キャンペーンで独立して**検証する — [B2Linked: 3 pitfalls](https://b2linked.com/blog-page/stop-wasting-money-on-linkedin-ads-3-pitfalls-every-b2b-marketer-must-avoid)／[B2Linked: 11 best practices 2026](https://b2linked.com/blog-page/11-linkedin-ads-best-practices-for-better-performance-in-2026)
- LANの由来（2017年）: 数万のサイトやアプリ・広告取引所に配信し、予算を消化しやすくするのが狙い。ベータでは、ユニークインプレッションが3〜13%増え、ユニーククリックは最大80%増えた（LinkedIn発表）。特定のパブリッシャーは除外できる — [LinkedIn News 2017](https://news.linkedin.com/2017/9/linkedin-marketing-solutions-announces-the-linkedin-audience-net)／[TechCrunch 2017](https://techcrunch.com/2017/09/06/linkedin-raises-its-ad-tech-game-launches-audience-network-across-tens-of-thousands-of-sites-and-apps/?ncid=rss)
- SMB向けには、連絡先リストや広告するURLを渡すと**AIが自動でターゲティング**する機能が強化されている — [eMarketer](https://www.emarketer.com/content/linkedin-expands-access-ai-automation-smbs)
- 転職目的のユーザーも多く、ターゲティングに無駄が出やすいという指摘がある — [The UPS Store (blog)](https://www.theupsstore.ca/?p=27579)

### Inferences
- **日本での現実的な組み方（推論）**
  - 「地域: 日本」＋（職務: 人事 OR シニオリティ: 部長・役員・CXO・オーナー）＋会社規模（例: 201名以上）を1キャンペーンにまとめる。カテゴリ内の条件はOR（どれか一つに該当）なので、**職務・役職はOR条件で広めに選び**、5万人前後を確保する。
  - 職務と役職名（Job Title）を細かく掛け合わせると、日本ではすぐ数千人規模になり、配信が止まりやすい。
- **ABM（会社名リスト）**: 「既存顧客と似た大企業100〜300社」のリストは、300人の下限は超えやすい。ただし少額では、リスト単独よりリターゲティング用途（サイト訪問者、ドキュメント広告の閲覧者）と組み合わせるのが現実的。
- 中小企業の経営者狙い（会社規模1〜50名・オーナー）は、条件上は作れる。ただし日本ではLinkedInを使っていない層が多いとみられ、規模も質も不確実。

### Gaps
- **Predictive Audiences（LinkedInのAIが見込み度の高い人を予測して作るオーディエンス）**の日本での提供状況と仕様は、今回の検索では確認できなかった。背景知識では、LinkedInは2024年に類似（Lookalike）オーディエンスを廃止し、Predictive Audiencesに置き換えたとされる。これが正しければ、**富士電機事例（2022年）の「類似オーディエンス」はもう使えない可能性がある**。いずれも未検証。
- 2026年時点のオーディエンス拡大／LANのデフォルト設定（初期状態でオンか）は、公式ヘルプで未確認。

---

## Q4. LinkedInのコスト（日本のCPC・CPM・CPL）と、少額での入札戦略・落とし穴

### Takeaway
日本のLinkedIn広告のCPCは「1クリック300〜1,500円」という国内記事の目安しかなく、根拠は示されていない。非日本データでは、米国・英国のCPCは約9ドル、グローバル平均は約11ドル、APAC（ある代理店の顧客データ）は約1.55ポンド。少額で運用するなら、**手動CPC入札（またはコスト上限）＋オーディエンス拡大オフ＋Audience Networkオフ**が定石。「最大配信」はCPCが高くなりやすい。

### Cited Findings
**日本のコスト（出典の信頼性は低い）**
- CPCの相場は**300〜1,500円/クリック程度**で、他のSNS広告より高め（株式会社renueの解説。算出根拠なし）— [renue](https://renue.co.jp/posts/linkedin-ad)
- 日本の登録者は400万人超。CPCは他のSNS広告より高いが、リードの質は高い（具体額なし）— [start-link](https://start-link.jp/hubspot-ai/btob-marketing/web-ads-abm/linkedin-ads-btob-guide)
- 日本のLinkedIn広告で、ホワイトペーパー型のCPL実績（円）は**見つからなかった** — [検索結果要約: SaaS Hero 他](https://www.saashero.net/strategy/2026-linkedin-ads-cpa-benchmarks/)

**非日本ベンチマーク**
- 【グローバル】全キャンペーン・全オファー種別の平均CPCは**11.03ドル**（Metadata.io。対象年は「昨年」とあり不明確）— [Metadata.io](https://metadata.io/resources/blog/linkedin-ad-benchmarks/)
- 【国別・2026】CPC: 米国8.99ドル、英国9.16ドル、オランダ6.40ドル（CTRは0.52〜0.72%）— [ZenABM 2026](https://zenabm.com/blog/linkedin-ads-benchmarks-2026)
- 【APAC・2024・代理店1社の顧客データ】単一画像広告: CTR 0.56%、CPC 1.55ポンド（EMEAは3.98ポンド、AMERは1.37ポンド）。カルーセル広告はCPC 0.31ポンドだがサンプルが少ない — [Huble](https://www.huble.com/blog/linkedin-ads-benchmarks)
- 【CPL】中間ファネルで120〜250ドル、B2B SaaS全般で75〜350ドル。北米の平均は230ドル、中南米は60ドル（代理店・ベンダーの数値で、互いに食い違いあり）— [Cleverly (2025-08)](https://www.cleverly.co/blog/linkedin-lead-generation-cost)／[SaaS Hero](https://www.saashero.net/?p=9614)
- 【ROAS】SaaS向けの2026年調査: LinkedIn約113%、Google 98%。年間契約額（ACV）1万ドル以上の高単価B2Bに最も向く — [SaaS Hero 2026 CPA benchmarks](https://www.saashero.net/strategy/2026-linkedin-ads-cpa-benchmarks/)
- 【事例・非日本】LinkedIn公式のSAS事例: LinkedInのCPAは、最もCPAが高い媒体の10%で、ホワイトペーパーのダウンロードも最も多かった — [LinkedIn SAS case study (PDF)](https://business.linkedin.com/content/dam/me/business/en-us/marketing-solutions/products/pdfs/lms-sas-case-study.pdf)。英国の代理店事例: 年次レポートをリードマグネットにして**CPA -62%、高品質リード318件**（2025年3月）— [Figaro Digital](https://figarodigital.co.uk/case-studies/crown-worldwide-group-discover-how-overdrive-digital-delivered-318-high-value-leads-for-crown-world-mobility-cutting-cpa-by-62/)

**入札戦略・落とし穴**
- **最大配信（Maximum Delivery）は、ほぼ確実に必要以上のCPCを払うことになる**。手動CPCに切り替えて入札額を自分で管理すれば、CPCを大きく下げられる（B2Linked／AJ Wilcox系）— [B2Linked](https://b2linked.com/blog-page/5-pitfalls-that-are-killing-your-linkedin-ads-performance)
- **入札は控えめに始め（北米なら約7ドル）、予算を消化しきれない場合だけ少しずつ上げる**（AJ Wilcoxの助言を引用）— [Dreamdata](https://dreamdata.io/blog/10-underutilized-linkedin-ads-best-practices)
- 予算を増やしてよいのは、成果が出ていてさらに広げたいときだけ — [Breadcrumbs (AJ Wilcox)](https://breadcrumbs.io/?p=8922)
- 1日50ドル程度の少額では、判断に足るデータがほとんど集まらない — [ZenABM](https://zenabm.com/blog/linkedin-ads-campaign-setup-mistakes-max-herczeg)
- CPCだけで判断せず、CPA・CPO（受注単価）・パイプラインへの貢献で評価すべき — [seo-day.de](https://www.seo-day.de/news/article/linkedin-ads-cpc-benchmarks-how-costs-compare-with-google?lang=en)／[B2Linked: CPM/CPC](https://b2linked.com/blog-page/linkedin-cpms-cpcs-what-you-should-really-focus-on)
- 公式の入札APIによると、「最低入札額」を下回るとSponsored Updateは配信が弱くなる。他の形式では最低入札額より下には設定できない — [Microsoft Learn: Ad Budget Pricing](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ad-budget-pricing?view=li-lms-2023-10)

### Inferences
- **月予算ごとの試算（推論。前提: CPC 500〜1,500円、リード獲得フォームの送信率5〜15%）**
  - CPL ≒ CPC ÷ 送信率なので、**約3,300円〜30,000円**。非日本のCPL（75〜350ドル、1ドル＝150円換算で約1.1万〜5.3万円）と比べ、日本の上限側（1万〜3万円台）を基準に見込むのが安全。
  - **月10万円 → 月3〜15件程度**のリード、**月30万円 → 10〜45件程度**、**月50万円 → 15〜75件程度**が目安。ただしドキュメント広告の「資料ダウンロードリード」は、商談化率がウェビナー申込や相談申込より低い点に注意。
- **少額での設定（推論）**
  - ①目的は「リード獲得」。②**手動CPC**（またはコスト上限）にし、推奨入札額の下限付近から開始。③Audience ExpansionとAudience Networkは**オフ**。④1日予算は最低額の2〜3倍（1日3,000〜5,000円）を1キャンペーンに集中させる。⑤クリエイティブは2〜4本をローテーション。
  - 月10万円では「キャンペーン1本＋リターゲティング1本」で上限。
- 研修・コンサル案件の受注単価（数十万〜数百万円）を考えると、CPL 2〜3万円でも、商談化率と受注率次第で投資を回収できる余地はある（推論）。

### Gaps
- 日本アカウントのCPC・CPM・CPLの信頼できる一次データ（年・業種別）は見つからなかった。renueの「300〜1,500円」は根拠不明。
- 2026年時点のコスト上限（Cost cap）とターゲットコストの仕様や挙動について、少額アカウントでの比較データはない。
- 日本特有の落とし穴（英語プロフィールの外国人に配信が漏れる、転職者に配信が偏るなど）を定量的に示す資料は見つからなかった。

---

## Q5. 日本のX広告: 利用者数・ビジネス層・フォーマット・ターゲティング・コスト・2023年以降の変化（ブランドセーフティ）・Xプレミアム・CV計測

### Takeaway
日本のXは、公式発表で月間利用者6,700万人（2023年平均）、第三者推計で約6,900万〜7,100万人の巨大市場。CPCは数十〜200円程度と安い（いずれも根拠は弱い）。最低出稿金額はない。**ネイティブのリード獲得広告（Lead Gen Ads）は2026年8月下旬に世界で開始**されたが、まず代理店経由（マネージドサービス）の広告主向けで、**セルフサーブの中小広告主への提供は段階的**。広告プラットフォームは2026年4月から全面的に作り直されている。ブランドセーフティへの不信（MRCの監査から撤退）は続いている。

### Cited Findings
**利用者数**
- X Corp. Japan（旧Twitter Japanから社名変更、告知は2024年11月）: 日本の**月間利用者数は6,700万人（2023年平均）** — [すまほん!!](https://smhn.info/202411-twitter-japan-x-corp-japan)
- Statista: 2025年10月時点で日本は**7,100万人超**で、米国（約9,904万人）に次ぐ2位 — [Statista](https://www.statista.com/statistics/242606/number-of-active-twitter-users-in-selected-countries/?code=xxx)／[DataReportal](https://datareportal.com/essential-x-stats)
- 他の推計: 6,930万人（Statistaを引用、時点不明）、6,800万人（推計値と明記）— [Charle Agency](https://charleagency.com/articles/twitter-x-statistics/)／[japanbuzz](https://japanbuzz.info/data/social-media-platforms-in-japan/)
- X Corp. Japanの2026年の公式な国内利用者数は見つからなかった — [検索結果要約](https://countly.net/how-many-x-users-are-there-in-2026-global-by-country)（この推計サイトの数値は表の中で矛盾しており、引用には不向き）
- XのCEOが日本にエンジニアチームを置く方針を表明（日本市場の重視）— [Verdict](https://www.verdict.co.uk/x-to-set-up-engineering-team-in-japan-says-ceo/)

**広告フォーマット・目的**
- **Lead Gen Ads（リード獲得広告）**: Xアプリ内でネイティブフォームが開き、2回目以降は入力済みの状態で表示される。広告マネージャーに専用の「**Leads**」目的と「**Leads Center**」（リードの書き出し・CRM連携）が追加された。画像・動画・カルーセル・タイムラインテイクオーバーに対応 — [WeRSM](https://wersm.com/x-brings-back-lead-gen-ads/)／[find-model.jp](https://find-model.jp/insta-lab/x-revives-lead-gen-ads/)／[The Media Leader FR](https://fr.themedialeader.com/?p=217948)
- 提供開始は**2026年8月下旬**。**マネージドサービスの広告主には全世界ですぐ提供**、中小企業とセルフサーブには段階的に提供（セルフサーブは「後日」）— [Media News4U (2026-08-28)](https://www.medianews4u.com/x-introduces-lead-gen-ads-to-help-brands-capture-leads-within-the-platform/)／[La Reclame (2026-08-27)](https://lareclame.fr/x-lead-gen-ads-340224)／[ATDb](https://www.theatdb.com/news/x-announces-global-launch-of-lead-gen-ads)
- ウェブサイトコンバージョン目的のキャンペーン（X Pixelと連携）— [X Business: Website conversions](https://business.x.com/en/advertising/campaign-types/website-conversions)

**広告プラットフォームの全面改修（2026年）**
- X自身が「**20年の歴史で最も野心的な広告プラットフォームの刷新**」と表現。ゼロから作り直し、2026年4月30日ごろから段階的に展開（5月発表とする報道もある）。柱は「シンプルさ・広告主によるコントロール・AIによるパフォーマンス」。**文脈・意味ベースのターゲティング**（AIによる検索とランキング）が中心 — [PPC Land](https://ppc.land/x-rebuilds-its-entire-ad-platform-from-scratch-in-a-20-year-first/)／[Campaign](https://campaignlive.com/article/x-kicks-off-global-overhaul-ad-platform/1956818)／[MediaPost](https://www.mediapost.com/publications/article/414738/x-overhauls-ad-platform-again-this-time-via-ai.html)
- Grokに広告戦略を相談でき、クリエイティブも生成するアシスタントを一部の広告主にベータ提供（ターゲティング機能ではない）— [Global Dating Insights](https://www.globaldatinginsights.com/featured/x-integrates-grok-ai-into-ads-manager-for-enhanced-advertising-tools/)

**ターゲティング**
- **キーワード**: 特定のキーワードで検索・投稿・反応したユーザーに配信（X広告の紹介資料）— [ppc-master X広告紹介資料 (PDF, 2026)](https://ppc-master.jp/cms2025/wp-content/uploads/2026/05/【公式】X旧Twitter広告紹介資料.pdf)
- **フォロワー類似**: 自社のフォロワーに近いユーザーに配信。**類似度を高くするほどCPAが上がりやすい**ので、少額のテスト予算で効果を測る — [renue: X広告ROI BtoB 2026](https://renue.co.jp/posts/x-ads-roi-btob-optimization-2026)／[app-tatsujin](https://app-tatsujin.com/x-advertising-guide-2026/)
- **会話トピック**: 特定の話題が盛り上がっている文脈に配信（BtoBでの成果データはない）— [kwm.co.jp](https://www.kwm.co.jp/service/ad-agency/twitter-ads/)
- X Pixelのベースコードは、イベント計測と**オーディエンス作成（リターゲティングなど）**の土台 — [X Business Help: Conversion tracking](https://business.x.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites)

**コスト（いずれも根拠は弱い）**
- **最低出稿金額はない**。日予算を設定すれば、その額を超えて課金されない（X Businessヘルプを根拠とする2026年8月時点の解説）— [TaTap](https://tatap.jp/knowledge/x-ads-cost-guide/)
- CPC: 一般的な目安は**約40〜90円**（2026年、BtoB限定ではなく算出方法も不明）— [app-tatsujin](https://app-tatsujin.com/?p=24567)。業界別では飲食35〜120円、EC45〜180円、**SaaS 60〜200円** — [app-tatsujin](https://app-tatsujin.com/?p=23384)
- CPM: ¥120〜180（税別）とする記事と、飲食業で約450円とする記事があり、数値が一致しない — [mobinc.jp](https://mobinc.jp/column/?p=1593)／[app-tatsujin](https://app-tatsujin.com/?p=68233)
- BtoBは「リード単価1,500円以下なら営業ROIがプラスになるケースが多い」とする記述がある。ただし根拠は不明で、BtoB高単価商材の実態とは合わない可能性が高い — [app-tatsujin](https://app-tatsujin.com/?p=24567)
- 参考（**X広告ではなく検索広告**）: 業種別CPL相場はIT・SaaS（高単価）約25,000円、金融・保険（法人）約35,000円 — [start-link CPL benchmark](https://start-link.jp/hubspot-ai/btob-marketing/web-ads-abm/cpl-benchmark-btob-guide)

**ブランドセーフティ・広告主の動向（2023年以降）**
- XはMRC（第三者認定機関）の**ブランドセーフティ認定のための外部監査から撤退**し、再開は未定（2023年の報道）— [livedoor（Business Insider系）](https://news.livedoor.com/article/detail/25423405/)
- 2023年の広告収入は、世界で前年比54.4%減と予測されていた（Insider Intelligence）— [livedoor](https://news.livedoor.com/article/detail/25423405/)
- Kantar（2024年9月）: マーケターの**26%が2025年にXへの広告費を減らす**計画。Xがブランドセーフティを提供していると考える人は**4%**（Googleは39%）— [Business Insider Japan](https://www.businessinsider.jp/article/274015/)／[Impress Internet Watch](https://internet.watch.impress.co.jp/docs/yajiuma/1622568.html)
- X側の主張: 上位100広告主（2023年）の約90%が今も出稿し、半数は前年より支出を増やした（独立した検証はない）— [livedoor](https://news.livedoor.com/article/detail/27301128/)
- 日本の広告主の離脱状況を示す一次データは見つからなかった — 同上の検索結果要約

**Xプレミアム・認証との関係**
- 2025年10月、旧「認証済み組織」は企業向けの「**Xプレミアムビジネス**」と、政府・国際機関向けの「プレミアム組織」に名称変更 — [roboin.io (2025-10-09)](https://roboin.io/article/2025/10/09/x-verified-organizations-rebranded-as-x-premium-business-and-premium-organizations/)／[X Help: プレミアム組織](https://help.x.com/ja/using-x/premium-organizations)
- Xプレミアムビジネスのヘルプには**広告クレジットの規約**がある（2026年4月1日最終更新）。広告ポリシーに違反するとクレジットが無効になることがあり、過去に違反したアカウントは対象外になりうる — [X Help: Premium Business 広告規約](https://help.x.com/ja/using-x/premium-business/ads-terms.html)／[X Help: Premium Business](https://help.x.com/ja/using-x/premium-business)
- プランの段数は、記事（ベーシック・フルアクセス・エンタープライズの3段）と公式ヘルプ（2段）で**食い違う** — [roboin.io](https://roboin.io/article/2025/10/09/x-verified-organizations-rebranded-as-x-premium-business-and-premium-organizations/)／[X Help](https://help.x.com/ja/using-x/premium-business)

**コンバージョン計測（X Pixel／CAPI）**
- X Pixelは、全ページに入れるベースコードと、特定の行動を計測するイベントコードの2部構成。Xは多くの広告主にPixelを推奨し、JavaScriptを実装できない場合はConversion API（CAPI、サーバーから直接成果を送る方式）を勧めている — [X Business Help](https://business.x.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites)
- CAPIでは、少なくとも1つのファネル下部のイベントを設定する必要がある。サーバーサイドAPIの権限を有効化し、データソースを接続しないと送信されない — [Datahash](https://www.datahash.com/docs/x-twitter-online-conversions-api/)
- 管理画面で「1st party cookieを許可」をオンにすると計測精度が上がる。X Pixel Helperで発火を確認できる。イベントマネージャーでは過去24時間に動きがあると「Active」と表示される — [Funnelfox](https://funnelfox.com/docs/integrations/tracking/set-up-x-pixel)／[X Business Help](https://business.x.com/help/conversion-tracking-for-websites)
- アトリビューション期間のデフォルト例: エンゲージメント後30日、表示後1日 — [Funnelfox](https://funnelfox.com/docs/integrations/tracking/set-up-x-pixel)
- Pixel（ブラウザ）とCAPI（サーバー）を併用する場合は、コンバージョンIDで重複計上を防ぐ — [mypresta](https://mypresta.rocks/x-pixel-web-conversions-api)

### Inferences
- **2026年10月時点で、少額のセルフサーブ広告主は「Leads」目的を使えない可能性が高い**（推論）。その場合は「ウェブサイトコンバージョン」目的＋X Pixel（＋可能ならCAPI）でLPのフォーム送信を最適化する形になる。管理画面にLeads目的が出ているかを、まず確認する。
- CPCは数十〜200円とLinkedInの数分の一だが、Xは一般消費者が大半の媒体なので、BtoBの意思決定者以外にも多く配信されやすい。**クリック単価の安さがそのままリードの質の高さにはつながらない**。キーワードターゲティング（例: 「人的資本開示」「インボイス」「36協定」「管理職研修」など、実務の課題語）で、関心を持っている層に絞るのが要。
- 1ドル単位の少額から出せ、最低出稿額もない。月10万円でもCPC 100円なら約1,000クリックで、検証に必要な量は集まる（推論）。
- ブランドセーフティへの懸念（MRC監査からの撤退、不適切な投稿の隣に広告が出るリスク）は、士業や企業研修のように「信頼」が商品の業種では評判リスクになる。除外キーワードや配信面の設定で抑える必要がある（推論）。
- 「Xプレミアムビジネスの広告クレジット」は、プランの金額と付与額次第では実質的な広告費の補助になりうる。ただし付与額は未確認（推論）。

### Gaps
- 日本でのLead Gen Adsの**提供状況（日本語UI・セルフサーブで使えるか）**は、2026年10月時点で未確認。
- X広告の2026年の改修後に、従来のターゲティング（フォロワー類似・会話トピック・興味関心）が**どこまで残っているか**の公式情報は確認できなかった。
- 日本のBtoB向けX広告のCPC・CPM・CPAの信頼できる一次データはない（国内の解説記事は数値が互いに矛盾）。
- 日本のXユーザーのうちビジネス層（経営者・人事など）の比率データはない。
- Xプレミアム加入者への広告表示がどう扱われるか（表示が減るのか）、Premium Businessの広告クレジットの金額は未確認。
- 背景知識では、Xは2025年に広告でのハッシュタグ使用を禁止したとされるが、今回は出典で確認できていない。

---

## Q6. 日本のコンサル・研修・士業によるXのBtoB活用（自然投稿＋広告、代表者アカウントの投稿の広告化）と事例

### Takeaway
「コンサル・研修・士業がX広告でBtoBリードを獲得した」具体的な事例（CPAつき）は、**公開情報からは見つからなかった**。見つかったのは、BtoB向けX運用代行会社の自社紹介と、ゼンリンのような大企業の自然投稿の例のみ。代表者や「中の人」の自然投稿で信頼を積み、反応の良い投稿を広告で伸ばす使い方は、運用代行会社が提唱する型にとどまる。

### Cited Findings
- BtoB向けX運用代行（Semuis）は、**CXO（経営者）・営業責任者・インサイドセールス・採用責任者などの個人アカウント**の特徴を生かして成果を出す設計を打ち出している。ノウハウを社内に残して内製化する方針も掲げる — [kigyolog: Semuis](https://kigyolog.com/tool.php?id=3720)
- 運用代行会社の顧客の声（BtoB SaaS・製造業）: 「フォロワー数ではなく売上で会話してくれる」「Xからの問い合わせが増えた」（広告主側の自己申告）— [stock-sun](https://stock-sun.com/?p=158402)
- ゼンリンはBtoB企業ながら、Xでユーザーを巻き込む企画が話題になり、2025年にKADOKAWAから書籍化された（フォロワー11.6万超。自然投稿の事例）— [digitalpr.jp](https://digitalpr.jp/r/135455)
- BtoBで推奨するSNSとして、IT・SaaSはX、製造業はTikTokとする記述があり、BtoB企業のSNS活用率は2023年42%→2025年67%とされる（いずれも運用代行会社の記事で、根拠の明示なし）— [s--line.co.jp](https://s--line.co.jp/btob-sns-marketing/)
- 【海外B2B調査】満足度の高い媒体はLinkedIn（89%）、次いでX（78%）。日本の経営者層の調査ではない — [ecnomikata](https://ecnomikata.com/pr/12823/)
- 国内BtoBマーケ担当者の調査（2025年3月、n=326）: 約半数がCPAの高騰を実感し、セミナー・ウェビナーの上昇が最も大きい。半数以上が「SNS」「SEO」を強化する方針 — [Web担当者Forum / PR TIMES](https://webtan.impress.co.jp/r/prtimes/items/000000379.000045863)
- IDEATECHの2026年調査（n=100）: 約4割がこの1年でCPAが上がったと感じ、高騰した施策のトップは**セミナー・ウェビナー（58.3%）**。目標CPAと実績CPAを把握していない担当者が37% — [PR TIMES](https://prtimes.jp/main/html/rd/p/000000642.000045863.html)
- 【LinkedIn側の類似手法】Thought Leader Adsで役員やコンサルタントの個人投稿を広告にできる（Q2参照）— [CMSWire](https://www.cmswire.com/the-wire/companies-can-now-promote-member-content-with-linkedin-thought-leader-ads)

### Inferences
- **Xでの勝ち筋の仮説（推論）**
  - ①代表者や講師の実名アカウントで、法改正の解説・人事施策の論点・研修の一場面などを継続して投稿し、反応のよかった投稿を「プロモ投稿」として、キーワードやフォロワー類似のターゲットに広告配信する。
  - ②プロフィールと固定ポストから、無料ウェビナーや資料ダウンロードのLPへ誘導する。
  - ③LP訪問者を、X Pixelでリターゲティングする。
- 中小企業の経営者・個人事業主・人事担当者（特に中小企業）・士業同士の紹介ネットワークは、LinkedInよりXのほうが日本では人数が多いとみられる（根拠はQ1・Q5の利用者数の差。推論）。
- ウェビナーのCPAは国内で上がっている（2025〜2026年調査）。安価なX広告でウェビナー申込を取りに行く余地はある。ただし参加率と商談化率まで追わないと判断を誤る（推論）。

### Gaps
- 日本のコンサル・研修会社・士業事務所が、X広告やLinkedIn広告で獲得したリード件数・CPA・受注の実績事例は、公開情報では見つからなかった。「士業 X 集客 セミナー」「税理士 社労士 X 問い合わせ 実績」などの追加調査が必要。
- XでBtoBの代表者投稿を広告化した日本の公式ケーススタディ（X Japan発表）は見つからなかった。

---

## Q7. 判断の目安: 月10〜50万円で、LinkedInが合う／合わない条件、Xが合う条件

### Takeaway
**LinkedIn**: ターゲットが「外資系・大企業・IT企業の人事、L&D（人材開発）、経営企画、役員」で、案件単価が高い（研修・コンサルで年数百万円以上）なら、月20〜30万円以上でテストする価値がある。月10万円では検証がやっとで、**中小企業経営者や地方企業がターゲットなら優先度は低い**。**X**: 中小企業の経営者・士業・個人事業主・現場の人事担当者まで広く届き、少額でも量が出る。ただしリードの質とブランドセーフティの管理が課題で、2026年10月時点ではネイティブのリード獲得広告をセルフサーブで使えない可能性がある。

### Cited Findings
- 広告予算が限られる中小企業は、大企業並みの広告費がないなら、会社ページを問い合わせの入口として期待しすぎるべきではない — [Alexandra Samuel (WSJ)](https://www.alexandrasamuel.com/wsj-clip/why-small-businesses-are-getting-linkedin-wrong)
- 小規模組織は、大規模組織よりLinkedInの機能を使う度合いが大幅に低い（学術研究、2014年）— [Georgia Southern AMTP 2014](https://digitalcommons.georgiasouthern.edu/amtp-proceedings_2014/48)
- LinkedInは年間契約額（ACV）1万ドル以上の高単価B2Bに最も向く（SaaSの調査）— [SaaS Hero](https://www.saashero.net/strategy/2026-linkedin-ads-cpa-benchmarks/)
- 日本のLinkedInは「認知は約6割だが利用頻度は低い」— [Web担当者Forum](https://webtan.impress.co.jp/n/2025/10/10/50187)
- Xの日本の利用者は6,700万人（2023年平均、公式）、7,100万人超（Statista、2025年10月）— [すまほん!!](https://smhn.info/202411-twitter-japan-x-corp-japan)／[Statista](https://www.statista.com/statistics/242606/number-of-active-twitter-users-in-selected-countries/?code=xxx)
- X広告は最低出稿金額がなく、CPCは数十〜200円程度（根拠は弱い）— [TaTap](https://tatap.jp/knowledge/x-ads-cost-guide/)／[app-tatsujin](https://app-tatsujin.com/?p=23384)
- X Lead Gen Adsは、マネージドサービスの広告主から順に提供（2026年8月下旬〜）— [La Reclame](https://lareclame.fr/x-lead-gen-ads-340224)
- XのブランドセーフティについてはMRC監査から撤退済み。Kantar調査でブランドセーフと考えるマーケターは4% — [livedoor](https://news.livedoor.com/article/detail/25423405/)／[Business Insider Japan](https://www.businessinsider.jp/article/274015/)
- LinkedInはSMB向けにAIによる自動ターゲティングを強化しており、少額の広告主が使いやすくなっている — [eMarketer](https://www.emarketer.com/content/linkedin-expands-access-ai-automation-smbs)

### Inferences（判断表の案。すべて推論）

| 条件 | LinkedIn | X |
|---|---|---|
| ターゲットが外資系・大企業（従業員300名以上）の人事・L&D・経営企画・役員 | ◎ 主力候補。ドキュメント広告＋リード獲得フォーム。月20万円以上推奨 | △ ターゲットを絞れない。補助的に使う |
| ターゲットがIT・SaaS企業の経営者・事業責任者 | ○ | ○ キーワード＋フォロワー類似 |
| ターゲットが中小企業の経営者（非IT・地方） | × 母数が小さく、質も不確実 | ○ キーワード（補助金・労務・税務・事業承継など）＋代表者アカウントの投稿の広告化 |
| 士業（税理士・社労士・行政書士・弁護士）の法人顧客開拓 | △ 大企業の法務・人事向けのみ | ○ 法改正ネタは反応が取りやすい（推論）。ただし広告規制や品位規定に注意 |
| 月10万円 | △ 1キャンペーンで検証。リード月数件の想定 | ○ 1,000クリック前後まで出せる |
| 月30〜50万円 | ○ フォーマット比較とリターゲティングまで回せる | ○ 量を出し、LP改善で質を担保する |
| 案件単価（LTV）が100万円未満 | × CPLが割に合わない可能性 | ○ |
| 案件単価が数百万円以上（企業研修の年間契約、コンサル） | ◎ | ○ 認知・ウェビナー集客用 |

- **推奨の進め方（推論）**: 月30万円なら、まず「X 10〜15万円（代表者投稿の広告化＋ウェビナー・資料LP）」と「LinkedIn 15〜20万円（ドキュメント広告＋リード獲得フォーム、大企業の人事・L&D向け）」で2〜3か月テストする。CPLではなく「**商談化単価**」で比べて、配分を決める。月10万円なら、ターゲットに合う方の**1媒体に集中**する（中小企業経営者ならX、大企業人事ならLinkedIn）。
- どちらの媒体も、計測（LinkedIn Insight Tag・X Pixel・CRMでのUTM管理）を先に整えておかないと、少額では判断できない（推論）。

### Gaps
- 日本で「LinkedIn対X」をBtoBのリード獲得で比べた、信頼できる調査や事例はない。
- 士業広告の業界規制（弁護士・税理士などの広告規程）とSNS広告の関係は、今回の調査範囲外。
- 2026年10月時点の円相場によるドル→円換算は、仮に1ドル≒150円としている（未確認）。
