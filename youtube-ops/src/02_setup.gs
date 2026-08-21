/**
 * 02_setup.gs
 * 初期セットアップ: シート作成（冪等・既存データは壊さない）とトリガー設定
 */

/** メニュー: シート一式を作成する。既にあるシートはヘッダー確認のみ */
function setupSpreadsheet() {
  const ss = ss_();
  buildSheet_(ss, SHEETS.DASHBOARD, null);
  buildSheet_(ss, SHEETS.POSTS, HEADERS.POSTS);
  buildSheet_(ss, SHEETS.VIDEOS, HEADERS.VIDEOS);
  buildSheet_(ss, SHEETS.DAILY, HEADERS.DAILY);
  buildSheet_(ss, SHEETS.CHANNELS, HEADERS.CHANNELS);
  buildSheet_(ss, SHEETS.TEMPLATES, HEADERS.TEMPLATES);
  buildSheet_(ss, SHEETS.ERRORS, HEADERS.ERRORS);

  setupChannelsSheetExtras_();
  setupPostsSheetExtras_();
  setupTemplatesSheetExtras_();
  orderSheets_();
  toast_('セットアップ完了。次は docs/01 の手順で スクリプトプロパティ → チャンネル設定 → 認証 へ進んでください。');
}

/** シートを作成しヘッダーを書く（既存シートのデータは保持） */
function buildSheet_(ss, name, headers) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (!headers) return sh;
  if (sh.getMaxColumns() < headers.length) {
    sh.insertColumnsAfter(sh.getMaxColumns(), headers.length - sh.getMaxColumns());
  }
  const current = sh.getRange(1, 1, 1, headers.length).getValues()[0];
  const empty = current.every(function (v) { return v === '' || v == null; });
  if (empty) {
    sh.getRange(1, 1, 1, headers.length).setValues([headers])
      .setFontWeight('bold').setBackground('#efefef');
    sh.setFrozenRows(1);
  } else if (String(current[0]) !== String(headers[0])) {
    logError_('セットアップ', '', name, 'ヘッダーが想定と異なります。列構成を確認してください', current.join(','));
  }
  return sh;
}

/** 04_チャンネル設定: 入力規則とサンプル行 */
function setupChannelsSheetExtras_() {
  const sh = sheet_(SHEETS.CHANNELS);
  const c = COL.CHANNELS;
  const rows = 200; // 将来の20〜50ch拡張を見込んだ入力規則の適用範囲
  sh.getRange(2, c.MODE, rows).setDataValidation(
    SpreadsheetApp.newDataValidation()
      .requireValueInList([MODES.ANALYTICS, MODES.DRAFT, MODES.FULL], true).setAllowInvalid(false).build());
  sh.getRange(2, c.REVIEW, rows).setDataValidation(
    SpreadsheetApp.newDataValidation()
      .requireValueInList([REVIEW_MODES.REVIEW, REVIEW_MODES.AUTO], true).setAllowInvalid(false).build());
  sh.getRange(2, c.ENABLED, rows).insertCheckboxes();
  if (sh.getLastRow() < 2) {
    const sample = new Array(HEADERS.CHANNELS.length).fill('');
    sample[c.KEY - 1] = 'sample';
    sample[c.NAME - 1] = 'サンプル（コピーして使う・行ごと削除可）';
    sample[c.MODE - 1] = MODES.ANALYTICS;
    sample[c.REVIEW - 1] = REVIEW_MODES.REVIEW;
    sh.getRange(2, 1, 1, sample.length).setValues([sample]);
    sh.getRange(2, c.ENABLED).setValue(false);
  }
}

/** 01_投稿管理: 承認チェックボックスと日時列の表示形式 */
function setupPostsSheetExtras_() {
  const sh = sheet_(SHEETS.POSTS);
  const c = COL.POSTS;
  const rows = 500;
  sh.getRange(2, c.APPROVED, rows).insertCheckboxes();
  sh.getRange(2, c.PUBLISH_AT, rows).setNumberFormat('yyyy-mm-dd hh:mm');
}

/** 05_概要欄テンプレ: チャプター生成チェックボックスとサンプルテンプレ */
function setupTemplatesSheetExtras_() {
  const sh = sheet_(SHEETS.TEMPLATES);
  const c = COL.TEMPLATES;
  sh.getRange(2, c.CHAPTERS, 100).insertCheckboxes();
  if (sh.getLastRow() < 2) {
    const sample = new Array(HEADERS.TEMPLATES.length).fill('');
    sample[c.ID - 1] = 'default';
    sample[c.NAME - 1] = '標準テンプレ';
    sample[c.HEADER - 1] = 'ご視聴ありがとうございます！';
    sample[c.FOOTER - 1] = '―――\nお仕事のご依頼はこちら: （連絡先）';
    sample[c.HASHTAGS - 1] = '';
    sample[c.CTA - 1] = 'チャンネル登録・高評価で応援よろしくお願いします！';
    sample[c.TONE - 1] = '丁寧語。絵文字は控えめに。';
    sh.getRange(2, 1, 1, sample.length).setValues([sample]);
    sh.getRange(2, c.CHAPTERS).setValue(false);
  }
}

/** シートを00〜06の順に並べる */
function orderSheets_() {
  const order = [SHEETS.DASHBOARD, SHEETS.POSTS, SHEETS.VIDEOS, SHEETS.DAILY,
    SHEETS.CHANNELS, SHEETS.TEMPLATES, SHEETS.ERRORS];
  const ss = ss_();
  order.forEach(function (name, i) {
    const sh = ss.getSheetByName(name);
    if (sh) {
      ss.setActiveSheet(sh);
      ss.moveActiveSheet(i + 1);
    }
  });
}

/**
 * メニュー: 自動実行トリガーを設定する。
 * - 毎日 CONFIG.DAILY_HOUR 時: 分析（runDailyAnalytics）
 * - CONFIG.QUEUE_INTERVAL_MIN 分ごと: 投稿キュー（processPostQueue）
 * 既存の同名トリガーは作り直す。
 */
function setupTriggers() {
  deleteTriggers();
  ScriptApp.newTrigger('runDailyAnalytics').timeBased()
    .everyDays(1).atHour(CONFIG.DAILY_HOUR).create();
  ScriptApp.newTrigger('processPostQueue').timeBased()
    .everyMinutes(CONFIG.QUEUE_INTERVAL_MIN).create();
  toast_('トリガーを設定しました（毎日' + CONFIG.DAILY_HOUR + '時に分析 / '
    + CONFIG.QUEUE_INTERVAL_MIN + '分ごとに投稿キュー）。');
}

/** メニュー: このプロジェクトの自動実行トリガーをすべて削除する */
function deleteTriggers() {
  const targets = ['runDailyAnalytics', 'processPostQueue'];
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (targets.indexOf(t.getHandlerFunction()) !== -1) ScriptApp.deleteTrigger(t);
  });
  setJsonProp_('oneoff_triggers', null);
}
