"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
const child_process_1 = require("child_process");
const path = require("path");
// ── Bridge Process ──────────────────────────────────────────────────────────
class AgentBridgeProcess {
    constructor() {
        this.proc = null;
        this.pending = [];
        this.stderrBuffer = "";
        this.stdoutBuffer = "";
        this.lastError = "";
    }
    start(workspaceRoot, pythonPath, bridgeScript) {
        if (this.proc)
            return;
        this.lastError = "";
        this.stderrBuffer = "";
        this.stdoutBuffer = "";
        const scriptPath = path.isAbsolute(bridgeScript)
            ? bridgeScript
            : path.join(workspaceRoot, bridgeScript);
        this.proc = (0, child_process_1.spawn)(pythonPath, [scriptPath], {
            cwd: workspaceRoot,
            stdio: ["pipe", "pipe", "pipe"],
        });
        this.proc.stdout.on("data", (chunk) => {
            this.stdoutBuffer += chunk.toString();
            this.flushStdoutBuffer();
        });
        this.proc.stderr.on("data", (chunk) => {
            this.stderrBuffer += chunk.toString();
        });
        this.proc.on("error", (err) => {
            this.lastError = err.message;
            this.proc = null;
            for (const p of this.pending) {
                clearTimeout(p.timer);
                p.resolve({ type: "error", ok: false, error: `Bridge process failed to start: ${err.message}` });
            }
            this.pending = [];
        });
        this.proc.on("exit", () => {
            this.proc = null;
            for (const p of this.pending) {
                clearTimeout(p.timer);
                const stderr = this.stderrBuffer.trim();
                const errorText = this.lastError || (stderr ? `Bridge process exited: ${stderr}` : "Bridge process exited");
                p.resolve({ type: "error", ok: false, error: errorText });
            }
            this.pending = [];
        });
    }
    flushStdoutBuffer() {
        const lines = this.stdoutBuffer.split("\n");
        this.stdoutBuffer = lines.pop() ?? "";
        for (const line of lines) {
            const text = line.trim();
            if (!text)
                continue;
            if (!text.startsWith("{"))
                continue;
            let payload = null;
            try {
                payload = JSON.parse(text);
            }
            catch {
                continue;
            }
            const entry = this.pending.shift();
            if (entry) {
                clearTimeout(entry.timer);
                entry.resolve(payload);
            }
        }
    }
    async request(payload, timeoutMs = 60000) {
        if (this.lastError) {
            throw new Error(this.lastError);
        }
        if (!this.proc || !this.proc.stdin.writable) {
            const stderr = this.stderrBuffer.trim();
            const detail = stderr || this.lastError;
            throw new Error(detail ? `桥接进程未运行: ${detail}` : "桥接进程未运行");
        }
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                const idx = this.pending.findIndex(e => e.resolve === resolve);
                if (idx >= 0)
                    this.pending.splice(idx, 1);
                reject(new Error(`请求超时 (${timeoutMs}ms)`));
            }, timeoutMs);
            this.pending.push({ resolve, timer });
            const line = `${JSON.stringify(payload)}\n`;
            this.proc?.stdin.write(line, (err) => {
                if (err) {
                    clearTimeout(timer);
                    const idx = this.pending.findIndex(e => e.resolve === resolve);
                    if (idx >= 0)
                        this.pending.splice(idx, 1);
                    reject(err);
                }
            });
        });
    }
    stop() {
        if (this.proc) {
            this.proc.kill();
            this.proc = null;
        }
        for (const p of this.pending)
            clearTimeout(p.timer);
        this.pending = [];
    }
    get isRunning() {
        return this.proc !== null;
    }
    getStderr() {
        return this.stderrBuffer.trim();
    }
}
// ── Webview View Provider ───────────────────────────────────────────────────
class VVSAgantViewProvider {
    constructor(extensionUri) {
        this.extensionUri = extensionUri;
        this.bridge = new AgentBridgeProcess();
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
        };
        webviewView.webview.html = this.getHtml(webviewView.webview);
        webviewView.webview.onDidReceiveMessage(async (msg) => {
            const type = String(msg?.type ?? "");
            if (type === "save_config") {
                await this.handleSaveConfig(msg);
                return;
            }
            if (type === "back_to_config") {
                const resp = await this.bridge.request({ type: "get_config" }).catch(() => null);
                if (resp?.ok) {
                    this.sendConfigToView(resp);
                }
                return;
            }
            if (type === "ask") {
                const text = String(msg?.text ?? "").trim();
                if (!text)
                    return;
                webviewView.webview.postMessage({ type: "loading", loading: true });
                try {
                    if (!this.bridge.isRunning)
                        throw new Error("桥接未连接");
                    const response = await this.bridge.request({ type: "ask", text });
                    webviewView.webview.postMessage({ type: "loading", loading: false });
                    if (response?.type === "approval_required") {
                        webviewView.webview.postMessage({
                            type: "approval_required",
                            approvalId: String(response.approval_id ?? ""),
                            toolName: String(response.tool_name ?? ""),
                            parameters: response.parameters ?? {},
                            prompt: String(response.prompt ?? ""),
                        });
                        return;
                    }
                    if (response?.ok) {
                        webviewView.webview.postMessage({
                            type: "answer", ok: true,
                            text: String(response.text ?? ""),
                            thought: String(response.thought ?? ""),
                            action: response.action ?? null,
                        });
                        await this.syncSessions();
                    }
                    else {
                        webviewView.webview.postMessage({
                            type: "answer", ok: false,
                            text: String(response?.error ?? "桥接返回未知错误"),
                        });
                    }
                }
                catch (err) {
                    webviewView.webview.postMessage({ type: "loading", loading: false });
                    webviewView.webview.postMessage({ type: "answer", ok: false, text: err instanceof Error ? err.message : String(err) });
                }
                return;
            }
            if (type === "sessions_refresh") {
                await this.syncSessions();
                return;
            }
            if (type === "new_session") {
                try {
                    const resp = await this.bridge.request({ type: "create_session" });
                    if (resp?.ok) {
                        webviewView.webview.postMessage({
                            type: "session_switched",
                            sessionId: String(resp.current_session_id ?? ""),
                            messages: Array.isArray(resp.messages) ? resp.messages : [],
                        });
                        await this.syncSessions();
                        await this.updateStatus();
                    }
                }
                catch {
                    // ignore
                }
                return;
            }
            if (type === "switch_session") {
                const sessionId = String(msg?.sessionId ?? "").trim();
                if (!sessionId)
                    return;
                try {
                    const resp = await this.bridge.request({ type: "switch_session", session_id: sessionId });
                    if (resp?.ok) {
                        webviewView.webview.postMessage({
                            type: "session_switched",
                            sessionId: String(resp.current_session_id ?? ""),
                            messages: Array.isArray(resp.messages) ? resp.messages : [],
                        });
                        await this.syncSessions();
                        await this.updateStatus();
                    }
                    else {
                        webviewView.webview.postMessage({
                            type: "answer", ok: false,
                            text: String(resp?.error ?? "切换会话失败"),
                        });
                    }
                }
                catch (err) {
                    webviewView.webview.postMessage({
                        type: "answer", ok: false,
                        text: err instanceof Error ? err.message : String(err),
                    });
                }
                return;
            }
            if (type === "rename_session") {
                const sessionId = String(msg?.sessionId ?? "").trim();
                const title = String(msg?.title ?? "").trim();
                if (!sessionId || !title)
                    return;
                try {
                    const resp = await this.bridge.request({ type: "rename_session", session_id: sessionId, title });
                    if (!resp?.ok) {
                        webviewView.webview.postMessage({
                            type: "answer", ok: false,
                            text: String(resp?.error ?? "会话重命名失败"),
                        });
                    }
                    await this.syncSessions();
                }
                catch (err) {
                    webviewView.webview.postMessage({
                        type: "answer", ok: false,
                        text: err instanceof Error ? err.message : String(err),
                    });
                }
                return;
            }
            if (type === "delete_session") {
                const sessionId = String(msg?.sessionId ?? "").trim();
                if (!sessionId)
                    return;
                try {
                    const resp = await this.bridge.request({ type: "delete_session", session_id: sessionId });
                    if (resp?.ok) {
                        webviewView.webview.postMessage({
                            type: "session_switched",
                            sessionId: String(resp.current_session_id ?? ""),
                            messages: Array.isArray(resp.messages) ? resp.messages : [],
                        });
                        await this.syncSessions();
                    }
                    else {
                        webviewView.webview.postMessage({
                            type: "answer", ok: false,
                            text: String(resp?.error ?? "删除会话失败"),
                        });
                    }
                }
                catch (err) {
                    webviewView.webview.postMessage({
                        type: "answer", ok: false,
                        text: err instanceof Error ? err.message : String(err),
                    });
                }
                return;
            }
            if (type === "tool_approval") {
                const approvalId = String(msg?.approvalId ?? "").trim();
                const decision = String(msg?.decision ?? "").trim().toLowerCase();
                if (!approvalId)
                    return;
                if (!["allow", "skip"].includes(decision))
                    return;
                webviewView.webview.postMessage({ type: "loading", loading: true });
                try {
                    if (!this.bridge.isRunning)
                        throw new Error("桥接未连接");
                    const response = await this.bridge.request({ type: "tool_approval", approval_id: approvalId, decision });
                    webviewView.webview.postMessage({ type: "loading", loading: false });
                    if (response?.type === "approval_required") {
                        webviewView.webview.postMessage({
                            type: "approval_required",
                            approvalId: String(response.approval_id ?? ""),
                            toolName: String(response.tool_name ?? ""),
                            parameters: response.parameters ?? {},
                            prompt: String(response.prompt ?? ""),
                        });
                        return;
                    }
                    if (response?.ok) {
                        webviewView.webview.postMessage({
                            type: "answer", ok: true,
                            text: String(response.text ?? ""),
                            thought: String(response.thought ?? ""),
                            action: response.action ?? null,
                        });
                        await this.syncSessions();
                    }
                    else {
                        webviewView.webview.postMessage({
                            type: "answer", ok: false,
                            text: String(response?.error ?? "审批处理失败"),
                        });
                    }
                }
                catch (err) {
                    webviewView.webview.postMessage({ type: "loading", loading: false });
                    webviewView.webview.postMessage({ type: "answer", ok: false, text: err instanceof Error ? err.message : String(err) });
                }
                return;
            }
            if (type === "reset") {
                try {
                    await this.bridge.request({ type: "reset" });
                    webviewView.webview.postMessage({ type: "reset_done" });
                    await this.syncSessions();
                }
                catch { /* ignore */ }
            }
        });
        this.initBridge();
    }
    sendConfigToView(data) {
        this._view?.webview.postMessage({
            type: "show_config",
            currentProvider: data.current_provider || "deepseek",
            providers: data.providers || [],
            active: data.active || {},
        });
    }
    providerRequiresApiKey(providerName) {
        return !["ollama", "vmlx"].includes((providerName || "").trim().toLowerCase());
    }
    isConfigReady(entry) {
        const provider = String(entry?.provider || "").trim().toLowerCase();
        const model = String(entry?.model || "").trim();
        const apiKey = String(entry?.api_key || "").trim();
        if (!provider || !model) {
            return false;
        }
        return !this.providerRequiresApiKey(provider) || Boolean(apiKey);
    }
    async initBridge() {
        const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
        if (!workspaceFolder) {
            vscode.window.showErrorMessage("VVSAgant: 请先打开项目工作区（agent 项目文件夹）");
            this._view?.webview.postMessage({ type: "bridge_error", text: "请先打开项目工作区" });
            return;
        }
        const cfg = vscode.workspace.getConfiguration("vvsagant");
        const pythonPath = cfg.get("pythonPath", "python");
        const bridgeScript = cfg.get("bridgeScript", "agent_server_bridge.py");
        try {
            this.bridge.start(workspaceFolder.uri.fsPath, pythonPath, bridgeScript);
            const pong = await this.bridge.request({ type: "ping" });
            if (!pong?.ok)
                throw new Error("桥接 ping 失败");
            const cfgResp = await this.bridge.request({ type: "get_config" });
            if (cfgResp?.ok) {
                if (this.isConfigReady(cfgResp.active)) {
                    this.switchToChat();
                    await this.updateStatus();
                    await this.syncSessions();
                }
                else {
                    this.sendConfigToView(cfgResp);
                }
            }
            else {
                this.sendConfigToView({ current_provider: "deepseek", providers: [], active: {} });
            }
        }
        catch (err) {
            const detail = err instanceof Error ? err.message : String(err);
            const fullMsg = `VVSAgant: 无法连接 Agent — ${detail}`;
            vscode.window.showErrorMessage(fullMsg);
            this._view?.webview.postMessage({ type: "bridge_error", text: `无法连接 Agent:\n${detail}` });
        }
    }
    async handleSaveConfig(msg) {
        try {
            if (!this.bridge.isRunning) {
                vscode.window.showErrorMessage("VVSAgant: 桥接进程未运行，请检查 vvsagant.pythonPath 设置");
                this._view?.webview.postMessage({ type: "config_saving", saving: false });
                this._view?.webview.postMessage({ type: "config_error", text: "桥接未连接，请检查设置" });
                return;
            }
            const provider = String(msg.provider || "");
            const model = String(msg.model || "");
            const api_key = String(msg.api_key || "");
            const endpoint = String(msg.endpoint || "");
            if (!provider) {
                this._view?.webview.postMessage({ type: "config_saving", saving: false });
                this._view?.webview.postMessage({ type: "config_error", text: "请选择 Provider" });
                return;
            }
            if (!model) {
                this._view?.webview.postMessage({ type: "config_saving", saving: false });
                this._view?.webview.postMessage({ type: "config_error", text: "请输入模型名称" });
                return;
            }
            this._view?.webview.postMessage({ type: "config_saving", saving: true });
            const resp = await this.bridge.request({ type: "set_config", provider, model, api_key, endpoint });
            if (resp?.ok) {
                this._view?.webview.postMessage({ type: "config_saving", saving: false });
                vscode.window.showInformationMessage("VVSAgant: 配置已保存");
                this.switchToChat();
                await this.updateStatus();
                await this.syncSessions();
            }
            else {
                this._view?.webview.postMessage({ type: "config_saving", saving: false });
                const errMsg = resp?.error || "保存配置失败";
                vscode.window.showErrorMessage(`VVSAgant: ${errMsg}`);
                this._view?.webview.postMessage({ type: "config_error", text: errMsg });
            }
        }
        catch (err) {
            this._view?.webview.postMessage({ type: "config_saving", saving: false });
            const detail = err instanceof Error ? err.message : String(err);
            vscode.window.showErrorMessage(`VVSAgant: 配置失败 — ${detail}`);
            this._view?.webview.postMessage({ type: "config_error", text: detail });
        }
    }
    switchToChat() {
        this._view?.webview.postMessage({ type: "switch_to_chat" });
    }
    async updateStatus() {
        if (!this.bridge.isRunning)
            return;
        try {
            const status = await this.bridge.request({ type: "status" });
            if (status?.ok) {
                this._view?.webview.postMessage({
                    type: "status", provider: status.provider, model: status.model, user: status.user,
                });
            }
        }
        catch { /* ignore */ }
    }
    async syncSessions() {
        if (!this.bridge.isRunning)
            return;
        try {
            const sessions = await this.bridge.request({ type: "list_sessions" });
            if (sessions?.ok) {
                this._view?.webview.postMessage({
                    type: "sessions_list",
                    currentSessionId: String(sessions.current_session_id ?? ""),
                    sessions: Array.isArray(sessions.sessions) ? sessions.sessions : [],
                });
            }
        }
        catch {
            // ignore session sync failures
        }
    }
    getHtml(webview) {
        const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "webview.js"));
        return /* html */ `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src ${webview.cspSource}; img-src ${webview.cspSource} data:;">
<title>VVSAgant</title>
<style>
  :root { --bg: #1a1b1e; --surface: #25262b; --border: #373a40; --text: #c1c2c5; --text-muted: #909296; --text-dim: #5c5f66; --accent: #4c9aff; --error: #fa5252; --success: #51cf66; --radius: 10px; --font: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Noto Sans SC", sans-serif; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: var(--font); background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; font-size: 13px; }
  #config-view { display: flex; flex-direction: column; padding: 20px 16px; overflow-y: auto; flex: 1; }
  #config-view.hidden { display: none; }
  #config-view-header { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  #config-view h2 { font-size: 18px; font-weight: 600; color: #e8e9eb; flex: 1; }
  #btn-back-to-chat { display: none; background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--text-dim); cursor: pointer; font-size: 13px; padding: 3px 9px; line-height: 1.5; }
  #btn-back-to-chat:hover { background: var(--surface); color: var(--text); }
  #btn-back-to-chat.visible { display: inline-flex; align-items: center; gap: 4px; }
  #config-view .subtitle { font-size: 12px; color: var(--text-dim); margin-bottom: 20px; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
  .form-group input, .form-group select { width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 9px 12px; font-size: 13px; color: var(--text); outline: none; font-family: var(--font); }
  .form-group input:focus, .form-group select:focus { border-color: var(--accent); }
  .form-group input::placeholder { color: var(--text-dim); }
  .form-group select option { background: var(--surface); color: var(--text); }
  .form-hint { font-size: 11px; color: var(--text-dim); margin-top: 3px; }
  .debug-status { margin-bottom: 14px; padding: 8px 10px; border-radius: 6px; background: rgba(76, 154, 255, 0.12); border: 1px solid rgba(76, 154, 255, 0.25); color: #dce9ff; font-size: 12px; line-height: 1.4; }
  .debug-status.error { background: rgba(250, 82, 82, 0.1); border-color: rgba(250, 82, 82, 0.3); color: #ffc9c9; }
  .debug-status.success { background: rgba(81, 207, 102, 0.1); border-color: rgba(81, 207, 102, 0.25); color: #d3f9d8; }
  .btn-primary { width: 100%; background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 10px; font-size: 14px; font-weight: 500; cursor: pointer; margin-top: 8px; }
  .btn-primary:hover { opacity: 0.9; }
  .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
  .config-error { color: var(--error); font-size: 12px; margin-top: 8px; padding: 8px; background: rgba(250, 82, 82, 0.1); border-radius: 6px; display: none; white-space: pre-wrap; }
  .config-error.visible { display: block; }
  .config-indicator { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--text-dim); margin-top: 2px; }
  .config-indicator .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
  .config-indicator .dot.saved { background: var(--success); }
  .config-indicator .dot.unsaved { background: var(--text-dim); }
  .config-saving { display: none; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); margin-top: 8px; justify-content: center; }
  .config-saving.visible { display: flex; }
  .config-saving .spinner { width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #chat-view { display: none; flex-direction: column; height: 100vh; }
  #chat-view.visible { display: flex; }
  #header { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; flex-wrap: wrap; gap: 4px; }
  #status-badges { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  #header-controls { display: flex; gap: 6px; align-items: center; }
  #session-select { max-width: 170px; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; color: var(--text); padding: 2px 6px; font-size: 11px; }
  #session-select:focus { outline: none; border-color: var(--accent); }
  .badge { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--bg); color: var(--text-muted); border: 1px solid var(--border); white-space: nowrap; }
  .badge .label { color: var(--text-dim); }
  .badge .value { color: var(--accent); font-weight: 500; }
  .badge-dot { width: 5px; height: 5px; border-radius: 50%; display: inline-block; }
  .badge-dot.online { background: var(--success); }
  .header-btn { background: none; border: 1px solid var(--border); border-radius: 4px; color: var(--text-muted); padding: 2px 8px; cursor: pointer; font-size: 11px; }
  .header-btn:hover { background: var(--surface-hover); color: var(--text); }
  #messages { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 10px; scroll-behavior: smooth; }
  #messages::-webkit-scrollbar { width: 4px; }
  #messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .msg { max-width: 100%; animation: msg-in 0.2s ease-out; }
  @keyframes msg-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .msg.user { align-self: flex-end; }
  .msg.user .bubble { background: rgba(76, 154, 255, 0.12); border: 1px solid rgba(76, 154, 255, 0.2); border-radius: var(--radius) var(--radius) 4px var(--radius); color: #dce9ff; }
  .msg.agent { align-self: flex-start; }
  .msg.agent .bubble { background: #25262b; border: 1px solid var(--border); border-radius: var(--radius) var(--radius) var(--radius) 4px; }
  .msg.error .bubble { border-color: rgba(250, 82, 82, 0.3); background: rgba(250, 82, 82, 0.1); color: var(--error); }
  .bubble { padding: 8px 12px; line-height: 1.5; word-wrap: break-word; font-size: 13px; }
  .msg-label { font-size: 10px; margin-bottom: 2px; padding: 0 4px; }
  .msg-label.user-label { color: var(--accent); text-align: right; }
  .msg-label.agent-label { color: var(--success); }
  .thought-toggle { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; color: var(--text-dim); cursor: pointer; margin: 2px 0; padding: 1px 4px; border-radius: 3px; user-select: none; }
  .thought-toggle:hover { background: var(--surface-hover); }
  .thought-toggle .arrow { display: inline-block; transition: transform 0.2s; font-size: 9px; }
  .thought-toggle .arrow.open { transform: rotate(90deg); }
  .thought-body { display: none; font-size: 11px; color: var(--text-dim); padding: 6px 8px; margin: 2px 0 4px; background: var(--bg); border-radius: 4px; border-left: 2px solid var(--accent); font-style: italic; }
  .thought-body.open { display: block; }
  .tool-call { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 2px 8px; margin: 2px 0; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); }
  .tool-call .tool-name { color: #da77f2; font-family: monospace; font-weight: 500; }
  .approval-actions { display: flex; gap: 8px; margin-top: 8px; }
  .approval-btn { border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; background: var(--surface); color: var(--text); cursor: pointer; font-size: 12px; }
  .approval-btn.allow { border-color: rgba(81, 207, 102, 0.45); color: #d3f9d8; background: rgba(81, 207, 102, 0.12); }
  .approval-btn.skip { border-color: rgba(250, 82, 82, 0.4); color: #ffc9c9; background: rgba(250, 82, 82, 0.12); }
  .approval-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .approval-note { display: inline-block; margin-top: 5px; color: var(--text-dim); font-size: 11px; }
  #loading { display: none; align-self: flex-start; padding: 8px 12px; gap: 6px; align-items: center; color: var(--text-muted); font-size: 12px; }
  #loading.visible { display: flex; }
  #loading .spinner { width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; }
  #input-area { display: flex; gap: 6px; padding: 8px 10px; background: var(--surface); border-top: 1px solid var(--border); flex-shrink: 0; align-items: flex-end; }
  #input { flex: 1; min-height: 36px; max-height: 140px; resize: vertical; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font: inherit; font-size: 12px; color: var(--text); outline: none; line-height: 1.4; }
  #input:focus { border-color: var(--accent); }
  #input::placeholder { color: var(--text-dim); }
  #send-btn { display: flex; align-items: center; justify-content: center; width: 36px; border: none; border-radius: 6px; background: var(--accent); color: #fff; cursor: pointer; }
  #send-btn:active { transform: scale(0.95); }
  #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .bubble pre { background: #1e1f23; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; margin: 6px 0; overflow-x: auto; font-family: monospace; font-size: 11px; }
  .bubble code { font-family: monospace; font-size: 11px; background: #1e1f23; padding: 1px 4px; border-radius: 3px; }
  .bubble pre code { background: none; padding: 0; }
  .bubble p { margin: 3px 0; }
  .bubble ul, .bubble ol { margin: 4px 0; padding-left: 16px; }
  .bubble a { color: var(--accent); text-decoration: none; }
  .bubble h1 { font-size: 14px; } .bubble h2 { font-size: 13px; } .bubble h3 { font-size: 12px; }
  .bubble hr { border: none; border-top: 1px solid var(--border); margin: 6px 0; }
  .bubble blockquote { border-left: 2px solid var(--accent); padding: 2px 8px; margin: 4px 0; color: var(--text-muted); background: var(--bg); border-radius: 0 3px 3px 0; }
</style>
</head>
<body>

<div id="config-view" class="hidden">
  <div id="config-view-header">
    <h2>VVSAgant</h2>
    <button id="btn-back-to-chat">&#8592; 返回对话</button>
  </div>
  <p class="subtitle">配置 AI 提供商 — 切换供应商自动加载设置</p>
  <div class="debug-status" id="debug-status">前端脚本尚未初始化</div>
  <div class="form-group">
    <label>Provider</label>
    <select id="cfg-provider">
      <option value="deepseek">DeepSeek</option>
      <option value="openai">OpenAI</option>
      <option value="ollama">Ollama (本地)</option>
      <option value="vmlx">MLX (本地)</option>
    </select>
    <div class="config-indicator" id="cfg-indicator"><span class="dot unsaved"></span> <span id="cfg-indicator-text">未配置</span></div>
  </div>
  <div class="form-group">
    <label>Model</label>
    <input id="cfg-model" type="text" placeholder="deepseek-v4-flash" />
  </div>
  <div class="form-group">
    <label>API Key</label>
    <input id="cfg-apikey" type="password" placeholder="sk-..." />
    <div class="form-hint">Ollama/MLX 可不填</div>
  </div>
  <div class="form-group">
    <label>Endpoint (可选)</label>
    <input id="cfg-endpoint" type="text" placeholder="留空使用默认" />
  </div>
  <div class="config-error" id="config-error"></div>
  <div class="config-saving" id="config-saving">
    <div class="spinner"></div>
    <span>保存并连接中...</span>
  </div>
  <button class="btn-primary" id="btn-save-config">保存 &amp; 连接</button>
  <details id="debug-panel" style="margin-top:16px;display:none">
    <summary style="cursor:pointer;font-size:11px;color:var(--text-dim)">🔍 调试信息</summary>
    <div id="debug-content" style="font-size:10px;color:var(--text-dim);font-family:monospace;margin-top:6px;padding:8px;background:var(--surface);border-radius:4px;white-space:pre-wrap;word-break:break-all;line-height:1.5"></div>
  </details>
</div>

<div id="chat-view">
  <div id="header">
    <div id="status-badges">
      <span class="badge"><span class="badge-dot online"></span> Connected</span>
      <span class="badge"><span class="label">P:</span> <span class="value" id="st-provider">?</span></span>
      <span class="badge"><span class="label">M:</span> <span class="value" id="st-model">?</span></span>
      <span class="badge"><span class="label">U:</span> <span class="value" id="st-user">?</span></span>
    </div>
    <div id="header-controls">
      <select id="session-select" title="选择历史会话"></select>
      <button class="header-btn" id="new-chat-btn">新会话</button>
      <button class="header-btn" id="rename-chat-btn">重命名</button>
      <button class="header-btn" id="delete-chat-btn">删除</button>
      <button class="header-btn" id="settings-btn">⚙</button>
      <button class="header-btn" id="reset-btn" title="清空当前会话">↺</button>
    </div>
  </div>
  <div id="messages"></div>
  <div id="loading"><div class="spinner"></div><span>思考中...</span></div>
  <div id="input-area">
    <textarea id="input" placeholder="输入消息...（Enter 发送，Shift+Enter 换行）" autofocus spellcheck="true"></textarea>
    <button id="send-btn"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg></button>
  </div>
</div>

<script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
VVSAgantViewProvider.viewType = "vvsagant.chatView";
function activate(context) {
    const provider = new VVSAgantViewProvider(context.extensionUri);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(VVSAgantViewProvider.viewType, provider, {
        webviewOptions: { retainContextWhenHidden: true },
    }));
    context.subscriptions.push(vscode.commands.registerCommand("vvsagant.open", () => {
        vscode.commands.executeCommand("workbench.view.extension.vvsagant-sidebar");
    }));
    context.subscriptions.push(vscode.commands.registerCommand("vvsagant.reset", () => {
        vscode.commands.executeCommand("workbench.view.extension.vvsagant-sidebar");
    }));
}
function deactivate() { }
function getNonce() {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let value = "";
    for (let index = 0; index < 32; index += 1) {
        value += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return value;
}
//# sourceMappingURL=extension.js.map