from __future__ import annotations

import os
import re
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

# Максимальная длина характера агента в символах.
PERSONA_MAX_CHARS = 10_000

# WxH  или  WxH+X+Y (позиция может быть отрицательной — второй монитор).
_GEOMETRY_RE = re.compile(r"^\d+x\d+([+-]\d+[+-]\d+)?$")

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


def tidy_persona(text: str) -> str:
    """Привести характер к аккуратному виду: без хвостовых пробелов и лишних пустых строк, абзацы сохраняются."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    blanks = 0
    for line in lines:
        line = line.rstrip()
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:  # между абзацами оставляем ровно одну пустую строку
                out.append("")
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def escape_env_value(value: str) -> str:
    """Уложить многострочное значение в одну строку .env: переносы → \\n, обратный слэш → \\\\."""
    return (str(value)
            .replace("\\", "\\\\")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
            .replace("\r", "\\n"))


def unescape_env_value(value: str) -> str:
    """Вернуть переносы строк из значения .env. Обрабатывает \\\\, \\n и \\r одним проходом."""
    text = str(value)
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _int_setting(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(_setting(env, name, str(default)))
    except ValueError:
        return default


def _bool_setting(env: dict[str, str], name: str, default: bool) -> bool:
    raw = _setting(env, name, "true" if default else "false")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int_or_none(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def env_has(name: str) -> bool:
    """Задан ли ключ в окружении или в .env (для разовой миграции старых настроек)."""
    if os.getenv(name) is not None:
        return True
    return name in _read_env_file()


def is_valid_geometry(value: str) -> bool:
    """Проверить строку геометрии окна вида WxH или WxH+X+Y."""
    return bool(_GEOMETRY_RE.match((value or "").strip()))


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
    stream: bool = True
    theme: str = "dark"
    api_key: str = ""
    # Характер агента (добавляется в системный промпт). Пусто — нейтральное поведение.
    persona: str = ""
    # Последняя активная сессия и геометрия окна (сохраняются автоматически в .env).
    last_session: int | None = None
    window_geometry: str = ""

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
            stream=_bool_setting(env, "OPENROUTER_STREAM", True),
            theme=_setting(env, "AGENT_THEME", cls.theme),
            api_key=_setting(env, "OPENROUTER_API_KEY", ""),
            persona=unescape_env_value(_setting(env, "AGENT_PERSONA", "")),
            last_session=_int_or_none(_setting(env, "AGENT_LAST_SESSION", "")),
            window_geometry=_setting(env, "AGENT_WINDOW_GEOMETRY", ""),
        )

    def save_api_key(self, api_key: str) -> None:
        """Сохранить API ключ в .env рядом с проектом."""
        self.api_key = api_key.strip()
        _write_env_file({"OPENROUTER_API_KEY": self.api_key})

    def save_model(self, model: str) -> None:
        """Запомнить последнюю выбранную модель в .env."""
        model = model.strip()
        if not model:
            return
        self.model = model
        _write_env_file({"OPENROUTER_MODEL": model})

    def save_last_session(self, session_id: int) -> None:
        """Запомнить последний активный чат в .env."""
        self.last_session = int(session_id)
        _write_env_file({"AGENT_LAST_SESSION": str(int(session_id))})

    def save_theme(self, name: str) -> None:
        """Запомнить выбранную тему в .env."""
        name = name.strip().lower()
        if name not in ("dark", "light"):
            return
        self.theme = name
        _write_env_file({"AGENT_THEME": name})

    def save_confirm(self, value: bool) -> None:
        """Запомнить переключатель подтверждений в .env."""
        self.confirm_dangerous = bool(value)
        _write_env_file({"AGENT_CONFIRM_DANGEROUS": "true" if self.confirm_dangerous else "false"})

    def save_window_geometry(self, geometry: str) -> None:
        """Запомнить размер и позицию окна в .env."""
        geometry = geometry.strip()
        if not is_valid_geometry(geometry):
            return
        self.window_geometry = geometry
        _write_env_file({"AGENT_WINDOW_GEOMETRY": geometry})

    def save_persona(self, persona: str) -> None:
        """Запомнить характер агента в .env. Пустая строка сбрасывает характер.

        Текст хранится в одной строке файла: переносы строк экранируются как \\n
        и восстанавливаются при чтении, поэтому абзацы не теряются.
        """
        text = tidy_persona(persona)[:PERSONA_MAX_CHARS]
        self.persona = text
        _write_env_file({"AGENT_PERSONA": escape_env_value(text)})
