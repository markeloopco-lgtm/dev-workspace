/**
 * 04_auth.gs
 * チャンネルごとのOAuth認証（apps-script-oauth2ライブラリ使用）
 *
 * 設計:
 * - スプレッドシート所有者とYouTubeチャンネル所有者が別アカウントでも動くよう、
 *   チャンネルごとに認証URLを発行し、各チャンネルのGoogleアカウントで承認してもらう。
 * - モードにより要求スコープを変える:
 *     ANALYTICS / DRAFT … 読み取りのみ（youtube.readonly + yt-analytics.readonly）
 *     FULL             … 上記＋アップロード（youtube.upload + youtube）
 * - refresh token等はOAuth2ライブラリ経由でスクリプトプロパティに保存される
 *   （キー: oauth2.yt_<チャンネルキー>）。シートには一切書かない。
 *
 * 注意: 認証URLを開くチャンネル所有者は、このスプレッドシートの閲覧権限が必要
 * （Apps Scriptのコールバックにアクセスするため）。閲覧者で共有すればよい。
 */

const SCOPES_READONLY = [
  'https://www.googleapis.com/auth/youtube.readonly',
  'https://www.googleapis.com/auth/yt-analytics.readonly',
];
const SCOPES_UPLOAD = SCOPES_READONLY.concat([
  'https://www.googleapis.com/auth/youtube.upload',
  'https://www.googleapis.com/auth/youtube',
]);

/** モードに応じた要求スコープ */
function scopesForMode_(mode) {
  return mode === MODES.FULL ? SCOPES_UPLOAD : SCOPES_READONLY;
}

/** チャンネル用のOAuth2サービスを作る */
function getChannelService_(channelKey, scopes) {
  const props = PropertiesService.getScriptProperties();
  const clientId = props.getProperty('OAUTH_CLIENT_ID');
  const clientSecret = props.getProperty('OAUTH_CLIENT_SECRET');
  if (!clientId || !clientSecret) {
    throw new Error('スクリプトプロパティに OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET を設定してください（docs/01参照）');
  }
  return OAuth2.createService('yt_' + channelKey)
    .setAuthorizationBaseUrl('https://accounts.google.com/o/oauth2/auth')
    .setTokenUrl('https://oauth2.googleapis.com/token')
    .setClientId(clientId)
    .setClientSecret(clientSecret)
    .setCallbackFunction('authCallback')
    .setPropertyStore(props)
    .setScope(scopes.join(' '))
    .setParam('access_type', 'offline')
    .setParam('prompt', 'consent');
}

/** 認証済みならアクセストークンを返す。未認証ならエラー */
function channelToken_(ch) {
  const service = getChannelService_(ch.key, scopesForMode_(ch.mode));
  if (!service.hasAccess()) {
    throw new Error('未認証です: ' + ch.key + '（メニュー「認証URLを発行」→ チャンネル所有者に承認してもらってください）');
  }
  return service.getAccessToken();
}

/** FULLに必要なアップロードスコープが付与済みか */
function hasUploadScope_(channelKey) {
  const service = getChannelService_(channelKey, SCOPES_UPLOAD);
  if (!service.hasAccess()) return false;
  const token = service.getToken() || {};
  return String(token.scope || '').indexOf('youtube.upload') !== -1;
}

/**
 * メニュー: 有効な全チャンネルの認証URLを発行してシートに書き込む。
 * 既に有効なトークンがあり、スコープも足りているチャンネルはスキップ。
 */
function issueAuthUrls() {
  const c = COL.CHANNELS;
  let issued = 0;
  getChannels_().forEach(function (ch) {
    if (!ch.enabled) return;
    const scopes = scopesForMode_(ch.mode);
    const service = getChannelService_(ch.key, scopes);
    if (service.hasAccess() && (ch.mode !== MODES.FULL || hasUploadScope_(ch.key))) {
      writeChannelCell_(ch, c.AUTH_STATUS, authLabel_(ch));
      writeChannelCell_(ch, c.AUTH_URL, '');
      return;
    }
    service.reset(); // 古い/権限不足のトークンを破棄してやり直す
    const url = getChannelService_(ch.key, scopes)
      .getAuthorizationUrl({ channelKey: ch.key, scopeSet: scopes.join(' ') });
    writeChannelCell_(ch, c.AUTH_STATUS, '未認証(URL発行済み)');
    writeChannelCell_(ch, c.AUTH_URL, url);
    issued++;
  });
  toast_('認証URLを ' + issued + ' 件発行しました。各チャンネルの所有者にURLを開いて承認してもらってください。');
}

