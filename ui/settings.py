from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class UiSettings:
    """Небольшие настройки интерфейса (тема, подтверждения) в data/ui_settings.json."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass