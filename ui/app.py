from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from typing import Any

from core import browser as browser_module
from core import terminals as terminals_module
from core.agent import AgentCancelled, AgentError, PersonalAgent
from core.config import APP_DATA, ENV_FILE, UI_SETTINGS_FILE, AgentConfig
from core.memory import MemoryStore
from core.openrouter import OpenRouterClient, OpenRouterError
from core.tools import ConfirmRequest, ToolRegistry

from . import hotkeys
from .chat import MAX_COLUMN, ChatView
from .dialogs import ConfirmDialog, InventoryDialog, MemoryDialog, PromptDialog, SettingsDialog
from .settings import UiSettings
from .theme import Theme
from .widgets import FlatButton, Tooltip, column_padding, fit_text_height, make_entry

DEFAULT_SESSION_NAME = "Новый чат"
MAX_RENDERED_MESSAGES = 200
APP_TITLE = "Personal AI Agent"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

WELCOME_TITLE = "Чем займёмся?"
WELCOME_SUBTITLE = (
    "Я работаю с файлами, терминалом и браузером, запоминаю важное и создаю себе новые инструменты и навыки.\n"
    "Опасные действия выполняю только после вашего подтверждения."
)
SUGGESTIONS = [
    # Файлы и workspace
    "Покажи, какие файлы лежат в workspace, и кратко опиши их",
    "Найди в workspace все Python-файлы и объясни, за что отвечает каждый",
    "Создай в workspace новый проект с правильной структурой папок",
    "Найди дубликаты файлов в workspace",

    # Браузер и интернет
    "Открой браузер, найди свежие новости по Python и перескажи главное",
    "Найди документацию по Selenium и покажи примеры использования",
    "Открой GitHub и найди интересные проекты на Python",
    "Найди несколько полезных библиотек Python для автоматизации",

    # Программирование
    "Создай в workspace простую HTML-страницу с игрой «Змейка»",
    "Напиши Python-скрипт, который сортирует файлы по расширениям",
    "Проверь мой проект на ошибки и предложи улучшения",
    "Создай простой REST API на FastAPI",
    "Найди и исправь ошибки в выбранном Python-файле",

    # Инструменты и навыки
    "Покажи список доступных инструментов и объясни, что они умеют",
    "Проверь библиотеку навыков и расскажи, какие навыки доступны",
    "Создай навык для автоматического анализа Python-проектов",
    "Создай Python-инструмент для получения информации о системе",

    # Долговременная память
    "Запомни, что я предпочитаю короткие ответы",
    "Покажи, что ты помнишь обо мне",
    "Запомни структуру моего текущего проекта",
    "Найди в памяти мои предпочтения по разработке",

    # Анализ и автоматизация
    "Проанализируй этот CSV-файл и найди интересные закономерности",
    "Создай скрипт для автоматического резервного копирования workspace",
    "Составь план разработки небольшого Python-приложения",
    "Помоги автоматизировать рутинную задачу на моём компьютере",
]


