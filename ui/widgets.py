from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .theme import Theme

PREVIEW_KEYS = ("path", "file", "filename", "command", "cmd", "url", "query", "name", "action", "text", "input")


def clip(text: str, limit: int = 4000) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"\n… [показано {limit} из {len(text)} символов]"


def pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def summarize_args(args: Any, limit: int = 70) -> str:
    """Короткая подпись вызова инструмента для заголовка карточки (путь, команда, адрес…)."""
    if not isinstance(args, dict) or not args:
        return ""
    text = ""
    for key in PREVIEW_KEYS:
        value = args.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            text = " ".join(str(value).split())
            break
    else:
        first = next(iter(args.values()))
        if isinstance(first, (str, int, float)):
            text = " ".join(str(first).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_arguments(args: Any, limit: int = 3000) -> str:
    """Читаемый вид аргументов: многострочные значения показываются как есть, а не с \\n-экранированием."""
    if not isinstance(args, dict):
        return clip(pretty(args), limit)
    if not args:
        return "(без аргументов)"
    parts = []
    for key, value in args.items():
        if isinstance(value, str):
            if "\n" in value or len(value) > 90:
                body = "\n".join("    " + line for line in clip(value, limit).split("\n"))
                parts.append(f"{key}:\n{body}")
            else:
                parts.append(f"{key}: {value}")
        elif value is None or isinstance(value, (int, float, bool)):
            parts.append(f"{key}: {value}")
        else:
            parts.append(f"{key}: " + pretty(value).replace("\n", "\n    "))
    return "\n".join(parts)


def column_padding(width: int, max_column: int, minimum: int) -> int:
    """Боковой отступ, чтобы колонка чата и поле ввода были не шире max_column и стояли по центру."""
    return max(minimum, (width - max_column) // 2)


def fit_text_height(text: tk.Text, min_lines: int = 1, max_lines: int | None = None) -> None:
    """Подогнать высоту Text под число отображаемых строк (с учётом переноса слов)."""
    try:
        res = text.count("1.0", "end-1c", "displaylines")
    except tk.TclError:
        return
    if res is None:
        lines = 1
    elif isinstance(res, (tuple, list)):
        lines = int(res[0]) if res else 1
    else:
        lines = int(res)
    lines = max(min_lines, lines)
    if max_lines:
        lines = min(lines, max_lines)
    try:
        if int(text.cget("height")) != lines:
            text.configure(height=lines)
    except (tk.TclError, ValueError):
        pass


class Tooltip:
    """Всплывающая подсказка для кнопок-иконок."""

    def __init__(self, widget: tk.Misc, text: str, theme: Theme, delay: int = 450):
        self.widget, self.text, self.theme, self.delay = widget, text, theme, delay
        self._job: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        try:
            self._job = self.widget.after(self.delay, self._show)
        except tk.TclError:
            self._job = None

    def _show(self) -> None:
        self._job = None
        c = self.theme.c
        try:
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(
                tip, text=self.text, bg=c["card"], fg=c["text"], font=self.theme.font_small, padx=9, pady=5,
                highlightthickness=1, highlightbackground=c["border"],
            ).pack()
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _event=None) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class FlatButton(tk.Label):
    """Плоская кнопка на Label: одинаково выглядит на Windows/macOS/Linux и полностью подчиняется теме."""

    def __init__(self, parent, theme: Theme, text: str, command: Callable[[], None] | None = None,
                 kind: str = "ghost", font=None, padx: int = 14, pady: int = 7, anchor: str = "center",
                 tooltip: str | None = None):
        self.theme = theme
        self.kind = kind
        self._command = command
        self._enabled = True
        bg, hover, fg, hover_fg = self._palette(kind)
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font or theme.font_ui, padx=padx, pady=pady,
                         cursor="hand2", anchor=anchor)
        self._bg, self._hover, self._fg, self._hover_fg = bg, hover, fg, hover_fg
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_release)
        if tooltip:
            Tooltip(self, tooltip, theme)

    def _palette(self, kind: str) -> tuple[str, str, str, str]:
        c = self.theme.c
        return {
            "accent": (c["accent"], c["accent_hover"], c["accent_text"], c["accent_text"]),
            "danger": (c["danger"], c["danger_hover"], c["accent_text"], c["accent_text"]),
            "ghost": (c["card"], c["hover"], c["text"], c["text"]),
            "sidebar": (c["sidebar"], c["hover"], c["muted"], c["text"]),
            "bar": (c["bg"], c["hover"], c["muted"], c["text"]),
            "panel": (c["panel"], c["hover"], c["muted"], c["text"]),
        }.get(kind, (c["card"], c["hover"], c["text"], c["text"]))

    def _on_enter(self, _event=None) -> None:
        if self._enabled:
            self.configure(bg=self._hover, fg=self._hover_fg)

    def _on_leave(self, _event=None) -> None:
        self.configure(bg=self._bg, fg=self._fg if self._enabled else self.theme.c["muted"])

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self._bg, self._hover, self._fg, self._hover_fg = self._palette(kind)
        self.configure(bg=self._bg, fg=self._fg if self._enabled else self.theme.c["muted"])

    def set_text(self, text: str) -> None:
        self.configure(text=text)

    def set_fg(self, color: str) -> None:
        self._fg = self._hover_fg = color
        if self._enabled:
            self.configure(fg=color)

    def set_command(self, command: Callable[[], None] | None) -> None:
        self._command = command

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(fg=self._fg if enabled else self.theme.c["muted"], cursor="hand2" if enabled else "arrow", bg=self._bg)

    def _on_release(self, event) -> None:
        if not self._enabled or self._command is None:
            return
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()


