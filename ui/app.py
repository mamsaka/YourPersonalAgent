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
from core.agent import PERSONAS, AgentCancelled, AgentError, PersonalAgent, resolve_persona
from core.config import APP_DATA, ENV_FILE, UI_SETTINGS_FILE, AgentConfig, env_has, is_valid_geometry
from core.memory import MemoryStore
from core.openrouter import OpenRouterClient, OpenRouterError
from core.tools import ConfirmRequest, ToolRegistry

from . import hotkeys
from .chat import MAX_COLUMN, ChatView
from .dialogs import ConfirmDialog, InventoryDialog, MemoryDialog, PersonaDialog, PromptDialog, SettingsDialog
from .settings import UiSettings
from .theme import Theme
from .widgets import FlatButton, Tooltip, column_padding, fit_text_height, make_entry

DEFAULT_SESSION_NAME = "Новый чат"
MAX_RENDERED_MESSAGES = 200
APP_TITLE = "Personal AI Agent"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
USAGE_TOOLTIP = (
    "Расход текущего чата.\n"
    "Запросы — обращения к модели (шаги).\n"
    "Контекст — входные токены (отправленная история).\n"
    "Токены — всего израсходовано (вход + выход).\n"
    "Расход — стоимость в долларах по данным OpenRouter."
)

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
        self._migrate_legacy_settings()
        self._restore_geometry()
        self.memory = MemoryStore(APP_DATA / "agent.sqlite3")
        self.client = OpenRouterClient(self.config_obj)
        confirm = bool(self.config_obj.confirm_dangerous)
        self.tools = ToolRegistry(self.memory, confirm_enabled=confirm)
        self.tools.confirm_hook = self._confirm_hook
        self.agent = PersonalAgent(self.config_obj, self.client, self.memory, self.tools)

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.busy = False
        self.theme: Theme | None = None
        self.chat: ChatView | None = None
        self.input: tk.Text | None = None
        self.status_label: tk.Label | None = None
        self.usage_label: tk.Label | None = None
        self.usage_tooltip: Tooltip | None = None
        self._confirm_dialog: ConfirmDialog | None = None
        self._session_ids: list[int] = []
        self._model_values: list[str] = []
        self._hover_index = -1
        self._spin_job: str | None = None
        self._spin_index = 0
        self._busy_since = 0.0
        self._status_text = "Готов"
        self._status_kind = "muted"
        self._editing_message_id: int | None = None
        self._ws = str(self.tk.call("tk", "windowingsystem"))
        self.current_session_id = self._restore_session()
        self.agent.set_session(self.current_session_id)
        self.status_var = tk.StringVar(value="Готов")
        self.usage_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value=self.config_obj.model)
        self.search_var = tk.StringVar()
        self.model_search_var = tk.StringVar()
        self._all_models: list[dict[str, Any]] = []
        self._model_list_data: list[str] = []

        hotkeys.install(self, lambda: self.theme.c if self.theme else {})
        hotkeys.install_shortcuts(self, {
            "new_chat": self._new_session, "search": self._focus_search, "settings": self._open_settings,
        })
        self.bind("<Escape>", self._on_escape)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._rebuild()
        self._poll_events()

    # ------------------------------------------------------------------ настройки из .env

    def _migrate_legacy_settings(self) -> None:
        """Разово перенести тему и подтверждения из data/ui_settings.json в .env."""
        if not env_has("AGENT_THEME"):
            legacy_theme = self.settings.get("theme")
            if legacy_theme in ("dark", "light"):
                self.config_obj.save_theme(str(legacy_theme))
        if not env_has("AGENT_CONFIRM_DANGEROUS"):
            legacy_confirm = self.settings.get("confirm_dangerous")
            if isinstance(legacy_confirm, bool):
                self.config_obj.save_confirm(legacy_confirm)

    def _restore_geometry(self) -> None:
        """Восстановить размер и позицию окна из .env, если они сохранены."""
        saved = (self.config_obj.window_geometry or "").strip()
        if not is_valid_geometry(saved):
            return
        try:
            self.geometry(saved)
        except tk.TclError:
            pass

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
        self.theme = Theme(self, self.config_obj.theme)
        self.theme.apply(self)
        self._hover_index = -1
        self._build_ui()
        if draft:
            self.input.insert("1.0", draft)
        self._refresh_sessions(select=self.current_session_id)
        self._render_history()
        self._update_key_state()
        self._update_confirm_toggle()
        self._refresh_usage()
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
        FlatButton(footer, th, "✎   Характер", self._open_persona, kind="sidebar", anchor="w", padx=12,
                   tooltip="Задать стиль общения агента").pack(fill="x", pady=1)
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
                   tooltip="Обновить список моделей").pack(side="right", pady=14)
        model_search_frame, self.model_search_entry = make_entry(toolbar, th, textvariable=self.model_search_var,
                                                                  bg=c["bg"])
        self.model_search_entry.configure(font=th.font_ui, width=16)
        model_search_frame.pack(side="right", padx=(6, 0), pady=13)
        Tooltip(self.model_search_entry, "Поиск модели по названию", th)
        self.model_search_var.trace_add("write", lambda *_: self._filter_models())
        # Поле текущей модели в панели (обычный вид)
        self.model_box = ttk.Combobox(toolbar, textvariable=self.model_var, width=34, values=self._model_values)
        self.model_box.pack(side="right", padx=6, pady=15)
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)
        self.model_box.bind("<Return>", self._model_changed)
        self.model_box.bind("<FocusOut>", self._model_changed)
        # Кнопка открыть цветной список моделей
        FlatButton(toolbar, th, "📋", self._open_model_picker, kind="bar", padx=8, pady=5,
                   tooltip="Выбрать модель из списка (:free — зелёный, остальные — красный)").pack(side="right", padx=(6, 0), pady=14)
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
        self.chat.set_message_actions(self._rollback_to_message, self._regenerate_message)
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
        self.usage_label = tk.Label(status_row, textvariable=self.usage_var, bg=c["bg"], fg=c["muted"],
                                    font=th.font_small)
        self.usage_label.pack(side="right", padx=(0, 16))
        self.usage_tooltip = Tooltip(self.usage_label, USAGE_TOOLTIP, th)

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

    @staticmethod
    def _format_number(value: Any) -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        return f"{number:,}".replace(",", " ")

    @staticmethod
    def _format_money(value: Any) -> str:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            return "$0.00"
        if amount >= 1:
            return f"${amount:,.2f}".replace(",", " ")
        if amount >= 0.01:
            return f"${amount:.4f}"
        return f"${amount:.6f}"

    def _format_usage(self, usage: dict[str, Any]) -> str:
        return (
            f"Запросы: {self._format_number(usage.get('requests'))}"
            f"   ·   Контекст: {self._format_number(usage.get('prompt_tokens'))}"
            f"   ·   Токены: {self._format_number(usage.get('total_tokens'))}"
            f"   ·   Расход: {self._format_money(usage.get('cost'))}"
        )

    def _refresh_usage(self, usage: dict[str, Any] | None = None) -> None:
        """Обновить счётчик расхода текущего чата и подсказку с общим итогом."""
        if usage is None:
            usage = self.memory.get_usage(self.current_session_id)
        self.usage_var.set(self._format_usage(usage))
        if self.usage_tooltip is not None:
            total = self.memory.get_total_usage()
            self.usage_tooltip.text = (
                f"{USAGE_TOOLTIP}\n\nВсего за всё время: "
                f"{self._format_number(total.get('requests'))} запросов, "
                f"{self._format_number(total.get('total_tokens'))} токенов, "
                f"{self._format_money(total.get('cost'))}."
            )

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
        self.config_obj.save_confirm(self.tools.confirm_enabled)
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
        self.config_obj.save_theme(name)
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

    def _open_persona(self) -> None:
        assert self.theme is not None
        PersonaDialog(self, self.theme, PERSONAS, self.config_obj.persona, self._save_persona)

    def _save_persona(self, value: str) -> None:
        self.config_obj.save_persona(value)
        label, _text = resolve_persona(value)
        shown = label or f"свой текст, {len(value)} симв."
        self._set_status(f"Характер: {shown}", "ok")
        if self.chat:
            self.chat.add_message("system", f"Характер агента обновлён: {shown}. Применяется со следующего запроса.")

    def _save_api_key(self, value: str) -> None:
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
            self.config_obj.save_model(value)
            self.model_var.set(value)
            self._set_status(f"Модель: {value}")

    def _load_models(self) -> None:
        self._set_status("Загрузка каталога моделей…")

        def worker() -> None:
            try:
                self.events.put(("models", self.client.list_models()))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _open_model_picker(self) -> None:
        """Открыть всплывающее окно с цветным списком моделей.
        :free — зелёный, остальные — красный. При выборе модель применяется.
        """
        if not self._all_models:
            self._set_status("Сначала загрузите список моделей (кнопка ↻)", "warn")
            return

        picker = tk.Toplevel(self)
        c = self.theme.c
        picker.title("Выбрать модель")
        picker.configure(bg=c["panel"])
        picker.transient(self)
        picker.grab_set()

        # Поиск внутри пикера
        top_frame = tk.Frame(picker, bg=c["panel"])
        top_frame.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(top_frame, text="Поиск:", bg=c["panel"], fg=c["text"], font=self.theme.font_small).pack(side="left", padx=(0, 6))
        search_var = tk.StringVar()
        entry_frame, entry = make_entry(top_frame, self.theme, textvariable=search_var, bg=c["input_bg"])
        entry_frame.pack(side="left", fill="x", expand=True)
        entry.focus_set()
        # Placeholder текст
        placeholder_text = "Поиск модели…"
        entry.insert(0, placeholder_text)
        entry.configure(fg=c["muted"])
        is_placeholder = [True]

        def on_focus_in(_e):
            if is_placeholder[0]:
                entry.delete(0, "end")
                entry.configure(fg=c["text"])
                is_placeholder[0] = False

        def on_focus_out(_e):
            if not entry.get().strip():
                entry.insert(0, placeholder_text)
                entry.configure(fg=c["muted"])
                is_placeholder[0] = True

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        # Кнопка очистки поиска
        clear_btn = tk.Label(top_frame, text="×", bg=c["panel"], fg=c["muted"],
                             font=self.theme.font_ui_bold, cursor="hand2", padx=6, pady=2)
        clear_btn.pack(side="left", padx=(6, 0))
        clear_btn_enabled = [False]

        def _clear_search():
            search_var.set("")
            entry.delete(0, "end")
            entry.insert(0, placeholder_text)
            entry.configure(fg=c["muted"])
            is_placeholder[0] = True
            clear_btn.configure(fg=c["muted"])

        def _on_clear_enter(_e):
            if clear_btn_enabled[0]:
                clear_btn.configure(fg=c["accent"])

        def _on_clear_leave(_e):
            if clear_btn_enabled[0]:
                clear_btn.configure(fg=c["muted"])

        def _on_clear_click(_e):
            if clear_btn_enabled[0]:
                _clear_search()

        clear_btn.bind("<Enter>", _on_clear_enter)
        clear_btn.bind("<Leave>", _on_clear_leave)
        clear_btn.bind("<ButtonRelease-1>", _on_clear_click)

        def _on_search_change(*_):
            val = search_var.get().strip()
            has_text = bool(val) and val != placeholder_text
            clear_btn_enabled[0] = has_text
            clear_btn.configure(fg=c["accent"] if has_text else c["muted"])

        search_var.trace_add("write", lambda *_: (_on_search_change(), fill_picker()))

        # Листбокс с цветами
        list_frame = tk.Frame(picker, bg=c["panel"])
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        picker_list = tk.Listbox(list_frame, width=40, height=15, bg=c["input_bg"], fg=c["text"],
                                 font=self.theme.font_ui, selectbackground=c["select"],
                                 selectforeground=c["text"], borderwidth=0, highlightthickness=0)
        picker_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=picker_list.yview)
        picker_list.configure(yscrollcommand=picker_scroll.set)
        picker_scroll.pack(side="right", fill="y")
        picker_list.pack(side="left", fill="both", expand=True)

        # Данные для отображения
        picker_data = []

        def fill_picker():
            picker_list.delete(0, "end")
            picker_data.clear()
            q = search_var.get().strip().lower()
            for item in self._all_models:
                mid = item.get("id", "")
                if q and q not in str(mid).lower() and q not in str(item.get("name", "")).lower():
                    continue
                picker_list.insert("end", mid)
                picker_data.append(mid)
                idx = picker_list.size() - 1
                if mid.endswith(":free"):
                    picker_list.itemconfigure(idx, fg=c["ok"])
                else:
                    picker_list.itemconfigure(idx, fg=c["danger"])
            # Выбрать текущую модель
            current = self.config_obj.model
            if current in picker_data:
                idx = picker_data.index(current)
                picker_list.selection_set(idx)
                picker_list.see(idx)

        fill_picker()
        search_var.trace_add("write", lambda *_: fill_picker())

        # Кнопки
        btn_frame = tk.Frame(picker, bg=c["panel"])
        btn_frame.pack(fill="x", padx=12, pady=(6, 12))

        def apply_choice():
            selection = picker_list.curselection()
            if selection:
                value = picker_list.get(selection[0])
            else:
                value = ""
            if value:
                self.config_obj.save_model(value)
                self.model_var.set(value)
                self._set_status(f"Модель: {value}")
            picker.destroy()

        FlatButton(btn_frame, self.theme, "Применить", apply_choice, kind="accent", padx=16).pack(side="right")
        FlatButton(btn_frame, self.theme, "Отмена", picker.destroy, kind="ghost", padx=16).pack(side="right", padx=(0, 8))

        picker.bind("<Escape>", lambda e: picker.destroy())
        picker.bind("<Return>", lambda e: apply_choice())
        picker_list.bind("<Double-Button-1>", lambda e: apply_choice())

        picker.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - picker.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - picker.winfo_height()) // 2
        picker.geometry(f"+{x}+{y}")

    def _filter_models(self) -> None:
        """Отфильтровать список моделей по поисковой строке (имя или ID).
        Модели, заканчивающиеся на :free, окрашиваются зелёным, остальные — красным.
        """
        if not hasattr(self, "model_box"):
            return
        # Обновить значения в Combobox панели
        query = self.model_search_var.get().strip().lower()
        if query:
            values = [
                item["id"] for item in self._all_models
                if query in str(item.get("id", "")).lower() or query in str(item.get("name", "")).lower()
            ]
        else:
            values = [item["id"] for item in self._all_models]
        self._model_values = values
        self.model_box["values"] = values
        # Выбрать текущую модель
        current = self.config_obj.model
        if current in values:
            self.model_var.set(current)

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

    def _restore_session(self) -> int:
        """Вернуть последний активный чат из .env, если он ещё существует."""
        last = self.config_obj.last_session
        if isinstance(last, int) and self.memory.get_session_name(last) is not None:
            return last
        return self.memory.get_default_session_id()

    def _switch_session(self, session_id: int) -> None:
        self._editing_message_id = None
        self.current_session_id = session_id
        self.agent.set_session(session_id)
        self.config_obj.save_last_session(session_id)
        self._refresh_sessions(select=session_id)
        self._render_history()
        self._refresh_usage()
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
            if role == "user":
                self.chat.add_message(role, row["content"], message_id=row.get("id"))
            elif role == "assistant" and row.get("content") is not None and not row.get("tool_calls"):
                self.chat.add_message(role, row["content"], message_id=row.get("id"))
            elif role == "tool":
                self.chat.add_tool_history(row.get("name") or "tool", row["content"])
        if not rows:
            notices = []
            if not self.client.api_key:
                notices.append(("Сначала добавь OpenRouter API key — нажми здесь, чтобы открыть настройки.", self._open_settings))
            self.chat.show_welcome(WELCOME_TITLE, WELCOME_SUBTITLE, SUGGESTIONS, 4, self._use_suggestion, notices)
        self.chat.scroll_to_bottom(force=True)

    def _rollback_to_message(self, message_id: int, original_text: str) -> None:
        """Удаляет выбранное сообщение и всю историю после него,
        перенося исходный текст в поле ввода."""
        if self.busy:
            self._set_status(
                "Сначала останови или дождись завершения текущего ответа.",
                "warn"
            )
            return

        rows = self.memory.get_session_messages(self.current_session_id)

        selected = next(
            (row for row in rows if row.get("id") == message_id),
            None
        )

        if selected is None or selected.get("role") != "user":
            self._set_status("Не удалось найти выбранное сообщение.", "warn")
            return

        prompt = str(selected.get("content") or original_text)

        delete_count = sum(
            1 for row in rows
            if row.get("id", 0) >= message_id
        )

        if delete_count and not messagebox.askyesno(
            "Откат чата",
            "Выбранное сообщение и все сообщения после него "
            "будут удалены из истории.\n\n"
            "Текст выбранного сообщения будет перенесён "
            "в поле ввода для редактирования.\n\n"
            "Продолжить?",
            parent=self,
        ):
            return

        # Удаляем выбранное сообщение ВКЛЮЧИТЕЛЬНО
        self.memory.delete_messages_after(
            self.current_session_id,
            message_id,
            include_message=True
        )

        # Больше не редактируем старое сообщение
        self._editing_message_id = None

        # Обновляем отображение чата
        self._render_history()

        # Возвращаем текст удалённого сообщения в поле ввода
        self.input.delete("1.0", "end")
        self.input.insert("1.0", prompt)

        self._on_input_change()
        self.input.focus_set()

        self._set_status(
            "Откат выполнен. Сообщение удалено из истории и восстановлено в поле ввода.",
            "ok"
        )

    def _regenerate_message(self, message_id: int) -> None:
        """Удалить выбранный ответ и всё после него, затем повторить запрос к модели."""
        if self.busy:
            self._set_status("Сначала останови или дождись завершения текущего ответа.", "warn")
            return
        if not self.client.api_key:
            self.chat.add_message("error", "Сначала укажи OpenRouter API key (Настройки → API key).")
            return

        rows = self.memory.get_session_messages(self.current_session_id)
        selected_index = next(
            (index for index, row in enumerate(rows) if row.get("id") == message_id),
            None,
        )
        if selected_index is None or rows[selected_index].get("role") != "assistant":
            self._set_status("Не удалось найти выбранный ответ.", "warn")
            return
        selected = rows[selected_index]
        if selected.get("tool_calls") or not selected.get("content"):
            self._set_status("Этот элемент не является готовым ответом модели.", "warn")
            return
        previous_user = next(
            (row for row in reversed(rows[:selected_index]) if row.get("role") == "user"),
            None,
        )
        if previous_user is None:
            self._set_status("Для этого ответа не найден исходный запрос.", "warn")
            return

        if not messagebox.askyesno(
            "Регенерировать ответ",
            "Выбранный ответ и все последующие сообщения будут удалены, после чего модель "
            "ответит на исходный запрос заново. Уже выполненные действия инструментов "
            "(например, изменение файлов) не отменяются. Продолжить?",
            parent=self,
        ):
            return

        self.memory.delete_messages_after(self.current_session_id, int(previous_user["id"]))
        self._editing_message_id = None
        self._render_history()
        self._start_agent_run(str(previous_user.get("content") or ""), self.current_session_id)

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
        self._model_changed()
        session_id = self.current_session_id
        if self.memory.get_session_name(session_id) == DEFAULT_SESSION_NAME:
            title = " ".join(text.split())
            self.memory.rename_session(session_id, title[:40] + ("…" if len(title) > 40 else ""))
            self._refresh_sessions(select=session_id)

        # Сохраняем запрос до запуска фонового потока: его DB ID нужен для контекстного меню.
        if self._editing_message_id is not None:
            message_id = self._editing_message_id
            if not self.memory.update_user_message(session_id, message_id, text):
                self._editing_message_id = None
                self._set_status("Не удалось обновить исходное сообщение. Отправь его ещё раз.", "danger")
                return
            self.memory.delete_messages_after(session_id, message_id)
            self._editing_message_id = None
            self._render_history()
        else:
            message_id = self.memory.add_message(session_id, "user", text)
            self.chat.add_message("user", text, message_id=message_id)

        self.input.delete("1.0", "end")
        self._on_input_change()
        self._start_agent_run(text, session_id)

    def _start_agent_run(self, text: str, session_id: int) -> None:
        """Запускает модель с уже сохранённым в истории пользовательским запросом."""
        if self.busy:
            self._set_status("Агент ещё работает — дождись ответа или нажми Esc.", "warn")
            return
        assert self.chat is not None
        self.chat.scroll_to_bottom(force=True)
        self._busy_since = time.monotonic()
        self._status_text, self._status_kind = "Агент работает…", "muted"
        self._set_busy(True)

        def on_event(kind: str, payload: str) -> None:
            self.events.put((kind, payload))

        def worker() -> None:
            try:
                answer = self.agent.run(
                    text,
                    on_event=on_event,
                    session_id=session_id,
                    save_user_message=False,
                )
                history = self.memory.get_session_messages(session_id)
                final_row = next(
                    (
                        row for row in reversed(history)
                        if row.get("role") == "assistant"
                        and row.get("content") is not None
                        and not row.get("tool_calls")
                    ),
                    None,
                )
                self.events.put(("answer", {
                    "text": answer,
                    "message_id": final_row.get("id") if final_row else None,
                }))
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
        elif kind == "answer_delta":
            if not self.chat.streaming:
                self.chat.start_stream()
            self.chat.stream_push(str(payload))
        elif kind == "tool_call":
            if self.chat.streaming:
                self.chat.finish_stream()
            if self.chat.reasoning_streaming:
                self.chat.finish_reasoning()
            data = json.loads(payload)
            self.chat.add_tool_call(data.get("name", "?"), data.get("arguments", {}))
        elif kind == "tool_result":
            data = json.loads(payload)
            if "error" in data:
                self.chat.finish_tool(data.get("name", "?"), data["error"], error=True)
            else:
                self.chat.finish_tool(data.get("name", "?"), data.get("result"))
        elif kind == "reasoning_delta":
            self.chat.push_reasoning(str(payload))
        elif kind == "reasoning":
            self.chat.finish_reasoning(str(payload))
        elif kind == "usage":
            try:
                data = json.loads(payload) if isinstance(payload, str) else payload
            except (ValueError, TypeError):
                data = None
            self._refresh_usage(data if isinstance(data, dict) else None)
        elif kind == "warning":
            self.chat.add_message("system", str(payload))
        elif kind == "answer":
            if isinstance(payload, dict):
                answer_text = str(payload.get("text", ""))
                message_id = payload.get("message_id")
            else:  # совместимость с очередью событий старого формата
                answer_text, message_id = str(payload), None
            if self.chat.streaming:
                self.chat.finish_stream(answer_text, message_id=message_id)
            else:
                self.chat.add_message("assistant", answer_text, message_id=message_id)
            if self.chat.reasoning_streaming:
                self.chat.finish_reasoning()
            self._status_text, self._status_kind = "Готов", "ok"
            self._finish_run()
        elif kind == "cancelled":
            if self.chat.streaming:
                self.chat.finish_stream()
            if self.chat.reasoning_streaming:
                self.chat.finish_reasoning()
            self.chat.add_message("system", "Остановлено.")
            self._status_text, self._status_kind = "Остановлено", "warn"
            self._finish_run()
        elif kind == "error":
            if self.chat.streaming:
                self.chat.finish_stream()
            if self.chat.reasoning_streaming:
                self.chat.finish_reasoning()
            self.chat.add_message("error", str(payload))
            self._status_text, self._status_kind = "Ошибка", "danger"
            if self.busy:
                self._finish_run()
            else:
                self._render_status()
        elif kind == "models":
            self._all_models = [item for item in payload if item.get("id")]
            self._filter_models()
            self._set_status(f"Загружено моделей: {len(self._all_models)}", "ok")
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
        try:
            self.config_obj.save_window_geometry(self.geometry())
        except tk.TclError:
            pass
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