from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Any, Callable

from .theme import Theme
from .widgets import (FlatButton, ReadOnlyText, Switch, clip, format_arguments, make_entry)


class Dialog(tk.Toplevel):
    """Базовое окно: тема, центрирование относительно главного окна, Esc для закрытия."""

    def __init__(self, parent, theme: Theme, title: str, width: int, height: int, modal: bool = False,
                 resizable: bool = True):
        super().__init__(parent)
        self.theme = theme
        self._modal = modal
        self._closed = False
        self.configure(bg=theme.c["panel"])
        self.title(title)
        self.transient(parent.winfo_toplevel())
        w, h = theme.px(width), theme.px(height)
        root = parent.winfo_toplevel()
        try:
            root.update_idletasks()
            x = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - h) // 3)
        except tk.TclError:
            x = y = 80
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.resizable(resizable, resizable)
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def ready(self) -> None:
        """Вызвать в конце __init__ подкласса."""
        self.after(30, lambda: self._grab(10))

    def _grab(self, attempts: int) -> None:
        try:
            self.deiconify()
            self.lift()
            if self._modal:
                self.grab_set()
            self.focus_force()
        except tk.TclError:
            if attempts > 0 and not self._closed:
                self.after(50, lambda: self._grab(attempts - 1))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def _label(parent, theme: Theme, text: str, kind: str = "text", **kw) -> tk.Label:
    c = theme.c
    fonts = {"text": theme.font_ui, "muted": theme.font_small, "title": theme.font_title, "bold": theme.font_ui_bold,
             "section": theme.font_small_bold}
    colors = {"text": c["text"], "muted": c["muted"], "title": c["text"], "bold": c["text"], "section": c["muted"]}
    return tk.Label(parent, text=text, bg=kw.pop("bg", c["panel"]), fg=colors[kind], font=fonts[kind], **kw)


class PromptDialog(Dialog):
    """Ввод одной строки (переименование чата и т.п.). Не блокирует: результат приходит в on_done."""

    def __init__(self, parent, theme: Theme, title: str, prompt: str, initial: str,
                 on_done: Callable[[str], None], button: str = "Сохранить"):
        super().__init__(parent, theme, title, 460, 180, modal=True, resizable=False)
        c = theme.c
        self._on_done = on_done
        body = tk.Frame(self, bg=c["panel"])
        body.pack(fill="both", expand=True, padx=22, pady=18)
        _label(body, theme, prompt, "bold").pack(anchor="w", pady=(0, 8))
        self.var = tk.StringVar(value=initial)
        frame, self.entry = make_entry(body, theme, textvariable=self.var)
        frame.pack(fill="x")
        buttons = tk.Frame(body, bg=c["panel"])
        buttons.pack(fill="x", pady=(16, 0))
        FlatButton(buttons, theme, button, self._ok, kind="accent", padx=18).pack(side="right")
        FlatButton(buttons, theme, "Отмена", self.close, kind="ghost", padx=18).pack(side="right", padx=(0, 8))
        self.entry.bind("<Return>", lambda e: self._ok())
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.ready()

    def _ok(self) -> None:
        value = self.var.get().strip()
        self.close()
        if value:
            self._on_done(value)


