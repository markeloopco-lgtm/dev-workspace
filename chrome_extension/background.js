// Service worker: Gemini API呼び出しと設定ページ起動を担当。
// APIキーをcontent script側に渡さず、通信はここに集約する。

const DEFAULT_MODEL = "gemini-2.5-flash";

const DEFAULTS = {
  apiKey: "",
  model: DEFAULT_MODEL,
  channels: {}, // { [channelId]: { name, profile, titleRules, defaultSnippetIds } }
  snippets: [], // [{ id, label, text }]
};

async function loadSettings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

function buildPrompt({ channel, videoInfo, snippetLabels }) {
  const lines = [];
  lines.push(
    "あなたは日本語のYouTube動画メタデータ作成アシスタントです。",
    "以下の情報をもとに、動画のタイトルと概要欄を作成してください。",
    ""
  );
  if (channel) {
    lines.push("# チャンネル情報");
    if (channel.name) lines.push(`チャンネル名: ${channel.name}`);
    if (channel.profile) lines.push(`基本情報（方向性・視聴者層・口調など）:\n${channel.profile}`);
    if (channel.titleRules) lines.push(`タイトルの傾向・ルール:\n${channel.titleRules}`);
    lines.push("");
  }
  lines.push("# 今回の動画の内容・キーワード", videoInfo, "");
  lines.push(
    "# 作成ルール",
    "- タイトル: 100文字以内。クリックしたくなる具体的な表現。日本語で28〜40文字程度を目安にする",
    "- 概要欄: 動画の内容紹介を2〜4段落程度。最後に関連ハッシュタグを3〜6個付ける",
    "- チャンネル情報の口調・雰囲気に合わせる",
    "- 誇張しすぎた釣りタイトルにはしない"
  );
  if (snippetLabels && snippetLabels.length > 0) {
    lines.push(
      `- 次の定型文は後から自動で概要欄の末尾に追加されるので、本文には同じ内容を含めない: ${snippetLabels.join("、")}`
    );
  }
  return lines.join("\n");
}

async function callGemini({ apiKey, model, prompt }) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(
    model
  )}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const body = {
    contents: [{ role: "user", parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: 0.8,
      responseMimeType: "application/json",
      responseSchema: {
        type: "OBJECT",
        properties: {
          title: { type: "STRING", description: "動画タイトル（100文字以内）" },
          description: { type: "STRING", description: "概要欄本文" },
        },
        required: ["title", "description"],
      },
    },
  };

  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err?.error?.message) detail = `${detail}: ${err.error.message}`;
    } catch (_) {
      /* JSONでないエラー応答はそのままHTTPコードだけ返す */
    }
    if (res.status === 400 || res.status === 403) {
      throw new Error(`APIキーが正しくない可能性があります（${detail}）`);
    }
    if (res.status === 429) {
      throw new Error(`無料枠のレート制限に達しました。少し待って再実行してください（${detail}）`);
    }
    throw new Error(`Gemini APIエラー（${detail}）`);
  }

  const data = await res.json();
  const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) {
    const reason = data?.candidates?.[0]?.finishReason || "応答が空でした";
    throw new Error(`生成結果を取得できませんでした（${reason}）`);
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_) {
    throw new Error("生成結果の形式が不正でした。もう一度お試しください");
  }
  if (!parsed.title || !parsed.description) {
    throw new Error("タイトルまたは概要欄が生成されませんでした。もう一度お試しください");
  }
  return { title: String(parsed.title).trim(), description: String(parsed.description).trim() };
}

async function handleGenerate(payload) {
  const settings = await loadSettings();
  if (!settings.apiKey) {
    return { ok: false, error: "Gemini APIキーが未設定です。設定画面から登録してください" };
  }
  const channel = payload.channelId ? settings.channels[payload.channelId] : null;
  const prompt = buildPrompt({
    channel,
    videoInfo: payload.videoInfo,
    snippetLabels: payload.snippetLabels,
  });
  const result = await callGemini({
    apiKey: settings.apiKey,
    model: settings.model || DEFAULT_MODEL,
    prompt,
  });
  return { ok: true, ...result };
}

async function handleRegisterChannel(payload) {
  const settings = await loadSettings();
  if (payload.channelId && !settings.channels[payload.channelId]) {
    settings.channels[payload.channelId] = {
      name: "",
      profile: "",
      titleRules: "",
      defaultSnippetIds: [],
    };
    await chrome.storage.local.set({ channels: settings.channels });
  }
  chrome.runtime.openOptionsPage();
  return { ok: true };
}

async function handleSaveDefaultSnippets(payload) {
  const settings = await loadSettings();
  const channel = settings.channels[payload.channelId];
  if (channel) {
    channel.defaultSnippetIds = payload.snippetIds || [];
    await chrome.storage.local.set({ channels: settings.channels });
  }
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const respond = (promise) =>
    promise
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));

  switch (message?.type) {
    case "generate":
      respond(handleGenerate(message.payload || {}));
      return true; // 非同期応答
    case "getSettings":
      respond(loadSettings().then((s) => ({ ok: true, settings: s })));
      return true;
    case "openOptions":
      chrome.runtime.openOptionsPage();
      sendResponse({ ok: true });
      return false;
    case "registerChannel":
      respond(handleRegisterChannel(message.payload || {}));
      return true;
    case "saveDefaultSnippets":
      respond(handleSaveDefaultSnippets(message.payload || {}));
      return true;
    default:
      return false;
  }
});
