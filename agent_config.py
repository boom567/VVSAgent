import getpass
import importlib
import importlib.util
import json
from pathlib import Path


if importlib.util.find_spec("prompt_toolkit"):
    prompt = importlib.import_module("prompt_toolkit").prompt
else:
    def prompt(prompt_text):
        return input(prompt_text)


AGENT_CONFIG_PATH = Path(__file__).resolve().parent / "agent_config.json"
DEFAULT_PROVIDER_SETUP = {
    "ollama": {
        "model": "llama3",
        "endpoint": "http://127.0.0.1:11434",
        "requires_api_key": False,
    },
    "deepseek": {
        "model": "deepseek-v4-flash",
        "endpoint": "https://api.deepseek.com",
        "requires_api_key": True,
    },
    "openai": {
        "model": "gpt-4o-mini",
        "endpoint": "https://api.openai.com/v1",
        "requires_api_key": True,
    },
    "vmlx": {
        "model": "llm/Gemma-4-26B-A4B-JANG_2L-CRACK",
        "endpoint": "http://127.0.0.1:8080",
        "requires_api_key": False,
    },
}


def _build_default_agent_config(current_provider: str = "ollama"):
    providers = {}
    for provider_name, defaults in DEFAULT_PROVIDER_SETUP.items():
        providers[provider_name] = {
            "model": defaults["model"],
            "api_key": "",
            "endpoint": defaults["endpoint"],
        }
    return {
        "current_provider": current_provider,
        "providers": providers,
    }


def _normalize_agent_config(raw):
    base = _build_default_agent_config()
    if not isinstance(raw, dict):
        return base

    normalized = _build_default_agent_config(raw.get("current_provider", "ollama"))
    current_provider = str(raw.get("current_provider", "")).strip().lower()
    if current_provider in DEFAULT_PROVIDER_SETUP:
        normalized["current_provider"] = current_provider

    providers_dict = raw.get("providers") if isinstance(raw.get("providers"), dict) else None
    if providers_dict:
        for provider_name in DEFAULT_PROVIDER_SETUP:
            provider_raw = providers_dict.get(provider_name, {})
            if not isinstance(provider_raw, dict):
                continue
            model = str(provider_raw.get("model", "")).strip()
            api_key = str(provider_raw.get("api_key", "")).strip()
            endpoint = str(provider_raw.get("endpoint", "")).strip()
            if model:
                normalized["providers"][provider_name]["model"] = model
            if endpoint:
                normalized["providers"][provider_name]["endpoint"] = endpoint
            normalized["providers"][provider_name]["api_key"] = api_key

    providers_list = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    for item in providers_list:
        if not isinstance(item, dict):
            continue
        provider_name = str(item.get("provider", "")).strip().lower()
        if provider_name not in DEFAULT_PROVIDER_SETUP:
            continue
        model = str(item.get("model", "")).strip()
        api_key = str(item.get("api_key", "")).strip()
        endpoint = str(item.get("endpoint", "")).strip()
        if model:
            normalized["providers"][provider_name]["model"] = model
        if endpoint:
            normalized["providers"][provider_name]["endpoint"] = endpoint
        normalized["providers"][provider_name]["api_key"] = api_key

    if normalized["current_provider"] not in DEFAULT_PROVIDER_SETUP:
        normalized["current_provider"] = "ollama"
    return normalized


def load_agent_config(config_path: Path | None = None):
    target = config_path or AGENT_CONFIG_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _normalize_agent_config(raw)


def save_agent_config(config_data, config_path: Path | None = None):
    target = config_path or AGENT_CONFIG_PATH
    normalized = _normalize_agent_config(config_data)
    target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


def _ask_with_default(prompt_text: str, default_value: str):
    value = prompt(f"{prompt_text} [{default_value}]: ").strip()
    return value or default_value


def _ask_provider_choice():
    providers = list(DEFAULT_PROVIDER_SETUP.keys())
    print("\n请选择供应商:")
    for index, provider_name in enumerate(providers, start=1):
        print(f"{index}. {provider_name}")

    while True:
        raw = prompt("输入序号或名称 (默认 1): ").strip().lower()
        if not raw:
            return providers[0]
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= len(providers):
                return providers[number - 1]
        if raw in DEFAULT_PROVIDER_SETUP:
            return raw
        print("无效输入，请重新输入。")


def run_first_time_setup_wizard(config_path: Path | None = None):
    print("\n[Setup] First-time model configuration")
    selected_provider = _ask_provider_choice()
    defaults = DEFAULT_PROVIDER_SETUP[selected_provider]

    model_name = _ask_with_default("模型名称", defaults["model"])
    endpoint = _ask_with_default("服务地址", defaults["endpoint"])
    api_key = ""
    if defaults["requires_api_key"]:
        api_key = getpass.getpass("API Key (输入时不显示，可留空后续再配): ").strip()

    config_data = _build_default_agent_config(current_provider=selected_provider)
    config_data["providers"][selected_provider]["model"] = model_name
    config_data["providers"][selected_provider]["endpoint"] = endpoint
    config_data["providers"][selected_provider]["api_key"] = api_key

    save_agent_config(config_data, config_path=config_path)
    print(f"[Setup] Configuration saved to: {config_path or AGENT_CONFIG_PATH}")
    return config_data


def resolve_or_create_agent_config(config_path: Path | None = None):
    target = config_path or AGENT_CONFIG_PATH
    config_data = load_agent_config(target)
    if config_data:
        return config_data
    return run_first_time_setup_wizard(config_path=target)
