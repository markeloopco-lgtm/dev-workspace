/**
 * 06_ai.gs
 * AIによる概要欄・固定コメント案の生成（Gemini API・無料枠で利用可）
 *
 * 概要欄 = テンプレの固定ヘッダー + AI生成部（要約/CTA/チャプター/ハッシュタグ） + 固定フッター
 * テンプレIDでチャンネルごとのトンマナを維持する。
 * SEOキーワードは、タグ列が空欄ならタグとしても流し込む。
 */

/** メニュー: 01_投稿管理で選択中の行の概要欄を生成 */
function generateSelectedDescriptions() {
  const sh = sheet_(SHEETS.POSTS);
  if (ss_().getActiveSheet().getName() !== SHEETS.POSTS) {
    toast_('「' + SHEETS.POSTS + '」シートで対象行を選択してから実行してください。');
    return;
  }
  const range = sh.getActiveRange();
  if (!range) return;
  const start = Math.max(2, range.getRow());
  const end = range.getLastRow();
  let done = 0;
  for (let row = start; row <= end; row++) {
    if (generateDescriptionForRow_(row, /*force=*/true)) done++;
  }
  toast_('AI概要欄を ' + done + ' 件生成しました。');
}

/** メニュー: 未生成（下書き）の行をまとめて生成 */
function generateAllPendingDescriptions() {
  const count = generatePendingDescriptions_(null);
  toast_('AI概要欄を ' + count + ' 件生成しました。');
}

/**
 * 下書き行のうち生成条件を満たすものを生成する。
 * @param {number=} budgetT0 実行時間ガードの開始時刻（キューから呼ぶとき用）
 * @return {number} 生成件数
 */
function generatePendingDescriptions_(budgetT0) {
  const sh = sheet_(SHEETS.POSTS);
  const data = sheetData_(sh);
  const c = COL.POSTS;
  let count = 0;
  for (let i = 0; i < data.length; i++) {
    const status = String(data[i][c.STATUS - 1] || '').trim();
    if (status !== STATUS.DRAFT && status !== '') continue;
    if (!String(data[i][c.TITLE - 1] || '').trim()) continue;
    if (budgetT0 && Date.now() - budgetT0 > CONFIG.TIME_BUDGET_MS) break;
    if (generateDescriptionForRow_(i + 2, /*force=*/false)) count++;
  }
  return count;
}

/**
 * 1行分の概要欄を生成してシートへ書き込む。
 * @param {number} row シート行番号
 * @param {boolean} force ステータスに関わらず生成し直すか
 * @return {boolean} 生成したか
 */
function generateDescriptionForRow_(row, force) {
  const sh = sheet_(SHEETS.POSTS);
  const c = COL.POSTS;
  const values = sh.getRange(row, 1, 1, HEADERS.POSTS.length).getValues()[0];
  const title = String(values[c.TITLE - 1] || '').trim();
  const channelKey = String(values[c.CHANNEL - 1] || '').trim();
  const status = String(values[c.STATUS - 1] || '').trim();

  if (!title || !channelKey) return false;
  if (!force && status !== STATUS.DRAFT && status !== '') return false;

  const ch = getChannel_(channelKey);
  if (!ch) {
    sh.getRange(row, c.ERROR).setValue('チャンネルキーが04_チャンネル設定にありません: ' + channelKey);
    return false;
  }
  if (ch.mode === MODES.ANALYTICS) {
    if (force) toast_(ch.name + ' はANALYTICSモードのためAI生成をスキップしました。');
    return false;
  }

  try {
    const tpl = getTemplate_(ch.templateId);
    const ai = callGeminiForDescription_({
      title: title,
      script: String(values[c.SCRIPT - 1] || ''),
      category: String(values[c.CATEGORY - 1] || ''),
      tags: String(values[c.TAGS - 1] || ''),
      channelName: ch.name,
      tpl: tpl,
    });

    const description = assembleDescription_(tpl, ai);
    sh.getRange(row, c.DESC).setValue(description);
    sh.getRange(row, c.PIN_COMMENT).setValue(ai.pinned_comment || '');
    if (!String(values[c.TAGS - 1] || '').trim() && ai.seo_keywords && ai.seo_keywords.length) {
      sh.getRange(row, c.TAGS).setValue(ai.seo_keywords.join(',').slice(0, 450));
    }
    // 生成後のステータス遷移: FULL×AUTO→投稿待ち / FULL×REVIEW→確認待ち / DRAFT→生成済み
    let next = STATUS.GENERATED;
    if (ch.mode === MODES.FULL) {
      next = ch.review === REVIEW_MODES.AUTO ? STATUS.QUEUED : STATUS.WAITING_REVIEW;
    }
    sh.getRange(row, c.STATUS).setValue(next);
    sh.getRange(row, c.ERROR).setValue('');
    return true;
  } catch (e) {
    sh.getRange(row, c.ERROR).setValue('AI生成失敗: ' + String(e.message).slice(0, 300));
    logError_('AI', channelKey, title, e.message, e.stack);
    return false;
  }
}

