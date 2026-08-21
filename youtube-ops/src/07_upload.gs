/**
 * 07_upload.gs
 * 投稿キュー処理: 承認反映 → （必要なら）AI生成 → YouTubeへ動画アップロード →
 * サムネ設定 → 予約公開 → 結果をシートへ記録
 *
 * アップロードはYouTube Data APIの「再開可能アップロード」を使い、
 * Driveから16MBずつ読み出して転送する。GASの6分制限にかかったら
 * 進捗をスクリプトプロパティに保存し、次回のトリガー実行で続きから再開する。
 */

/** メニュー/トリガー: 投稿キューを実行 */
function processPostQueue() {
  const lock = tryLock_(20 * 1000);
  if (!lock) return; // 他の実行が動作中
  try {
    const t0 = Date.now();
    promoteApprovedRows_();
    if (CONFIG.AUTO_GENERATE_IN_QUEUE) {
      try { generatePendingDescriptions_(t0); } catch (e) { logError_('AI', '', '', e.message, e.stack); }
    }

    const sh = sheet_(SHEETS.POSTS);
    const data = sheetData_(sh);
    const c = COL.POSTS;
    for (let i = 0; i < data.length; i++) {
      const status = String(data[i][c.STATUS - 1] || '').trim();
      if (status !== STATUS.QUEUED && status !== STATUS.UPLOAD_RESUME) continue;
      if (Date.now() - t0 > CONFIG.TIME_BUDGET_MS) {
        scheduleContinuation_('processPostQueue');
        return;
      }
      uploadOne_(i + 2, t0);
    }
  } finally {
    lock.releaseLock();
  }
}

/** REVIEWモード: 「確認待ち」かつ承認チェック済みの行を「投稿待ち」へ進める */
function promoteApprovedRows_() {
  const sh = sheet_(SHEETS.POSTS);
  const data = sheetData_(sh);
  const c = COL.POSTS;
  for (let i = 0; i < data.length; i++) {
    if (String(data[i][c.STATUS - 1]).trim() !== STATUS.WAITING_REVIEW) continue;
    if (data[i][c.APPROVED - 1] !== true) continue;
    const ch = getChannel_(String(data[i][c.CHANNEL - 1]).trim());
    if (ch && ch.mode === MODES.FULL) {
      sh.getRange(i + 2, c.STATUS).setValue(STATUS.QUEUED);
    }
  }
}

/**
 * 1行分のアップロードを実行（または再開）する。
 * @param {number} row シート行番号
 * @param {number} t0 実行時間ガードの開始時刻
 */
