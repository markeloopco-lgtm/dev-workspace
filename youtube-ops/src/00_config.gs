/**
 * 00_config.gs
 * 全体設定。シート名・列定義・ステータス・既定しきい値をここに集約する。
 * しきい値は「04_チャンネル設定」の行ごとの値で上書きできる。
 *
 * 機密情報（クライアントID/シークレット、APIキー、トークン）はシートに置かず、
 * スクリプトプロパティ（プロジェクトの設定 > スクリプト プロパティ）で管理する:
 *   OAUTH_CLIENT_ID     … GCPのOAuthクライアントID
 *   OAUTH_CLIENT_SECRET … GCPのOAuthクライアントシークレット
 *   GEMINI_API_KEY      … Gemini APIキー（AI概要欄生成用）
 *   GEMINI_MODEL        … （任意）使用モデル。省略時は CONFIG.GEMINI_MODEL_DEFAULT
 *   IMP_METRICS         … （任意）インプレッション系メトリクス名の上書き
 */

const CONFIG = {
  // 分析対象: 公開からこの日数以内の動画を毎日更新する（=「過去3ヶ月・90日分」）
  ANALYSIS_WINDOW_DAYS: 90,

  // 毎日の分析実行時刻（時・日本時間）
  DAILY_HOUR: 5,

  // 投稿キュー（アップロード・AI生成・承認反映）の監視間隔（分）
  QUEUE_INTERVAL_MIN: 10,

  // GASの6分実行制限対策: この時間を超えたら中断して自動継続する
  TIME_BUDGET_MS: 4.5 * 60 * 1000,

  // 動画アップロードのチャンクサイズ（256KBの倍数であること）
  UPLOAD_CHUNK_BYTES: 16 * 1024 * 1024,

  // 予約日時が空欄のときの扱い: 'private'（非公開でアップのみ） or 'public'（即公開）
  DEFAULT_PRIVACY_WHEN_NO_SCHEDULE: 'private',

  // 予約日時は最低これだけ先であること（分）
  MIN_SCHEDULE_LEAD_MIN: 10,

  // 子ども向けフラグ（selfDeclaredMadeForKids）
  MADE_FOR_KIDS: false,

  // カテゴリ未指定時のYouTubeカテゴリID（22 = ブログ・人物）
  DEFAULT_CATEGORY_ID: '22',

  // AIモデル（Gemini・無料枠で利用可）
  GEMINI_MODEL_DEFAULT: 'gemini-2.0-flash',

  // 投稿キューが「下書き」を見つけたら自動でAI概要欄を生成するか
  AUTO_GENERATE_IN_QUEUE: true,

  // サムネイルの上限サイズ（YouTube仕様: 2MB）
  THUMB_MAX_BYTES: 2 * 1024 * 1024,

  // インプレッション/サムネCTRのメトリクス名（2026-01-15にAnalytics APIへ追加）
  // 取得できない環境ではCTR列が空欄になり、CTR系の自動判定はスキップされる
  IMP_METRICS: 'videoThumbnailImpressions,videoThumbnailImpressionsClickRate',

  // CTRメトリクスが 0〜1 の比率で返る場合 true（%に換算する）
  CTR_IS_RATIO: true,

  // チャンネル平均CTRの算出対象: 直近何本の動画を使うか
  CTR_BASELINE_COUNT: 10,

  // 日次ログの最大行数（超えた分は古い順に削除。5ch×90動画×1年 ≒ 16万行になるため）
  DAILY_LOG_MAX_ROWS: 50000,

  // エラーログの最大行数
  ERROR_LOG_MAX_ROWS: 2000,
};

/** 自動判定の既定しきい値（04_チャンネル設定の列が空欄のときに使う） */
const DEFAULT_THRESHOLDS = {
  CTR_LOW_RATIO: 0.8,   // CTRがチャンネル平均のこの倍率未満なら「低CTR」
  CTR_DROP_PT: 0.5,     // 7日前と比べCTRがこのポイント以上下がったら「CTR低下」
  HIT_MULT: 2,          // 7日再生がチャンネル平均のこの倍率以上なら「ヒット」
  SPIKE_MULT: 3,        // 日次再生が直近7日平均のこの倍率以上なら「急上昇」
  IMP_MULT: 1.5,        // インプレッションが平均のこの倍率以上あれば「表示は足りている」
  WEAK_VIEW_RATIO: 0.5, // 90日再生が中央値のこの倍率未満なら「再生弱い」
};

/** シート名 */
const SHEETS = {
  DASHBOARD: '00_ダッシュボード',
  POSTS: '01_投稿管理',
  VIDEOS: '02_動画分析',
  DAILY: '03_日次ログ',
  CHANNELS: '04_チャンネル設定',
  TEMPLATES: '05_概要欄テンプレ',
  ERRORS: '06_エラーログ',
};

/** 運用モード */
const MODES = {
  ANALYTICS: 'ANALYTICS', // 分析のみ
  DRAFT: 'DRAFT',         // 分析＋AI生成まで
  FULL: 'FULL',           // 分析＋AI生成＋YouTube投稿まで
};

/** 承認モード */
const REVIEW_MODES = {
  REVIEW: 'REVIEW', // AI生成後に人間が承認チェックを入れてから投稿
  AUTO: 'AUTO',     // 完全自動（生成後そのまま投稿キューへ）
};