/** テンプレ＋AI出力から最終的な概要欄テキストを組み立てる（5000字制限対応） */
function assembleDescription_(tpl, ai) {
  const parts = [];
  if (tpl.header) parts.push(tpl.header);
  if (ai.summary) parts.push(ai.summary);
  const cta = tpl.cta || ai.cta;
  if (cta) parts.push(cta);
  if (tpl.chapters && ai.chapters && ai.chapters.length) {
    parts.push(ai.chapters.map(function (ch) {
      return (ch.time || '00:00') + ' ' + (ch.label || '');
    }).join('\n'));
  }
  const fixedTags = String(tpl.hashtags || '').split(/[\s,、]+/).filter(Boolean);
  const aiTags = (ai.hashtags || []).map(function (t) {
    t = String(t).trim();
    return t && t[0] !== '#' ? '#' + t : t;
  });
  const seen = {};
  const tags = fixedTags.concat(aiTags).filter(function (t) {
    if (!t || seen[t]) return false;
    seen[t] = true;
    return true;
  }).slice(0, 15);
  if (tags.length) parts.push(tags.join(' '));
  if (tpl.footer) parts.push(tpl.footer);
  return parts.join('\n\n').slice(0, 4900);
}

/**
 * Geminiで概要欄素材を生成する。JSONで返させる。
 * @return {Object} {summary, cta, hashtags[], seo_keywords[], chapters[{time,label}], pinned_comment}
 */
function callGeminiForDescription_(input) {
  const props = PropertiesService.getScriptProperties();
  const apiKey = props.getProperty('GEMINI_API_KEY');
  if (!apiKey) throw new Error('スクリプトプロパティに GEMINI_API_KEY を設定してください');
  const model = props.getProperty('GEMINI_MODEL') || CONFIG.GEMINI_MODEL_DEFAULT;

  const scriptText = String(input.script || '').slice(0, 20000);
  const prompt = [
    'あなたはYouTube運用のプロの編集者です。以下の動画情報から、概要欄の素材をJSONで生成してください。',
    '',
    '# チャンネル', input.channelName,
    '# 動画タイトル', input.title,
    input.category ? '# カテゴリ\n' + input.category : '',
    input.tags ? '# 既存タグ\n' + input.tags : '',
    input.tpl.tone ? '# トンマナ・追加指示（必ず守ること）\n' + input.tpl.tone : '',
    scriptText ? '# 台本・文字起こし（抜粋）\n' + scriptText : '（台本なし。タイトルから推測して作成）',
    '',
    '# 出力仕様（必ずこのキーのJSONのみを出力）',
    JSON.stringify({
      summary: '動画の要約2〜4文。冒頭に検索されやすいキーワードを自然に含める',
      cta: 'チャンネル登録や関連動画視聴を促す1〜2文',
      hashtags: ['#ハッシュタグを5〜10個'],
      seo_keywords: ['SEOキーワードを8〜15個（日本語）'],
      chapters: [{ time: '00:00', label: 'チャプター名' }],
      pinned_comment: '固定コメント案1〜2文（視聴者への問いかけなど）',
    }),
    '',
    input.tpl.chapters
      ? 'chaptersは台本から推定できる場合のみ作成（推定できなければ空配列）。時間はMM:SS形式、最初は必ず00:00。'
      : 'chaptersは空配列にすること。',
  ].filter(Boolean).join('\n');

  const body = {
    contents: [{ role: 'user', parts: [{ text: prompt }] }],
    generationConfig: { temperature: 0.7, responseMimeType: 'application/json' },
  };
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent?key=' + apiKey;

  let res;
  for (let attempt = 0; attempt < 2; attempt++) {
    res = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(body),
      muteHttpExceptions: true,
    });
    const code = res.getResponseCode();
    if (code < 400) break;
    if (attempt === 0 && (code === 429 || code >= 500)) {
      Utilities.sleep(3000); // レート制限・一時障害は1回だけリトライ
      continue;
    }
    throw new Error('Gemini APIエラー HTTP' + code + ': ' + res.getContentText().slice(0, 300));
  }
  const json = JSON.parse(res.getContentText());
  const text = (((json.candidates || [])[0] || {}).content || {}).parts;
  const raw = text && text[0] ? text[0].text : '';
  if (!raw) throw new Error('Geminiの応答が空でした');
  try {
    return JSON.parse(raw);
  } catch (e) {
    // JSONの前後に余計な文字が付くことがあるため、最初の{から最後の}までを抜き出して再試行
    const m = raw.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    throw new Error('Gemini応答のJSON解析に失敗: ' + raw.slice(0, 200));
  }
}
