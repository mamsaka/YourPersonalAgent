
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Соединение с commit/rollback и обязательным закрытием."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT 'Новая сессия',
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL DEFAULT 1,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tool_call_id TEXT,
                    name TEXT,
                    tool_calls_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Миграции для существующих баз данных.
            migrations = (
                "ALTER TABLE messages ADD COLUMN session_id INTEGER DEFAULT 1",
                "ALTER TABLE messages ADD COLUMN tool_call_id TEXT",
                "ALTER TABLE messages ADD COLUMN name TEXT",
                "ALTER TABLE messages ADD COLUMN tool_calls_json TEXT",
            )

            for ddl in migrations:
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует.

            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id)
            """)

            row = conn.execute(
                "SELECT id FROM sessions ORDER BY id ASC LIMIT 1"
            ).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO sessions(name, created_at) VALUES (?, ?)",
                    ("Основная", self._now()),
                )

            default_session = conn.execute(
                "SELECT id FROM sessions ORDER BY id ASC LIMIT 1"
            ).fetchone()

            conn.execute(
                """
                UPDATE messages
                SET session_id = ?
                WHERE session_id IS NULL OR session_id = 0
                """,
                (int(default_session["id"]),),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---------------------------------------------------------
    # СЕССИИ
    # ---------------------------------------------------------

    def create_session(self, name: str = "Новая сессия") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions(name, created_at) VALUES (?, ?)",
                (name, self._now()),
            )
            return int(cur.lastrowid)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM sessions ORDER BY id DESC"
            ).fetchall()

        return [dict(row) for row in rows]

    def get_session_name(self, session_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

        return row["name"] if row else None

    def rename_session(self, session_id: int, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET name = ? WHERE id = ?",
                (name, session_id),
            )

    def delete_session(self, session_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )

    def get_default_session_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions ORDER BY id ASC LIMIT 1"
            ).fetchone()

        if row:
            return int(row["id"])

        return self.create_session("Основная")

    # ---------------------------------------------------------
    # СООБЩЕНИЯ
    # ---------------------------------------------------------

    def add_message(
        self,
        session_id: int,
        role: str,
        content: str | None,
        tool_call_id: str | None = None,
        name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        Сохраняет сообщение.

        tool_calls — полный список вызовов инструментов,
        полученный в assistant message от OpenRouter.
        """

        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        tool_calls_json = None
        if tool_calls is not None:
            tool_calls_json = json.dumps(
                tool_calls,
                ensure_ascii=False,
            )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    name,
                    tool_calls_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    name,
                    tool_calls_json,
                    self._now(),
                ),
            )

    def add_chat_message(
        self,
        session_id: int,
        message: dict[str, Any],
    ) -> None:
        """
        Сохраняет сообщение OpenRouter.

        Поддерживает:
        - user
        - assistant
        - assistant с tool_calls
        - tool с tool_call_id
        """

        self.add_message(
            session_id=session_id,
            role=message.get("role", "assistant"),
            content=message.get("content"),
            tool_call_id=message.get("tool_call_id"),
            name=message.get("name"),
            tool_calls=message.get("tool_calls"),
        )

    def get_session_messages(
        self,
        session_id: int,
        roles: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Возвращает историю сессии в правильном порядке.

        ВАЖНО:
        Если запрошены user и assistant, сообщения tool
        тоже включаются автоматически.

        Это необходимо для корректной истории вызовов инструментов.
        """

        sql = """
            SELECT
                role,
                content,
                tool_call_id,
                name,
                tool_calls_json
            FROM messages
            WHERE session_id = ?
        """

        params: list[Any] = [session_id]

        if roles:
            selected_roles = list(roles)

            # Нельзя выбрасывать результаты инструментов,
            # если мы загружаем историю ассистента.
            if "assistant" in selected_roles and "tool" not in selected_roles:
                selected_roles.append("tool")

            placeholders = ",".join("?" for _ in selected_roles)
            sql += f" AND role IN ({placeholders})"
            params.extend(selected_roles)

        if limit and limit > 0:
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
        else:
            sql += " ORDER BY id ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        if limit and limit > 0:
            rows = list(reversed(rows))

        result: list[dict[str, Any]] = []

        for row in rows:
            item: dict[str, Any] = {
                "role": row["role"],
                "content": row["content"],
            }

            if row["tool_call_id"] is not None:
                item["tool_call_id"] = row["tool_call_id"]

            if row["name"] is not None:
                item["name"] = row["name"]

            if row["tool_calls_json"]:
                try:
                    item["tool_calls"] = json.loads(
                        row["tool_calls_json"]
                    )
                except (ValueError, TypeError):
                    item["tool_calls"] = []

                # Стандартный формат assistant tool-call message.
                if not item["content"]:
                    item["content"] = None

            result.append(item)

        return result

    def clear_session(self, session_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )

    # ---------------------------------------------------------
    # ВОСПОМИНАНИЯ
    # ---------------------------------------------------------

    def remember(self, content: str, tags: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories(content, tags, created_at)
                VALUES (?, ?, ?)
                """,
                (content.strip(), tags.strip(), self._now()),
            )

            return int(cur.lastrowid)

    def delete_memory(self, memory_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            )

            return cur.rowcount > 0

    def get_all_memories(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, tags, created_at
                FROM memories
                ORDER BY id ASC
                """
            ).fetchall()

        return [dict(row) for row in rows]

    @staticmethod
    def _like_escape(word: str) -> str:
        return (
            word.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    def search(
        self,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, str]]:

        words = [
            w.strip(".,!?;:()[]{}\"'`).").lower()
            for w in query.split()
            if len(w) > 2
        ]

        words = [w for w in words if w]

        if not words:
            return []

        clauses = " OR ".join(
            "lower(content) LIKE ? ESCAPE '\\' "
            "OR lower(tags) LIKE ? ESCAPE '\\'"
            for _ in words
        )

        params: list[object] = []

        for word in words:
            pattern = f"%{self._like_escape(word)}%"
            params.extend((pattern, pattern))

        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, content, tags, created_at
                FROM memories
                WHERE {clauses}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [dict(row) for row in rows]
