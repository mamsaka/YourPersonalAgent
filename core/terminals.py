"""Настоящие постоянные терминалы для инструмента use_cmd.

Каждый терминал — это живой процесс shell (bash / cmd.exe) с потоком чтения вывода.
Состояние (cd, переменные окружения, активированные venv) сохраняется между командами.
"""
from __future__ import annotations

import atexit
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import WORKSPACE

IS_WINDOWS = os.name == "nt"
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0


class TerminalError(RuntimeError):
    pass


def _resolve_cwd(cwd: str | None) -> Path:
    if not cwd:
        return WORKSPACE
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        path = WORKSPACE / path
    if not path.is_dir():
        raise TerminalError(f"Папка не найдена: {path}")
    return path


def _clamp_timeout(value: Any) -> float:
    try:
        timeout = float(value) if value is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return max(0.5, min(timeout, MAX_TIMEOUT))


class Terminal:
    def __init__(self, terminal_id: int, cwd: Path):
        self.id = terminal_id
        self.cwd = str(cwd)
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._pending_marker: str | None = None
        self._lock = threading.Lock()

        if IS_WINDOWS:
            argv = ["cmd.exe", "/Q"]
            extra: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        else:
            bash = shutil.which("bash")
            argv = [bash, "--noprofile", "--norc"] if bash else [shutil.which("sh") or "/bin/sh"]
            extra = {"start_new_session": True}  # своя группа процессов: можно убить всё дерево
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # stdout и stderr в одном потоке — порядок строк сохраняется
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.cwd,
            **extra,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        if IS_WINDOWS:
            self._write("chcp 65001>nul\n")  # UTF-8 вместо OEM-кодировки (иначе кириллица в выводе ломается)

    # --- внутреннее ---

    def _read_loop(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self._lines.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self._lines.put(None)

    def _write(self, text: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(text)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise TerminalError("Терминал завершил работу. Создай новый (action=create).") from exc

    def _collect(self, timeout: float) -> dict[str, Any]:
        marker = self._pending_marker
        deadline = time.monotonic() + timeout
        parts: list[str] = []
        size = 0
        truncated = False
        returncode: int | None = None
        done = marker is None  # без активной команды просто сливаем то, что успело накопиться
        exited = False

        def add(chunk: str) -> None:
            nonlocal size, truncated
            if size >= MAX_OUTPUT_CHARS:
                truncated = True
                return
            take = chunk[: MAX_OUTPUT_CHARS - size]
            parts.append(take)
            size += len(take)
            if len(take) < len(chunk):
                truncated = True

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if marker is None:
                    break
                continue
            if line is None:  # поток вывода закрылся: процесс завершился
                exited = True
                done = True
                self._pending_marker = None
                break
            if marker and marker in line:
                before, _, after = line.partition(marker)
                if before:
                    add(before)
                tail = after.strip().split()
                try:
                    returncode = int(tail[0])
                except (IndexError, ValueError):
                    returncode = None
                done = True
                self._pending_marker = None
                break
            add(line)

        if exited and returncode is None:
            try:
                returncode = self.proc.wait(timeout=2)  # поток закрылся чуть раньше, чем ОС отдала код возврата
            except subprocess.TimeoutExpired:
                returncode = self.proc.poll()
        result: dict[str, Any] = {
            "terminal_id": self.id,
            "output": "".join(parts),
            "returncode": returncode,
            "timed_out": not done,
        }
        if truncated:
            result["truncated"] = True
        if exited:
            result["exited"] = True
        if not done:
            result["hint"] = (
                "Команда ещё выполняется. Вызови use_cmd action=read, чтобы дождаться и забрать остаток вывода, "
                "или action=close, чтобы прервать терминал."
            )
        return result

    # --- публичное ---

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def busy(self) -> bool:
        return self._pending_marker is not None

    def execute(self, command: str, timeout: float, cwd: str | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.alive:
                raise TerminalError("Терминал завершил работу. Создай новый (action=create).")
            if self._pending_marker:
                raise TerminalError(
                    "Предыдущая команда ещё выполняется. Вызови action=read, чтобы дождаться её вывода, "
                    "или action=close."
                )
            marker = f"__AGENT_DONE_{uuid.uuid4().hex[:12]}__"
            lines: list[str] = []
            if cwd:
                target = _resolve_cwd(cwd)
                lines.append(f'cd /d "{target}"' if IS_WINDOWS else f"cd {shlex.quote(str(target))}")
            lines.append(command)
            lines.append(f"echo {marker} %errorlevel%" if IS_WINDOWS else f'echo "{marker} $?"')
            self._pending_marker = marker
            self._write("\n".join(lines) + "\n")
            return self._collect(timeout)

    def read(self, timeout: float) -> dict[str, Any]:
        with self._lock:
            return self._collect(timeout if self._pending_marker else 0.3)

    def close(self) -> None:
        if self.alive:
            try:
                if IS_WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, timeout=10)
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass


class TerminalManager:
    def __init__(self) -> None:
        self._terminals: dict[int, Terminal] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def create(self, cwd: str | None = None) -> Terminal:
        path = _resolve_cwd(cwd)
        with self._lock:
            term = Terminal(self._next_id, path)
            self._terminals[term.id] = term
            self._next_id += 1
            return term

    def get(self, terminal_id: Any) -> Terminal:
        try:
            tid = int(terminal_id)
        except (TypeError, ValueError):
            raise TerminalError("terminal_id должен быть числом.") from None
        with self._lock:
            term = self._terminals.get(tid)
        if term is None:
            raise TerminalError(f"Терминал {tid} не найден. Список: action=list.")
        return term

    def default(self) -> Terminal:
        with self._lock:
            for term in self._terminals.values():
                if term.alive:
                    return term
        return self.create()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"terminal_id": t.id, "pid": t.proc.pid, "alive": t.alive, "busy": t.busy, "cwd": t.cwd}
                for t in self._terminals.values()
            ]

    def close(self, terminal_id: Any) -> None:
        term = self.get(terminal_id)
        term.close()
        with self._lock:
            self._terminals.pop(term.id, None)

    def close_all(self) -> None:
        with self._lock:
            terms = list(self._terminals.values())
            self._terminals.clear()
        for term in terms:
            term.close()


MANAGER = TerminalManager()
atexit.register(MANAGER.close_all)


def terminal_tool(
    action: str,
    terminal_id: int | None = None,
    command: str | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Точка входа инструмента use_cmd."""
    action = (action or "").strip().lower()
    if action == "create":
        term = MANAGER.create(cwd)
        return {"terminal_id": term.id, "pid": term.proc.pid, "cwd": term.cwd, "status": "created"}
    if action == "list":
        return {"terminals": MANAGER.list()}
    if action == "execute":
        if not command or not command.strip():
            raise ValueError("Для action=execute нужен параметр command.")
        term = MANAGER.get(terminal_id) if terminal_id is not None else MANAGER.default()
        return term.execute(command, _clamp_timeout(timeout), cwd)
    if action == "read":
        term = MANAGER.get(terminal_id) if terminal_id is not None else MANAGER.default()
        return term.read(_clamp_timeout(timeout))
    if action == "close":
        if terminal_id is None:
            raise ValueError("Для action=close нужен terminal_id.")
        MANAGER.close(terminal_id)
        return {"terminal_id": int(terminal_id), "status": "closed"}
    raise ValueError(f"Неизвестное действие: {action!r}. Доступно: create, list, execute, read, close.")


def shutdown() -> None:
    MANAGER.close_all()