function uploadOne_(row, t0) {
  const sh = sheet_(SHEETS.POSTS);
  const c = COL.POSTS;
  const values = sh.getRange(row, 1, 1, HEADERS.POSTS.length).getValues()[0];
  const postId = ensurePostId_(sh, row, values);
  const title = String(values[c.TITLE - 1] || '').trim();
  const channelKey = String(values[c.CHANNEL - 1] || '').trim();
  const stateKey = 'upload_state_' + postId;

  const fail = function (msg) {
    sh.getRange(row, c.STATUS).setValue(STATUS.ERROR);
    sh.getRange(row, c.ERROR).setValue(String(msg).slice(0, 400));
    setJsonProp_(stateKey, null);
    logError_('投稿', channelKey, title, msg, '');
    notifyEvent_('upload_error', { channelKey: channelKey, title: title, message: String(msg) });
  };

  try {
    const ch = getChannel_(channelKey);
    if (!ch) return fail('チャンネルキーが04_チャンネル設定にありません: ' + channelKey);
    if (!ch.enabled) return fail('チャンネルが無効になっています');
    if (ch.mode !== MODES.FULL) return fail('モードがFULLではないため投稿できません（現在: ' + ch.mode + '）');
    if (!hasUploadScope_(ch.key)) return fail('アップロード権限がありません。認証URLを再発行して承認し直してください');

    const fileId = driveFileId_(values[c.VIDEO_URL - 1]);
    if (!fileId) return fail('動画DriveURLが空か、URL形式を認識できません');

    // 予約日時の検証（空欄ならCONFIGの既定プライバシーで即アップ）
    const publishAt = values[c.PUBLISH_AT - 1];
    let publishAtUtc = null;
    if (publishAt) {
      if (!(publishAt instanceof Date)) return fail('予約公開日時は日付として入力してください');
      const lead = (publishAt.getTime() - Date.now()) / 60000;
      if (lead < CONFIG.MIN_SCHEDULE_LEAD_MIN) {
        return fail('予約日時は' + CONFIG.MIN_SCHEDULE_LEAD_MIN + '分以上先にしてください');
      }
      publishAtUtc = rfc3339Utc_(publishAt);
    }

    // 進捗の復元（前回タイムアウトで中断した場合）
    let state = jsonProp_(stateKey);
    if (!state) {
      const meta = driveFileMeta_(fileId);
      if (!meta.size) return fail('Driveのファイルサイズが取得できません（共有ドライブの場合は所有者のマイドライブへ）');
      const metadata = {
        snippet: {
          title: title.slice(0, 100),
          description: String(values[c.DESC - 1] || ''),
          tags: String(values[c.TAGS - 1] || '').split(/[,、]/).map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 30),
          categoryId: String(values[c.CATEGORY - 1] || CONFIG.DEFAULT_CATEGORY_ID).trim() || CONFIG.DEFAULT_CATEGORY_ID,
        },
        status: {
          privacyStatus: publishAtUtc ? 'private' : CONFIG.DEFAULT_PRIVACY_WHEN_NO_SCHEDULE,
          selfDeclaredMadeForKids: CONFIG.MADE_FOR_KIDS,
        },
      };
      if (publishAtUtc) metadata.status.publishAt = publishAtUtc;

      const uploadUrl = initResumableUpload_(ch, metadata, meta);
      state = { fileId: fileId, uploadUrl: uploadUrl, size: Number(meta.size), mime: meta.mimeType, offset: 0 };
      setJsonProp_(stateKey, state);
    }
    sh.getRange(row, c.STATUS).setValue(STATUS.UPLOADING);

    // チャンク転送ループ（時間切れなら保存して中断→次回再開）
    let result = null;
    while (state.offset < state.size) {
      if (Date.now() - t0 > CONFIG.TIME_BUDGET_MS) {
        setJsonProp_(stateKey, state);
        sh.getRange(row, c.STATUS).setValue(STATUS.UPLOAD_RESUME);
        scheduleContinuation_('processPostQueue');
        return;
      }
      const end = Math.min(state.offset + CONFIG.UPLOAD_CHUNK_BYTES, state.size) - 1;
      const bytes = driveFetchChunk_(state.fileId, state.offset, end);
      const put = UrlFetchApp.fetch(state.uploadUrl, {
        method: 'put',
        contentType: 'application/octet-stream',
        headers: { 'Content-Range': 'bytes ' + state.offset + '-' + end + '/' + state.size },
        payload: bytes,
        muteHttpExceptions: true,
      });
      const code = put.getResponseCode();
      if (code === 308) {
        // 継続。サーバーが受領済み範囲を返した場合はそれに合わせる
        const range = put.getAllHeaders()['Range'] || put.getAllHeaders()['range'];
        const m = range && String(range).match(/bytes=0-(\d+)/);
        state.offset = m ? Number(m[1]) + 1 : end + 1;
      } else if (code === 200 || code === 201) {
        result = JSON.parse(put.getContentText());
        state.offset = state.size;
      } else {
        return fail('アップロード失敗 HTTP' + code + ': ' + put.getContentText().slice(0, 300));
      }
    }
    if (!result || !result.id) return fail('アップロードは終了しましたがvideoIdが取得できませんでした');
    const videoId = result.id;
    setJsonProp_(stateKey, null);

    // サムネイル設定（失敗しても投稿自体は成功扱い。エラー内容欄にメモ）
    let thumbNote = '';
    const thumbId = driveFileId_(values[c.THUMB_URL - 1]);
    if (thumbId) {
      try {
        setThumbnail_(ch, videoId, thumbId);
      } catch (e) {
        thumbNote = 'サムネ設定失敗: ' + String(e.message).slice(0, 200);
        logError_('投稿', channelKey, title, thumbNote, e.stack);
      }
    }

    // 結果の記録
    const finalStatus = publishAtUtc ? STATUS.SCHEDULED
      : (CONFIG.DEFAULT_PRIVACY_WHEN_NO_SCHEDULE === 'public' ? STATUS.PUBLIC : STATUS.PRIVATE_DONE);
    sh.getRange(row, c.VIDEO_ID).setValue(videoId);
    sh.getRange(row, c.YT_URL).setValue('https://youtu.be/' + videoId);
    sh.getRange(row, c.DONE_AT).setValue(dateTimeStr_(new Date()));
    sh.getRange(row, c.STATUS).setValue(finalStatus);
    sh.getRange(row, c.ERROR).setValue(thumbNote);
    notifyEvent_('upload_success', { channelKey: channelKey, title: title, videoId: videoId, status: finalStatus });
  } catch (e) {
    fail(e.message + '\n' + (e.stack || ''));
  }
}

