// 設定ページ: APIキー・チャンネル基本情報・定型文の管理

const DEFAULTS = {
  apiKey: "",
  model: "gemini-2.5-flash",
  channels: {},
  snippets: [],
};

// 編集中の状態（保存ボタンでまとめてstorageへ書き込む）
let state = null;

function newId() {
  return `sn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function element(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function labeledInput(labelText, value, placeholder, onInput) {
  const input = element("input", { type: "text", placeholder });
  input.value = value || "";
  input.addEventListener("input", () => onInput(input.value));
  return element("label", { text: labelText }, [input]);
}

function labeledTextarea(labelText, value, placeholder, rows, onInput) {
  const textarea = element("textarea", { placeholder, rows: String(rows) });
  textarea.value = value || "";
  textarea.addEventListener("input", () => onInput(textarea.value));
  return element("label", { text: labelText }, [textarea]);
}

// ---- チャンネル ------------------------------------------------------------

function renderChannels() {
  const list = document.getElementById("channel-list");
  list.textContent = "";
  const ids = Object.keys(state.channels);
  if (ids.length === 0) {
    list.appendChild(
      element("p", { class: "empty", text: "まだチャンネルが登録されていません。" })
    );
    return;
  }
  for (const id of ids) {
    const ch = state.channels[id];
    const card = element("div", { class: "card" });
    card.appendChild(
      element("div", { class: "card-header" }, [
        element("code", { text: id }),
        element("button", {
          class: "danger",
          text: "削除",
          onclick: () => {
            if (confirm(`チャンネル ${ch.name || id} を削除しますか？`)) {
              delete state.channels[id];
              renderChannels();
            }
          },
        }),
      ])
    );
    card.appendChild(
      labeledInput("チャンネル名", ch.name, "例: ○○ちゃんねる", (v) => (ch.name = v))
    );
    card.appendChild(
      labeledTextarea(
        "基本情報（方向性・視聴者層・口調・NGなど）",
        ch.profile,
        "例: ゲーム実況メインのVTuberチャンネル。視聴者は10〜20代。明るく丁寧な口調。ネタバレ表現はNG",
        4,
        (v) => (ch.profile = v)
      )
    );
    card.appendChild(
      labeledTextarea(
        "タイトルの傾向・ルール（任意）",
        ch.titleRules,
        "例: 【○○】で始める。35文字以内。絵文字は1つまで",
        2,
        (v) => (ch.titleRules = v)
      )
    );
    list.appendChild(card);
  }
}

function addChannel() {
  const id = prompt(
    "チャンネルIDを入力してください（YouTube StudioのURLの UC から始まる部分）"
  );
  if (!id) return;
  const trimmed = id.trim();
  if (!/^UC[\w-]{10,}$/.test(trimmed)) {
    alert("チャンネルIDの形式が正しくないようです（UCから始まる文字列）");
    return;
  }
  if (!state.channels[trimmed]) {
    state.channels[trimmed] = { name: "", profile: "", titleRules: "", defaultSnippetIds: [] };
  }
  renderChannels();
}

// ---- 定型文 ----------------------------------------------------------------

function renderSnippets() {
  const list = document.getElementById("snippet-list");
  list.textContent = "";
  if (state.snippets.length === 0) {
    list.appendChild(
      element("p", { class: "empty", text: "まだ定型文が登録されていません。" })
    );
    return;
  }
  state.snippets.forEach((snippet, index) => {
    const card = element("div", { class: "card" });
    const nameInput = element("input", {
      type: "text",
      placeholder: "例: LINE誘導",
      class: "snippet-label",
    });
    nameInput.value = snippet.label || "";
    nameInput.addEventListener("input", () => (snippet.label = nameInput.value));
    card.appendChild(
      element("div", { class: "card-header" }, [
        nameInput,
        element("button", {
          class: "danger",
          text: "削除",
          onclick: () => {
            if (confirm(`定型文「${snippet.label || "無題"}」を削除しますか？`)) {
              state.snippets.splice(index, 1);
              renderSnippets();
            }
          },
        }),
      ])
    );
    const textarea = element("textarea", {
      rows: "4",
      placeholder:
        "例: 🎁限定情報はLINEで配信中！\nhttps://line.me/xxxx",
    });
    textarea.value = snippet.text || "";
    textarea.addEventListener("input", () => (snippet.text = textarea.value));
    card.appendChild(element("label", { text: "本文（概要欄にそのまま追加されます）" }, [textarea]));
    list.appendChild(card);
  });
}

function addSnippet() {
  state.snippets.push({ id: newId(), label: "", text: "" });
  renderSnippets();
}

// ---- 保存・読み込み --------------------------------------------------------

async function save() {
  state.apiKey = document.getElementById("api-key").value.trim();
  state.model = document.getElementById("model").value.trim() || DEFAULTS.model;
  await chrome.storage.local.set(state);
  const status = document.getElementById("status");
  status.textContent = "保存しました ✓";
  setTimeout(() => (status.textContent = ""), 2500);
}

async function load() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  state = { ...DEFAULTS, ...stored };
  document.getElementById("api-key").value = state.apiKey;
  document.getElementById("model").value = state.model;
  renderChannels();
  renderSnippets();
}

document.getElementById("add-channel").addEventListener("click", addChannel);
document.getElementById("add-snippet").addEventListener("click", addSnippet);
document.getElementById("save").addEventListener("click", save);
load();
