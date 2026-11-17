from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator

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
        # Расход последнего запроса (prompt_tokens, completion_tokens, total_tokens).
        self.last_usage: dict[str, Any] = {}
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

    def _post(self, payload: dict[str, Any], stream: bool = False) -> requests.Response:
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
                    stream=stream,
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
                delay = self._retry_delay(response, attempt)
                response.close()
                time.sleep(delay)
                continue
            break
        assert response is not None
        return response

    def _raise_for_error(self, status_code: int, detail: str, has_tools: bool) -> None:
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
            has_tools
            and status_code in (400, 404)
            and any(phrase in lower_detail for phrase in unsupported_phrases)
        ):
            raise OpenRouterToolsUnsupported(
                f"Модель «{self.config.model}» не поддерживает вызов инструментов: {detail}"
            )

        raise OpenRouterError(
            f"OpenRouter вернул HTTP {status_code}: {detail}"
        )

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
            self._raise_for_error(response.status_code, detail, bool(tools))
        try:
            data = response.json()
            self.last_usage = data.get("usage") or {}
            return data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"Неожиданный ответ OpenRouter: {response.text[:800]}") from exc

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Потоковый вызов chat-completions (OpenAI-совместимый SSE).

        Генерирует словари:
          {"kind": "content",   "text": "..."}  — очередной кусок текста ответа;
          {"kind": "reasoning", "text": "..."}  — очередной кусок рассуждений;
          {"kind": "done",      "message": {...}} — собранное сообщение ассистента.
        """
        self._validate_messages(messages)
        self.last_usage = {}
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._post(payload, stream=True)
        if not response.ok:
            detail = self._error_detail(response)
            response.close()
            self._raise_for_error(response.status_code, detail, bool(tools))

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}

        try:
            for raw_line in response.iter_lines():
                if not raw_line or not raw_line.startswith(b"data:"):
                    continue
                data = raw_line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    chunk = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue

                usage = chunk.get("usage")
                if isinstance(usage, dict) and usage:
                    self.last_usage = usage

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                text = self._delta_text(delta.get("content"))
                if text:
                    content_parts.append(text)
                    yield {"kind": "content", "text": text}

                reasoning = self._reasoning_text(
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("reasoning_details")
                )
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield {"kind": "reasoning", "text": reasoning}

                for call in delta.get("tool_calls") or []:
                    self._merge_tool_call(tool_calls, call)
        except requests.RequestException as exc:
            raise OpenRouterError(f"Поток OpenRouter прерван: {exc}") from exc
        finally:
            response.close()

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]

        yield {"kind": "done", "message": message}

    @staticmethod
    def _delta_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "".join(parts)
        return str(value)

    @staticmethod
    def _reasoning_text(value: Any) -> str:
        """Текст рассуждений из строки, списка reasoning_details или объекта."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("text", "summary", "content"):
                text = value.get(key)
                if isinstance(text, str) and text:
                    return text
            return ""
        if isinstance(value, list):
            return "".join(OpenRouterClient._reasoning_text(item) for item in value)
        return str(value)

    @staticmethod
    def _merge_tool_call(store: dict[int, dict[str, Any]], call: Any) -> None:
        if not isinstance(call, dict):
            return
        index = call.get("index")
        if not isinstance(index, int):
            index = len(store)
        entry = store.get(index)
        if entry is None:
            entry = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            store[index] = entry

        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            if not entry["id"]:
                entry["id"] = call_id
            elif not entry["id"].endswith(call_id):
                entry["id"] += call_id

        call_type = call.get("type")
        if isinstance(call_type, str) and call_type:
            entry["type"] = call_type

        function = call.get("function") or {}
        if not isinstance(function, dict):
            return
        name = function.get("name")
        if isinstance(name, str) and name:
            if not entry["function"]["name"]:
                entry["function"]["name"] = name
            elif not entry["function"]["name"].endswith(name):
                entry["function"]["name"] += name
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            entry["function"]["arguments"] += arguments

    def list_models(self, query: str = "") -> list[dict[str, Any]]:
        """Загрузить каталог OpenRouter. При необходимости — поиск по названию/ID."""
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

        needle = (query or "").strip().lower()
        models: list[dict[str, Any]] = []
        for item in entries:
            model_id = str(item.get("id", ""))
            if not model_id:
                continue
            name = str(item.get("name") or model_id)
            if needle and needle not in model_id.lower() and needle not in name.lower():
                continue
            pricing = item.get("pricing") or {}
            is_free = str(model_id).endswith(":free") or (
                pricing.get("prompt") == "0" and pricing.get("completion") == "0"
            )
            models.append({
                "id": model_id,
                "name": name,
                "free": is_free,
                "context_length": item.get("context_length"),
            })
        return sorted(models, key=lambda item: item["name"].lower())
