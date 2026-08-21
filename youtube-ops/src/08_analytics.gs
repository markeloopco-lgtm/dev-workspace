/**
 * 08_analytics.gs
 * 毎日の分析データ取得（YouTube Data API + YouTube Analytics API）
 *
 * - 公開90日以内の動画を対象に、累計再生（Data API）と90日間の詳細指標
 *   （Analytics API: 視聴時間・視聴率・登録者増・インプレッション・サムネCTR等）を取得
 * - 02_動画分析を更新し、03_日次ログへ当日のスナップショットを追記
 * - 日次ログから前日比・3日/7日増加・公開後24h/3日/7日/30日の成績を計算
 * - チャンネルごとの自動判定（09_judgement.gs）→ ダッシュボード更新
 * - 6分制限にかかったら進捗を保存して1分後に自動再開（チャンネル単位）
 *
 * サムネのインプレッション/CTRは2026-01-15にAnalytics APIへ追加されたメトリクスを
 * 使う。APIが対応していない場合は空欄のままにし、CTR系の判定はスキップされる。
 */

let impressionsUnsupportedLogged_ = false;

/** メニュー/トリガー: 分析を実行（当日分。途中中断からの再開にも対応） */
function runDailyAnalytics() {
  const lock = tryLock_(20 * 1000);
  if (!lock) return;
  try {
    cleanupOneOffTrigger_('runDailyAnalytics');
    const t0 = Date.now();
    const now = new Date();
    const today = dateStr_(now);

    let progress = jsonProp_('analytics_progress') || {};
    if (progress.date !== today) progress = { date: today, done: [] };

    const channels = getChannels_().filter(function (ch) { return ch.enabled; });
    for (let i = 0; i < channels.length; i++) {
      const ch = channels[i];
      if (progress.done.indexOf(ch.key) !== -1) continue;
      try {
        processChannelAnalytics_(ch, today, now);
      } catch (e) {
        logError_('分析', ch.key, '', e.message, e.stack);
      }
      progress.done.push(ch.key);
      setJsonProp_('analytics_progress', progress);
      if (Date.now() - t0 > CONFIG.TIME_BUDGET_MS && i < channels.length - 1) {
        scheduleContinuation_('runDailyAnalytics');
        return; // 続きは1分後の自動実行で
      }
    }
    rebuildDashboard();
    pruneDailyLog_();
    toast_('分析を更新しました（' + channels.length + 'チャンネル）。');
  } finally {
    lock.releaseLock();
  }
}

/** 1チャンネル分の分析処理 */
function processChannelAnalytics_(ch, today, now) {
  // チャンネル情報（登録者数・アップロード動画プレイリスト）
  const chRes = ytApi_(ch, 'https://www.googleapis.com/youtube/v3/channels?part=id,snippet,statistics,contentDetails&mine=true');
  const chItem = (chRes.items || [])[0];
  if (!chItem) throw new Error('チャンネル情報が取得できません（認証アカウントにチャンネルがない可能性）');
  if (!ch.channelId) writeChannelCell_(ch, COL.CHANNELS.CHANNEL_ID, chItem.id);
  else if (ch.channelId !== chItem.id) {
    throw new Error('認証アカウントのチャンネル(' + chItem.snippet.title + ')がシートのチャンネルIDと一致しません');
  }
  cacheChannelStats_(ch.key, {
    subscribers: Number((chItem.statistics || {}).subscriberCount || 0),
    title: chItem.snippet.title,
    updatedAt: dateTimeStr_(now),
  });

  // 対象動画（公開90日以内）
  const uploadsId = ((chItem.contentDetails || {}).relatedPlaylists || {}).uploads;
  const videoIds = listRecentVideoIds_(ch, uploadsId, now);
  if (!videoIds.length) return;

  // 各種データ取得
  const meta = videosMeta_(ch, videoIds);            // Data API: タイトル/公開日/累計統計
  const core = analyticsByVideo_(ch, videoIds, now,  // Analytics API: 90日指標
    'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,subscribersGained,subscribersLost',
    '-views');
  const imp = tryImpressions_(ch, videoIds, now);    // Analytics API: インプレッション/CTR（取得可否は環境依存）

  // レコード組み立て
  const records = [];
  videoIds.forEach(function (id) {
    const m = meta[id];
    if (!m) return;
    const a = core[id] || {};
    const p = imp && imp[id] ? imp[id] : {};
    let ctr = p.ctr;
    if (ctr != null && CONFIG.CTR_IS_RATIO) ctr = ctr * 100;
    const subsNet = (a.subscribersGained != null || a.subscribersLost != null)
      ? Number(a.subscribersGained || 0) - Number(a.subscribersLost || 0) : null;
    records.push({
      videoId: id,
      title: m.title,
      publishedAt: m.publishedAt,
      viewsTotal: m.views,
      likes: m.likes,
      comments: m.comments,
      views90: a.views != null ? Number(a.views) : null,
      watchMin: a.estimatedMinutesWatched != null ? Number(a.estimatedMinutesWatched) : null,
      avgDur: a.averageViewDuration != null ? Number(a.averageViewDuration) : null,
      avgPct: a.averageViewPercentage != null ? round2_(Number(a.averageViewPercentage)) : null,
      subsNet: subsNet,
      impressions: p.impressions != null ? Number(p.impressions) : null,
      ctr: ctr != null ? round2_(ctr) : null,
      ageDays: ageDays_(m.publishedAt, now),
      d1: null, d3: null, d7: null,
      v24: null, v3: null, v7: null, v30: null,
      ctr7ago: null,
      judge: '', judgeMemo: '',
    });
  });

  // 日次ログ: 既存ログ読み込み → 当日分を追記 → 増分・マイルストーン計算
  const log = getChannelLog_(ch.key);
  appendDailyLog_(ch, records, log, today);
  computeDerived_(records, log, today);

  // 自動判定 → 02_動画分析へ反映
  judgeChannelRecords_(ch, records);
  syncVideosSheet_(ch, records, now);
}

