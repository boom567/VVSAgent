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

    def _get_system_prompt(self):
        tools_def = self.registry.get_tools_definition()
        return f"""You are a helpful AI Agent. You have access to the following tools:
{json.dumps(tools_def, indent=2)}

Rules:
- Keep responses concise and practical.
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
                    model=self.model_name,
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
        print("Commands: /help, /reset, /voice, /exit")

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
                print("/exit  Exit the console")
                print("In voice mode, say 退出对话 to exit the voice conversation.")
                print("Ask normal questions directly. If a command is needed, the agent will ask for confirmation.")
                continue

            if user_input == "/reset":
                self.history = []
                print("Conversation history cleared.")
                continue

            if user_input == "/voice":
                voice_tool = self.registry.skills.get("voice_chat_loop")
                if not voice_tool:
                    print("Agent> Voice skill is not loaded.")
                    continue

                result = voice_tool["func"]()
                print(f"Agent> {result}")
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
