import json


class AgentUserMixin:
    def _get_current_user_profile_data(self, conclusions_limit=5):
        profile_tool = self.registry.skills.get("get_user_profile_data")
        if not profile_tool or not self.current_user:
            return None

        try:
            data = profile_tool["func"](self.current_user, conclusions_limit=conclusions_limit)
        except Exception as exc:
            print(f"[Knowledge Base] Failed to read profile for {self.current_user}: {exc}")
            return None

        if not isinstance(data, dict) or not data.get("found"):
            return None
        return data

    def _format_current_user_context(self):
        if not self.current_user:
            return "No active user selected."

        profile = self._get_current_user_profile_data(conclusions_limit=5)
        if not profile:
            return (
                f"Current active user: {self.current_user}\n"
                "No stored profile was found for this user."
            )

        lines = [f"Current active user: {profile.get('user_name', self.current_user)}"]
        lines.append(f"Launch model: {self.model_name or '(not set)'}")
        lines.append(f"Profile fallback model: {self._get_profile_model_name() or '(not set)'}")
        lines.append(f"Active model: {self._get_active_model_name()}")

        preferences = profile.get("preferences", {})
        lines.append("Preferences:")
        if preferences:
            for key, value in preferences.items():
                lines.append(f"- {key}: {value}")
        else:
            lines.append("- (none)")

        conclusions = profile.get("conclusions", [])
        lines.append("Recent conclusions:")
        if conclusions:
            for item in conclusions:
                prefix = f"[{item.get('timestamp', '')}]"
                if item.get("topic"):
                    prefix += f" ({item['topic']})"
                lines.append(f"- {prefix} {item.get('text', '')}")
        else:
            lines.append("- (none)")

        return "\n".join(lines)

    def _get_profile_model_name(self):
        profile = self._get_current_user_profile_data(conclusions_limit=0)
        if not profile:
            return ""
        return (profile.get("model_name") or "").strip()

    def _get_voice_skill_options(self):
        profile = self._get_current_user_profile_data(conclusions_limit=0)
        preferences = profile.get("preferences", {}) if profile else {}

        language = (preferences.get("voice_language") or preferences.get("language") or "zh-CN").strip()
        voice = (preferences.get("voice_name") or preferences.get("voice") or "").strip()
        rate_text = str(preferences.get("voice_rate") or preferences.get("rate") or "180").strip()

        try:
            rate = int(rate_text)
        except ValueError:
            rate = 180

        return {
            "language": language,
            "voice": voice,
            "rate": rate,
        }

    def _start_voice_chat_loop(self):
        voice_tool = self.registry.skills.get("voice_chat_loop")
        if not voice_tool:
            return "Voice skill is not loaded."

        voice_options = self._get_voice_skill_options()
        print(
            "[Voice] Starting loop "
            f"for user={self.current_user}, language={voice_options['language']}, "
            f"voice={voice_options['voice'] or '(default)'}, rate={voice_options['rate']}"
        )
        return voice_tool["func"](**voice_options)

    def _parse_assignment_arguments(self, raw_text: str):
        assignments = {}
        for item in raw_text.split():
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key:
                assignments[key] = value
        return assignments

    def _update_current_user_voice_config(self, raw_text: str):
        update_profile_tool = self.registry.skills.get("upsert_user_profile")
        if not update_profile_tool:
            return "Knowledge base skill is not loaded."

        assignments = self._parse_assignment_arguments(raw_text)
        if not assignments:
            options = self._get_voice_skill_options()
            return (
                f"Current voice config for {self.current_user}: "
                f"language={options['language']}, voice={options['voice'] or '(default)'}, rate={options['rate']}"
            )

        mapping = {
            "language": "voice_language",
            "voice": "voice_name",
            "rate": "voice_rate",
            "voice_language": "voice_language",
            "voice_name": "voice_name",
            "voice_rate": "voice_rate",
        }

        normalized_preferences = {}
        for key, value in assignments.items():
            mapped_key = mapping.get(key)
            if mapped_key:
                normalized_preferences[mapped_key] = value

        if not normalized_preferences:
            return "Usage: /voice-config language=zh-CN voice=Tingting rate=210"

        preference_text = ";".join(f"{key}={value}" for key, value in normalized_preferences.items())
        update_profile_tool["func"](self.current_user, "", preference_text)
        options = self._get_voice_skill_options()
        return (
            f"Updated voice config for {self.current_user}: "
            f"language={options['language']}, voice={options['voice'] or '(default)'}, rate={options['rate']}"
        )

    def _build_memory_decision_prompt(self, user_input: str, assistant_answer: str):
        return f"""You decide whether a conversation turn should be saved into a long-term local knowledge base for one user.

Active user: {self.current_user}

Save only information that is stable and likely useful later, such as:
- user preferences
- user decisions
- persistent project conventions
- explicit conclusions reached in the conversation

Do not save:
- casual chit-chat
- temporary troubleshooting noise
- obvious one-off answers with no future value

Return exactly one JSON object with this schema:
{{
  "should_save": true or false,
  "topic": "short topic string",
  "conclusion": "one concise sentence to store"
}}

User message:
{user_input}

Assistant answer:
{assistant_answer}
"""

    def _deduplicate_conclusion(self, candidate_text: str):
        candidate = " ".join(candidate_text.strip().split())
        if not candidate:
            return False

        profile = self._get_current_user_profile_data(conclusions_limit=10)
        if not profile:
            return False

        existing_conclusions = profile.get("conclusions", [])
        for item in existing_conclusions:
            existing = " ".join((item.get("text") or "").strip().split())
            if existing == candidate:
                return True
        return False

    def _maybe_store_conversation_conclusion(self, user_input: str, assistant_answer: str):
        if not self.auto_memory_enabled:
            return

        add_conclusion_tool = self.registry.skills.get("add_conversation_conclusion")
        if not add_conclusion_tool or not self.current_user:
            return

        try:
            response = self.chat_completion(
                model=self._get_active_model_name(),
                messages=[
                    {
                        "role": "system",
                        "content": self._build_memory_decision_prompt(user_input, assistant_answer),
                    }
                ],
                format="json",
            )
            content = response["message"]["content"]
            json_str = self._extract_json(content)
            if not json_str:
                return
            decision = json.loads(json_str)
        except Exception as exc:
            print(f"[Knowledge Base] Auto-save decision failed: {exc}")
            return

        should_save = bool(decision.get("should_save"))
        conclusion = str(decision.get("conclusion", "")).strip()
        topic = str(decision.get("topic", "general")).strip() or "general"
        if not should_save or not conclusion:
            return

        if self._deduplicate_conclusion(conclusion):
            print(f"[Knowledge Base] Skipped duplicate conclusion for user {self.current_user}")
            return

        try:
            add_conclusion_tool["func"](self.current_user, conclusion, topic)
            print(f"[Knowledge Base] Stored conclusion for {self.current_user}: {conclusion}")
        except Exception as exc:
            print(f"[Knowledge Base] Failed to store conclusion: {exc}")

    def _set_current_user(self, user_name: str):
        normalized = user_name.strip()
        if not normalized:
            raise ValueError("user name cannot be empty")
        self.current_user = normalized

    def _get_system_prompt(self):
        tools_def = self.registry.get_tools_definition()
        active_model_name = self._get_active_model_name()
        current_user_context = self._format_current_user_context()
        return f"""You are a helpful AI Agent. You have access to the following tools:
{json.dumps(tools_def, indent=2)}

Current conversation context:
- Active user: {self.current_user}
- Active provider: {self.provider}
- Active model: {active_model_name}

User profile context from the local XML knowledge base:
{current_user_context}

Rules:
- Keep responses concise and practical.
- Respect the active user's stored preferences and recent conclusions when answering.
- Use tools only when they are needed.
- If a user asks you to run a command, use the execute_shell tool instead of pretending to run it.
- The execute_shell tool requires confirmation only for file-creation or file-deletion commands.
- When enough information is available, return a direct final answer.
- Respond with exactly one complete JSON object.
- Do not wrap JSON in Markdown.
- If you call a tool, use the exact tool name and exact parameter names from the tool definitions.
- Do not invent parameter names.

You must respond in a valid JSON format. Your JSON must contain:
1. "thought": A brief explanation of your reasoning.
2. Either "action": A tool call, OR "final_answer": Your ultimate response to the user.

If you use "action", it must include:
- "name": The name of the tool to use.
- "parameters": A dictionary of arguments for the tool.

Example Tool Call:
{{
  "thought": "I need to check the current directory.",
  "action": {{
    "name": "execute_shell",
    "parameters": {{"command": "ls"}}
  }}
}}

Example Final Answer:
{{
  "thought": "I have found the file.",
  "final_answer": "The file exists in the current directory."
}}
"""