/**
 * アップロードプレイリストから、公開がANALYSIS_WINDOW_DAYS以内の動画IDを集める。
 * プレイリストは新しい順のため、古い動画ばかりのページに達したら打ち切る。
 */
function listRecentVideoIds_(ch, uploadsId, now) {
  if (!uploadsId) return [];
  const cutoff = new Date(now.getTime() - CONFIG.ANALYSIS_WINDOW_DAYS * 24 * 60 * 60 * 1000);
  const ids = [];
  let pageToken = '';
  for (let page = 0; page < 20; page++) {
    const res = ytApi_(ch, 'https://www.googleapis.com/youtube/v3/playlistItems?part=contentDetails&maxResults=50'
      + '&playlistId=' + encodeURIComponent(uploadsId)
      + (pageToken ? '&pageToken=' + pageToken : ''));
    const items = res.items || [];
    let anyRecent = false;
    items.forEach(function (item) {
      const d = item.contentDetails || {};
      const published = d.videoPublishedAt ? new Date(d.videoPublishedAt) : null;
      if (published && published >= cutoff) {
        ids.push(d.videoId);
        anyRecent = true;
      }
    });
    pageToken = res.nextPageToken;
    if (!pageToken || (!anyRecent && items.length)) break;
  }
  return ids;
}

/** videos.listで基本情報と累計統計を取得（50件ずつ） */
function videosMeta_(ch, videoIds) {
  const map = {};
  for (let i = 0; i < videoIds.length; i += 50) {
    const batch = videoIds.slice(i, i + 50);
    const res = ytApi_(ch, 'https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id=' + batch.join(','));
    (res.items || []).forEach(function (item) {
      const st = item.statistics || {};
      map[item.id] = {
        title: item.snippet.title,
        publishedAt: new Date(item.snippet.publishedAt),
        views: Number(st.viewCount || 0),
        likes: st.likeCount != null ? Number(st.likeCount) : null,
        comments: st.commentCount != null ? Number(st.commentCount) : null,
      };
    });
    Utilities.sleep(100);
  }
  return map;
}

/**
 * Analytics APIで動画別の90日集計を取得する。
 * @return {Object} videoId → {メトリクス名: 値}
 */
function analyticsByVideo_(ch, videoIds, now, metrics, sort) {
  const startDate = dateStr_(new Date(now.getTime() - CONFIG.ANALYSIS_WINDOW_DAYS * 24 * 60 * 60 * 1000));
  const endDate = dateStr_(now);
  const map = {};
  for (let i = 0; i < videoIds.length; i += 50) {
    const batch = videoIds.slice(i, i + 50);
    const url = 'https://youtubeanalytics.googleapis.com/v2/reports'
      + '?ids=' + encodeURIComponent('channel==MINE')
      + '&startDate=' + startDate + '&endDate=' + endDate
      + '&metrics=' + encodeURIComponent(metrics)
      + '&dimensions=video'
      + '&filters=' + encodeURIComponent('video==' + batch.join(','))
      + (sort ? '&sort=' + encodeURIComponent(sort) : '')
      + '&maxResults=200';
    const res = ytApi_(ch, url);
    const headers = (res.columnHeaders || []).map(function (h) { return h.name; });
    (res.rows || []).forEach(function (row) {
      const obj = {};
      headers.forEach(function (name, idx) { obj[name] = row[idx]; });
      map[obj.video] = obj;
    });
    Utilities.sleep(100);
  }
  return map;
}

