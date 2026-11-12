from __future__ import annotations

import re
from pathlib import Path


class SkillStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def slugify(name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-_")
        if not slug:
            raise ValueError("Название навыка должно содержать латинские буквы или цифры.")
        return slug[:48]

    def create(self, name: str, description: str, instructions: str) -> str:
        slug = self.slugify(name)
        folder = self.root / slug
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "SKILL.md"
        if target.exists():
            raise FileExistsError(f"Навык '{slug}' уже существует.")
        content = (
            f"# {name.strip()}\n\n{description.strip()}\n\n"
            "## Instructions\n\n" + instructions.strip() + "\n"
        )
        if len(content) > 30_000:
            raise ValueError("Инструкция навыка слишком длинная (лимит 30 000 символов).")
        target.write_text(content, encoding="utf-8")
        return slug

    def list_skills(self) -> list[dict[str, str]]:
        result = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            title = path.parent.name
            description = ""
            for line in text.splitlines():
                if line.strip() and not line.startswith("#"):
                    description = line.strip()
                    break
            result.append({"name": title, "description": description})
        return result

    def read(self, name: str) -> str:
        path = self.root / self.slugify(name) / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"Навык '{name}' не найден.")
        return path.read_text(encoding="utf-8")[:30_000]

    def prompt_summary(self, max_chars: int = 5000) -> str:
        chunks = []
        for item in self.list_skills():
            chunks.append(f"- {item['name']}: {item['description']}")
        return "\n".join(chunks)[:max_chars] or "Пока нет пользовательских навыков."
