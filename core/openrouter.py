from __future__ import annotations

import os
import time
from typing import Any

import requests

from .config import AgentConfig

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class OpenRouterError(RuntimeError):
    pass


class OpenRouterToolsUnsupported(OpenRouterError):
    """Выбранная модель/провайдер не умеет вызывать инструменты (tool calling)."""


class OpenRouterClient:
    """Small OpenRouter client using its OpenAI-compatible chat-completions API."""

    def __init__(self, config: AgentConfig, api_key: str | None = None):
        self.config = config
        self.api_key = api_key or config.api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.session = requests.Session()
        # Прокси подхватываются из переменных окружения HTTP_PROXY / HTTPS_PROXY автоматически.

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise OpenRouterError("Сначала укажи OpenRouter API key.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "Personal Tkinter Agent",
        }

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            data = response.json()
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:1200]
        except ValueError:
            pass
        return response.text[:1200]

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(float(retry_after), 20.0)
        except ValueError:
            return min(2.0 * (2 ** attempt), 15.0)

    @staticmethod
    def _validate_messages(messages: list[dict[str, Any]]) -> None:
        for index, message in enumerate(messages):
            role = message.get("role")

            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                    raise OpenRouterError(
                        f"Некорректная история: messages[{index}] "
                        "имеет role='tool', но отсутствует tool_call_id. "
                        "Проверь сохранение и восстановление истории инструментов."
                    )

            if role == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not call_id.strip():
                        raise OpenRouterError(
                            f"Некорректная история: messages[{index}] "
                            "содержит tool_call без id."
                        )

    def _post(self, payload: dict[str, Any]) -> requests.Response:
        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        response: requests.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.session.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.config.request_timeout,
                    verify=self.config.verify_ssl,
                )
            except requests.exceptions.SSLError as exc:
                raise OpenRouterError(
                    "SSL-ошибка при подключении к OpenRouter. "
                    "Если за корпоративным прокси/антивирусом — попробуй установить "
                    "OPENROUTER_VERIFY_SSL=false (менее безопасно) или настрой HTTPS_PROXY."
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                reason = str(exc)
                if "getaddrinfo failed" in reason or "Failed to resolve" in reason:
                    raise OpenRouterError(
                        "Не удалось найти сервер openrouter.ai (DNS ошибка). "
                        "Проверь интернет-соединение и настройки прокси (HTTP_PROXY / HTTPS_PROXY)."
                    ) from exc
                raise OpenRouterError(f"Не удалось связаться с OpenRouter: {exc}") from exc
            except requests.exceptions.Timeout as exc:
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2.0)
                    continue
                raise OpenRouterError(f"OpenRouter не ответил за {self.config.request_timeout} с.") from exc
            except requests.RequestException as exc:
                raise OpenRouterError(f"Не удалось связаться с OpenRouter: {exc}") from exc

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(self._retry_delay(response, attempt))
                continue
            break
        assert response is not None
        return response

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._validate_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._post(payload)
        if not response.ok:
            detail = self._error_detail(response)
            lower_detail = detail.lower()

            # Только реальные сообщения о неподдерживаемом tool calling.
            unsupported_phrases = (
                "tool calling is not supported",
                "does not support tool calling",
                "tool calls are not supported",
                "does not support tool calls",
                "tools are not supported",
                "tool use is not supported",
                "function calling is not supported",
                "does not support function calling",
            )

            if (
                tools
                and response.status_code in (400, 404)
                and any(phrase in lower_detail for phrase in unsupported_phrases)
            ):
                raise OpenRouterToolsUnsupported(
                    f"Модель «{self.config.model}» не поддерживает вызов инструментов: {detail}"
                )

            raise OpenRouterError(
                f"OpenRouter вернул HTTP {response.status_code}: {detail}"
            )
        try:
            data = response.json()
            return data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"Неожиданный ответ OpenRouter: {response.text[:800]}") from exc

    def list_free_models(self) -> list[dict[str, Any]]:
        """Fetch models and keep explicit :free variants or zero-priced entries."""
        try:
            response = self.session.get(
                f"{self.config.api_base.rstrip('/')}/models",
                timeout=30,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
            response.raise_for_status()
            entries = response.json().get("data", [])
        except (requests.RequestException, ValueError) as exc:
            raise OpenRouterError(f"Не удалось загрузить каталог моделей: {exc}") from exc

        free: list[dict[str, Any]] = []
        for item in entries:
            pricing = item.get("pricing") or {}
            is_suffix_free = str(item.get("id", "")).endswith(":free")
            is_zero_price = pricing.get("prompt") == "0" and pricing.get("completion") == "0"
            if is_suffix_free or is_zero_price:
                free.append({"id": item.get("id", ""), "name": item.get("name", item.get("id", ""))})
        return sorted(free, key=lambda x: (not x["id"].endswith(":free"), x["name"].lower()))
