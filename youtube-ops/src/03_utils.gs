/**
 * 03_utils.gs
 * 共通ユーティリティ（シート取得・日付・エラーログ・Drive URL解析など）
 */

/** アクティブなスプレッドシート（コンテナバインド前提） */
function ss_() {
  return SpreadsheetApp.getActive();
}

/** 名前でシートを取得。無ければエラー */
function sheet_(name) {
  const sh = ss_().getSheetByName(name);
  if (!sh) throw new Error('シートが見つかりません: ' + name + ' （メニュー「初期セットアップ」を実行してください）');
  return sh;
}

/** ヘッダーを除く全データを2次元配列で返す */
function sheetData_(sh) {
  const lastRow = sh.getLastRow();
  const lastCol = sh.getLastColumn();
  if (lastRow < 2) return [];
  return sh.getRange(2, 1, lastRow - 1, lastCol).getValues();
}

/** 'yyyy-MM-dd' 形式（プロジェクトのタイムゾーン） */
function dateStr_(date) {
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

/** 'yyyy-MM-dd HH:mm' 形式 */
function dateTimeStr_(date) {
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm');
}

/** RFC3339（UTC）。YouTubeのpublishAtで使う */
function rfc3339Utc_(date) {
  return Utilities.formatDate(date, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");
}

/** n日前のDate */
function daysAgo_(n) {
  return new Date(Date.now() - n * 24 * 60 * 60 * 1000);
}

/** 2つの日時の差（日数・小数切り捨て） */
function ageDays_(published, now) {
  return Math.floor(((now || new Date()).getTime() - published.getTime()) / (24 * 60 * 60 * 1000));
}

/** 数値なら数値、そうでなければ既定値 */
function numOr_(v, fallback) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  return isNaN(n) ? fallback : n;
}

/** 小数第2位で丸め */
function round2_(v) {
  return v == null || v === '' ? '' : Math.round(v * 100) / 100;
}

/** 配列の平均（null/空を除外）。要素が無ければnull */
function avg_(arr) {
  const xs = arr.filter(function (v) { return v != null && v !== '' && !isNaN(v); });
  if (!xs.length) return null;
  return xs.reduce(function (a, b) { return a + Number(b); }, 0) / xs.length;
}

/** 配列の中央値（null/空を除外）。要素が無ければnull */
function median_(arr) {
  const xs = arr.filter(function (v) { return v != null && v !== '' && !isNaN(v); })
    .map(Number).sort(function (a, b) { return a - b; });
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

/**
 * Google DriveのURLまたはIDからファイルIDを取り出す。
 * 対応形式: .../file/d/{id}/... , .../d/{id}/... , ...?id={id} , 生のID
 */
function driveFileId_(urlOrId) {
  const s = String(urlOrId || '').trim();
  if (!s) return null;
  let m = s.match(/\/d\/([-\w]{20,})/);
  if (m) return m[1];
  m = s.match(/[?&]id=([-\w]{20,})/);
  if (m) return m[1];
  if (/^[-\w]{20,}$/.test(s)) return s;
  return null;
}

/** スクリプトプロパティからJSONを読む */
function jsonProp_(key) {
  const raw = PropertiesService.getScriptProperties().getProperty(key);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return null; }
}

/** スクリプトプロパティへJSONを書く（nullで削除） */
function setJsonProp_(key, obj) {
  const props = PropertiesService.getScriptProperties();
  if (obj == null) props.deleteProperty(key);
  else props.setProperty(key, JSON.stringify(obj));
}

/**
 * 06_エラーログへ1行追記する。ログ自体の失敗で本処理を止めない。
 * @param {string} type 種別（例: '投稿' '分析' 'AI' '認証'）
 * @param {string} channelKey 対象チャンネルキー（無ければ''）
 * @param {string} target 対象（動画タイトル・videoIdなど）
 * @param {string} message 概要
 * @param {string} detail 詳細（スタックトレース等）
 */
function logError_(type, channelKey, target, message, detail) {
  try {
    const sh = ss_().getSheetByName(SHEETS.ERRORS);
    if (!sh) return;
    sh.appendRow([dateTimeStr_(new Date()), type, channelKey || '', target || '',
      String(message || '').slice(0, 500), String(detail || '').slice(0, 1500)]);
    const over = sh.getLastRow() - 1 - CONFIG.ERROR_LOG_MAX_ROWS;
    if (over > 0) sh.deleteRows(2, over);
  } catch (e) {
    Logger.log('エラーログ書き込み失敗: ' + e.message);
  }
}

/** スクリプトロックを取る。取れなければnull（多重実行防止） */
function tryLock_(ms) {
  const lock = LockService.getScriptLock();
  return lock.tryLock(ms || 30 * 1000) ? lock : null;
}

/**
 * 実行時間切れで中断したときの自動継続用ワンショットトリガーを作る。
 * 作成したトリガーIDをプロパティに記録し、次回実行の頭で掃除する。
 */
function scheduleContinuation_(handlerName) {
  const trigger = ScriptApp.newTrigger(handlerName).timeBased().after(60 * 1000).create();
  const ids = jsonProp_('oneoff_triggers') || {};
  ids[handlerName] = trigger.getUniqueId();
  setJsonProp_('oneoff_triggers', ids);
}

/** scheduleContinuation_で作ったワンショットトリガーを削除する */
function cleanupOneOffTrigger_(handlerName) {
  const ids = jsonProp_('oneoff_triggers') || {};
  const id = ids[handlerName];
  if (!id) return;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getUniqueId() === id) ScriptApp.deleteTrigger(t);
  });
  delete ids[handlerName];
  setJsonProp_('oneoff_triggers', ids);
}

/** トースト表示（UIが無い文脈では無視） */
function toast_(msg) {
  try { ss_().toast(msg, 'YouTube管理', 8); } catch (e) { /* トリガー実行時など */ }
}