def _enable_hidpi() -> None:
    """На Windows без этого интерфейс размывается при масштабе экрана больше 100%."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


_enable_hidpi()


class AgentApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        try:
            scale = min(3.0, max(1.0, float(self.winfo_fpixels("1i")) / 96.0))
        except (tk.TclError, TypeError, ValueError):
            scale = 1.0
        self.geometry(f"{int(1240 * scale)}x{int(800 * scale)}")
        self.minsize(int(960 * scale), int(600 * scale))

        self.config_obj = AgentConfig.from_env()
        self.settings = UiSettings(UI_SETTINGS_FILE)
        self.memory = MemoryStore(APP_DATA / "agent.sqlite3")
        self.client = OpenRouterClient(self.config_obj)
        confirm = bool(self.settings.get("confirm_dangerous", self.config_obj.confirm_dangerous))
        self.tools = ToolRegistry(self.memory, confirm_enabled=confirm)
        self.tools.confirm_hook = self._confirm_hook
        self.agent = PersonalAgent(self.config_obj, self.client, self.memory, self.tools)

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.busy = False
        self.theme: Theme | None = None
        self.chat: ChatView | None = None
        self.input: tk.Text | None = None
        self.status_label: tk.Label | None = None
        self._confirm_dialog: ConfirmDialog | None = None
        self._session_ids: list[int] = []
        self._model_values: list[str] = []
        self._hover_index = -1
        self._spin_job: str | None = None
        self._spin_index = 0
        self._busy_since = 0.0
        self._status_text = "Готов"
        self._status_kind = "muted"
        self._ws = str(self.tk.call("tk", "windowingsystem"))
        self.current_session_id = self.memory.get_default_session_id()
        self.agent.set_session(self.current_session_id)
        self.status_var = tk.StringVar(value="Готов")
        self.model_var = tk.StringVar(value=self.config_obj.model)
        self.search_var = tk.StringVar()

        hotkeys.install(self, lambda: self.theme.c if self.theme else {})
        hotkeys.install_shortcuts(self, {
            "new_chat": self._new_session, "search": self._focus_search, "settings": self._open_settings,
        })
        self.bind("<Escape>", self._on_escape)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._rebuild()
        self._poll_events()

    # ------------------------------------------------------------------ построение интерфейса

    def _rebuild(self) -> None:
        draft = ""
        if self.input is not None:
            try:
                draft = self.input.get("1.0", "end-1c")
            except tk.TclError:
                draft = ""
        if self._spin_job is not None:
            try:
                self.after_cancel(self._spin_job)
            except tk.TclError:
                pass
            self._spin_job = None
        for child in self.winfo_children():
            child.destroy()
        self._confirm_dialog = None if self._confirm_dialog is None or not self._confirm_dialog.winfo_exists() else self._confirm_dialog
        self.theme = Theme(self, str(self.settings.get("theme", "dark")))
        self.theme.apply(self)
        self._hover_index = -1
        self._build_ui()
        if draft:
            self.input.insert("1.0", draft)
        self._refresh_sessions(select=self.current_session_id)
        self._render_history()
        self._update_key_state()
        self._update_confirm_toggle()
        self._on_input_change()
        self.input.focus_set()

    def _build_ui(self) -> None:
        th = self.theme
        assert th is not None
        c, px = th.c, th.px

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(2, weight=1)

        # ------------------------------ боковая панель ------------------------------
        sidebar = tk.Frame(self, bg=c["sidebar"], width=px(272))
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(4, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)
        tk.Frame(self, bg=c["border"], width=1).grid(row=0, column=1, sticky="ns")

        brand = tk.Frame(sidebar, bg=c["sidebar"])
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 16))
        tk.Label(brand, text="◆", bg=c["sidebar"], fg=c["accent"], font=th.font_title).pack(side="left", padx=(0, 8))
        tk.Label(brand, text="Personal AI", bg=c["sidebar"], fg=c["text"], font=th.font_title).pack(side="left")

        FlatButton(sidebar, th, "＋   Новый чат", self._new_session, kind="accent", pady=9, padx=14,
                   font=th.font_ui_bold, tooltip="Ctrl+N").grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))

        search_frame, self.search_entry = make_entry(sidebar, th, textvariable=self.search_var, bg=c["sidebar"])
        search_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        self.search_entry.configure(font=th.font_ui)
        self.search_var.trace_add("write", lambda *_: self._refresh_sessions(select=self.current_session_id))
        Tooltip(self.search_entry, "Поиск по чатам (Ctrl+F)", th)

        tk.Label(sidebar, text="ЧАТЫ", bg=c["sidebar"], fg=c["muted"], font=th.font_small_bold,
                 anchor="w").grid(row=3, column=0, sticky="ew", padx=20, pady=(2, 4))

        list_frame = tk.Frame(sidebar, bg=c["sidebar"])
        list_frame.grid(row=4, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        self.session_list = tk.Listbox(
            list_frame, bg=c["sidebar"], fg=c["text"], font=th.font,
            selectbackground=c["row_selected"], selectforeground=c["text"],
            activestyle="none", highlightthickness=0, borderwidth=0, relief="flat",
            exportselection=False, selectborderwidth=0,
        )
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.session_list.yview)
        self.session_list.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side="right", fill="y")
        self.session_list.pack(side="left", fill="both", expand=True)
        self.session_list.bind("<<ListboxSelect>>", self._on_session_select)
        self.session_list.bind("<Double-Button-1>", lambda e: self._rename_session_at(self.session_list.nearest(e.y)))
        self.session_list.bind("<Button-3>", self._session_menu)
        if self._ws == "aqua":
            self.session_list.bind("<Button-2>", self._session_menu)
        self.session_list.bind("<Delete>", lambda e: self._delete_session_at(self._selected_index()))
        self.session_list.bind("<F2>", lambda e: self._rename_session_at(self._selected_index()))
        self.session_list.bind("<Motion>", self._on_list_motion)
        self.session_list.bind("<Leave>", lambda e: self._clear_hover())

        footer = tk.Frame(sidebar, bg=c["sidebar"])
        footer.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 12))
        tk.Frame(footer, bg=c["border"], height=1).pack(fill="x", padx=4, pady=(0, 8))
        FlatButton(footer, th, "✦   Память агента", self._open_memory, kind="sidebar", anchor="w", padx=12).pack(fill="x", pady=1)
        FlatButton(footer, th, "⚒   Инструменты и навыки", self._open_inventory, kind="sidebar", anchor="w", padx=12).pack(fill="x", pady=1)
        self.settings_btn = FlatButton(footer, th, "⚙   Настройки", self._open_settings, kind="sidebar", anchor="w", padx=12,
                                       tooltip="Ctrl+,")
        self.settings_btn.pack(fill="x", pady=1)

        # ------------------------------ основная область ------------------------------
        main = tk.Frame(self, bg=c["bg"])
        main.grid(row=0, column=2, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        top = tk.Frame(main, bg=c["bg"], height=px(58))
        top.grid(row=0, column=0, sticky="ew")
        top.pack_propagate(False)
        toolbar = tk.Frame(top, bg=c["bg"])
        toolbar.pack(side="right", fill="y", padx=(4, 16))
        FlatButton(toolbar, th, "↻", self._load_models, kind="bar", padx=10, pady=5,
                   tooltip="Обновить список бесплатных моделей").pack(side="right", pady=14)
        self.model_box = ttk.Combobox(toolbar, textvariable=self.model_var, width=30, values=self._model_values)
        self.model_box.pack(side="right", padx=6, pady=15)
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)
        self.model_box.bind("<Return>", self._model_changed)
        self.model_box.bind("<FocusOut>", self._model_changed)
        tk.Label(toolbar, text="Модель", bg=c["bg"], fg=c["muted"], font=th.font_small).pack(side="right", padx=(0, 2))
        self.safety_chip = tk.Label(toolbar, text="⚠ без подтверждений", bg=c["bg"], fg=c["warn"], font=th.font_small_bold,
                                    cursor="hand2")
        self.safety_chip.bind("<ButtonRelease-1>", lambda e: self._open_settings())
        Tooltip(self.safety_chip, "Агент действует без вопросов. Нажми, чтобы изменить.", th)

        title_area = tk.Frame(top, bg=c["bg"])
        title_area.pack(side="left", fill="both", expand=True, padx=(24, 8))
        self.title_label = tk.Label(title_area, text="", bg=c["bg"], fg=c["text"], font=th.font_title, anchor="w",
                                    cursor="hand2")
        self.title_label.pack(side="left", fill="x", expand=True)
        self.title_label.bind("<Double-Button-1>", lambda e: self._rename_session_at(self._current_index()))
        Tooltip(self.title_label, "Двойной щелчок — переименовать чат", th)
        tk.Frame(main, bg=c["border"], height=1).grid(row=0, column=0, sticky="sew")

        self.chat = ChatView(main, th)
        self.chat.grid(row=1, column=0, sticky="nsew")

        # ------------------------------ поле ввода ------------------------------
        self.bottom = tk.Frame(main, bg=c["bg"])
        self.bottom.grid(row=2, column=0, sticky="ew", padx=px(28), pady=(6, 12))
        self.bottom.grid_columnconfigure(0, weight=1)
        main.bind("<Configure>", self._on_main_resize, add="+")

        self.input_box = tk.Frame(self.bottom, bg=c["input_bg"], highlightbackground=c["border"],
                                  highlightcolor=c["accent"], highlightthickness=1)
        self.input_box.grid(row=0, column=0, sticky="ew")
        self.input_box.grid_columnconfigure(0, weight=1)
        self.input = tk.Text(
            self.input_box, height=2, wrap="word", font=th.font, bg=c["input_bg"], fg=c["text"],
            insertbackground=c["text"], relief="flat", borderwidth=0, highlightthickness=0,
            padx=14, pady=12, undo=True, maxundo=200, selectbackground=c["select"], selectforeground=c["text"],
        )
        self.input.grid(row=0, column=0, sticky="nsew", padx=(3, 0), pady=3)
        self.placeholder = tk.Label(self.input_box, text="Опиши задачу…   (Enter — отправить, Shift+Enter — новая строка)",
                                    bg=c["input_bg"], fg=c["muted"], font=th.font, anchor="w")
        self.placeholder.bind("<Button-1>", lambda e: self.input.focus_set())
        self.send_btn = FlatButton(self.input_box, th, "Отправить", self._on_send_click, kind="accent", padx=18, pady=9,
                                   font=th.font_ui_bold)
        self.send_btn.grid(row=0, column=1, sticky="se", padx=9, pady=9)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<KP_Enter>", self._on_return)
        self.input.bind("<Control-Return>", lambda e: (self._send(), "break")[1])
        self.input.bind("<KeyRelease>", lambda e: self._on_input_change())
        for virtual in ("<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            self.input.bind(virtual, lambda e: self.after_idle(self._on_input_change), add="+")
        self.input.bind("<FocusIn>", lambda e: self.input_box.configure(highlightbackground=c["accent"]))
        self.input.bind("<FocusOut>", lambda e: self.input_box.configure(highlightbackground=c["border"]))

        status_row = tk.Frame(self.bottom, bg=c["bg"])
        status_row.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        self.hint_label = tk.Label(status_row, text="Esc — остановить   ·   Ctrl+N — новый чат", bg=c["bg"],
                                   fg=c["muted"], font=th.font_small)
        self.hint_label.pack(side="left")
        self.status_label = tk.Label(status_row, textvariable=self.status_var, bg=c["bg"], fg=c["muted"], font=th.font_small)
        self.status_label.pack(side="right")

        self._set_busy(self.busy)
        self._render_status()

    def _on_main_resize(self, event) -> None:
        if self.theme is None:
            return
        pad = column_padding(event.width, self.theme.px(MAX_COLUMN) + 2 * self.theme.px(32), self.theme.px(24))
        try:
            self.bottom.grid_configure(padx=pad)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ вспомогательное

    def _on_wheel(self, event) -> str | None:
        """Совместимость со старыми вызовами; сама лента обрабатывает колесо локально."""
        if self.chat is None or not self.chat.owns(event.widget):
            return None
        return self.chat._on_wheel(event)

    def _resize_input(self) -> None:
        fit_text_height(self.input, 2, 8)

    def _on_input_change(self) -> None:
        self._resize_input()
        empty = not self.input.get("1.0", "end-1c").strip()
        if empty:
            self.placeholder.place(x=17, y=14)
        else:
            self.placeholder.place_forget()
        self._sync_send_button(empty)

    def _sync_send_button(self, empty: bool | None = None) -> None:
        if empty is None:
            empty = not self.input.get("1.0", "end-1c").strip()
        if self.busy:
            self.send_btn.set_text("Стоп")
            self.send_btn.set_kind("danger")
            self.send_btn.set_enabled(True)
        else:
            self.send_btn.set_text("Отправить")
            self.send_btn.set_kind("ghost" if empty else "accent")
            self.send_btn.set_enabled(not empty)

    def _on_return(self, event):
        if event.state & 0x0001:  # Shift+Enter — обычный перенос строки
            return None
        self._send()
        return "break"

    def _on_escape(self, _event=None):
        if self.busy and self._confirm_dialog is None:
            self._stop()
            return "break"
        return None

    def _focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _use_suggestion(self, text: str) -> None:
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self._on_input_change()
        self.input.focus_set()

    def _set_status(self, text: str, kind: str = "muted") -> None:
        self._status_text, self._status_kind = text, kind
        self._render_status()

    def _render_status(self) -> None:
        text = self._status_text
        if self.busy:
            elapsed = int(time.monotonic() - self._busy_since)
            text = f"{SPINNER[self._spin_index % len(SPINNER)]}  {text}  ·  {elapsed} с"
        self.status_var.set(text)
        label = self.status_label
        if label is not None and self.theme is not None:
            colors = {"muted": "muted", "ok": "ok", "warn": "warn", "danger": "danger"}
            try:
                label.configure(fg=self.theme.c[colors.get(self._status_kind, "muted")])
            except tk.TclError:
                pass

    def _update_window_title(self) -> None:
        name = self.memory.get_session_name(self.current_session_id) or ""
        prefix = "● " if self.busy else ""
        self.title(f"{prefix}{name} — {APP_TITLE}" if name else f"{prefix}{APP_TITLE}")

    def _update_key_state(self) -> None:
        assert self.theme is not None
        has_key = bool(self.client.api_key)
        self.settings_btn.set_text("⚙   Настройки" if has_key else "⚙   Настройки  ·  нет API key")
        self.settings_btn.set_fg(self.theme.c["muted"] if has_key else self.theme.c["danger"])

    def _update_confirm_toggle(self) -> None:
        if self.tools.confirm_enabled:
            self.safety_chip.pack_forget()
        else:
            self.safety_chip.pack(side="right", padx=(0, 12))

    def _set_confirm(self, value: bool) -> None:
        self.tools.confirm_enabled = bool(value)
        self.settings.set("confirm_dangerous", self.tools.confirm_enabled)
        self._update_confirm_toggle()
        if self.tools.confirm_enabled:
            self._set_status("Подтверждения включены")
        else:
            self._set_status("Подтверждения ОТКЛЮЧЕНЫ: агент действует без вопросов", "warn")

    def _toggle_confirm(self) -> None:
        self._set_confirm(not self.tools.confirm_enabled)

    def _set_theme(self, name: str, reopen_settings: bool = False) -> None:
        if self.busy or self._confirm_dialog is not None:
            self._set_status("Смени тему, когда агент закончит работу.", "warn")
            return
        assert self.theme is not None
        if name == self.theme.name:
            return
        self.settings.set("theme", name)
        self._rebuild()
        if reopen_settings:
            self.after(50, self._open_settings)

    def _toggle_theme(self) -> None:
        assert self.theme is not None
        self._set_theme("light" if self.theme.name == "dark" else "dark")

    # ------------------------------------------------------------------ ключ и модели

    def _open_settings(self) -> None:
        assert self.theme is not None
        SettingsDialog(
            self, self.theme, has_key=bool(self.client.api_key), theme_name=self.theme.name,
            confirm_enabled=self.tools.confirm_enabled, on_save_key=self._save_api_key,
            on_set_theme=lambda name: self._set_theme(name, reopen_settings=True), on_set_confirm=self._set_confirm,
        )

    def _save_api_key(self, value: str) -> None:
        self.config_obj.save_api_key(value)
        self.client.set_api_key(value)
        self._update_key_state()
        self._set_status("API key сохранён в .env", "ok")
        if self.chat:
            self.chat.add_message("system", f"API key сохранён в {ENV_FILE}. Не публикуй этот файл.")

    def _set_api_key(self) -> None:
        """Совместимость: открыть настройки (там поле ключа)."""
        self._open_settings()

    def _model_changed(self, _event=None) -> None:
        value = self.model_var.get().strip()
        if value and value != self.config_obj.model:
            self.config_obj.model = value
            self._set_status(f"Модель: {value}")

    def _load_models(self) -> None:
        self._set_status("Загрузка каталога бесплатных моделей…")

        def worker() -> None:
            try:
                self.events.put(("models", self.client.list_free_models()))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ сессии

    def _ask_text(self, title: str, prompt: str, initial: str, on_done) -> None:
        assert self.theme is not None
        PromptDialog(self, self.theme, title, prompt, initial, on_done)

    def _selected_index(self) -> int | None:
        selection = self.session_list.curselection()
        return selection[0] if selection else None

    def _current_index(self) -> int | None:
        return self._session_ids.index(self.current_session_id) if self.current_session_id in self._session_ids else None

    def _refresh_sessions(self, select: int | None = None) -> None:
        sessions = self.memory.list_sessions()
        query = self.search_var.get().strip().lower()
        visible = [s for s in sessions if not query or query in str(s["name"]).lower()]
        self._session_ids = [s["id"] for s in visible]
        self._hover_index = -1
        self.session_list.delete(0, "end")
        for session in visible:
            self.session_list.insert("end", "   " + session["name"])
        if select in self._session_ids:
            index = self._session_ids.index(select)
            self.session_list.selection_clear(0, "end")
            self.session_list.selection_set(index)
            self.session_list.see(index)
        self.title_label.configure(text=self.memory.get_session_name(self.current_session_id) or "")
        self._update_window_title()

    def _on_list_motion(self, event) -> None:
        index = self.session_list.nearest(event.y)
        try:
            box = self.session_list.bbox(index)
        except tk.TclError:
            box = None
        if index < 0 or index >= len(self._session_ids) or not box or not (box[1] <= event.y < box[1] + box[3]):
            index = -1
        if index == self._hover_index:
            return
        self._clear_hover()
        if index >= 0 and self.theme is not None and index not in self.session_list.curselection():
            try:
                self.session_list.itemconfigure(index, background=self.theme.c["hover"])
                self._hover_index = index
            except tk.TclError:
                pass

    def _clear_hover(self) -> None:
        if self._hover_index >= 0 and self.theme is not None:
            try:
                self.session_list.itemconfigure(self._hover_index, background=self.theme.c["sidebar"])
            except tk.TclError:
                pass
        self._hover_index = -1

    def _on_session_select(self, _event=None) -> None:
        selection = self.session_list.curselection()
        if not selection:
            return
        session_id = self._session_ids[selection[0]]
        if session_id == self.current_session_id:
            return
        if self.busy:
            self._set_status("Дождись завершения работы агента или нажми «Стоп».", "warn")
            self._refresh_sessions(select=self.current_session_id)
            return
        self._switch_session(session_id)

    def _switch_session(self, session_id: int) -> None:
        self.current_session_id = session_id
        self.agent.set_session(session_id)
        self._refresh_sessions(select=session_id)
        self._render_history()
        self.input.focus_set()

    def _new_session(self) -> None:
        if self.busy:
            self._set_status("Дождись завершения работы агента или нажми «Стоп».", "warn")
            return
        if self.search_var.get():
            self.search_var.set("")
        self._switch_session(self.memory.create_session(DEFAULT_SESSION_NAME))

    def _session_menu(self, event) -> str:
        index = self.session_list.nearest(event.y)
        if index < 0 or index >= len(self._session_ids):
            return "break"
        assert self.theme is not None
        c = self.theme.c
        menu = tk.Menu(self, tearoff=0, bg=c["card"], fg=c["text"], activebackground=c["select"],
                       activeforeground=c["text"], borderwidth=0)
        menu.add_command(label="Переименовать", command=lambda: self._rename_session_at(index))
        menu.add_command(label="Удалить", command=lambda: self._delete_session_at(index))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()
        return "break"

    def _rename_session_at(self, index: int | None) -> None:
        if index is None or index < 0 or index >= len(self._session_ids):
            return
        session_id = self._session_ids[index]

        def apply(name: str) -> None:
            self.memory.rename_session(session_id, name)
            self._refresh_sessions(select=self.current_session_id)

        self._ask_text("Переименовать чат", "Название чата", self.memory.get_session_name(session_id) or "", apply)

    def _delete_session_at(self, index: int | None) -> None:
        if index is None or index < 0 or index >= len(self._session_ids):
            return
        if self.busy:
            self._set_status("Дождись завершения работы агента или нажми «Стоп».", "warn")
            return
        session_id = self._session_ids[index]
        name = self.memory.get_session_name(session_id) or ""
        if not messagebox.askyesno("Удалить чат", f"Удалить чат «{name}» и всю его историю?", parent=self):
            return
        self.memory.delete_session(session_id)
        remaining = self.memory.list_sessions()
        next_id = remaining[0]["id"] if remaining else self.memory.create_session(DEFAULT_SESSION_NAME)
        if session_id == self.current_session_id or not remaining:
            self._switch_session(next_id)
        else:
            self._refresh_sessions(select=self.current_session_id)

    # ------------------------------------------------------------------ история

    def _render_history(self) -> None:
        assert self.chat is not None
        self.chat.clear()
        rows = self.memory.get_session_messages(self.current_session_id)
        hidden = max(0, len(rows) - MAX_RENDERED_MESSAGES)
        if hidden:
            self.chat.add_message("system", f"Показаны последние {MAX_RENDERED_MESSAGES} записей, ещё {hidden} скрыто (они остаются в базе).")
        for row in rows[hidden:]:
            role = row["role"]
            if role in ("user", "assistant"):
                self.chat.add_message(role, row["content"])
            elif role == "tool":
                self.chat.add_tool_history(row.get("name") or "tool", row["content"])
        if not rows:
            notices = []
            if not self.client.api_key:
                notices.append(("Сначала добавь OpenRouter API key — нажми здесь, чтобы открыть настройки.", self._open_settings))
            self.chat.show_welcome(WELCOME_TITLE, WELCOME_SUBTITLE, SUGGESTIONS, 4, self._use_suggestion, notices)
        self.chat.scroll_to_bottom(force=True)

    # ------------------------------------------------------------------ отправка и остановка

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if self._spin_job is not None:
            try:
                self.after_cancel(self._spin_job)
            except tk.TclError:
                pass
            self._spin_job = None
        if busy:
            if not self._busy_since or self._status_text == "Готов":
                self._busy_since = time.monotonic()
            self._spin_job = self.after(110, self._spin_tick)
        self._sync_send_button()
        self._render_status()
        self._update_window_title()

    def _spin_tick(self) -> None:
        self._spin_job = None
        if not self.busy:
            return
        self._spin_index += 1
        self._render_status()
        self._spin_job = self.after(110, self._spin_tick)

    def _on_send_click(self) -> None:
        if self.busy:
            self._stop()
        else:
            self._send()

    def _stop(self) -> None:
        self.agent.cancel()
        self._set_status("Останавливаю после текущего запроса…", "warn")
        if self._confirm_dialog is not None:
            self._confirm_dialog.deny()

    def _send(self) -> None:
        if self.busy:
            self._set_status("Агент ещё работает — дождись ответа или нажми Esc.", "warn")
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        assert self.chat is not None
        if not self.client.api_key:
            self.chat.add_message("error", "Сначала укажи OpenRouter API key (Настройки → API key).")
            self.chat.scroll_to_bottom(force=True)
            return
        if self.chat.welcome_visible:
            self.chat.clear()
        self.input.delete("1.0", "end")
        self._on_input_change()
        self._model_changed()
        session_id = self.current_session_id
        if self.memory.get_session_name(session_id) == DEFAULT_SESSION_NAME:
            title = " ".join(text.split())
            self.memory.rename_session(session_id, title[:40] + ("…" if len(title) > 40 else ""))
            self._refresh_sessions(select=session_id)
        self.chat.add_message("user", text)
        self.chat.scroll_to_bottom(force=True)
        self._busy_since = time.monotonic()
        self._status_text, self._status_kind = "Агент работает…", "muted"
        self._set_busy(True)

        def on_event(kind: str, payload: str) -> None:
            self.events.put((kind, payload))

        def worker() -> None:
            try:
                answer = self.agent.run(text, on_event=on_event, session_id=session_id)
                self.events.put(("answer", answer))
            except AgentCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except (AgentError, OpenRouterError) as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", f"Внутренняя ошибка: {type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ подтверждения

    def _confirm_hook(self, request: ConfirmRequest) -> str:
        """Вызывается из потока агента: ждёт ответа пользователя из главного потока."""
        holder = {"choice": "deny"}
        done = threading.Event()
        self.events.put(("confirm", (request, holder, done)))
        done.wait(timeout=900)
        return holder["choice"]

    def _show_confirm(self, payload) -> None:
        request, holder, done = payload

        def on_choice(choice: str) -> None:
            holder["choice"] = choice
            self._confirm_dialog = None
            if self.busy:
                if choice == "deny":
                    self._set_status("Действие отклонено", "warn")
                else:
                    self._set_status("Агент работает…")
            done.set()

        assert self.theme is not None
        self._set_status("Ждёт вашего подтверждения…", "warn")
        try:
            self.deiconify()
            self.lift()
        except tk.TclError:
            pass
        self._confirm_dialog = ConfirmDialog(self, self.theme, request.tool, request.reason, request.arguments,
                                             request.allow_session, on_choice)

    # ------------------------------------------------------------------ диалоги

    def _open_memory(self) -> None:
        assert self.theme is not None
        MemoryDialog(self, self.theme, self.memory)

    def _open_inventory(self) -> None:
        assert self.theme is not None
        InventoryDialog(self, self.theme, self.tools)

    # ------------------------------------------------------------------ события из потоков

    def _finish_run(self) -> None:
        self._set_busy(False)
        self._refresh_sessions(select=self.current_session_id)

    def _handle_event(self, kind: str, payload: Any) -> None:
        assert self.chat is not None
        if kind == "status":
            self._set_status(str(payload))
        elif kind == "tool_call":
            data = json.loads(payload)
            self.chat.add_tool_call(data.get("name", "?"), data.get("arguments", {}))
        elif kind == "tool_result":
            data = json.loads(payload)
            if "error" in data:
                self.chat.finish_tool(data.get("name", "?"), data["error"], error=True)
            else:
                self.chat.finish_tool(data.get("name", "?"), data.get("result"))
        elif kind == "reasoning":
            self.chat.add_reasoning(str(payload))
        elif kind == "warning":
            self.chat.add_message("system", str(payload))
        elif kind == "answer":
            self.chat.add_message("assistant", str(payload))
            self._status_text, self._status_kind = "Готов", "ok"
            self._finish_run()
        elif kind == "cancelled":
            self.chat.add_message("system", "Остановлено.")
            self._status_text, self._status_kind = "Остановлено", "warn"
            self._finish_run()
        elif kind == "error":
            self.chat.add_message("error", str(payload))
            self._status_text, self._status_kind = "Ошибка", "danger"
            if self.busy:
                self._finish_run()
            else:
                self._render_status()
        elif kind == "models":
            values = [item["id"] for item in payload if item.get("id")]
            self._model_values = values
            self.model_box["values"] = values
            self._set_status(f"Загружено бесплатных моделей: {len(values)}", "ok")
        elif kind == "confirm":
            self._show_confirm(payload)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except Exception:  # noqa: BLE001 — ошибка отрисовки не должна ронять цикл опроса
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ выход

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("Выход", "Агент ещё работает. Остановить и выйти?", parent=self):
            return
        self.agent.cancel()
        try:
            terminals_module.shutdown()
            browser_module.shutdown()
        finally:
            self.destroy()


def main() -> None:
    app = AgentApp()
    app.mainloop()


if __name__ == "__main__":
    main()