class Switch(tk.Canvas):
    """Переключатель «вкл/выкл»."""

    def __init__(self, parent, theme: Theme, value: bool, command: Callable[[bool], None] | None = None,
                 bg: str | None = None):
        self.theme = theme
        self._sw, self._sh = theme.px(44), theme.px(24)
        super().__init__(parent, width=self._sw, height=self._sh, bg=bg or theme.c["panel"],
                         highlightthickness=0, borderwidth=0, cursor="hand2")
        self.value = bool(value)
        self._command = command
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self._draw()

    def toggle(self) -> None:
        self.value = not self.value
        self._draw()
        if self._command is not None:
            self._command(self.value)

    def _draw(self) -> None:
        self.delete("all")
        c = self.theme.c
        w, h = self._sw, self._sh
        pad = max(2, h // 10)
        track = c["accent"] if self.value else c["border"]
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(h / 2, 0, w - h / 2, h, fill=track, outline=track)
        d = h - 2 * pad
        x = w - pad - d if self.value else pad
        self.create_oval(x, pad, x + d, pad + d, fill="#ffffff", outline="#ffffff")


def make_entry(parent, theme: Theme, textvariable=None, show: str | None = None, bg: str | None = None):
    """Поле ввода с рамкой, подсвечивающейся при фокусе. Возвращает (рамка, Entry)."""
    c = theme.c
    bg = bg or c["input_bg"]
    frame = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=c["border"], highlightcolor=c["accent"])
    entry = tk.Entry(
        frame, textvariable=textvariable, show=show or "", bg=bg, fg=c["text"], insertbackground=c["text"],
        relief="flat", borderwidth=0, highlightthickness=0, font=theme.font,
        selectbackground=c["select"], selectforeground=c["text"],
    )
    entry.pack(fill="x", padx=10, pady=7)
    entry.bind("<FocusIn>", lambda e: frame.configure(highlightbackground=c["accent"]), add="+")
    entry.bind("<FocusOut>", lambda e: frame.configure(highlightbackground=c["border"]), add="+")
    return frame, entry


def configure_text_tags(text: tk.Text, theme: Theme) -> None:
    """Теги Markdown (используются лентой чата и окнами просмотра)."""
    c = theme.c
    px = theme.px
    bullet = theme.font.measure("\u2022  ")
    text.tag_configure("b", font=theme.font_bold)
    text.tag_configure("i", font=theme.font_italic)
    text.tag_configure("code", font=theme.font_mono, background=c["code_bg"], foreground=c["code_fg"])
    text.tag_configure("codeblock", font=theme.font_mono, background=c["code_bg"], foreground=c["code_fg"],
                       lmargin1=px(12), lmargin2=px(12), rmargin=px(12), spacing1=1, spacing3=1)
    text.tag_configure("h1", font=theme.font_h1, spacing1=px(10), spacing3=px(3))
    text.tag_configure("h2", font=theme.font_h2, spacing1=px(8), spacing3=px(2))
    text.tag_configure("h3", font=theme.font_h3, spacing1=px(6), spacing3=px(1))
    text.tag_configure("li", lmargin1=px(8), lmargin2=px(8) + bullet)
    text.tag_configure("li2", lmargin1=px(30), lmargin2=px(30) + bullet)
    text.tag_configure("quote", foreground=c["muted"], lmargin1=px(14), lmargin2=px(14))
    text.tag_configure("url", foreground=c["accent"], underline=True)
    text.tag_raise("sel")


class ReadOnlyText(tk.Frame):
    """Текст только для чтения с выделением, копированием и скроллбаром."""

    def __init__(self, parent, theme: Theme, bg: str | None = None, fg: str | None = None, font=None,
                 height: int = 8, padx: int = 12, pady: int = 8, markdown_tags: bool = False):
        c = theme.c
        bg = bg or c["code_bg"]
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=c["border"])
        self.theme = theme
        self.text = tk.Text(
            self, wrap="word", height=height, width=1, borderwidth=0, highlightthickness=0, relief="flat",
            padx=padx, pady=pady, bg=bg, fg=fg or c["text"], insertbackground=c["text"],
            selectbackground=c["select"], selectforeground=c["text"], inactiveselectbackground=c["select"],
            font=font or theme.font_mono, cursor="xterm", state="disabled", spacing2=2,
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        if markdown_tags:
            configure_text_tags(self.text, theme)
        # В отключённом Text щелчок не даёт фокус — без этого Ctrl+C после выделения мышью не работает.
        self.text.bind("<Button-1>", lambda e: self.text.focus_set(), add="+")

    def set_content(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", str(content).rstrip("\n"))
        self.text.configure(state="disabled")
        self.text.yview_moveto(0.0)