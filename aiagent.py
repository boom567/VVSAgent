import subprocess
import json
import ast
import operator
import importlib.util
import re
from pathlib import Path

import ollama

from prompt_toolkit import prompt

# ===========================================
# 1. 技能注册中心 (Skill Registry)
# ===========================================
class SkillRegistry:
    def __init__(self):
        self.skills = {}

    def register(self, name, func, description, parameters):
        """
        name: 技能名称
        func: 执行函数
        description: 技能描述
        parameters: 参数的 JSON Schema 格式
        """
        self.skills[name] = {
            "func": func,
            "description": description,
            "parameters": parameters
        }

    def get_tools_definition(self):
        """生成给 LLM 看的工具描述列表"""
        tools_list = []
        for name, info in self.skills.items():
            tools_list.append({
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"]
            })
        return tools_list

# ===========================================
# 2. 系统执行器 (System Executor)
#===========================================
class SystemExecutor:
    @staticmethod
    def execute_shell(command: str):
        """执行系统命令并返回结果"""
        try:
            print(f"  [System] Executing: {command}")
            # 使用 check_output 并捕获 stderr
            result = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, text=True)
            return result if result else "Command executed successfully (no output)."
        except subprocess.CalledProcessError as e:
            return f"Error: {e.output}"
        except Exception as e:
            return f"System Error: {str(e)}"

