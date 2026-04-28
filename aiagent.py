import ast
import importlib.util
import operator
from pathlib import Path

from agent_config import AGENT_CONFIG_PATH, resolve_or_create_agent_config, save_agent_config
from agent_core import SkillRegistry
from agent_provider_mixin import AgentProviderMixin
from agent_tooling_mixin import AgentToolingMixin
from agent_user_mixin import AgentUserMixin


class ModularAgent(AgentProviderMixin, AgentUserMixin, AgentToolingMixin):
    def __init__(self, model_name="llama3", provider="ollama"):
        self.model_name = model_name
        self.provider_adapters = self._build_provider_adapters()
        self.provider_aliases = self._build_provider_aliases()
        self.provider = self._normalize_provider_name(provider) or "ollama"
        self.model_availability_cache = {}
        self.client = self
        self.registry = SkillRegistry()
        self.history = []
        self.max_iterations = 100
        self.current_user = "default"
        self.auto_memory_enabled = True
        self.last_thought = ""
        self.last_action = None
        self.tool_approval_handler = None
        self.pending_tool_call = None
        self.provider_models = {}
        self.config_path = AGENT_CONFIG_PATH
        self.auto_persist_config = False
        print("model:", model_name)

        self.registry.register(
            name="execute_shell",
            func=self._execute_shell_with_confirmation,
            description="Execute a shell command on the local system after user confirmation.",
            parameters={"command": "string"},
        )
        self.load_skills()

    def enable_config_persistence(self, config_path: Path | None = None):
        self.config_path = config_path or AGENT_CONFIG_PATH
        self.auto_persist_config = True

    def _persist_agent_config(self):
        if not self.auto_persist_config:
            return
        try:
            save_agent_config(self._build_agent_config_snapshot(), self.config_path)
        except Exception as exc:
            print(f"[Config] Failed to save config: {exc}")

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

    def run(self, user_input, reset_history=True):
        if self.pending_tool_call:
            return "Pending approval exists. Please allow or skip the current file action first."

        if reset_history:
            self.history = []

        self.history.append({"role": "user", "content": user_input})
        return self._run_iterations(0, user_input)

    def chat_loop(self):
        print("AI Agent console started.")
        print(f"Current user: {self.current_user}")
        print("Commands: /help, /reset, /voice, /voice-config, /provider, /model, /apikey, /endpoint, /user <name>, /whoami, /autosave, /exit")

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
                print(f"/provider Show or switch LLM provider: {self._supported_provider_names()}")
                print("/model Show or set model name for current provider")
                print("/apikey Configure API key: /apikey <key> or /apikey <provider> <key>")
                print("/endpoint Configure provider endpoint/host URL")
                print("/user <name> Switch the active user profile")
                print("/whoami Show the active user profile context")
                print("/autosave Show or toggle automatic knowledge-base saving")
                print("/exit  Exit the console")
                print("In voice mode, say 退出对话 to exit the voice conversation.")
                print("Example: /voice-config language=zh-CN voice=Tingting rate=210")
                print("Example: /provider deepseek")
                print("Example: /model deepseek-v4-flash")
                print("Example: /apikey sk-xxxx")
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

            if user_input.startswith("/provider"):
                parts = user_input.split()
                if len(parts) == 1:
                    print(self._get_provider_status())
                    continue

                result = self._set_provider(parts[1])
                print(f"Agent> {result}")
                print(self._get_provider_status())
                continue

            if user_input.startswith("/model"):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    print(f"Agent> Current model: {self._get_active_model_name()}")
                    continue
                result = self._set_model_name(parts[1])
                print(f"Agent> {result}")
                continue

            if user_input.startswith("/apikey"):
                parts = user_input.split()
                if len(parts) == 1:
                    print("Agent> Usage: /apikey <key> OR /apikey <provider> <key>")
                    continue

                if len(parts) == 2:
                    result = self._set_api_key(parts[1])
                else:
                    result = self._set_api_key(parts[2], provider_name=parts[1])
                print(f"Agent> {result}")
                continue

            if user_input.startswith("/endpoint"):
                parts = user_input.split(maxsplit=1)
                if len(parts) == 1:
                    print("Agent> Usage: /endpoint <url>")
                    continue
                result = self._set_base_url(parts[1])
                print(f"Agent> {result}")
                continue

            if user_input == "/whoami":
                print(self._format_current_user_context())
                print(self._get_provider_status())
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


def build_agent_from_config(config_path: Path | None = None):
    config_data = resolve_or_create_agent_config(config_path=config_path)
    active_provider = config_data.get("current_provider", "ollama")
    active_provider_settings = config_data.get("providers", {}).get(active_provider, {})
    model_name = (active_provider_settings.get("model") or "llama3").strip() or "llama3"

    agent = ModularAgent(model_name=model_name, provider=active_provider)
    agent.apply_agent_config(config_data)
    agent.enable_config_persistence(config_path=config_path or AGENT_CONFIG_PATH)
    return agent


if __name__ == "__main__":
    agent = build_agent_from_config(config_path=AGENT_CONFIG_PATH)
    agent.add_skill(
        name="calculator",
        func=safe_calculate,
        description="Evaluate a mathematical expression.",
        parameters={"expression": "string"},
    )
    agent.chat_loop()