/**
 * サムネのインプレッション/CTRの取得を試みる。未対応環境ではnullを返す
 * （初回のみエラーログに記録し、CTR列は空欄のままになる）。
 */
function tryImpressions_(ch, videoIds, now) {
  const metrics = PropertiesService.getScriptProperties().getProperty('IMP_METRICS') || CONFIG.IMP_METRICS;
  const names = metrics.split(',');
  try {
    const raw = analyticsByVideo_(ch, videoIds, now, metrics, '-' + names[0]);
    const map = {};
    Object.keys(raw).forEach(function (id) {
      map[id] = { impressions: raw[id][names[0]], ctr: raw[id][names[1]] };
    });
    return map;
  } catch (e) {
    if (!impressionsUnsupportedLogged_) {
      impressionsUnsupportedLogged_ = true;
      logError_('分析', ch.key, 'インプレッション',
        'サムネ表示回数/CTRを取得できませんでした。この環境ではAPI未対応の可能性があります（CTR列は空欄になります）',
        e.message);
    }
    return null;
  }
}

/**
 * 03_日次ログからチャンネル分を読み込む。
 * @return {Object} videoId → [{d:'yyyy-MM-dd', views, ctr}] 日付昇順
 */
function getChannelLog_(channelKey) {
  const data = sheetData_(sheet_(SHEETS.DAILY));
  const c = COL.DAILY;
  const map = {};
  data.forEach(function (r) {
    if (String(r[c.CHANNEL - 1]) !== channelKey) return;
    const id = String(r[c.VIDEO_ID - 1]);
    const d = r[c.DATE - 1] instanceof Date ? dateStr_(r[c.DATE - 1]) : String(r[c.DATE - 1]);
    if (!map[id]) map[id] = [];
    map[id].push({ d: d, views: Number(r[c.VIEWS_TOTAL - 1] || 0), ctr: numOr_(r[c.CTR - 1], null) });
  });
  Object.keys(map).forEach(function (id) {
    map[id].sort(function (a, b) { return a.d < b.d ? -1 : 1; });
  });
  return map;
}

/** 当日分のスナップショットを03_日次ログへ追記（同日分が既にあればスキップ） */
function appendDailyLog_(ch, records, log, today) {
  const sh = sheet_(SHEETS.DAILY);
  const rows = [];
  records.forEach(function (r) {
    const entries = log[r.videoId] || [];
    if (entries.some(function (e) { return e.d === today; })) return;
    const prev = entries.length ? entries[entries.length - 1] : null;
    const delta = prev ? r.viewsTotal - prev.views : '';
    rows.push([
      today, ch.key, r.videoId, r.title, r.viewsTotal, delta,
      r.impressions != null ? r.impressions : '', r.ctr != null ? r.ctr : '',
      r.avgPct != null ? r.avgPct : '', r.watchMin != null ? r.watchMin : '',
      r.subsNet != null ? r.subsNet : '', r.likes != null ? r.likes : '',
      r.comments != null ? r.comments : '',
    ]);
    // メモリ上のログにも反映（この後の増分計算で使う）
    entries.push({ d: today, views: r.viewsTotal, ctr: r.ctr });
    log[r.videoId] = entries;
  });
  if (rows.length) {
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, HEADERS.DAILY.length).setValues(rows);
  }
}

/** 指定日以前で最も新しいログエントリを返す */
function entryAtOrBefore_(entries, targetD) {
  let hit = null;
  for (let i = 0; i < entries.length; i++) {
    if (entries[i].d <= targetD) hit = entries[i];
    else break;
  }
  return hit;
}

/** 指定日以降で最も古いログエントリを返す */
function entryAtOrAfter_(entries, targetD) {
  for (let i = 0; i < entries.length; i++) {
    if (entries[i].d >= targetD) return entries[i];
  }
  return null;
}

/** 前日比・3日/7日増加・公開後24h/3日/7日/30日の成績・7日前CTRを計算 */
function computeDerived_(records, log, today) {
  records.forEach(function (r) {
    const entries = log[r.videoId] || [];
    if (!entries.length) return;
    const latest = entries[entries.length - 1];

    const d1 = entryAtOrBefore_(entries, dateStr_(daysAgo_(1)));
    const d3 = entryAtOrBefore_(entries, dateStr_(daysAgo_(3)));
    const d7 = entryAtOrBefore_(entries, dateStr_(daysAgo_(7)));
    if (d1 && d1.d !== today) r.d1 = latest.views - d1.views;
    if (d3 && d3.d !== today) r.d3 = latest.views - d3.views;
    if (d7 && d7.d !== today) r.d7 = latest.views - d7.views;
    if (d7 && d7.d !== today && d7.ctr != null) r.ctr7ago = d7.ctr;

    // 公開後N日のマイルストーン: 公開日+N日以降の最初のスナップショットの累計再生
    // （毎日1回の記録のため最大1日程度の誤差あり。投稿時期が違う動画同士の比較用）
    // 記録開始がN日時点より2日以上遅れた動画は「取得不能」として空欄のままにする
    const pub = r.publishedAt;
    const milestones = [[1, 'v24'], [3, 'v3'], [7, 'v7'], [30, 'v30']];
    milestones.forEach(function (ms) {
      const days = ms[0], field = ms[1];
      if (r.ageDays < days) return;
      const target = dateStr_(new Date(pub.getTime() + days * 24 * 60 * 60 * 1000));
      const limit = dateStr_(new Date(pub.getTime() + (days + 2) * 24 * 60 * 60 * 1000));
      const e = entryAtOrAfter_(entries, target);
      if (e && e.d <= limit) r[field] = e.views;
    });
  });
}