# ===========================================
# 3. 核心 Agent (Modular Agent)
# ===========================================
class ModularAgent:
    def __init__(self, model_name="llama3"):
        self.model_name = model_name
        self.client = ollama.Client(host="http://127.0.0.1:11434", trust_env=False)
        self.registry = SkillRegistry()
        self.history = []
        self.max_iterations = 16
        self.current_user = "default"
        self.auto_memory_enabled = True
        
        # 默认注册系统命令工具
        self.registry.register(
            name="execute_shell",
            func=self._execute_shell_with_confirmation,
            description="Execute a shell command on the local system after user confirmation.",
            parameters={"command": "string"}
        )
        self.load_skills()

    def add_skill(self, name, func, description, parameters):
        self.registry.register(name, func, description, parameters)

    def load_skills(self):
        skills_dir = Path(__file__).parent / "skills"
        if not skills_dir.exists():
            return

        for skill_file in sorted(skills_dir.glob("*_skill.py")):
            module_name = f"skills.{skill_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, skill_file)
            if not spec or not spec.loader:
                print(f"[Skill Loader] Skipped invalid skill file: {skill_file.name}")
                continue

            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if callable(register):
                    register(self)
                    print(f"[Skill Loader] Loaded {skill_file.stem}")
                else:
                    print(f"[Skill Loader] Missing register(agent) in {skill_file.name}")
            except Exception as exc:
                print(f"[Skill Loader] Failed to load {skill_file.name}: {exc}")

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
        lines.append(f"Preferred model: {profile.get('model_name') or self.model_name}")

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

    def _get_active_model_name(self):
        profile = self._get_current_user_profile_data(conclusions_limit=0)
        if profile:
            preferred_model = (profile.get("model_name") or "").strip()
            if preferred_model:
                return preferred_model
        return self.model_name

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
        update_profile_tool["func"](self.current_user, self._get_active_model_name(), preference_text)
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
            response = self.client.chat(
                model=self._get_active_model_name(),
                messages=[
                    {"role": "system", "content": self._build_memory_decision_prompt(user_input, assistant_answer)}
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
- Active model: {active_model_name}

User profile context from the local XML knowledge base:
{current_user_context}

Rules:
- Keep responses concise and practical.
- Respect the active user's stored preferences and recent conclusions when answering.
- Use tools only when they are needed.
- If a user asks you to run a command, use the execute_shell tool instead of pretending to run it.
- The execute_shell tool requires confirmation from the human before the command will run.
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

    def _execute_shell_with_confirmation(self, command: str):
        answer = self._read_console_input(f"[Confirm] Run shell command '{command}'? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            return "Command execution cancelled by user."
        return SystemExecutor.execute_shell(command)

    def _read_console_input(self, prompt_text: str):
        return prompt(prompt_text)

    def _canonical_parameter_name(self, name: str):
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def _normalize_action_parameters(self, tool_name: str, params):
        if not isinstance(params, dict):
            return params

        expected = self.registry.skills.get(tool_name, {}).get("parameters", {})
        if not expected:
            return params

        canonical_to_actual = {
            self._canonical_parameter_name(parameter_name): parameter_name
            for parameter_name in expected.keys()
        }

        normalized = {}
        for key, value in params.items():
            if key in expected:
                normalized[key] = value
                continue

            canonical = self._canonical_parameter_name(str(key))
            actual_name = canonical_to_actual.get(canonical)
            if actual_name:
                normalized[actual_name] = value

        return normalized

    def _request_json_retry(self, content: str, reason: str):
        self.history.append({"role": "assistant", "content": content})
        self.history.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response was invalid: {reason}. "
                    "Respond again with exactly one complete JSON object only. "
                    "Use the exact tool name and exact parameter names from the tool definitions."
                ),
            }
        )

    def run(self, user_input, reset_history=True):
        if reset_history:
            self.history = []

        # 初始用户输入
        self.history.append({"role": "user", "content": user_input})
        
        # 最大迭代次数，防止死循环
        for i in range(self.max_iterations):
            print(f"\n--- Iteration {i+1} ---")
            
            # 1. 调用 LLM
            try:
                response = self.client.chat(
                    model=self._get_active_model_name(),
                    messages=[{"role": "system", "content": self._get_system_prompt()}] + self.history,
                    format="json"
                )
                content = response['message']['content']
                
                # 2. 解析 JSON (由于 LLM 可能在 JSON 前后加 Markdown 代码块，需要清洗)
                json_str = self._extract_json(content)
                if not json_str:
                    self._request_json_retry(content, "response was not complete valid JSON")
                    continue
                
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError as exc:
                    self._request_json_retry(content, f"JSON decode error: {exc}")
                    continue
                #不需要思考过程
                #print(f"[AI Thought]: {data.get('thought')}")

                # 3. 检查是 Action 还是 Final Answer
                if "final_answer" in data:
                    #print(f"[Final Answer]: {data['final_answer']}")
                    self.history.append({"role": "assistant", "content": content})
                    self._maybe_store_conversation_conclusion(user_input, data["final_answer"])
                    return data["final_answer"]

                elif "action" in data:
                    action = data["action"]
                    tool_name = action["name"]
                    params = self._normalize_action_parameters(tool_name, action.get("parameters", {}))

                    # 4. 查找并执行技能
                    if tool_name in self.registry.skills:
                        print(f"[Action]: Calling {tool_name} with {params}")
                        func = self.registry.skills[tool_name]["func"]
                        try:
                            observation = str(func(**params))
                        except Exception as e:
                            observation = f"Error executing tool: {str(e)}"
                    else:
                        observation = f"Error: Tool '{tool_name}' not found."

                    # 5. 将观察结果反馈给 AI
                    print(f"[Observation]: {observation[:100]}...")
                    self.history.append({"role": "assistant", "content": content})
                    self.history.append({"role": "user", "content": f"Observation: {observation}"})
                
                else:
                    raise ValueError("JSON missing both 'action' and 'final_answer'.")

            except Exception as e:
                error_msg = f"Error in Agent loop: {str(e)}"
                print(error_msg)
                self.history.append({"role": "user", "content": error_msg})
                break
        
        return "Agent stopped: Max iterations reached or error occurred."

    def chat_loop(self):
        print("AI Agent console started.")
        print(f"Current user: {self.current_user}")
        print("Commands: /help, /reset, /voice, /voice-config, /user <name>, /whoami, /autosave, /exit")

        while True:
            try:
                user_input = self._read_console_input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting agent console.")
                break

            if not user_input:
                continue

            if user_input in {"/exit", "/quit"}:
                print("Exiting agent console.")
                break

            if user_input == "/help":
                print("/help  Show available console commands")
                print("/reset Clear conversation history")
                print("/voice Enter continuous voice conversation mode")
                print("/voice-config Show or update voice settings for the current user")
                print("/user <name> Switch the active user profile")
                print("/whoami Show the active user profile context")
                print("/autosave Show or toggle automatic knowledge-base saving")
                print("/exit  Exit the console")
                print("In voice mode, say 退出对话 to exit the voice conversation.")
                print("Example: /voice-config language=zh-CN voice=Tingting rate=210")
                print("Ask normal questions directly. If a command is needed, the agent will ask for confirmation.")
                continue

            if user_input == "/reset":
                self.history = []
                print("Conversation history cleared.")
                continue

            if user_input == "/voice":
                result = self._start_voice_chat_loop()
                print(f"Agent> {result}")
                continue

            if user_input.startswith("/voice-config"):
                raw_text = user_input[len("/voice-config") :].strip()
                result = self._update_current_user_voice_config(raw_text)
                print(f"Agent> {result}")
                continue

            if user_input.startswith("/user "):
                next_user = user_input.split(" ", 1)[1].strip()
                try:
                    self._set_current_user(next_user)
                except ValueError as exc:
                    print(f"Agent> {exc}")
                    continue
                print(f"Agent> Switched active user to: {self.current_user}")
                continue

            if user_input == "/whoami":
                print(self._format_current_user_context())
                print(f"Active model: {self._get_active_model_name()}")
                print(f"Auto memory save: {'on' if self.auto_memory_enabled else 'off'}")
                continue

            if user_input.startswith("/autosave"):
                parts = user_input.split()
                if len(parts) == 1:
                    print(f"Agent> Auto memory save is {'on' if self.auto_memory_enabled else 'off'}.")
                    continue

                option = parts[1].strip().lower()
                if option in {"on", "true", "1"}:
                    self.auto_memory_enabled = True
                elif option in {"off", "false", "0"}:
                    self.auto_memory_enabled = False
                else:
                    print("Agent> Usage: /autosave [on|off]")
                    continue

                print(f"Agent> Auto memory save is now {'on' if self.auto_memory_enabled else 'off'}.")
                continue

            answer = self.run(user_input, reset_history=False)
            print(f"Agent> {answer}")

    def _extract_json(self, text):
        """清洗 LLM 输出，提取 JSON 字符串"""
        text = text.strip()
        if not text:
            return None

        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        if start == -1:
            return None

        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text[start:])
            return json.dumps(obj)
        except json.JSONDecodeError:
            return None


ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_calculate(expression: str):
    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            return ALLOWED_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_OPERATORS:
            operand = evaluate(node.operand)
            return ALLOWED_OPERATORS[type(node.op)](operand)
        raise ValueError("Unsupported mathematical expression.")

    parsed = ast.parse(expression, mode="eval")
    return str(evaluate(parsed))

# ===========================================
# 4. 测试与扩展演示
# ===========================================
if __name__ == "__main__":
    agent = ModularAgent(model_name="gemma4:26b")
    agent.add_skill(
        name="calculator",
        func=safe_calculate,
        description="Evaluate a mathematical expression.",
        parameters={"expression": "string"}
    )
    agent.chat_loop()