/** 01_投稿管理のステータス */
const STATUS = {
  DRAFT: '下書き',
  GENERATED: '生成済み',        // DRAFTチャンネルの終着点
  WAITING_REVIEW: '確認待ち',   // REVIEWモード: 承認チェック待ち
  QUEUED: '投稿待ち',
  UPLOADING: 'アップロード中',
  UPLOAD_RESUME: 'アップロード中(継続)',
  SCHEDULED: '予約済み',
  PUBLIC: '公開済み',
  PRIVATE_DONE: 'アップ済(非公開)',
  ERROR: 'エラー',
};

/** 自動判定ラベル */
const JUDGE = {
  THUMB_IMPROVE: 'サムネ改善候補',
  THUMB_SWAP: 'サムネ差し替え優先',
  IMP_SHORTAGE: 'インプレッション不足',
  HIT: 'ヒット',
  CTR_DROP: 'CTR低下',
  SPIKE: '急上昇',
};

/** 列定義（1始まり）。ヘッダー配列と必ず一致させること */
const COL = {
  POSTS: {
    ID: 1, CHANNEL: 2, TITLE: 3, VIDEO_URL: 4, THUMB_URL: 5, PUBLISH_AT: 6,
    CATEGORY: 7, TAGS: 8, SCRIPT: 9, DESC: 10, PIN_COMMENT: 11, APPROVED: 12,
    STATUS: 13, VIDEO_ID: 14, YT_URL: 15, DONE_AT: 16, ERROR: 17,
  },
  CHANNELS: {
    KEY: 1, NAME: 2, CHANNEL_ID: 3, MODE: 4, REVIEW: 5, TEMPLATE: 6, ENABLED: 7,
    AUTH_STATUS: 8, AUTH_URL: 9, CTR_LOW: 10, CTR_DROP: 11, HIT_MULT: 12,
    SPIKE_MULT: 13, IMP_MULT: 14, NOTE: 15,
  },
  VIDEOS: {
    VIDEO_ID: 1, CHANNEL: 2, CH_NAME: 3, TITLE: 4, PUBLISHED: 5, URL: 6,
    AGE_DAYS: 7, VIEWS_TOTAL: 8, VIEWS_90D: 9, IMPRESSIONS: 10, CTR: 11,
    AVG_DUR: 12, AVG_PCT: 13, WATCH_MIN: 14, SUBS: 15, LIKES: 16, COMMENTS: 17,
    D1_DELTA: 18, D3_DELTA: 19, D7_DELTA: 20,
    V24H: 21, V3D: 22, V7D: 23, V30D: 24,
    SUB_EFF: 25, JUDGE: 26, JUDGE_MEMO: 27, UPDATED: 28,
  },
  DAILY: {
    DATE: 1, CHANNEL: 2, VIDEO_ID: 3, TITLE: 4, VIEWS_TOTAL: 5, VIEWS_DELTA: 6,
    IMPRESSIONS: 7, CTR: 8, AVG_PCT: 9, WATCH_MIN: 10, SUBS: 11, LIKES: 12,
    COMMENTS: 13,
  },
  TEMPLATES: {
    ID: 1, NAME: 2, HEADER: 3, FOOTER: 4, HASHTAGS: 5, CTA: 6, TONE: 7, CHAPTERS: 8,
  },
  ERRORS: { AT: 1, TYPE: 2, CHANNEL: 3, TARGET: 4, MESSAGE: 5, DETAIL: 6 },
};

/** ヘッダー行（COLと同順） */
const HEADERS = {
  POSTS: [
    '投稿ID', 'チャンネルキー', 'タイトル', '動画DriveURL', 'サムネDriveURL',
    '予約公開日時', 'カテゴリID', 'タグ(カンマ区切り)', '台本・文字起こし',
    'AI概要欄', '固定コメント案', '承認', 'ステータス', 'videoId', 'YouTube URL',
    '投稿完了日時', 'エラー内容',
  ],
  CHANNELS: [
    'チャンネルキー', 'チャンネル名', 'YouTubeチャンネルID', 'モード', '承認モード',
    'テンプレID', '有効', '認証状態', '認証URL', 'CTR改善閾値(%)', 'CTR低下警告(pt)',
    'ヒット倍率', '急上昇倍率', 'インプ判定倍率', '備考',
  ],
  VIDEOS: [
    'videoId', 'チャンネルキー', 'チャンネル名', 'タイトル', '公開日時', 'URL',
    '経過日数', '再生回数(累計)', '再生(90日)', 'インプレッション(90日)', 'CTR(%)',
    '平均視聴時間(秒)', '平均視聴率(%)', '総再生時間(分)', '登録者増(90日)',
    '高評価', 'コメント数', '前日比再生', '3日増加', '7日増加',
    '24h再生', '3日再生', '7日再生', '30日再生',
    '登録獲得効率(/千再生)', '判定', '判定メモ', '最終更新',
  ],
  DAILY: [
    '日付', 'チャンネルキー', 'videoId', 'タイトル', '再生回数(累計)', '日次再生',
    'インプレッション(90日)', 'CTR(%)', '平均視聴率(%)', '総再生時間(分)',
    '登録者増(90日)', '高評価(累計)', 'コメント(累計)',
  ],
  TEMPLATES: [
    'テンプレID', 'テンプレ名', '固定ヘッダー', '固定フッター', '固定ハッシュタグ',
    'CTA文', 'トンマナ・AI追加指示', 'チャプター生成',
  ],
  ERRORS: ['日時', '種別', 'チャンネルキー', '対象', 'メッセージ', '詳細'],
};
