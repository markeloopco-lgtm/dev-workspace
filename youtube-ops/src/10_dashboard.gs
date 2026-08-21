/**
 * 10_dashboard.gs
 * 00_ダッシュボード: 全チャンネルの状況を一画面に集約する。
 * 上段: チャンネル別サマリー表 / 下段: 判定付き動画（要改善・ヒット・急上昇）の一覧
 */

const DASH_HEADERS = [
  'チャンネル', 'モード', '認証', '登録者数', '90日再生', '平均CTR(%)',
  '投稿本数(90日)', '直近7日再生', '要改善', 'ヒット', '急上昇', '投稿予定', 'エラー',
];
const DASH_LIST_HEADERS = ['チャンネル', 'タイトル', '判定', 'メモ', 'URL'];

/** メニュー/分析後: ダッシュボードを組み立て直す */
function rebuildDashboard() {
  const sh = sheet_(SHEETS.DASHBOARD);
  const c = COL.VIDEOS;
  const pc = COL.POSTS;
  const channels = getChannels_();
  const videos = sheetData_(sheet_(SHEETS.VIDEOS));
  const posts = sheetData_(sheet_(SHEETS.POSTS));

  // チャンネル別に集計
  const rows = [];
  let flaggedList = [];
  channels.forEach(function (ch) {
    const vs = videos.filter(function (r) { return String(r[c.CHANNEL - 1]) === ch.key; });
    const within90 = vs.filter(function (r) { return numOr_(r[c.AGE_DAYS - 1], 999) <= CONFIG.ANALYSIS_WINDOW_DAYS; });
    const views90 = within90.reduce(function (a, r) { return a + numOr_(r[c.VIEWS_90D - 1], 0); }, 0);
    const avgCtr = avg_(within90.map(function (r) { return numOr_(r[c.CTR - 1], null); }));
    const last7 = within90.reduce(function (a, r) { return a + numOr_(r[c.D7_DELTA - 1], 0); }, 0);
    const judgeCount = function (label) {
      return within90.filter(function (r) { return String(r[c.JUDGE - 1]).indexOf(label) !== -1; }).length;
    };
    const needFix = within90.filter(function (r) {
      const j = String(r[c.JUDGE - 1]);
      return j.indexOf(JUDGE.THUMB_IMPROVE) !== -1 || j.indexOf(JUDGE.THUMB_SWAP) !== -1
        || j.indexOf(JUDGE.IMP_SHORTAGE) !== -1 || j.indexOf(JUDGE.CTR_DROP) !== -1;
    }).length;

    const chPosts = posts.filter(function (r) { return String(r[pc.CHANNEL - 1]) === ch.key; });
    const pending = chPosts.filter(function (r) {
      const s = String(r[pc.STATUS - 1]);
      return s === STATUS.WAITING_REVIEW || s === STATUS.QUEUED
        || s === STATUS.UPLOADING || s === STATUS.UPLOAD_RESUME || s === STATUS.SCHEDULED;
    }).length;
    const errors = chPosts.filter(function (r) { return String(r[pc.STATUS - 1]) === STATUS.ERROR; }).length;

    const stats = getChannelStatsCache_(ch.key);
    rows.push([
      ch.name, ch.mode, ch.enabled ? shortAuth_(ch.authStatus) : '無効',
      stats.subscribers != null ? stats.subscribers : '',
      views90, avgCtr != null ? round2_(avgCtr) : '',
      within90.length, last7, needFix, judgeCount(JUDGE.HIT), judgeCount(JUDGE.SPIKE),
      pending, errors,
    ]);

    // 判定付き動画一覧（下段用）
    within90.forEach(function (r) {
      const j = String(r[c.JUDGE - 1]);
      if (!j) return;
      flaggedList.push({
        needFix: j.indexOf(JUDGE.THUMB_SWAP) !== -1 || j.indexOf(JUDGE.THUMB_IMPROVE) !== -1
          || j.indexOf(JUDGE.CTR_DROP) !== -1 || j.indexOf(JUDGE.IMP_SHORTAGE) !== -1,
        row: [ch.name, String(r[c.TITLE - 1]).slice(0, 40), j,
          String(r[c.JUDGE_MEMO - 1]).slice(0, 80), String(r[c.URL - 1])],
      });
    });
  });

  // 要改善を先頭に、最大30件
  flaggedList.sort(function (a, b) { return (b.needFix ? 1 : 0) - (a.needFix ? 1 : 0); });
  flaggedList = flaggedList.slice(0, 30).map(function (f) { return f.row; });

  // 描画
  sh.clearContents();
  sh.getRange(1, 1).setValue('📺 マルチチャンネル ダッシュボード');
  sh.getRange(1, 5).setValue('最終更新: ' + dateTimeStr_(new Date()));
  sh.getRange(3, 1, 1, DASH_HEADERS.length).setValues([DASH_HEADERS]).setFontWeight('bold');
  if (rows.length) {
    sh.getRange(4, 1, rows.length, DASH_HEADERS.length).setValues(rows);
    // 合計行
    const total = ['合計', '', '', '',
      rows.reduce(function (a, r) { return a + numOr_(r[4], 0); }, 0), '',
      rows.reduce(function (a, r) { return a + numOr_(r[6], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[7], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[8], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[9], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[10], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[11], 0); }, 0),
      rows.reduce(function (a, r) { return a + numOr_(r[12], 0); }, 0)];
    sh.getRange(4 + rows.length, 1, 1, DASH_HEADERS.length).setValues([total]).setFontWeight('bold');
  }

  const listTop = 4 + rows.length + 3;
  sh.getRange(listTop - 1, 1).setValue('⚠ 判定付き動画（要改善 → ヒット/急上昇の順・最大30件）').setFontWeight('bold');
  sh.getRange(listTop, 1, 1, DASH_LIST_HEADERS.length).setValues([DASH_LIST_HEADERS]).setFontWeight('bold');
  if (flaggedList.length) {
    sh.getRange(listTop + 1, 1, flaggedList.length, DASH_LIST_HEADERS.length).setValues(flaggedList);
  } else {
    sh.getRange(listTop + 1, 1).setValue('（判定付きの動画はありません）');
  }
  sh.getRange(listTop + Math.max(flaggedList.length, 1) + 2, 1)
    .setValue('チャンネル別の詳細は「' + SHEETS.VIDEOS + '」でチャンネルキー列をフィルタして確認');
}

/** 認証状態の短縮表示 */
function shortAuth_(status) {
  const s = String(status || '');
  if (!s || s.indexOf('未認証') === 0) return '未認証';
  if (s.indexOf('⚠') !== -1) return '⚠不一致';
  if (s.indexOf('エラー') !== -1) return 'エラー';
  if (s.indexOf('要再認証') !== -1) return '要再認証';
  if (s.indexOf('認証済み') !== -1) return '✓';
  return s.slice(0, 6);
}
