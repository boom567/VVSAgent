import json

from agent_config import _build_default_agent_config, _normalize_agent_config
from provider_adapters import MLXProviderAdapter, OllamaProviderAdapter, OpenAICompatibleProviderAdapter


class AgentProviderMixin:
    def apply_agent_config(self, config_data):
        normalized = _normalize_agent_config(config_data)
        self.provider_models = {}

        for provider_name, values in normalized.get("providers", {}).items():
            adapter = self.provider_adapters.get(provider_name)
            if not adapter:
                continue

            saved_model = str(values.get("model", "")).strip()
            if saved_model:
                self.provider_models[provider_name] = saved_model

            saved_endpoint = str(values.get("endpoint", "")).strip()
            if saved_endpoint:
                try:
                    adapter.set_base_url(saved_endpoint)
                except Exception:
                    pass

            saved_key = str(values.get("api_key", "")).strip()
            if adapter.supports_api_key() and saved_key:
                adapter.set_api_key(saved_key)

        saved_provider = self._normalize_provider_name(normalized.get("current_provider", ""))
        if saved_provider:
            self.provider = saved_provider

        saved_model_for_provider = self.provider_models.get(self.provider, "").strip()
        if saved_model_for_provider:
            self.model_name = saved_model_for_provider

    def _build_agent_config_snapshot(self):
        snapshot = _build_default_agent_config(current_provider=self.provider)
        for provider_name, adapter in self.provider_adapters.items():
            provider_item = snapshot["providers"][provider_name]
            provider_item["endpoint"] = adapter.get_endpoint()
            provider_item["model"] = self.provider_models.get(provider_name) or provider_item["model"]
            if adapter.supports_api_key():
                provider_item["api_key"] = getattr(adapter, "api_key", "") or ""

        snapshot["providers"][self.provider]["model"] = self.model_name
        return snapshot

    def _build_provider_adapters(self):
        adapters = {
            "ollama": OllamaProviderAdapter(host="http://127.0.0.1:11434"),
            "deepseek": OpenAICompatibleProviderAdapter(
                name="deepseek",
                aliases={"deepseek-official"},
                base_url="https://api.deepseek.com",
                env_var_name="DEEPSEEK_API_KEY",
            ),
            "openai": OpenAICompatibleProviderAdapter(
                name="openai",
                aliases={"openai-compatible", "compatible"},
                base_url="https://api.openai.com/v1",
                env_var_name="OPENAI_API_KEY",
            ),
            "vmlx": MLXProviderAdapter(base_url="http://127.0.0.1:8080"),
        }
        return adapters

    def _build_provider_aliases(self):
        aliases = {}
        for provider_name, adapter in self.provider_adapters.items():
            aliases[provider_name] = provider_name
            for alias in adapter.aliases:
                aliases[alias] = provider_name
        return aliases

    def _supported_provider_names(self):
        return ", ".join(sorted(self.provider_adapters.keys()))

    def _get_active_provider_adapter(self):
        adapter = self.provider_adapters.get(self.provider)
        if not adapter:
            raise RuntimeError(f"Unsupported provider: {self.provider}")
        return adapter

    def _clear_model_availability_cache(self):
        self.model_availability_cache = {}

    def _normalize_provider_name(self, provider_name: str):
        normalized = (provider_name or "").strip().lower()
        return self.provider_aliases.get(normalized)

    def _set_provider(self, provider_name: str):
        normalized = self._normalize_provider_name(provider_name)
        if not normalized:
            return f"Unsupported provider. Use: {self._supported_provider_names()}"

        current_model = (self.model_name or "").strip()
        if current_model:
            self.provider_models[self.provider] = current_model

        self.provider = normalized
        provider_model = (self.provider_models.get(normalized, "") or "").strip()
        if provider_model:
            self.model_name = provider_model
        self._clear_model_availability_cache()
        self._persist_agent_config()
        return f"LLM provider switched to: {self.provider}"

    def _set_api_key(self, key: str, provider_name: str = ""):
        target_provider = self.provider
        if provider_name.strip():
            normalized = self._normalize_provider_name(provider_name)
            if not normalized:
                return f"Unsupported provider for key. Use: {self._supported_provider_names()}"
            target_provider = normalized

        adapter = self.provider_adapters[target_provider]
        result = adapter.set_api_key(key)
        self._persist_agent_config()
        return result

    def _set_base_url(self, base_url: str):
        adapter = self._get_active_provider_adapter()
        result = adapter.set_base_url(base_url)
        self._clear_model_availability_cache()
        self._persist_agent_config()
        return result

    def _set_model_name(self, model_name: str):
        value = (model_name or "").strip()
        if not value:
            return "Model name cannot be empty."
        self.model_name = value
        self.provider_models[self.provider] = value
        self._clear_model_availability_cache()
        self._persist_agent_config()
        return f"Model set to: {self.model_name}"

    def _get_provider_status(self):
        adapter = self._get_active_provider_adapter()
        key_state = adapter.get_api_key_state()
        model_name = self._get_active_model_name()
        endpoint = adapter.get_endpoint()

        return (
            f"Provider: {self.provider}\n"
            f"Model: {model_name}\n"
            f"Endpoint: {endpoint}\n"
            f"API key: {key_state}"
        )

    def chat_completion(self, model: str, messages, format=None):
        adapter = self._get_active_provider_adapter()
        return adapter.chat_completion(model=model, messages=messages, format=format)

    def chat(self, model: str, messages, format=None):
        return self.chat_completion(model=model, messages=messages, format=format)

    def _is_model_available(self, model_name: str):
        candidate = (model_name or "").strip()
        if not candidate:
            return False

        adapter = self._get_active_provider_adapter()
        cache_key = (self.provider, adapter.get_endpoint(), candidate)
        cached = self.model_availability_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            is_available = adapter.is_model_available(candidate)
        except Exception as exc:
            print(f"[Model] Failed to validate model '{candidate}': {exc}")
            is_available = False

        self.model_availability_cache[cache_key] = is_available
        return is_available

    def _get_active_model_name(self):
        explicit_model = (self.model_name or "").strip()
        if explicit_model and self._is_model_available(explicit_model):
            return explicit_model

        profile_model = self._get_profile_model_name()
        if profile_model and self._is_model_available(profile_model):
            return profile_model

        return explicit_model or profile_model or "llama3"
