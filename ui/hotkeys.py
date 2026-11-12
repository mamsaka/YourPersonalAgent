"""Горячие клавиши, не зависящие от раскладки (Ctrl+C/V/X/A/Z/Y на русской раскладке и др.).

Почему не работало раньше: на русской раскладке Tk присылает keysym «Cyrillic_es» (а не «c»), поэтому
привязки <Control-c> не срабатывают, а привязки с буквой «с» в тексте скрипта не совпадают с keysym.
Здесь мы ловим <Control-KeyPress> на уровне классов Text/Entry/Combobox, определяем нужное действие по
keysym (кириллица) или аппаратному коду клавиши и генерируем стандартные виртуальные события
<<Copy>>, <<Paste>> и т.д. Это работает во всех полях ввода, включая стандартные диалоги.
"""
from __future__ import annotations

from typing import Callable

ACTIONS = {"c": "Copy", "v": "Paste", "x": "Cut", "a": "SelectAll", "z": "Undo", "y": "Redo"}

# Клавиши ЙЦУКЕН, находящиеся на тех же физических местах, что C V X A Z Y
CYRILLIC_KEYSYMS = {
    "cyrillic_es": "c", "cyrillic_em": "v", "cyrillic_che": "x",
    "cyrillic_ef": "a", "cyrillic_ya": "z", "cyrillic_en": "y",
}
# Запасной путь, если keysym неизвестен (другая раскладка): аппаратные коды клавиш
WINDOWS_KEYCODES = {67: "c", 86: "v", 88: "x", 65: "a", 90: "z", 89: "y"}
X11_KEYCODES = {54: "c", 55: "v", 53: "x", 38: "a", 52: "z", 29: "y"}


def resolve_action(keysym: str, keycode: int = 0, windowing: str = "", shift: bool = False) -> str | None:
    """Вернуть имя виртуального события (Copy, Paste, ...) для Ctrl+клавиша или None."""
    key = (keysym or "").lower()
    letter = CYRILLIC_KEYSYMS.get(key) or (key if key in ACTIONS else None)
    if letter is None:
        if windowing == "win32":
            letter = WINDOWS_KEYCODES.get(keycode)
        elif windowing == "x11":
            letter = X11_KEYCODES.get(keycode)
    if letter is None:
        return None
    if letter == "z" and shift:
        return "Redo"
    return ACTIONS[letter]


def install(root, get_menu_colors: Callable[[], dict] | None = None) -> None:
    """Подключить горячие клавиши и контекстное меню ко всем полям ввода приложения."""
    import tkinter as tk

    windowing = str(root.tk.call("tk", "windowingsystem"))

    def on_ctrl_key(event):
        action = resolve_action(event.keysym, event.keycode, windowing, bool(event.state & 0x1))
        if action is None:
            return None
        try:
            event.widget.event_generate(f"<<{action}>>")
        except tk.TclError:
            pass
        return "break"

    # Привязка к классу (а не bind_all): на латинской раскладке побеждает более специфичная стандартная
    # привязка Tk, поэтому действие не выполняется дважды.
    for cls in ("Text", "Entry", "TEntry", "TCombobox", "TSpinbox"):
        root.bind_class(cls, "<Control-KeyPress>", on_ctrl_key, add="+")

    def show_menu(event):
        widget = event.widget
        try:
            widget.focus_set()
            colors = get_menu_colors() if get_menu_colors else {}
            menu = tk.Menu(
                widget, tearoff=0,
                bg=colors.get("card", "#222222"), fg=colors.get("text", "#ffffff"),
                activebackground=colors.get("select", "#444444"), activeforeground=colors.get("text", "#ffffff"),
                borderwidth=0,
            )
            try:
                readonly = str(widget.cget("state")) in ("disabled", "readonly")
            except tk.TclError:
                readonly = False
            for label, action, needs_edit in (
                ("Вырезать", "Cut", True), ("Копировать", "Copy", False),
                ("Вставить", "Paste", True), ("Выделить всё", "SelectAll", False),
            ):
                state = "disabled" if (needs_edit and readonly) else "normal"
                menu.add_command(label=label, state=state, command=lambda a=action: widget.event_generate(f"<<{a}>>"))
            menu.tk_popup(event.x_root, event.y_root)
            menu.grab_release()
        except tk.TclError:
            pass
        return "break"

    buttons = ["<Button-3>"] + (["<Button-2>", "<Control-Button-1>"] if windowing == "aqua" else [])
    for cls in ("Text", "Entry", "TEntry", "TCombobox"):
        for sequence in buttons:
            root.bind_class(cls, sequence, show_menu, add="+")


# ------------------------------------------------------------------ сочетания приложения
# Ctrl+N — новый чат, Ctrl+F — поиск по чатам, Ctrl+, — настройки. Работают на любой раскладке.
SHORTCUTS = {"n": "new_chat", "f": "search", "comma": "settings"}
CYRILLIC_SHORTCUTS = {"cyrillic_te": "n", "cyrillic_a": "f", "cyrillic_be": "comma"}
WINDOWS_SHORTCUT_CODES = {78: "n", 70: "f", 188: "comma"}
X11_SHORTCUT_CODES = {57: "n", 41: "f", 59: "comma"}


def resolve_shortcut(keysym: str, keycode: int = 0, windowing: str = "") -> str | None:
    """Вернуть имя команды приложения для Ctrl+клавиша или None."""
    key = (keysym or "").lower()
    letter = CYRILLIC_SHORTCUTS.get(key) or (key if key in SHORTCUTS else None)
    if letter is None:
        if windowing == "win32":
            letter = WINDOWS_SHORTCUT_CODES.get(keycode)
        elif windowing == "x11":
            letter = X11_SHORTCUT_CODES.get(keycode)
    return SHORTCUTS.get(letter) if letter else None


def install_shortcuts(root, handlers: dict[str, Callable[[], None]]) -> None:
    """Подключить глобальные сочетания приложения (handlers: имя команды -> функция)."""
    windowing = str(root.tk.call("tk", "windowingsystem"))
    alt_mask = 0x20000 if windowing == "win32" else 0x8  # Alt/AltGr: не перехватываем

    def on_key(event):
        if event.state & alt_mask:
            return None
        name = resolve_shortcut(event.keysym, event.keycode, windowing)
        handler = handlers.get(name) if name else None
        if handler is None:
            return None
        handler()
        return "break"

    root.bind_all("<Control-KeyPress>", on_key, add="+")