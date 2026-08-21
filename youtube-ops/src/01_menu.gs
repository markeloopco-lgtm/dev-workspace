/**
 * 01_menu.gs
 * スプレッドシートを開いたときのカスタムメニュー
 */

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📺 YouTube管理')
    .addItem('① 初期セットアップ（シート作成）', 'setupSpreadsheet')
    .addItem('② トリガー設定（毎日自動実行を開始）', 'setupTriggers')
    .addSeparator()
    .addItem('認証URLを発行（全チャンネル）', 'issueAuthUrls')
    .addItem('認証状態を確認', 'refreshAuthStatus')
    .addSeparator()
    .addItem('AI概要欄を生成（選択行）', 'generateSelectedDescriptions')
    .addItem('AI概要欄を生成（未生成の下書きすべて）', 'generateAllPendingDescriptions')
    .addSeparator()
    .addItem('投稿キューを今すぐ実行', 'processPostQueue')
    .addItem('分析を今すぐ更新', 'runDailyAnalytics')
    .addItem('ダッシュボードを更新', 'rebuildDashboard')
    .addSeparator()
    .addItem('自動実行を停止（トリガー削除）', 'deleteTriggersWithToast')
    .addToUi();
}

/** メニュー用: トリガー削除＋完了表示 */
function deleteTriggersWithToast() {
  deleteTriggers();
  toast_('自動実行トリガーを削除しました。再開は「② トリガー設定」。');
}