/** 投稿IDが空なら採番して書き込む */
function ensurePostId_(sh, row, values) {
  const c = COL.POSTS;
  let id = String(values[c.ID - 1] || '').trim();
  if (!id) {
    id = 'p' + Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyyMMddHHmmss') + '_r' + row;
    sh.getRange(row, c.ID).setValue(id);
  }
  return id;
}

/** 再開可能アップロードを開始し、アップロード先URLを返す */
function initResumableUpload_(ch, metadata, meta) {
  const res = UrlFetchApp.fetch(
    'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status', {
      method: 'post',
      contentType: 'application/json; charset=UTF-8',
      headers: {
        Authorization: 'Bearer ' + channelToken_(ch),
        'X-Upload-Content-Length': String(meta.size),
        'X-Upload-Content-Type': meta.mimeType || 'video/*',
      },
      payload: JSON.stringify(metadata),
      muteHttpExceptions: true,
    });
  if (res.getResponseCode() >= 400) {
    throw new Error('アップロード開始失敗 HTTP' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 400));
  }
  const headers = res.getAllHeaders();
  const location = headers['Location'] || headers['location'];
  if (!location) throw new Error('アップロードURL(Location)が返されませんでした');
  return location;
}

/** Driveファイルのメタ情報（スプレッドシート所有者の権限で読む） */
function driveFileMeta_(fileId) {
  const res = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/files/' + fileId + '?fields=id,name,size,mimeType&supportsAllDrives=true', {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true,
    });
  if (res.getResponseCode() >= 400) {
    throw new Error('Driveファイル取得失敗 HTTP' + res.getResponseCode() + '（URLと共有設定を確認）');
  }
  return JSON.parse(res.getContentText());
}

/** Driveファイルの指定バイト範囲を取得 */
function driveFetchChunk_(fileId, start, end) {
  const res = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/files/' + fileId + '?alt=media&supportsAllDrives=true', {
      headers: {
        Authorization: 'Bearer ' + ScriptApp.getOAuthToken(),
        Range: 'bytes=' + start + '-' + end,
      },
      muteHttpExceptions: true,
    });
  if (res.getResponseCode() >= 300) {
    throw new Error('Driveチャンク取得失敗 HTTP' + res.getResponseCode());
  }
  return res.getContent();
}

/** サムネイルを設定（2MB制限・要「電話番号確認済み」アカウント） */
function setThumbnail_(ch, videoId, thumbFileId) {
  const meta = driveFileMeta_(thumbFileId);
  if (Number(meta.size) > CONFIG.THUMB_MAX_BYTES) {
    throw new Error('サムネが2MBを超えています(' + Math.round(meta.size / 1024) + 'KB)。圧縮してください');
  }
  const bytes = driveFetchChunk_(thumbFileId, 0, Number(meta.size) - 1);
  const res = UrlFetchApp.fetch(
    'https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=' + videoId + '&uploadType=media', {
      method: 'post',
      contentType: meta.mimeType || 'image/jpeg',
      headers: { Authorization: 'Bearer ' + channelToken_(ch) },
      payload: bytes,
      muteHttpExceptions: true,
    });
  if (res.getResponseCode() >= 400) {
    throw new Error('HTTP' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 300));
  }
}