class ConfirmDialog(Dialog):
    """Подтверждение опасного действия. Не блокирует цикл событий: результат приходит в on_choice."""

    GUARD_MS = 700  # защита от случайного клика/Enter в момент появления окна

    def __init__(self, parent, theme: Theme, tool: str, reason: str, arguments: dict[str, Any],
                 allow_session: bool, on_choice: Callable[[str], None]):
        super().__init__(parent, theme, "Подтверждение действия", 640, 440, modal=True, resizable=True)
        c = theme.c
        self._on_choice = on_choice
        self._done = False
        self.minsize(theme.px(520), theme.px(340))

        head = tk.Frame(self, bg=c["panel"])
        head.pack(fill="x", padx=22, pady=(20, 4))
        tk.Label(head, text="⚠", bg=c["panel"], fg=c["warn"], font=theme.font_hero).pack(side="left", padx=(0, 12))
        titles = tk.Frame(head, bg=c["panel"])
        titles.pack(side="left", fill="x", expand=True)
        _label(titles, theme, "Агент просит разрешение", "title").pack(anchor="w")
        tk.Label(titles, text=reason, bg=c["panel"], fg=c["warn"], font=theme.font_ui_bold, wraplength=theme.px(500),
                 justify="left").pack(anchor="w", pady=(2, 0))
        _label(self, theme, f"Инструмент: {tool}", "muted").pack(anchor="w", padx=22, pady=(10, 6))

        self.details = ReadOnlyText(self, theme, bg=c["code_bg"], fg=c["code_fg"], font=theme.font_mono_small, height=9)
        self.details.pack(fill="both", expand=True, padx=22)
        self.details.set_content(format_arguments(arguments))

        buttons = tk.Frame(self, bg=c["panel"])
        buttons.pack(fill="x", padx=22, pady=18)
        self.allow_btn = FlatButton(buttons, theme, "Разрешить", lambda: self._finish("allow"), kind="accent", padx=20)
        self.allow_btn.pack(side="left")
        self.session_btn = None
        if allow_session:
            self.session_btn = FlatButton(buttons, theme, "Разрешать в этой сессии", lambda: self._finish("session"),
                                          kind="ghost", padx=16)
            self.session_btn.pack(side="left", padx=8)
        FlatButton(buttons, theme, "Отклонить  (Esc)", lambda: self._finish("deny"), kind="danger", padx=18).pack(side="right")

        self.allow_btn.set_enabled(False)
        if self.session_btn is not None:
            self.session_btn.set_enabled(False)
        self.after(self.GUARD_MS, self._arm)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("deny"))
        self.bind("<Escape>", lambda e: self._finish("deny"))
        try:
            self.bell()
        except tk.TclError:
            pass
        self.ready()

    def _arm(self) -> None:
        if self._done:
            return
        try:
            self.allow_btn.set_enabled(True)
            if self.session_btn is not None:
                self.session_btn.set_enabled(True)
        except tk.TclError:
            pass

    def deny(self) -> None:
        self._finish("deny")

    def _finish(self, choice: str) -> None:
        if self._done:
            return
        self._done = True
        self.close()
        self._on_choice(choice)


class TextViewer(Dialog):
    def __init__(self, parent, theme: Theme, title: str, content: str):
        super().__init__(parent, theme, title, 760, 540)
        c = theme.c
        self._content = content
        body = ReadOnlyText(self, theme, bg=c["code_bg"], fg=c["code_fg"], font=theme.font_mono, height=20, padx=14, pady=10)
        buttons = tk.Frame(self, bg=c["panel"])
        buttons.pack(side="bottom", fill="x", padx=16, pady=12)
        FlatButton(buttons, theme, "Закрыть", self.close, kind="ghost").pack(side="right")
        self.copy_btn = FlatButton(buttons, theme, "Копировать всё", self._copy, kind="ghost")
        self.copy_btn.pack(side="right", padx=8)
        body.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        body.set_content(content)
        self.ready()

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._content)
        self.copy_btn.set_text("Скопировано ✓")


