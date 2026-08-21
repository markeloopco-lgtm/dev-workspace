/**
 * 09_judgement.gs
 * 自動判定: チャンネルごとの基準値（04_チャンネル設定で上書き可）に基づいて
 * 各動画へ判定ラベルとメモを付ける。
 *
 * ルール（要件対応）:
 * - CTRが低い                       → サムネ改善候補
 * - CTRが低い＋インプレッション多い   → サムネ差し替え優先
 * - CTRが高い＋再生が弱い            → インプレッション不足
 * - 7日再生がチャンネル平均を大幅超え  → ヒット
 * - CTRが7日前より低下              → CTR低下（警告）
 * - 日次再生が直近平均を大幅超え      → 急上昇
 *
 * チャンネル平均CTRは「直近N本（既定10本）」の平均。CTRが取得できない環境では
 * CTR系ルールをスキップし、再生系ルールのみ動く。
 */

/**
 * 1チャンネル分のレコードへ判定を書き込む（records を直接更新）。
 * @param {Object} ch チャンネル
 * @param {Array<Object>} records 08_analytics.gsで組み立てたレコード
 */
function judgeChannelRecords_(ch, records) {
  if (!records.length) return;
  const th = ch.thresholds;

  // チャンネル集計値
  const recent = records.slice().sort(function (a, b) {
    return b.publishedAt.getTime() - a.publishedAt.getTime();
  }).slice(0, CONFIG.CTR_BASELINE_COUNT);
  const avgCtr = avg_(recent.map(function (r) { return r.ctr; }));
  const avgImp = avg_(records.map(function (r) { return r.impressions; }));
  const medViews90 = median_(records.map(function (r) { return r.views90; }));
  const avgV7 = avg_(records.map(function (r) { return r.v7; }));

  records.forEach(function (r) {
    const labels = [];
    const memo = [];

    // --- CTR系（CTRが取得できている場合のみ） ---
    if (r.ctr != null && avgCtr != null && r.ageDays >= 2) {
      const lowLine = th.ctrLowAbs != null ? th.ctrLowAbs : avgCtr * DEFAULT_THRESHOLDS.CTR_LOW_RATIO;
      const isLow = r.ctr < lowLine;
      const impHigh = r.impressions != null && avgImp != null && r.impressions >= avgImp * th.impMult;

      if (isLow && impHigh) {
        labels.push(JUDGE.THUMB_SWAP);
        memo.push('CTR ' + r.ctr + '% / 直近' + recent.length + '本平均 ' + round2_(avgCtr) + '% → サムネ変更推奨（表示は十分）');
      } else if (isLow) {
        labels.push(JUDGE.THUMB_IMPROVE);
        memo.push('CTR ' + r.ctr + '% / 直近' + recent.length + '本平均 ' + round2_(avgCtr) + '%');
      }
      if (!isLow && medViews90 != null && r.views90 != null
          && r.views90 < medViews90 * DEFAULT_THRESHOLDS.WEAK_VIEW_RATIO) {
        labels.push(JUDGE.IMP_SHORTAGE);
        memo.push('CTR良好だが再生 ' + r.views90 + '（ch中央値 ' + Math.round(medViews90) + '）→ 露出不足');
      }
      if (r.ctr7ago != null && r.ctr7ago - r.ctr >= th.ctrDropPt) {
        labels.push(JUDGE.CTR_DROP);
        memo.push('CTRが7日で ' + round2_(r.ctr7ago) + '%→' + r.ctr + '% に低下');
      }
    }

    // --- 再生系 ---
    if (r.v7 != null && avgV7 != null && avgV7 > 0 && r.v7 >= avgV7 * th.hitMult) {
      labels.push(JUDGE.HIT);
      memo.push('7日再生 ' + r.v7 + '（ch平均 ' + Math.round(avgV7) + ' の ' + round2_(r.v7 / avgV7) + '倍）');
    }
    if (r.d1 != null && r.d7 != null && r.ageDays > 3) {
      const dailyAvg = r.d7 / 7;
      if (dailyAvg > 0 && r.d1 >= dailyAvg * th.spikeMult) {
        labels.push(JUDGE.SPIKE);
        memo.push('日次再生 ' + r.d1 + '（直近7日平均 ' + Math.round(dailyAvg) + '/日 の ' + round2_(r.d1 / dailyAvg) + '倍）');
      }
    }

    r.judge = labels.join('、');
    r.judgeMemo = memo.join(' / ');
  });
}