/**
 * OAuthコールバック。認証URLの承認後にGoogleから呼ばれる。
 * getAuthorizationUrlに渡した channelKey / scopeSet がそのまま返ってくる。
 */
function authCallback(request) {
  const channelKey = request.parameter.channelKey;
  const scopes = String(request.parameter.scopeSet || SCOPES_READONLY.join(' ')).split(' ');
  const service = getChannelService_(channelKey, scopes);
  const ok = service.handleCallback(request);
  if (ok) {
    try {
      const ch = getChannel_(channelKey);
      if (ch) {
        writeChannelCell_(ch, COL.CHANNELS.AUTH_STATUS, authLabel_(ch));
        writeChannelCell_(ch, COL.CHANNELS.AUTH_URL, '');
      }
    } catch (e) {
      logError_('認証', channelKey, '', 'コールバック後のシート更新に失敗', e.stack);
    }
    return HtmlService.createHtmlOutput(
      '<p>✅ 認証が完了しました（チャンネルキー: ' + channelKey + '）。このタブは閉じて構いません。</p>');
  }
  return HtmlService.createHtmlOutput('<p>❌ 認証に失敗しました。もう一度認証URLを開き直してください。</p>');
}

/** 認証状態のラベルを作る（実際にAPIを呼んでチャンネル一致まで確認するのはrefreshAuthStatus） */
function authLabel_(ch) {
  const service = getChannelService_(ch.key, scopesForMode_(ch.mode));
  if (!service.hasAccess()) return '未認証';
  if (ch.mode === MODES.FULL && !hasUploadScope_(ch.key)) return '要再認証(アップロード権限なし)';
  return ch.mode === MODES.FULL ? '認証済み(投稿可)' : '認証済み(分析のみ)';
}

/**
 * メニュー: 全チャンネルの認証状態を実際にAPIを呼んで確認し、シートに反映する。
 * 認証アカウントのチャンネルIDがシートのIDと違う場合は警告を出す。
 */
function refreshAuthStatus() {
  const c = COL.CHANNELS;
  getChannels_().forEach(function (ch) {
    if (!ch.enabled) return;
    let label;
    try {
      const service = getChannelService_(ch.key, scopesForMode_(ch.mode));
      if (!service.hasAccess()) {
        label = '未認証';
      } else {
        const res = ytApi_(ch, 'https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true');
        const item = (res.items || [])[0];
        if (!item) {
          label = 'エラー(チャンネル取得不可)';
        } else if (ch.channelId && ch.channelId !== item.id) {
          label = '⚠アカウント不一致(' + item.snippet.title + ')';
        } else {
          if (!ch.channelId) writeChannelCell_(ch, c.CHANNEL_ID, item.id);
          label = authLabel_(ch) + ' / ' + item.snippet.title;
        }
      }
    } catch (e) {
      label = 'エラー: ' + String(e.message).slice(0, 80);
      logError_('認証', ch.key, '', e.message, e.stack);
    }
    writeChannelCell_(ch, c.AUTH_STATUS, label);
  });
  toast_('認証状態を更新しました。');
}

/**
 * チャンネルのトークンでYouTube系APIを叩く共通関数。
 * @param {Object} ch チャンネル
 * @param {string} url フルURL
 * @param {Object=} options UrlFetchAppオプション
 * @return {Object} JSONレスポンス
 */
function ytApi_(ch, url, options) {
  const opt = options || {};
  opt.headers = opt.headers || {};
  opt.headers['Authorization'] = 'Bearer ' + channelToken_(ch);
  opt.muteHttpExceptions = true;
  const res = UrlFetchApp.fetch(url, opt);
  const code = res.getResponseCode();
  if (code >= 400) {
    throw new Error('APIエラー HTTP' + code + ': ' + res.getContentText().slice(0, 500));
  }
  const text = res.getContentText();
  return text ? JSON.parse(text) : {};
}