class SettingsDialog(Dialog):
    def __init__(self, parent, theme: Theme, *, has_key: bool, theme_name: str, confirm_enabled: bool,
                 on_save_key: Callable[[str], None], on_set_theme: Callable[[str], None],
                 on_set_confirm: Callable[[bool], None]):
        super().__init__(parent, theme, "Настройки", 560, 600, modal=True, resizable=False)
        c = theme.c
        self._on_save_key, self._on_set_theme, self._on_set_confirm = on_save_key, on_set_theme, on_set_confirm
        wrap = tk.Frame(self, bg=c["panel"])
        wrap.pack(fill="both", expand=True, padx=26, pady=20)
        _label(wrap, theme, "Настройки", "title").pack(anchor="w", pady=(0, 14))

        # --- API key
        _label(wrap, theme, "OPENROUTER API KEY", "section").pack(anchor="w")
        self.key_var = tk.StringVar()
        row = tk.Frame(wrap, bg=c["panel"])
        row.pack(fill="x", pady=(6, 4))
        frame, self.key_entry = make_entry(row, theme, textvariable=self.key_var, show="•")
        frame.pack(side="left", fill="x", expand=True)
        self.show_btn = FlatButton(row, theme, "Показать", self._toggle_show, kind="ghost", padx=12, pady=8)
        self.show_btn.pack(side="left", padx=(8, 0))
        FlatButton(row, theme, "Сохранить", self._save_key, kind="accent", padx=14, pady=8).pack(side="left", padx=(8, 0))
        self.key_entry.bind("<Return>", lambda e: self._save_key())
        self.key_status = tk.Label(wrap, bg=c["panel"], font=theme.font_small, anchor="w")
        self.key_status.pack(fill="x")
        self._show_key_status(has_key)
        link = tk.Label(wrap, text="Получить ключ: openrouter.ai/keys", bg=c["panel"], fg=c["accent"],
                        font=theme.font_small, cursor="hand2")
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<ButtonRelease-1>", lambda e: webbrowser.open("https://openrouter.ai/keys"))
        _label(wrap, theme, "Ключ сохраняется локально в файле .env рядом с проектом.", "muted").pack(anchor="w", pady=(2, 0))

        tk.Frame(wrap, bg=c["border"], height=1).pack(fill="x", pady=18)

        # --- Тема
        _label(wrap, theme, "ВНЕШНИЙ ВИД", "section").pack(anchor="w")
        seg = tk.Frame(wrap, bg=c["panel"])
        seg.pack(anchor="w", pady=(8, 0))
        for name, label in (("dark", "Тёмная"), ("light", "Светлая")):
            FlatButton(seg, theme, label, lambda n=name: self._set_theme(n),
                       kind="accent" if theme_name == name else "ghost", padx=22, pady=8).pack(side="left", padx=(0, 8))

        tk.Frame(wrap, bg=c["border"], height=1).pack(fill="x", pady=18)

        # --- Безопасность
        _label(wrap, theme, "БЕЗОПАСНОСТЬ", "section").pack(anchor="w")
        row2 = tk.Frame(wrap, bg=c["panel"])
        row2.pack(fill="x", pady=(8, 0))
        texts = tk.Frame(row2, bg=c["panel"])
        texts.pack(side="left", fill="x", expand=True)
        _label(texts, theme, "Подтверждать опасные действия", "bold").pack(anchor="w")
        tk.Label(texts, text="Удаление, команды, запись вне workspace, новые инструменты, ввод текста в браузере.",
                 bg=c["panel"], fg=c["muted"], font=theme.font_small, wraplength=theme.px(380), justify="left").pack(anchor="w")
        Switch(row2, theme, confirm_enabled, self._set_confirm, bg=c["panel"]).pack(side="right")
        self.warn = tk.Label(wrap, text="⚠  Подтверждения выключены: агент действует без вопросов.", bg=c["panel"],
                             fg=c["warn"], font=theme.font_small, anchor="w")
        if not confirm_enabled:
            self.warn.pack(fill="x", pady=(8, 0))

        buttons = tk.Frame(self, bg=c["panel"])
        buttons.pack(side="bottom", fill="x", padx=26, pady=18)
        FlatButton(buttons, theme, "Закрыть", self.close, kind="ghost", padx=20).pack(side="right")
        self.key_entry.focus_set()
        self.ready()

    def _show_key_status(self, ok: bool, text: str | None = None) -> None:
        c = self.theme.c
        self.key_status.configure(text=text or ("✓ Ключ задан" if ok else "● Ключ не задан"),
                                  fg=c["ok"] if ok else c["danger"])

    def _toggle_show(self) -> None:
        hidden = str(self.key_entry.cget("show")) != ""
        self.key_entry.configure(show="" if hidden else "•")
        self.show_btn.set_text("Скрыть" if hidden else "Показать")

    def _save_key(self) -> None:
        value = self.key_var.get().strip()
        if not value:
            return
        self._on_save_key(value)
        self.key_var.set("")
        self._show_key_status(True, "✓ Ключ сохранён")

    def _set_theme(self, name: str) -> None:
        self.close()
        self._on_set_theme(name)

    def _set_confirm(self, value: bool) -> None:
        self._on_set_confirm(value)
        if value:
            self.warn.pack_forget()
        else:
            self.warn.pack(fill="x", pady=(8, 0))


