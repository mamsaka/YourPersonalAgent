from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# PERSONAL_AGENT_HOME позволяет запускать приложение/тесты из другой папки данных.
ROOT = Path(os.getenv("PERSONAL_AGENT_HOME") or Path(__file__).resolve().parent.parent).resolve()
APP_DATA = ROOT / "data"
WORKSPACE = ROOT / "workspace"
SKILLS_DIR = ROOT / "skills"
GENERATED_TOOLS_DIR = ROOT / "tools" / "generated"
BACKUPS_DIR = APP_DATA / "backups"
ENV_FILE = ROOT / ".env"
UI_SETTINGS_FILE = APP_DATA / "ui_settings.json"

for _path in (APP_DATA, WORKSPACE, SKILLS_DIR, GENERATED_TOOLS_DIR, BACKUPS_DIR):
    _path.mkdir(parents=True, exist_ok=True)


def _read_env_file() -> dict[str, str]:
    """Прочитать .env файл, если он есть."""
    result: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _write_env_file(updates: dict[str, str]) -> None:
    """Обновить ключи в .env, не трогая комментарии и чужие строки."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)  # на Windows почти no-op, на Linux/macOS закрывает файл от других пользователей
    except OSError:
        pass


def _setting(env: dict[str, str], name: str, default: str) -> str:
    return os.getenv(name, env.get(name, default))


def _int_setting(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(_setting(env, name, str(default)))
    except ValueError:
        return default


def _bool_setting(env: dict[str, str], name: str, default: bool) -> bool:
    raw = _setting(env, name, "true" if default else "false")
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class AgentConfig:
    model: str = "nvidia/nemotron-3.5-super-vl:free"
    api_base: str = "https://openrouter.ai/api/v1"
    request_timeout: int = 90
    max_tool_rounds: int = 40  # 0 = без лимита (не рекомендуется: цикл может сжечь деньги/лимиты)
    max_history_messages: int = 24
    max_tokens: int = 8192
    verify_ssl: bool = True
    confirm_dangerous: bool = True
    api_key: str = ""

    @classmethod
    def from_env(cls) -> "AgentConfig":
        env = _read_env_file()
        # Переменные окружения имеют приоритет над значениями из .env
        return cls(
            model=_setting(env, "OPENROUTER_MODEL", cls.model),
            api_base=_setting(env, "OPENROUTER_BASE_URL", cls.api_base),
            request_timeout=_int_setting(env, "OPENROUTER_TIMEOUT", cls.request_timeout),
            max_tool_rounds=_int_setting(env, "OPENROUTER_MAX_TOOL_ROUNDS", cls.max_tool_rounds),
            max_history_messages=_int_setting(env, "OPENROUTER_MAX_HISTORY", cls.max_history_messages),
            max_tokens=_int_setting(env, "OPENROUTER_MAX_TOKENS", cls.max_tokens),
            verify_ssl=_bool_setting(env, "OPENROUTER_VERIFY_SSL", True),
            confirm_dangerous=_bool_setting(env, "AGENT_CONFIRM_DANGEROUS", True),
            api_key=_setting(env, "OPENROUTER_API_KEY", ""),
        )

    def save_api_key(self, api_key: str) -> None:
        """Сохранить API ключ в .env рядом с проектом."""
        self.api_key = api_key.strip()
        _write_env_file({"OPENROUTER_API_KEY": self.api_key})
