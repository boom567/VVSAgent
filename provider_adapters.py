import base64
import importlib
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
from urllib import error, request


ollama = importlib.import_module("ollama") if importlib.util.find_spec("ollama") else None


class ProviderAdapter:
    def __init__(self, name: str, aliases=None):
        self.name = name
        self.aliases = set(aliases or []) | {name}

    def supports_api_key(self):
        return False

    def set_api_key(self, key: str):
        return "Current provider does not require an API key."

    def get_api_key_state(self):
        return "n/a"

    def set_base_url(self, base_url: str):
        raise NotImplementedError

    def get_endpoint(self):
        raise NotImplementedError

    def is_model_available(self, model: str):
        return bool((model or "").strip())

    def chat_completion(self, model: str, messages, format=None):
        raise NotImplementedError


class OllamaProviderAdapter(ProviderAdapter):
    def __init__(self, host: str):
        super().__init__("ollama", aliases={"local"})
        self.host = host
        self.client = ollama.Client(host=self.host, trust_env=False) if ollama else None

    def set_base_url(self, base_url: str):
        value = base_url.strip().rstrip("/")
        if not value.startswith("http://") and not value.startswith("https://"):
            return "Base URL must start with http:// or https://"
        if not ollama:
            return "Ollama package is not installed. Install 'ollama' or switch provider."

        self.host = value
        self.client = ollama.Client(host=self.host, trust_env=False)
        return f"Ollama host set to: {self.host}"

    def get_endpoint(self):
        return self.host

    def is_model_available(self, model: str):
        candidate = (model or "").strip()
        if not candidate or not self.client:
            return False

        try:
            self.client.show(candidate)
            return True
        except Exception:
            return False

    def chat_completion(self, model: str, messages, format=None):
        if not self.client:
            raise RuntimeError("Ollama provider requires the 'ollama' Python package. Install it or switch provider.")
        return self.client.chat(model=model, messages=messages, format=format)


class OpenAICompatibleProviderAdapter(ProviderAdapter):
    def __init__(self, name: str, aliases, base_url: str, env_var_name: str):
        super().__init__(name, aliases=aliases)
        self.base_url = base_url.rstrip("/")
        self.env_var_name = env_var_name
        self.api_key = (os.getenv(env_var_name) or "").strip()

    def supports_api_key(self):
        return True

    def set_api_key(self, key: str):
        normalized_key = key.strip()
        self.api_key = normalized_key
        if not normalized_key:
            return f"Cleared API key for provider: {self.name}"
        return f"API key saved for provider: {self.name}"

    def get_api_key_state(self):
        return "set" if self.api_key else "not set"

    def set_base_url(self, base_url: str):
        value = base_url.strip().rstrip("/")
        if not value.startswith("http://") and not value.startswith("https://"):
            return "Base URL must start with http:// or https://"
        self.base_url = value
        return f"{self.name} base URL set to: {self.base_url}"

    def get_endpoint(self):
        return self.base_url

    def _image_path_to_data_url(self, image_path: str):
        target = Path(image_path).expanduser()
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Image does not exist: {target}")

        content = target.read_bytes()
        encoded = base64.b64encode(content).decode("ascii")
        mime_type, _ = mimetypes.guess_type(str(target))
        if not mime_type:
            mime_type = "application/octet-stream"
        return f"data:{mime_type};base64,{encoded}"

    def _to_openai_messages(self, messages):
        converted = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            images = message.get("images") or []

            if not images:
                converted.append({"role": role, "content": content})
                continue

            parts = []
            if content:
                parts.append({"type": "text", "text": str(content)})

            for image in images:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self._image_path_to_data_url(str(image))},
                    }
                )

            converted.append({"role": role, "content": parts})

        return converted

    def _extract_openai_content(self, payload):
        choices = payload.get("choices", [])
        if not choices:
            return ""

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
            return "\n".join(part for part in chunks if part)
        return str(content)

    def chat_completion(self, model: str, messages, format=None):
        if not self.api_key:
            raise RuntimeError(
                f"No API key configured for provider '{self.name}'. "
                f"Use /apikey <key> or set env var {self.env_var_name}."
            )

        request_body = {
            "model": model,
            "messages": self._to_openai_messages(messages),
        }
        if format == "json":
            request_body["response_format"] = {"type": "json_object"}

        body = json.dumps(request_body).encode("utf-8")
        endpoint = f"{self.base_url}/chat/completions"
        req = request.Request(
            endpoint,
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with request.urlopen(req, timeout=300) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} from {self.name}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Failed to connect to {self.name}: {exc}") from exc

        content = self._extract_openai_content(payload)
        return {"message": {"content": content}}


class MLXProviderAdapter(OpenAICompatibleProviderAdapter):
    """Adapter for mlx_lm.server (Apple MLX local inference, no API key required)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        super().__init__(
            name="vmlx",
            aliases={"mlx", "mlx_lm"},
            base_url=base_url,
            env_var_name="MLX_API_KEY",
        )

    def supports_api_key(self):
        return False

    def get_api_key_state(self):
        return "n/a"

    def chat_completion(self, model: str, messages, format=None):
        request_body = {
            "model": model,
            "messages": self._to_openai_messages(messages),
        }
        if format == "json":
            request_body["response_format"] = {"type": "json_object"}

        body = json.dumps(request_body).encode("utf-8")
        endpoint = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = request.Request(endpoint, method="POST", data=body, headers=headers)
        try:
            with request.urlopen(req, timeout=300) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} from vmlx: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Failed to connect to vmlx server: {exc}") from exc

        content = self._extract_openai_content(payload)
        return {"message": {"content": content}}
