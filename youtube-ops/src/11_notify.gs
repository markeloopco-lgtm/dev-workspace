/**
 * 11_notify.gs
 * 通知フック（現時点では何もしない）
 *
 * Slack連携は今後の追加予定。実装するときは、このnotifyEvent_の中で
 * Incoming WebhookへPOSTする（Webhook URLはスクリプトプロパティ SLACK_WEBHOOK_URL に置く）。
 * 呼び出し箇所は既に用意済み:
 *   - upload_success … 投稿成功（channelKey, title, videoId, status）
 *   - upload_error   … 投稿失敗（channelKey, title, message）
 *
 * 追加予定のイベント（呼び出し側の実装もあわせて行う）:
 *   - CTR低下 / サムネ差し替え推奨 / 急上昇（09_judgement.gsの結果から）
 *   - 毎朝の分析サマリー（runDailyAnalyticsの最後から）
 */

/**
 * @param {string} type イベント種別
 * @param {Object} payload イベント内容
 */
function notifyEvent_(type, payload) {
  // Slack通知は未実装（要件上「まだ不要」）。ログにだけ残す。
  Logger.log('[notify:' + type + '] ' + JSON.stringify(payload || {}));
}