class MemoryDialog(Dialog):
    """Просмотр, поиск, добавление и удаление долговременной памяти агента."""

    def __init__(self, parent, theme: Theme, memory):
        super().__init__(parent, theme, "Память агента", 880, 660)
        c = theme.c
        self.memory = memory
        self._rows: list[dict[str, Any]] = []
        self.minsize(theme.px(700), theme.px(520))

        top = tk.Frame(self, bg=c["panel"])
        top.pack(fill="x", padx=20, pady=(18, 0))
        _label(top, theme, "Память агента", "title").pack(anchor="w")
        _label(top, theme, "Эти записи попадают в системный промпт каждого чата. Удаляй устаревшее и неверное.",
               "muted").pack(anchor="w", pady=(2, 10))
        search_row = tk.Frame(top, bg=c["panel"])
        search_row.pack(fill="x")
        self.search_var = tk.StringVar()
        frame, entry = make_entry(search_row, theme, textvariable=self.search_var)
        frame.pack(side="left", fill="x", expand=True)
        self.count_label = _label(search_row, theme, "", "muted")
        self.count_label.pack(side="left", padx=(12, 0))
        self.search_var.trace_add("write", lambda *_: self._fill())

        add = tk.Frame(self, bg=c["panel"])
        add.pack(side="bottom", fill="x", padx=20, pady=(0, 18))
        _label(add, theme, "НОВАЯ ЗАПИСЬ", "section").pack(anchor="w", pady=(0, 6))
        self.new_text = tk.Text(add, height=3, wrap="word", font=theme.font_ui, bg=c["input_bg"], fg=c["text"],
                                insertbackground=c["text"], relief="flat", borderwidth=0, padx=10, pady=8,
                                highlightthickness=1, highlightbackground=c["border"], highlightcolor=c["accent"],
                                selectbackground=c["select"], selectforeground=c["text"], undo=True)
        self.new_text.pack(fill="x")
        bottom = tk.Frame(add, bg=c["panel"])
        bottom.pack(fill="x", pady=(8, 0))
        self.tags_var = tk.StringVar()
        tag_frame, _tag_entry = make_entry(bottom, theme, textvariable=self.tags_var)
        tag_frame.pack(side="left", fill="x", expand=True)
        _label(bottom, theme, "← теги через запятую (необязательно)", "muted").pack(side="left", padx=8)
        FlatButton(bottom, theme, "Добавить", self._add, kind="accent", padx=18, pady=8).pack(side="right")

        self.preview = ReadOnlyText(self, theme, font=theme.font_ui, height=5)
        self.preview.pack(side="bottom", fill="x", padx=20, pady=14)
        pr = tk.Frame(self, bg=c["panel"])
        pr.pack(side="bottom", fill="x", padx=20, pady=(8, 0))
        FlatButton(pr, theme, "Удалить выбранное", self._delete, kind="ghost", padx=14, pady=5).pack(side="right")

        body = tk.Frame(self, bg=c["panel"])
        body.pack(fill="both", expand=True, padx=20, pady=(12, 0))
        self.tree = ttk.Treeview(body, columns=("id", "tags", "content"), show="headings", selectmode="browse", height=8)
        self.tree.heading("id", text="ID")
        self.tree.heading("tags", text="Теги")
        self.tree.heading("content", text="Содержимое")
        self.tree.column("id", width=theme.px(52), anchor="center", stretch=False)
        self.tree.column("tags", width=theme.px(160), stretch=False)
        self.tree.column("content", width=theme.px(560))
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
        self.tree.bind("<Delete>", lambda e: self._delete())
        self.refresh()
        entry.focus_set()
        self.ready()

    def refresh(self) -> None:
        self._rows = list(self.memory.get_all_memories())
        self._fill()

    def _fill(self) -> None:
        query = self.search_var.get().strip().lower()
        for item in self.tree.get_children():
            self.tree.delete(item)
        shown = 0
        for mem in self._rows:
            if query and query not in str(mem["content"]).lower() and query not in str(mem["tags"]).lower():
                continue
            shown += 1
            self.tree.insert("", "end", iid=str(mem["id"]),
                             values=(mem["id"], mem["tags"], str(mem["content"]).replace("\n", " ")[:300]))
        self.count_label.configure(text=f"{shown} из {len(self._rows)}")
        self.preview.set_content("Выбери запись, чтобы увидеть её полностью." if shown else "Ничего не найдено.")

    def _selected_id(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _show_selected(self) -> None:
        memory_id = self._selected_id()
        for mem in self._rows:
            if mem["id"] == memory_id:
                self.preview.set_content(mem["content"])
                return

    def _add(self) -> None:
        content = self.new_text.get("1.0", "end").strip()
        if not content:
            self.new_text.focus_set()
            return
        self.memory.remember(content, self.tags_var.get().strip())
        self.new_text.delete("1.0", "end")
        self.tags_var.set("")
        self.refresh()

    def _delete(self) -> None:
        memory_id = self._selected_id()
        if memory_id is not None:
            self.memory.delete_memory(memory_id)
            self.refresh()


class InventoryDialog(Dialog):
    """Инструменты и навыки агента: вкладки, поиск и подробное описание."""

    def __init__(self, parent, theme: Theme, tools):
        super().__init__(parent, theme, "Инструменты и навыки", 900, 620)
        c = theme.c
        self.tools = tools
        self._tool_rows = list(tools.describe_tools())
        self._skill_rows = list(tools.list_skills())
        self._tab = "tools"
        self.minsize(theme.px(700), theme.px(480))

        top = tk.Frame(self, bg=c["panel"])
        top.pack(fill="x", padx=20, pady=(18, 0))
        _label(top, theme, "Инструменты и навыки", "title").pack(anchor="w", pady=(0, 10))
        row = tk.Frame(top, bg=c["panel"])
        row.pack(fill="x")
        self.tab_tools = FlatButton(row, theme, "", lambda: self._set_tab("tools"), kind="accent", padx=16, pady=6)
        self.tab_tools.pack(side="left")
        self.tab_skills = FlatButton(row, theme, "", lambda: self._set_tab("skills"), kind="ghost", padx=16, pady=6)
        self.tab_skills.pack(side="left", padx=8)
        self.search_var = tk.StringVar()
        frame, entry = make_entry(row, theme, textvariable=self.search_var)
        frame.pack(side="right", fill="x", expand=True, padx=(16, 0))
        self.search_var.trace_add("write", lambda *_: self._fill())

        buttons = tk.Frame(self, bg=c["panel"])
        buttons.pack(side="bottom", fill="x", padx=20, pady=14)
        FlatButton(buttons, theme, "Закрыть", self.close, kind="ghost", padx=18).pack(side="right")
        self.open_btn = FlatButton(buttons, theme, "Открыть SKILL.md", self._open_skill, kind="ghost", padx=14)
        self.detail = ReadOnlyText(self, theme, font=theme.font_ui, height=6)
        self.detail.pack(side="bottom", fill="x", padx=20, pady=(10, 0))

        body = tk.Frame(self, bg=c["panel"])
        body.pack(fill="both", expand=True, padx=20, pady=(12, 0))
        self.tree = ttk.Treeview(body, columns=("name", "kind", "description"), show="headings", selectmode="browse")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
        self.tree.bind("<Double-1>", lambda e: self._open_skill())
        self._set_tab("tools")
        entry.focus_set()
        self.ready()

    def _set_tab(self, tab: str) -> None:
        self._tab = tab
        self.tab_tools.set_kind("accent" if tab == "tools" else "ghost")
        self.tab_skills.set_kind("accent" if tab == "skills" else "ghost")
        self.tab_tools.set_text(f"Инструменты ({len(self._tool_rows)})")
        self.tab_skills.set_text(f"Навыки ({len(self._skill_rows)})")
        px = self.theme.px
        if tab == "tools":
            self.tree.configure(columns=("name", "kind", "description"), displaycolumns=("name", "kind", "description"))
            for column, title, width in (("name", "Имя", 170), ("kind", "Тип", 120), ("description", "Описание", 560)):
                self.tree.heading(column, text=title)
                self.tree.column(column, width=px(width), stretch=(column == "description"))
            self.open_btn.pack_forget()
        else:
            self.tree.configure(columns=("name", "kind", "description"), displaycolumns=("name", "description"))
            self.tree.heading("name", text="Навык")
            self.tree.heading("description", text="Описание (двойной щелчок — открыть)")
            self.tree.column("name", width=px(190), stretch=False)
            self.tree.column("description", width=px(650), stretch=True)
            self.open_btn.pack(side="left")
        self._fill()

    def _rows(self) -> list[tuple[str, str, str]]:
        if self._tab == "tools":
            return [(i["name"], i["kind"], str(i["description"])) for i in self._tool_rows]
        return [(s["name"], "", str(s["description"])) for s in self._skill_rows]

    def _fill(self) -> None:
        query = self.search_var.get().strip().lower()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._visible: dict[str, tuple[str, str, str]] = {}
        for index, (name, kind, desc) in enumerate(self._rows()):
            if query and query not in name.lower() and query not in desc.lower():
                continue
            iid = str(index)
            self._visible[iid] = (name, kind, desc)
            self.tree.insert("", "end", iid=iid, values=(name, kind, desc.replace("\n", " ")[:220]))
        self.detail.set_content("Выбери строку, чтобы увидеть полное описание.")

    def _selected(self) -> tuple[str, str, str] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return getattr(self, "_visible", {}).get(selection[0])

    def _show_selected(self) -> None:
        row = self._selected()
        if row:
            self.detail.set_content(f"{row[0]}\n\n{row[2]}")

    def _open_skill(self) -> None:
        if self._tab != "skills":
            return
        row = self._selected()
        if not row:
            return
        try:
            content = self.tools.read_skill(row[0])
        except Exception as exc:  # noqa: BLE001
            content = f"Не удалось прочитать навык: {exc}"
        TextViewer(self, self.theme, f"Навык: {row[0]}", clip(content, 60000))