/**
 * 02_動画分析へレコードを反映する（既存行は更新・新規は追記・他チャンネル行は不変）。
 * 24h/3日/7日/30日再生は一度入った値を保持する（キャプチャ方式）。
 */
function syncVideosSheet_(ch, records, now) {
  const sh = sheet_(SHEETS.VIDEOS);
  const c = COL.VIDEOS;
  const width = HEADERS.VIDEOS.length;
  const data = sheetData_(sh);
  const rowByVideo = {};
  data.forEach(function (r, i) { rowByVideo[String(r[c.VIDEO_ID - 1])] = i; });

  const nowStr = dateTimeStr_(now);
  records.forEach(function (r) {
    const existing = rowByVideo[r.videoId] != null ? data[rowByVideo[r.videoId]] : null;
    const keep = function (col, newVal) {
      // 既存値があれば保持（マイルストーンのキャプチャ用）
      if (existing && existing[col - 1] !== '' && existing[col - 1] != null) return existing[col - 1];
      return newVal != null ? newVal : '';
    };
    const row = new Array(width).fill('');
    row[c.VIDEO_ID - 1] = r.videoId;
    row[c.CHANNEL - 1] = ch.key;
    row[c.CH_NAME - 1] = ch.name;
    row[c.TITLE - 1] = r.title;
    row[c.PUBLISHED - 1] = r.publishedAt;
    row[c.URL - 1] = 'https://youtu.be/' + r.videoId;
    row[c.AGE_DAYS - 1] = r.ageDays;
    row[c.VIEWS_TOTAL - 1] = r.viewsTotal;
    row[c.VIEWS_90D - 1] = r.views90 != null ? r.views90 : '';
    row[c.IMPRESSIONS - 1] = r.impressions != null ? r.impressions : '';
    row[c.CTR - 1] = r.ctr != null ? r.ctr : '';
    row[c.AVG_DUR - 1] = r.avgDur != null ? r.avgDur : '';
    row[c.AVG_PCT - 1] = r.avgPct != null ? r.avgPct : '';
    row[c.WATCH_MIN - 1] = r.watchMin != null ? r.watchMin : '';
    row[c.SUBS - 1] = r.subsNet != null ? r.subsNet : '';
    row[c.LIKES - 1] = r.likes != null ? r.likes : '';
    row[c.COMMENTS - 1] = r.comments != null ? r.comments : '';
    row[c.D1_DELTA - 1] = r.d1 != null ? r.d1 : '';
    row[c.D3_DELTA - 1] = r.d3 != null ? r.d3 : '';
    row[c.D7_DELTA - 1] = r.d7 != null ? r.d7 : '';
    row[c.V24H - 1] = keep(c.V24H, r.v24);
    row[c.V3D - 1] = keep(c.V3D, r.v3);
    row[c.V7D - 1] = keep(c.V7D, r.v7);
    row[c.V30D - 1] = keep(c.V30D, r.v30);
    row[c.SUB_EFF - 1] = (r.subsNet != null && r.views90) ? round2_(r.subsNet / r.views90 * 1000) : '';
    row[c.JUDGE - 1] = r.judge || '';
    row[c.JUDGE_MEMO - 1] = r.judgeMemo || '';
    row[c.UPDATED - 1] = nowStr;

    if (existing) {
      data[rowByVideo[r.videoId]] = row;
    } else {
      rowByVideo[r.videoId] = data.length;
      data.push(row);
    }
  });

  if (data.length) {
    sh.getRange(2, 1, data.length, width).setValues(data.map(function (r) {
      while (r.length < width) r.push('');
      return r.slice(0, width);
    }));
  }
}

/** 日次ログの行数が上限を超えたら古い順に削除 */
function pruneDailyLog_() {
  const sh = sheet_(SHEETS.DAILY);
  const over = sh.getLastRow() - 1 - CONFIG.DAILY_LOG_MAX_ROWS;
  if (over > 0) sh.deleteRows(2, over);
}
