from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES: dict[str, dict[str, str]] = {
    "dark": dict(
        bg="#0f1115", sidebar="#0b0d10", panel="#161920", card="#1a1e26", card_user="#1d2a44",
        border="#262b36", text="#e8eaf0", muted="#8b93a5", accent="#6a8dff", accent_hover="#86a3ff",
        accent_text="#ffffff", danger="#ef5b66", danger_hover="#f57882", danger_bg="#2a1a1e",
        warn="#e8ac4a", ok="#4cc38a", code_bg="#0a0c10", code_fg="#d7dff0", input_bg="#161920",
        select="#2d4a85", hover="#1e222c", reasoning="#a78bfa", divider="#232833", row_selected="#1d2433",
    ),
    "light": dict(
        bg="#ffffff", sidebar="#f4f5f8", panel="#f8f9fb", card="#ffffff", card_user="#dbe8ff",
        border="#e1e4ea", text="#1b1f2a", muted="#6b7280", accent="#2f6feb", accent_hover="#1d5bd1",
        accent_text="#ffffff", danger="#d92d3a", danger_hover="#b81f2b", danger_bg="#fdecee",
        warn="#b7791f", ok="#1f9d63", code_bg="#f0f2f6", code_fg="#1b1f2a", input_bg="#ffffff",
        select="#bcd3ff", hover="#e9ebf0", reasoning="#7c3aed", divider="#e8eaef", row_selected="#e2e8f7",
    ),
}

SANS = ("Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans", "Arial")
MONO = ("Cascadia Mono", "Consolas", "JetBrains Mono", "SF Mono", "Menlo", "Ubuntu Mono", "DejaVu Sans Mono", "Courier New")


def _pick(candidates: tuple[str, ...], available: set[str], fallback: str) -> str:
    lowered = {name.lower(): name for name in available}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return fallback


class Theme:
    """Палитра + шрифты + настройка ttk-стилей. При смене темы приложение пересоздаёт интерфейс."""

    def __init__(self, root: tk.Misc, name: str = "dark"):
        self.name = name if name in PALETTES else "dark"
        self.c = dict(PALETTES[self.name])
        try:
            self.scale = min(3.0, max(1.0, float(root.winfo_fpixels("1i")) / 96.0))
        except (tk.TclError, TypeError, ValueError):
            self.scale = 1.0
        families = set(tkfont.families(root))
        sans = _pick(SANS, families, "TkDefaultFont")
        mono = _pick(MONO, families, "TkFixedFont")

        def make(family: str, size: int, **kw) -> tkfont.Font:
            return tkfont.Font(root=root, family=family, size=size, **kw)

        self.font = make(sans, 11)
        self.font_bold = make(sans, 11, weight="bold")
        self.font_italic = make(sans, 11, slant="italic")
        self.font_ui = make(sans, 10)
        self.font_ui_bold = make(sans, 10, weight="bold")
        self.font_small = make(sans, 9)
        self.font_small_bold = make(sans, 9, weight="bold")
        self.font_title = make(sans, 14, weight="bold")
        self.font_hero = make(sans, 22, weight="bold")
        self.font_h1 = make(sans, 15, weight="bold")
        self.font_h2 = make(sans, 13, weight="bold")
        self.font_h3 = make(sans, 11, weight="bold")
        self.font_mono = make(mono, 10)
        self.font_mono_small = make(mono, 9)
        # служебные шрифты для разделителей между блоками чата
        self.font_gap = make(sans, 8)
        self.font_rule = make(sans, 1)

    def px(self, value: float) -> int:
        """Пиксели с учётом масштаба экрана (HiDPI)."""
        return int(round(value * self.scale))

    def apply(self, root: tk.Tk) -> None:
        c = self.c
        root.configure(bg=c["bg"])
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            ".", background=c["bg"], foreground=c["text"], fieldbackground=c["input_bg"],
            bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
            troughcolor=c["bg"], focuscolor=c["bg"], font=self.font_ui,
        )
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure(
            "TCombobox", fieldbackground=c["input_bg"], background=c["panel"], foreground=c["text"],
            arrowcolor=c["muted"], bordercolor=c["border"], lightcolor=c["input_bg"], darkcolor=c["input_bg"],
            padding=5, selectbackground=c["input_bg"], selectforeground=c["text"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["input_bg"])], foreground=[("readonly", c["text"])],
            selectbackground=[("readonly", c["input_bg"])], selectforeground=[("readonly", c["text"])],
            bordercolor=[("focus", c["accent"])],
        )
        root.option_add("*TCombobox*Listbox.background", c["input_bg"])
        root.option_add("*TCombobox*Listbox.foreground", c["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", c["select"])
        root.option_add("*TCombobox*Listbox.selectForeground", c["text"])
        root.option_add("*TCombobox*Listbox.font", self.font_ui)
        # Тонкий скроллбар без стрелок
        try:
            style.layout(
                "Vertical.TScrollbar",
                [("Vertical.Scrollbar.trough", {
                    "sticky": "ns",
                    "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
                })],
            )
        except tk.TclError:
            pass
        try:
            style.configure(
                "Vertical.TScrollbar", background=c["border"], troughcolor=c["bg"], bordercolor=c["bg"],
                arrowcolor=c["muted"], lightcolor=c["border"], darkcolor=c["border"], gripcount=0,
                arrowsize=self.px(11), width=self.px(11),
            )
        except tk.TclError:
            style.configure("Vertical.TScrollbar", background=c["border"], troughcolor=c["bg"], arrowsize=self.px(11))
        style.map("Vertical.TScrollbar", background=[("active", c["muted"])])
        style.configure(
            "Treeview", background=c["card"], fieldbackground=c["card"], foreground=c["text"],
            bordercolor=c["border"], borderwidth=0, rowheight=self.px(28), font=self.font_ui,
        )
        style.configure("Treeview.Heading", background=c["panel"], foreground=c["muted"], relief="flat", font=self.font_small_bold)
        style.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["text"])])
        style.map("Treeview.Heading", background=[("active", c["hover"])])