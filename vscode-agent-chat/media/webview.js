// @ts-check
(function () {
  "use strict";

  const vscode = acquireVsCodeApi();

  // ── DOM Elements ──
  const configView = document.getElementById("config-view");
  const chatView = document.getElementById("chat-view");
  const cfgProvider = /** @type {HTMLSelectElement} */ (document.getElementById("cfg-provider"));
  const cfgModel = /** @type {HTMLInputElement} */ (document.getElementById("cfg-model"));
  const cfgApikey = /** @type {HTMLInputElement} */ (document.getElementById("cfg-apikey"));
  const cfgEndpoint = /** @type {HTMLInputElement} */ (document.getElementById("cfg-endpoint"));
  const btnSave = /** @type {HTMLButtonElement} */ (document.getElementById("btn-save-config"));
  const configError = document.getElementById("config-error");
  const configSaving = document.getElementById("config-saving");
  const cfgIndicator = document.getElementById("cfg-indicator");
  const cfgIndicatorText = document.getElementById("cfg-indicator-text");
  const debugStatus = document.getElementById("debug-status");
  const messages = document.getElementById("messages");
  const input = /** @type {HTMLTextAreaElement} */ (document.getElementById("input"));
  const sendBtn = document.getElementById("send-btn");
  const resetBtn = document.getElementById("reset-btn");
  const settingsBtn = document.getElementById("settings-btn");
  const sessionSelect = /** @type {HTMLSelectElement} */ (document.getElementById("session-select"));
  const newChatBtn = /** @type {HTMLButtonElement} */ (document.getElementById("new-chat-btn"));
  const renameChatBtn = /** @type {HTMLButtonElement} */ (document.getElementById("rename-chat-btn"));
  const deleteChatBtn = /** @type {HTMLButtonElement} */ (document.getElementById("delete-chat-btn"));
  const loading = document.getElementById("loading");
  const btnBackToChat = document.getElementById("btn-back-to-chat");

  // ── State ──
  let providersMap = {};
  let hasChatSession = false;
  let pendingApprovalId = "";
  let currentSessionId = "";
  let isUpdatingSessionSelect = false;

  // ── Helpers ──
  function setDebugStatus(text, tone) {
    if (!debugStatus) { return; }
    debugStatus.textContent = text;
    debugStatus.className = "debug-status" + (tone ? " " + tone : "");
  }

  function providerRequiresApiKey(providerName) {
    return !["ollama", "vmlx"].includes(String(providerName || "").trim().toLowerCase());
  }

  function hasSavedConfig(entry) {
    if (!entry) { return false; }
    const provider = String(entry.provider || "").trim().toLowerCase();
    const model = String(entry.model || "").trim();
    const apiKey = String(entry.api_key || "").trim();
    if (!provider || !model) { return false; }
    return !providerRequiresApiKey(provider) || Boolean(apiKey);
  }

  function updateIndicator(providerName) {
    const entry = providersMap[providerName];
    if (!cfgIndicator || !cfgIndicatorText) { return; }
    if (hasSavedConfig(entry)) {
      cfgIndicator.querySelector(".dot").className = "dot saved";
      cfgIndicatorText.textContent = "已配置 (" + (entry.model || "未设置模型") + ")";
    } else {
      cfgIndicator.querySelector(".dot").className = "dot unsaved";
      cfgIndicatorText.textContent = "未配置";
    }
  }

  function showConfigError(text) {
    setDebugStatus(text, "error");
    configError.textContent = text;
    configError.classList.add("visible");
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderMarkdown(text) {
    let html = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    html = html.replace(/```(\w*)\n([\s\S]*?)\n```/g, function(_, l, c) { return '<pre><code>' + c.trim() + '</code></pre>'; });
    html = html.replace(/`([^`\n]+?)`/g, '<code>$1</code>');
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, '<em>$1</em>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    return '<p>' + html + '</p>';
  }

  function appendMessage(text, role, isError, thought, action) {
    const c = document.createElement("div");
    c.className = "msg " + role + (isError ? " error" : "");
    const l = document.createElement("div");
    l.className = "msg-label " + role + "-label";
    l.textContent = { user: "You", agent: "Agent", error: "Error" }[role] || role;
    c.appendChild(l);
    const b = document.createElement("div");
    b.className = "bubble";
    if (isError || role === "user") {
      b.textContent = text;
    } else {
      b.innerHTML = renderMarkdown(text);
    }
    c.appendChild(b);
    if (thought && role === "agent") {
      const tid = "t" + Date.now();
      const tog = document.createElement("div");
      tog.className = "thought-toggle";
      tog.innerHTML = '<span class="arrow">\u25B6</span> \u601D\u8003\u8FC7\u7A0B';
      tog.addEventListener("click", function () {
        const body = document.getElementById(tid);
        const arr = this.querySelector(".arrow");
        const isOpen = body.classList.toggle("open");
        arr.classList.toggle("open", isOpen);
      });
      c.appendChild(tog);
      const body = document.createElement("div");
      body.className = "thought-body";
      body.id = tid;
      body.textContent = thought;
      c.appendChild(body);
    }
    if (action && action.name && role === "agent") {
      const tc = document.createElement("div");
      tc.className = "tool-call";
      tc.innerHTML = ' <span class="tool-name">' + escapeHtml(action.name) + '</span>' +
        (action.parameters ? ' ' + escapeHtml(JSON.stringify(action.parameters)) : '');
      c.appendChild(tc);
    }
    messages.appendChild(c);
    messages.scrollTop = messages.scrollHeight;
  }

  function appendApprovalCard(approvalId, toolName, parameters, promptText) {
    const c = document.createElement("div");
    c.className = "msg agent";

    const l = document.createElement("div");
    l.className = "msg-label agent-label";
    l.textContent = "Approval";
    c.appendChild(l);

    const b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = "<strong>文件操作需要确认</strong><br>" +
      "工具: <code>" + escapeHtml(toolName || "unknown") + "</code><br>" +
      "参数: <code>" + escapeHtml(JSON.stringify(parameters || {})) + "</code><br>" +
      (promptText ? "<span class=\"approval-note\">" + escapeHtml(promptText) + "</span>" : "");
    c.appendChild(b);

    const actions = document.createElement("div");
    actions.className = "approval-actions";

    const allowBtn = document.createElement("button");
    allowBtn.className = "approval-btn allow";
    allowBtn.textContent = "允许";

    const skipBtn = document.createElement("button");
    skipBtn.className = "approval-btn skip";
    skipBtn.textContent = "skip";

    const lockButtons = function () {
      allowBtn.disabled = true;
      skipBtn.disabled = true;
    };

    allowBtn.addEventListener("click", function () {
      lockButtons();
      pendingApprovalId = approvalId;
      vscode.postMessage({ type: "tool_approval", approvalId: approvalId, decision: "allow" });
    });

    skipBtn.addEventListener("click", function () {
      lockButtons();
      pendingApprovalId = approvalId;
      vscode.postMessage({ type: "tool_approval", approvalId: approvalId, decision: "skip" });
    });

    actions.appendChild(allowBtn);
    actions.appendChild(skipBtn);
    c.appendChild(actions);

    messages.appendChild(c);
    messages.scrollTop = messages.scrollHeight;
  }

  function renderSessionOptions(list, selectedId) {
    if (!sessionSelect) { return; }
    isUpdatingSessionSelect = true;
    sessionSelect.innerHTML = "";
    for (const item of (list || [])) {
      const option = document.createElement("option");
      option.value = item.id;
      const turns = Number(item.user_turns || 0);
      option.textContent = (item.title || "未命名会话") + " (" + turns + ")";
      sessionSelect.appendChild(option);
    }
    if (selectedId) {
      sessionSelect.value = selectedId;
      currentSessionId = selectedId;
    }
    isUpdatingSessionSelect = false;
  }

  function restoreMessagesFromSession(items) {
    messages.innerHTML = "";
    const list = Array.isArray(items) ? items : [];
    for (const item of list) {
      const role = item.role === "user" ? "user" : "agent";
      appendMessage(String(item.text || ""), role, Boolean(item.is_error));
    }
    if (!list.length) {
      appendMessage("新会话已创建，可以开始提问。", "agent", false);
    }
  }

  function sendAsk() {
    if (pendingApprovalId) {
      appendMessage("请先处理当前文件操作审批（允许 或 skip）。", "agent", true);
      return;
    }
    const text = input.value.trim();
    if (!text) { return; }
    if (text.startsWith("/")) {
      const parts = text.split(" ");
      const cmd = parts[0].substring(1).toLowerCase();
      const args = parts.slice(1).join(" ");
      vscode.postMessage({ type: "command", command: cmd, args: args });
      input.value = "";
      return;
    }
    appendMessage(text, "user", false);
    input.value = "";
    vscode.postMessage({ type: "ask", text: text });
  }
  // ── Event Listeners ──
  cfgProvider.addEventListener("change", function () {
    const providerName = cfgProvider.value;
    const entry = providersMap[providerName];
    if (entry) {
      cfgModel.value = entry.model || "";
      cfgApikey.value = entry.api_key || "";
      cfgEndpoint.value = entry.endpoint || "";
    } else {
      cfgModel.value = "";
      cfgApikey.value = "";
      cfgEndpoint.value = "";
    }
    updateIndicator(providerName);
  });

  btnSave.addEventListener("click", function () {
    const provider = cfgProvider.value;
    const model = cfgModel.value.trim();
    const api_key = cfgApikey.value.trim();
    const endpoint = cfgEndpoint.value.trim();
    if (!model) {
      showConfigError("\u8BF7\u8F93\u5165\u6A21\u578B\u540D\u79F0");
      return;
    }
    setDebugStatus("\u6B63\u5728\u4FDD\u5B58\u914D\u7F6E...", "success");
    configError.classList.remove("visible");
    configSaving.classList.add("visible");
    btnSave.disabled = true;
    vscode.postMessage({ type: "save_config", provider: provider, model: model, api_key: api_key, endpoint: endpoint });
  });

  sendBtn.addEventListener("click", sendAsk);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAsk();
    }
  });
  resetBtn.addEventListener("click", function () {
    messages.innerHTML = "";
    appendMessage("\u4F1A\u8BDD\u5DF2\u91CD\u7F6E\u3002", "agent", false);
    vscode.postMessage({ type: "reset" });
  });
  settingsBtn.addEventListener("click", function () {
    vscode.postMessage({ type: "back_to_config" });
  });

  sessionSelect.addEventListener("change", function () {
    if (isUpdatingSessionSelect) { return; }
    const targetId = sessionSelect.value;
    if (!targetId || targetId === currentSessionId) { return; }
    vscode.postMessage({ type: "switch_session", sessionId: targetId });
  });

  newChatBtn.addEventListener("click", function () {
    pendingApprovalId = "";
    vscode.postMessage({ type: "new_session" });
  });

  renameChatBtn.addEventListener("click", function () {
    if (!currentSessionId) { return; }
    const currentName = sessionSelect.options[sessionSelect.selectedIndex]
      ? sessionSelect.options[sessionSelect.selectedIndex].textContent.replace(/\s\(\d+\)$/, "")
      : "";
    const nextName = window.prompt("请输入新的会话名称", currentName || "");
    if (!nextName) { return; }
    const title = nextName.trim();
    if (!title) { return; }
    vscode.postMessage({ type: "rename_session", sessionId: currentSessionId, title: title });
  });

  deleteChatBtn.addEventListener("click", function () {
    if (!currentSessionId) { return; }
    const ok = window.confirm("确认删除当前会话？删除后不可恢复。");
    if (!ok) { return; }
    pendingApprovalId = "";
    vscode.postMessage({ type: "delete_session", sessionId: currentSessionId });
  });

  btnBackToChat.addEventListener("click", function () {
    configView.classList.add("hidden");
    chatView.classList.add("visible");
  });

  // ── Message Handler ──
  window.addEventListener("message", function (event) {
    const msg = event.data;
    switch (msg.type) {
      case "show_config": {
        setDebugStatus("\u5DF2\u6536\u5230\u6269\u5C55\u914D\u7F6E\u6570\u636E", "success");
        const pmap = {};
        if (msg.providers) {
          for (const entry of msg.providers) {
            pmap[entry.provider] = entry;
          }
        }
        providersMap = pmap;
        if (msg.active) {
          cfgProvider.value = msg.active.provider || "deepseek";
          cfgModel.value = msg.active.model || "";
          cfgApikey.value = msg.active.api_key || "";
          cfgEndpoint.value = msg.active.endpoint || "";
        }
        updateIndicator(cfgProvider.value);
        configView.classList.remove("hidden");
        chatView.classList.remove("visible");
        break;
      }
      case "switch_to_chat":
        setDebugStatus("\u914D\u7F6E\u5DF2\u4FDD\u5B58\uFF0C\u5DF2\u5207\u6362\u5230\u804A\u5929\u9875", "success");
        hasChatSession = true;
        btnBackToChat.classList.add("visible");
        configView.classList.add("hidden");
        chatView.classList.add("visible");
        vscode.postMessage({ type: "sessions_refresh" });
        break;
      case "sessions_list":
        renderSessionOptions(msg.sessions || [], msg.currentSessionId || "");
        break;
      case "session_switched":
        currentSessionId = msg.sessionId || "";
        restoreMessagesFromSession(msg.messages || []);
        break;
      case "config_saving":
        setDebugStatus(msg.saving ? "\u6269\u5C55\u5DF2\u6536\u5230\u4FDD\u5B58\u8BF7\u6C42..." : "\u6269\u5C55\u5904\u7406\u5B8C\u6210", msg.saving ? "success" : "");
        configSaving.classList.toggle("visible", msg.saving);
        btnSave.disabled = msg.saving;
        break;
      case "config_error":
        showConfigError(msg.text);
        configSaving.classList.remove("visible");
        btnSave.disabled = false;
        break;
      case "bridge_error":
        showConfigError(msg.text);
        break;
      case "answer":
        pendingApprovalId = "";
        appendMessage(msg.text || "", "agent", !msg.ok, msg.thought || "", msg.action || null);
        break;
      case "approval_required":
        pendingApprovalId = msg.approvalId || "";
        appendApprovalCard(
          pendingApprovalId,
          msg.toolName || "",
          msg.parameters || {},
          msg.prompt || "",
        );
        break;
      case "loading":
        loading.classList.toggle("visible", msg.loading);
        break;
      case "status":
        document.getElementById("st-provider").textContent = msg.provider || "?";
        document.getElementById("st-model").textContent = msg.model || "?";
        document.getElementById("st-user").textContent = msg.user || "?";
        break;
    }
  });

  // ── Init ──
  setDebugStatus("\u524D\u7AEF\u811A\u672C\u5DF2\u521D\u59CB\u5316\uFF0C\u7B49\u5F85\u6269\u5C55\u6D88\u606F...", "success");
  appendMessage("\u4F60\u597D\uFF01\u6211\u662F VVSAgant\u3002\u6709\u4EC0\u4E48\u53EF\u4EE5\u5E2E\u4F60\u7684\uFF1F", "agent", false);
  vscode.postMessage({ type: "sessions_refresh" });
}());
