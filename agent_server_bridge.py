import json
import sys
import os
import io
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiagent import ModularAgent, ToolApprovalPending  # noqa: E402

CONFIG_FILE = ROOT / "agent_config.json"
SESSIONS_FILE = ROOT / "agent_chat_sessions.json"

PROVIDER_NAMES = ["deepseek", "openai", "ollama", "vmlx"]

DEFAULT_PROVIDER_ENTRIES = [
    {"provider": "deepseek", "model": "deepseek-v4-flash", "api_key": "", "endpoint": ""},
    {"provider": "openai",   "model": "",                  "api_key": "", "endpoint": ""},
    {"provider": "ollama",   "model": "",                  "api_key": "", "endpoint": ""},
    {"provider": "vmlx",     "model": "",                  "api_key": "", "endpoint": ""},
]


def _migrate_old_config(old: dict) -> dict:
    """Convert old flat config to new list format."""
    old_provider = old.get("provider", "deepseek")
    old_model = old.get("model", "")
    old_key = old.get("api_key", "")
    old_endpoint = old.get("endpoint", "")

    entries = []
    for entry in DEFAULT_PROVIDER_ENTRIES:
        e = dict(entry)
        if e["provider"] == old_provider:
            e["model"] = old_model
            e["api_key"] = old_key
            e["endpoint"] = old_endpoint
        entries.append(e)

    return {"current_provider": old_provider, "providers": entries}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"current_provider": "deepseek", "providers": list(DEFAULT_PROVIDER_ENTRIES)}

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"current_provider": "deepseek", "providers": list(DEFAULT_PROVIDER_ENTRIES)}

    # Auto-migrate old format
    if "providers" not in data:
        data = _migrate_old_config(data)
        save_config(data)

    return data


def save_config(config: dict):
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def find_provider_entry(providers: list, name: str) -> dict:
    for entry in providers:
        if entry["provider"] == name:
            return entry
    return {"provider": name, "model": "", "api_key": "", "endpoint": ""}


def apply_entry_to_agent(agent: ModularAgent, entry: dict):
    """Apply a single provider entry to the agent."""
    provider = entry.get("provider", "ollama")
    agent._set_provider(provider)
    api_key = entry.get("api_key", "").strip()
    if api_key:
        agent._set_api_key(api_key, provider)
    endpoint = entry.get("endpoint", "").strip()
    if endpoint:
        agent._set_base_url(endpoint)
    model = entry.get("model", "").strip()
    if model:
        agent.model_name = model
        agent._clear_model_availability_cache()


