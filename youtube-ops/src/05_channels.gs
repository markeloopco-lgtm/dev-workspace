/**
 * 05_channels.gs
 * 04_チャンネル設定・05_概要欄テンプレの読み込みと、チャンネル別しきい値の解決
 */

/**
 * チャンネル設定を配列で返す。
 * @return {Array<Object>} {row(シート行番号), key, name, channelId, mode, review,
 *                          templateId, enabled, authStatus, thresholds{...}}
 */
function getChannels_() {
  const sh = sheet_(SHEETS.CHANNELS);
  const data = sheetData_(sh);
  const c = COL.CHANNELS;
  const list = [];
  data.forEach(function (r, i) {
    const key = String(r[c.KEY - 1] || '').trim();
    if (!key) return;
    list.push({
      row: i + 2,
      key: key,
      name: String(r[c.NAME - 1] || key),
      channelId: String(r[c.CHANNEL_ID - 1] || '').trim(),
      mode: String(r[c.MODE - 1] || MODES.ANALYTICS).trim().toUpperCase(),
      review: String(r[c.REVIEW - 1] || REVIEW_MODES.REVIEW).trim().toUpperCase(),
      templateId: String(r[c.TEMPLATE - 1] || '').trim(),
      enabled: r[c.ENABLED - 1] === true,
      authStatus: String(r[c.AUTH_STATUS - 1] || ''),
      thresholds: {
        ctrLowAbs: numOr_(r[c.CTR_LOW - 1], null),          // %の絶対値。空ならチャンネル平均×既定倍率
        ctrDropPt: numOr_(r[c.CTR_DROP - 1], DEFAULT_THRESHOLDS.CTR_DROP_PT),
        hitMult: numOr_(r[c.HIT_MULT - 1], DEFAULT_THRESHOLDS.HIT_MULT),
        spikeMult: numOr_(r[c.SPIKE_MULT - 1], DEFAULT_THRESHOLDS.SPIKE_MULT),
        impMult: numOr_(r[c.IMP_MULT - 1], DEFAULT_THRESHOLDS.IMP_MULT),
      },
    });
  });
  return list;
}

/** チャンネルキーで1件取得（無ければnull） */
function getChannel_(key) {
  const hit = getChannels_().filter(function (ch) { return ch.key === key; });
  return hit.length ? hit[0] : null;
}

/** チャンネル行の任意の列へ書き込む */
function writeChannelCell_(ch, colIndex, value) {
  sheet_(SHEETS.CHANNELS).getRange(ch.row, colIndex).setValue(value);
}

/**
 * 概要欄テンプレをIDで取得。IDが空/未登録なら空テンプレを返す。
 * @return {Object} {id, name, header, footer, hashtags, cta, tone, chapters}
 */
function getTemplate_(templateId) {
  const empty = { id: '', name: '', header: '', footer: '', hashtags: '', cta: '', tone: '', chapters: false };
  if (!templateId) return empty;
  const data = sheetData_(sheet_(SHEETS.TEMPLATES));
  const c = COL.TEMPLATES;
  for (let i = 0; i < data.length; i++) {
    if (String(data[i][c.ID - 1]).trim() === templateId) {
      return {
        id: templateId,
        name: String(data[i][c.NAME - 1] || ''),
        header: String(data[i][c.HEADER - 1] || ''),
        footer: String(data[i][c.FOOTER - 1] || ''),
        hashtags: String(data[i][c.HASHTAGS - 1] || ''),
        cta: String(data[i][c.CTA - 1] || ''),
        tone: String(data[i][c.TONE - 1] || ''),
        chapters: data[i][c.CHAPTERS - 1] === true,
      };
    }
  }
  return empty;
}

/** チャンネル統計（登録者数など）のキャッシュ書き込み（分析時に更新→ダッシュボードで参照） */
function cacheChannelStats_(key, stats) {
  setJsonProp_('chstats_' + key, stats);
}

/** チャンネル統計キャッシュの読み込み */
function getChannelStatsCache_(key) {
  return jsonProp_('chstats_' + key) || {};
}
