// YouTube Studioの動画詳細編集画面（アップロードダイアログ含む）に
// 「AI入力」ボタンとパネルを注入する。

(() => {
  const BUTTON_ID = "yaia-launch-button";
  const PANEL_ID = "yaia-panel";

  // ---- YouTube Studio側のDOM取得 -------------------------------------------

  function getChannelId() {
    const m = location.href.match(/\/channel\/(UC[\w-]+)/);
    return m ? m[1] : null;
  }

  function findTitleBox() {
    return (
      document.querySelector("#title-textarea #textbox") ||
      document.querySelector("ytcp-video-title #textbox")
    );
  }

  function findDescriptionBox() {
    return (
      document.querySelector("#description-textarea #textbox") ||
      document.querySelector("ytcp-video-description #textbox")
    );
  }

  function editorIsOpen() {
    return !!(findTitleBox() && findDescriptionBox());
  }

  // contenteditableへ、YouTube Studio(Polymer)が変更を認識する形で書き込む
  function setEditorText(box, text) {
    box.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(box);
    selection.removeAllRanges();
    selection.addRange(range);

    let inserted = false;
    try {
      inserted = document.execCommand("insertText", false, text);
    } catch (_) {
      inserted = false;
    }
    if (!inserted || box.textContent !== text) {
      box.textContent = text;
      box.dispatchEvent(
        new InputEvent("input", { bubbles: true, composed: true, data: text })
      );
    }
    box.dispatchEvent(new Event("change", { bubbles: true }));
    box.blur();
  }

  function sendMessage(type, payload) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, payload }, (res) => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            resolve(res || { ok: false, error: "応答がありません" });
          }
        });
      } catch (e) {
        resolve({ ok: false, error: e.message });
      }
    });
  }

  // ---- パネルUI -------------------------------------------------------------

  function el(tag, attrs = {}, children = []) {
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

  function closePanel() {
    document.getElementById(PANEL_ID)?.remove();
  }

  async function openPanel() {
    closePanel();

    const res = await sendMessage("getSettings");
    if (!res.ok) {
      alert(`設定を読み込めませんでした: ${res.error}`);
      return;
    }
    const settings = res.settings;
    const channelId = getChannelId();
    const channel = channelId ? settings.channels[channelId] : null;

    const panel = el("div", { id: PANEL_ID });

    // ヘッダー
    const header = el("div", { class: "yaia-header" }, [
      el("span", { class: "yaia-title", text: "AIタイトル・概要欄アシスタント" }),
      el("button", {
        class: "yaia-icon-btn",
        title: "設定を開く",
        text: "⚙",
        onclick: () => sendMessage("openOptions"),
      }),
      el("button", {
        class: "yaia-icon-btn",
        title: "閉じる",
        text: "✕",
        onclick: closePanel,
      }),
    ]);
    panel.appendChild(header);

    const body = el("div", { class: "yaia-body" });
    panel.appendChild(body);

    // APIキー未設定の案内
    if (!settings.apiKey) {
      body.appendChild(
        el("div", { class: "yaia-notice yaia-warn" }, [
          el("span", {
            text: "Gemini APIキーが未設定です。先に設定画面で登録してください。",
          }),
          el("button", {
            class: "yaia-btn",
            text: "設定を開く",
            onclick: () => sendMessage("openOptions"),
          }),
        ])
      );
    }

    // チャンネル表示
    if (channelId) {
      if (channel) {
        body.appendChild(
          el("div", {
            class: "yaia-notice",
            text: `チャンネル: ${channel.name || channelId}（基本情報を反映します）`,
          })
        );
      } else {
        body.appendChild(
          el("div", { class: "yaia-notice yaia-warn" }, [
            el("span", {
              text: "このチャンネルは未登録です。登録すると方向性・口調などをAIに伝えられます。",
            }),
            el("button", {
              class: "yaia-btn",
              text: "このチャンネルを登録",
              onclick: () => sendMessage("registerChannel", { channelId }),
            }),
          ])
        );
      }
    } else {
      body.appendChild(
        el("div", {
          class: "yaia-notice yaia-warn",
          text: "URLからチャンネルIDを取得できませんでした（チャンネル情報なしで生成します）",
        })
      );
    }

    // 動画内容の入力
    body.appendChild(el("label", { class: "yaia-label", text: "動画の内容・キーワード" }));
    const videoInfoInput = el("textarea", {
      class: "yaia-textarea",
      rows: "3",
      placeholder: "例: 新衣装お披露目の雑談配信。視聴者の質問に答えながら夏の予定を話す",
    });
    body.appendChild(videoInfoInput);

    // 定型文チェックボックス
    const snippetChecks = [];
    if (settings.snippets.length > 0) {
      body.appendChild(
        el("label", { class: "yaia-label", text: "概要欄の末尾に追加する定型文" })
      );
      const list = el("div", { class: "yaia-snippets" });
      const defaults = new Set(channel?.defaultSnippetIds || []);
      for (const snippet of settings.snippets) {
        const checkbox = el("input", { type: "checkbox" });
        checkbox.checked = defaults.has(snippet.id);
        snippetChecks.push({ snippet, checkbox });
        list.appendChild(
          el("label", { class: "yaia-snippet-item" }, [
            checkbox,
            el("span", { text: snippet.label }),
          ])
        );
      }
      body.appendChild(list);
    } else {
      body.appendChild(
        el("div", {
          class: "yaia-notice",
          text: "定型文（LINE誘導・チャンネル説明など）は設定画面で登録できます。",
        })
      );
    }

    // 生成ボタン・ステータス
    const status = el("div", { class: "yaia-status" });
    const generateBtn = el("button", {
      class: "yaia-btn yaia-primary",
      text: "AIで生成する",
    });
    body.appendChild(generateBtn);
    body.appendChild(status);

    // 結果表示
    const resultArea = el("div", { class: "yaia-result yaia-hidden" });
    resultArea.appendChild(el("label", { class: "yaia-label", text: "タイトル（編集可）" }));
    const titleOut = el("textarea", { class: "yaia-textarea", rows: "2" });
    const titleCount = el("div", { class: "yaia-count" });
    titleOut.addEventListener("input", () => {
      titleCount.textContent = `${titleOut.value.length} / 100文字`;
      titleCount.classList.toggle("yaia-over", titleOut.value.length > 100);
    });
    resultArea.appendChild(titleOut);
    resultArea.appendChild(titleCount);
    resultArea.appendChild(el("label", { class: "yaia-label", text: "概要欄（編集可・定型文追加済み）" }));
    const descOut = el("textarea", { class: "yaia-textarea", rows: "10" });
    resultArea.appendChild(descOut);
    const applyBtn = el("button", {
      class: "yaia-btn yaia-primary",
      text: "アップロード画面に反映",
    });
    resultArea.appendChild(applyBtn);
    body.appendChild(resultArea);

    generateBtn.addEventListener("click", async () => {
      const videoInfo = videoInfoInput.value.trim();
      if (!videoInfo) {
        status.textContent = "動画の内容・キーワードを入力してください";
        status.className = "yaia-status yaia-error";
        return;
      }
      const selected = snippetChecks.filter((s) => s.checkbox.checked);
      generateBtn.disabled = true;
      status.textContent = "生成中…（数秒かかります）";
      status.className = "yaia-status";

      const genRes = await sendMessage("generate", {
        channelId,
        videoInfo,
        snippetLabels: selected.map((s) => s.snippet.label),
      });
      generateBtn.disabled = false;

      if (!genRes.ok) {
        status.textContent = `エラー: ${genRes.error}`;
        status.className = "yaia-status yaia-error";
        return;
      }

      titleOut.value = genRes.title;
      titleOut.dispatchEvent(new Event("input"));
      const parts = [genRes.description, ...selected.map((s) => s.snippet.text)];
      descOut.value = parts.join("\n\n");
      resultArea.classList.remove("yaia-hidden");
      status.textContent = "生成しました。内容を確認して「反映」を押してください";
      status.className = "yaia-status yaia-success";

      // 次回のために、このチャンネルの定型文の選択状態を記憶する
      if (channelId && channel) {
        sendMessage("saveDefaultSnippets", {
          channelId,
          snippetIds: selected.map((s) => s.snippet.id),
        });
      }
    });

    applyBtn.addEventListener("click", () => {
      const titleBox = findTitleBox();
      const descBox = findDescriptionBox();
      if (!titleBox || !descBox) {
        status.textContent =
          "入力欄が見つかりません。アップロード画面（詳細タブ）が開いているか確認してください";
        status.className = "yaia-status yaia-error";
        return;
      }
      setEditorText(titleBox, titleOut.value.slice(0, 100));
      setEditorText(descBox, descOut.value.slice(0, 5000));
      status.textContent = "反映しました。画面上で最終確認してください";
      status.className = "yaia-status yaia-success";
    });

    document.body.appendChild(panel);
  }

  // ---- 起動ボタンの表示制御 -------------------------------------------------

  function ensureButton() {
    let button = document.getElementById(BUTTON_ID);
    if (!button) {
      button = el("button", {
        id: BUTTON_ID,
        text: "🤖 AI入力",
        title: "タイトル・概要欄をAIで生成",
        onclick: openPanel,
      });
      document.body.appendChild(button);
    }
    const visible = editorIsOpen();
    button.style.display = visible ? "block" : "none";
    if (!visible) closePanel();
  }

  // アップロードダイアログはSPA的に出入りするため、定期チェックで表示を追従する
  setInterval(ensureButton, 1000);
  ensureButton();
})();
