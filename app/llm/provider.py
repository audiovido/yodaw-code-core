import json
import os
import httpx


class LLMError(RuntimeError):
    pass


class LocalLLMProvider:
    """
    Generic local LLM adapter.

    Supports:
      - Ollama native /api/chat
      - OpenAI-compatible /v1/chat/completions

    Configuration:
      YODAW_LLM_STYLE=ollama|openai
      YODAW_LLM_BASE_URL=http://127.0.0.1:11434
      YODAW_LLM_MODEL=<model-name>
      YODAW_LLM_API_KEY=<optional>
    """

    def __init__(self):
        self.style = os.environ.get(
            "YODAW_LLM_STYLE",
            "ollama",
        ).lower()

        self.base_url = os.environ.get(
            "YODAW_LLM_BASE_URL",
            "http://127.0.0.1:11434",
        ).rstrip("/")

        self.model = os.environ.get(
            "YODAW_LLM_MODEL",
            "",
        )

        self.api_key = os.environ.get(
            "YODAW_LLM_API_KEY",
            "",
        )

        if not self.model:
            raise LLMError(
                "YODAW_LLM_MODEL is not configured"
            )

    def health(self):
        return {
            "style": self.style,
            "base_url": self.base_url,
            "model": self.model,
        }

    def chat(self, system: str, user: str) -> str:
        if self.style == "ollama":
            return self._ollama(system, user)

        if self.style == "openai":
            return self._openai(system, user)

        raise LLMError(
            f"Unsupported YODAW_LLM_STYLE: {self.style}"
        )

    def _ollama(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": user,
                },
            ],
        }

        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=1200,
            )
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]

        except Exception as exc:
            raise LLMError(
                f"Ollama request failed: {exc}"
            ) from exc

    def _openai(self, system: str, user: str) -> str:
        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key:
            headers["Authorization"] = (
                f"Bearer {self.api_key}"
            )

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": user,
                },
            ],
        }

        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=1200,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

        except Exception as exc:
            raise LLMError(
                f"OpenAI-compatible request failed: {exc}"
            ) from exc