class AgentBridge:
    def __init__(self):
        self.config = load_config()
        self.current_provider = self.config.get("current_provider", "deepseek")
        active_entry = find_provider_entry(self.config.get("providers", []), self.current_provider)

        api_key = active_entry.get("api_key", "").strip()
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = api_key
            os.environ["OPENAI_API_KEY"] = api_key

        model = active_entry.get("model", "deepseek-v4-flash")
        provider = active_entry.get("provider", "deepseek")

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            self.agent = ModularAgent(model_name=model, provider=provider)
        finally:
            sys.stdout = old_stdout

        self.pending_approval = None
        self.agent.tool_approval_handler = self._request_tool_approval

        apply_entry_to_agent(self.agent, active_entry)
        self.sessions = {}
        self.current_session_id = ""
        self._load_sessions()
        self._apply_current_session_to_agent()

    def _now_iso(self) -> str:
        return datetime.utcnow().isoformat() + "Z"

    def _new_session_title(self) -> str:
        return f"会话 {len(self.sessions) + 1}"

    def _first_user_message_snippet(self, history: list) -> str:
        for item in history or []:
            if str(item.get("role", "")) != "user":
                continue
            content = str(item.get("content", "")).strip()
            if not content or content.startswith("Observation:") or content.startswith("Error in Agent loop:"):
                continue
            return content[:24]
        return ""

    def _make_session(self, title: str = "") -> dict:
        now = self._now_iso()
        return {
            "id": f"session-{uuid.uuid4().hex}",
            "title": title.strip() or self._new_session_title(),
            "created_at": now,
            "updated_at": now,
            "history": [],
            "messages": [],
        }

    def _session_summary(self, session: dict) -> dict:
        history = session.get("history") or []
        user_turns = 0
        for item in history:
            if str(item.get("role", "")) == "user":
                content = str(item.get("content", ""))
                if not content.startswith("Observation:") and not content.startswith("Error in Agent loop:"):
                    user_turns += 1
        return {
            "id": session.get("id", ""),
            "title": session.get("title", ""),
            "updated_at": session.get("updated_at", ""),
            "created_at": session.get("created_at", ""),
            "user_turns": user_turns,
        }

    def _persist_sessions(self):
        payload = {
            "current_session_id": self.current_session_id,
            "sessions": list(self.sessions.values()),
        }
        SESSIONS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_sessions(self):
        data = None
        if SESSIONS_FILE.exists():
            try:
                data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None

        sessions = {}
        if isinstance(data, dict):
            for raw in data.get("sessions", []) or []:
                sid = str(raw.get("id", "")).strip()
                if not sid:
                    continue
                sessions[sid] = {
                    "id": sid,
                    "title": str(raw.get("title", "")).strip() or "会话",
                    "created_at": str(raw.get("created_at", "")).strip() or self._now_iso(),
                    "updated_at": str(raw.get("updated_at", "")).strip() or self._now_iso(),
                    "history": raw.get("history", []) if isinstance(raw.get("history", []), list) else [],
                    "messages": raw.get("messages", []) if isinstance(raw.get("messages", []), list) else [],
                }

        if not sessions:
            session = self._make_session()
            sessions[session["id"]] = session

        current_session_id = ""
        if isinstance(data, dict):
            current_session_id = str(data.get("current_session_id", "")).strip()
        if current_session_id not in sessions:
            current_session_id = next(iter(sessions.keys()))

        self.sessions = sessions
        self.current_session_id = current_session_id
        self._persist_sessions()

    def _get_current_session(self) -> dict:
        session = self.sessions.get(self.current_session_id)
        if session:
            return session
        session = self._make_session()
        self.sessions[session["id"]] = session
        self.current_session_id = session["id"]
        self._persist_sessions()
        return session

    def _save_agent_history_to_current_session(self):
        session = self._get_current_session()
        session["history"] = json.loads(json.dumps(self.agent.history, ensure_ascii=False))
        snippet = self._first_user_message_snippet(session["history"])
        if snippet and session.get("title", "").startswith("会话 "):
            session["title"] = snippet
        session["updated_at"] = self._now_iso()
        self._persist_sessions()

    def _apply_current_session_to_agent(self):
        session = self._get_current_session()
        self.agent.history = json.loads(json.dumps(session.get("history", []), ensure_ascii=False))

    def _append_session_message(self, role: str, text: str, is_error: bool = False):
        session = self._get_current_session()
        messages = session.get("messages") or []
        messages.append({
            "role": role,
            "text": str(text),
            "is_error": bool(is_error),
            "ts": self._now_iso(),
        })
        session["messages"] = messages[-300:]
        session["updated_at"] = self._now_iso()

    def _list_sessions_payload(self):
        ordered = sorted(
            self.sessions.values(),
            key=lambda x: str(x.get("updated_at", "")),
            reverse=True,
        )
        return {
            "type": "list_sessions",
            "ok": True,
            "current_session_id": self.current_session_id,
            "sessions": [self._session_summary(item) for item in ordered],
        }

    def _ensure_active_session_exists(self):
        if self.current_session_id in self.sessions:
            return
        if self.sessions:
            self.current_session_id = next(iter(self.sessions.keys()))
            return
        session = self._make_session()
        self.sessions[session["id"]] = session
        self.current_session_id = session["id"]

    def _request_tool_approval(self, tool_name: str, params: dict, prompt_text: str):
        approval_id = f"approval-{uuid.uuid4().hex}"
        self.pending_approval = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "parameters": params,
            "prompt": prompt_text,
        }
        return None

    def _approval_required_response(self):
        pending = self.pending_approval or {}
        return {
            "type": "approval_required",
            "ok": True,
            "approval_id": pending.get("approval_id"),
            "tool_name": pending.get("tool_name"),
            "parameters": pending.get("parameters") or {},
            "prompt": pending.get("prompt") or "",
        }

    def _get_active_entry(self) -> dict:
        return find_provider_entry(self.config.get("providers", []), self.agent.provider)

    def _get_status_dict(self):
        adapter = self.agent._get_active_provider_adapter()
        return {
            "provider": self.agent.provider,
            "model": self.agent._get_active_model_name(),
            "user": self.agent.current_user,
            "endpoint": adapter.get_endpoint(),
            "api_key_state": adapter.get_api_key_state(),
        }

    def handle(self, payload):
        try:
            return self._handle(payload)
        except Exception as exc:
            return {"type": "error", "ok": False, "error": str(exc)}

    def _handle(self, payload):
        msg_type = str(payload.get("type", "")).strip().lower()

        if msg_type == "ask":
            if self.pending_approval:
                return {
                    "type": "answer",
                    "ok": False,
                    "error": "请先处理当前审批请求（允许 或 skip）。",
                }

            text = str(payload.get("text", "")).strip()
            if not text:
                return {"type": "answer", "ok": False, "error": "empty question"}

            self._append_session_message("user", text)

            try:
                thought_before = self.agent.last_thought
                action_before = self.agent.last_action
                answer = self.agent.run(text, reset_history=False)
                self._append_session_message("agent", str(answer), False)
                self._save_agent_history_to_current_session()
                return {
                    "type": "answer",
                    "ok": True,
                    "text": str(answer),
                    "thought": self.agent.last_thought if self.agent.last_thought != thought_before else "",
                    "action": self.agent.last_action if self.agent.last_action != action_before else None,
                }
            except ToolApprovalPending:
                self._save_agent_history_to_current_session()
                return self._approval_required_response()
            except Exception as exc:
                self._append_session_message("agent", str(exc), True)
                self._save_agent_history_to_current_session()
                return {"type": "answer", "ok": False, "error": str(exc)}

        if msg_type == "tool_approval":
            pending = self.pending_approval
            if not pending:
                return {"type": "answer", "ok": False, "error": "当前没有待审批操作。"}

            approval_id = str(payload.get("approval_id", "")).strip()
            decision = str(payload.get("decision", "")).strip().lower()
            if approval_id != str(pending.get("approval_id", "")):
                return {"type": "answer", "ok": False, "error": "审批请求已过期，请重试。"}
            if decision not in {"allow", "skip"}:
                return {"type": "answer", "ok": False, "error": "decision must be allow or skip"}

            self.pending_approval = None
            try:
                thought_before = self.agent.last_thought
                action_before = self.agent.last_action
                answer = self.agent.resume_pending_approval(approved=(decision == "allow"))
                self._append_session_message("agent", str(answer), False)
                self._save_agent_history_to_current_session()
                return {
                    "type": "answer",
                    "ok": True,
                    "text": str(answer),
                    "thought": self.agent.last_thought if self.agent.last_thought != thought_before else "",
                    "action": self.agent.last_action if self.agent.last_action != action_before else None,
                }
            except ToolApprovalPending:
                self._save_agent_history_to_current_session()
                return self._approval_required_response()
            except Exception as exc:
                self._append_session_message("agent", str(exc), True)
                self._save_agent_history_to_current_session()
                return {"type": "answer", "ok": False, "error": str(exc)}

        if msg_type == "reset":
            self.agent.history = []
            self.pending_approval = None
            session = self._get_current_session()
            session["history"] = []
            session["messages"] = []
            session["updated_at"] = self._now_iso()
            self._persist_sessions()
            return {"type": "reset", "ok": True}

        if msg_type == "list_sessions":
            return self._list_sessions_payload()

        if msg_type == "create_session":
            self._save_agent_history_to_current_session()
            title = str(payload.get("title", "")).strip()
            session = self._make_session(title)
            self.sessions[session["id"]] = session
            self.current_session_id = session["id"]
            self.pending_approval = None
            self.agent.history = []
            self._persist_sessions()
            return {
                "type": "create_session",
                "ok": True,
                "current_session_id": self.current_session_id,
                "messages": session.get("messages", []),
            }

        if msg_type == "switch_session":
            target_id = str(payload.get("session_id", "")).strip()
            if not target_id:
                return {"type": "switch_session", "ok": False, "error": "session_id is required"}
            target = self.sessions.get(target_id)
            if not target:
                return {"type": "switch_session", "ok": False, "error": "session not found"}

            self._save_agent_history_to_current_session()
            self.current_session_id = target_id
            self.pending_approval = None
            self._apply_current_session_to_agent()
            target["updated_at"] = self._now_iso()
            self._persist_sessions()
            return {
                "type": "switch_session",
                "ok": True,
                "current_session_id": self.current_session_id,
                "messages": target.get("messages", []),
            }

        if msg_type == "rename_session":
            target_id = str(payload.get("session_id", "")).strip()
            title = str(payload.get("title", "")).strip()
            if not target_id:
                return {"type": "rename_session", "ok": False, "error": "session_id is required"}
            if not title:
                return {"type": "rename_session", "ok": False, "error": "title is required"}
            target = self.sessions.get(target_id)
            if not target:
                return {"type": "rename_session", "ok": False, "error": "session not found"}
            target["title"] = title[:60]
            target["updated_at"] = self._now_iso()
            self._persist_sessions()
            return {
                "type": "rename_session",
                "ok": True,
                "session": self._session_summary(target),
            }

        if msg_type == "delete_session":
            target_id = str(payload.get("session_id", "")).strip()
            if not target_id:
                return {"type": "delete_session", "ok": False, "error": "session_id is required"}
            target = self.sessions.get(target_id)
            if not target:
                return {"type": "delete_session", "ok": False, "error": "session not found"}

            current_before_delete = self.current_session_id
            if current_before_delete == target_id:
                self._save_agent_history_to_current_session()

            self.sessions.pop(target_id, None)
            self._ensure_active_session_exists()
            self.pending_approval = None

            if current_before_delete == target_id:
                self._apply_current_session_to_agent()

            active = self._get_current_session()
            active["updated_at"] = self._now_iso()
            self._persist_sessions()
            return {
                "type": "delete_session",
                "ok": True,
                "current_session_id": self.current_session_id,
                "messages": active.get("messages", []),
            }

        if msg_type == "ping":
            return {"type": "pong", "ok": True}

        if msg_type == "status":
            return {"type": "status", "ok": True, **self._get_status_dict()}

        if msg_type == "get_config":
            active = self._get_active_entry()
            return {
                "type": "get_config",
                "ok": True,
                "current_provider": self.agent.provider,
                "providers": self.config.get("providers", []),
                "active": active,
            }

        if msg_type == "set_config":
            provider_name = str(payload.get("provider", "")).strip()
            model = str(payload.get("model", "")).strip()
            api_key = str(payload.get("api_key", "")).strip()
            endpoint = str(payload.get("endpoint", "")).strip()

            if not provider_name:
                return {"type": "set_config", "ok": False, "error": "provider is required"}

            providers = self.config.get("providers", [])

            # Update the matching provider entry
            found = False
            for entry in providers:
                if entry["provider"] == provider_name:
                    entry["model"] = model
                    entry["api_key"] = api_key
                    entry["endpoint"] = endpoint
                    found = True
                    break
            if not found:
                providers.append({"provider": provider_name, "model": model, "api_key": api_key, "endpoint": endpoint})

            # Apply to live agent
            if api_key:
                os.environ["DEEPSEEK_API_KEY"] = api_key
                os.environ["OPENAI_API_KEY"] = api_key

            self.agent._set_provider(provider_name)
            if api_key:
                self.agent._set_api_key(api_key, provider_name)
            if endpoint:
                self.agent._set_base_url(endpoint)
            if model:
                self.agent.model_name = model
                self.agent._clear_model_availability_cache()

            # Update config
            self.config["current_provider"] = provider_name
            self.config["providers"] = providers
            save_config(self.config)

            return {
                "type": "set_config",
                "ok": True,
                **self._get_status_dict(),
            }

        return {"type": "error", "ok": False, "error": "unknown message type"}


def main():
    bridge = AgentBridge()
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            response = {"type": "error", "ok": False, "error": "invalid json"}
            print(json.dumps(response, ensure_ascii=False), flush=True)
            continue

        response = bridge.handle(payload)
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
