from __future__ import annotations

import tkinter as tk
import webbrowser
import random
from tkinter import ttk
from typing import Any, Callable

from . import markdown
from .theme import Theme
from .widgets import FlatButton, clip, column_padding, configure_text_tags, format_arguments, summarize_args

ROLE_TITLES = {"user": "Вы", "assistant": "Агент", "error": "Ошибка", "system": "Система"}
MAX_COLUMN = 900  # максимальная ширина колонки чата, px


class ChatView(tk.Frame):
    """Лента чата на одном Text-виджете: один источник прокрутки, выделение через все сообщения."""

    def __init__(self, parent, theme: Theme):
        c = theme.c
        super().__init__(parent, bg=c["bg"])
        self.theme = theme
        self._follow_tail = True
        self._restore_job: str | None = None
        self._wheel_remainder = 0.0
        self._has_content = False
        self._counter = 0
        self._pending_tools: list[dict[str, Any]] = []
        self._tool_records: list[dict[str, Any]] = []
        self._raw: dict[int, str] = {}
        self._card_tags: list[str] = []
        self._marks: list[str] = []
        self._pad = theme.px(32)
        self._jump_visible = False
        self.welcome_visible = False

        self.text = tk.Text(
            self, wrap="word", state="disabled", takefocus=True, undo=False,
            bg=c["bg"], fg=c["text"], insertbackground=c["text"], insertwidth=0,
            selectbackground=c["select"], selectforeground=c["text"], inactiveselectbackground=c["select"],
            relief="flat", borderwidth=0, highlightthickness=0,
            padx=self._pad, pady=theme.px(18), spacing1=0, spacing2=theme.px(3), spacing3=0, font=theme.font,
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._on_scrollbar)
        self.text.configure(yscrollcommand=self._on_text_scroll)
        self.scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.jump_btn = FlatButton(self, theme, "↓  К последним", self._jump_to_end, kind="accent", padx=14, pady=5,
                                   font=theme.font_small_bold)

        self._configure_tags()
        self.text.bind("<MouseWheel>", self._on_wheel, add="+")
        self.text.bind("<Button-4>", self._on_wheel, add="+")
        self.text.bind("<Button-5>", self._on_wheel, add="+")
        for key in ("Prior", "Next", "Home", "End", "Up", "Down"):
            self.text.bind(f"<KeyRelease-{key}>", self._on_keyboard_scroll, add="+")
        # В отключённом Text щелчок не даёт фокус — без этого Ctrl+C после выделения мышью не работает.
        self.text.bind("<Button-1>", lambda e: self.text.focus_set(), add="+")
        self.text.bind("<Configure>", self._on_resize, add="+")

    # ------------------------------ оформление ------------------------------

    def _configure_tags(self) -> None:
        c, th = self.theme.c, self.theme
        px = th.px
        t = self.text
        t.tag_configure("gap", font=th.font_gap)
        t.tag_configure("rule", font=th.font_rule, background=c["divider"])
        for role, color in (("user", c["accent"]), ("assistant", c["ok"]), ("error", c["danger"])):
            t.tag_configure(f"hdr_{role}", font=th.font_small_bold, foreground=color, spacing3=px(4))
        t.tag_configure("hdr", spacing3=px(4))
        t.tag_configure("hdr_copy", font=th.font_small, foreground=c["muted"])
        t.tag_configure("body", spacing3=0)
        t.tag_configure("body_error", foreground=c["danger"])
        t.tag_configure("sys", font=th.font_small, foreground=c["muted"], justify="center",
                        lmargin1=px(20), lmargin2=px(20), rmargin=px(20))
        t.tag_configure("hero", font=th.font_hero, spacing1=px(30), spacing3=px(4))
        t.tag_configure("hero_sub", foreground=c["muted"], spacing3=px(16), spacing2=px(3))
        t.tag_configure("hero_label", font=th.font_small_bold, foreground=c["muted"], spacing1=px(8), spacing3=px(6))
        t.tag_configure("suggest", foreground=c["accent"], lmargin1=px(4), lmargin2=px(24), spacing1=px(4), spacing3=px(4))
        t.tag_configure("note", foreground=c["warn"], spacing3=px(6))
        t.tag_configure("arrow", foreground=c["muted"])
        t.tag_configure("tool_name", font=th.font_small_bold, foreground=c["text"])
        t.tag_configure("tool_sum", font=th.font_mono_small, foreground=c["muted"])
        configure_text_tags(t, th)
        t.tag_bind("url", "<Button-1>", self._open_url)
        t.tag_bind("url", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("url", "<Leave>", lambda e: t.configure(cursor=""))

    def _on_resize(self, event) -> None:
        pad = column_padding(event.width, self.theme.px(MAX_COLUMN), self.theme.px(28))
        if abs(pad - self._pad) > 2:
            self._pad = pad
            try:
                self.text.configure(padx=pad)
            except tk.TclError:
                pass

    # ------------------------------ прокрутка ------------------------------

    def _on_text_scroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)

    def _at_bottom(self) -> bool:
        try:
            _first, last = self.text.yview()
            return float(last) >= 0.995
        except (tk.TclError, TypeError, ValueError):
            return self._follow_tail

    def _set_follow(self, value: bool) -> None:
        self._follow_tail = value
        show = not value
        if show and not self._jump_visible:
            self.jump_btn.place(relx=0.5, rely=1.0, y=-self.theme.px(14), anchor="s")
            self._jump_visible = True
        elif not show and self._jump_visible:
            self.jump_btn.place_forget()
            self._jump_visible = False

    def _jump_to_end(self) -> None:
        self.scroll_to_bottom(force=True)

    def _cancel_restore(self) -> None:
        if self._restore_job is not None:
            try:
                self.after_cancel(self._restore_job)
            except tk.TclError:
                pass
            self._restore_job = None

    def _on_scrollbar(self, *args) -> None:
        self._cancel_restore()
        self.text.yview(*args)
        self._set_follow(self._at_bottom())

    def _on_wheel(self, event):
        self._cancel_restore()
        number = getattr(event, "num", None)
        if number == 4:
            units = -3
        elif number == 5:
            units = 3
        else:
            delta = float(getattr(event, "delta", 0) or 0)
            if self.winfo_toplevel().tk.call("tk", "windowingsystem") == "aqua":
                amount = -delta * 2.0
            else:
                amount = -delta * 3.0 / 120.0
            self._wheel_remainder += amount
            units = int(self._wheel_remainder)
            self._wheel_remainder -= units
        if units:
            self.text.yview_scroll(units, "units")
            self._set_follow(self._at_bottom())
        return "break"

    def _on_keyboard_scroll(self, _event=None) -> None:
        self.after_idle(lambda: self._set_follow(self._at_bottom()))

    def owns(self, widget) -> bool:
        try:
            path, text_path = str(widget), str(self.text)
            return path == text_path or path.startswith(text_path + ".")
        except Exception:  # noqa: BLE001
            return False

    def scroll_units(self, units: int) -> None:
        if units:
            self._cancel_restore()
            self.text.yview_scroll(units, "units")
            self._set_follow(self._at_bottom())

    def scroll_to_bottom(self, force: bool = False) -> None:
        if force:
            self._set_follow(True)
        if self._follow_tail:
            self._cancel_restore()
            self._schedule_view_restore()

    def _schedule_view_restore(self) -> None:
        if self._restore_job is not None or not self._follow_tail:
            return
        try:
            self._restore_job = self.after_idle(self._restore_tail)
        except tk.TclError:
            self._restore_job = None

    def _restore_tail(self) -> None:
        self._restore_job = None
        if not self._follow_tail:
            return
        try:
            self.text.yview_moveto(1.0)
            # Tk досчитывает высоту строк лениво — повторяем, чтобы действительно оказаться внизу.
            self.after(60, self._restore_tail_again)
        except tk.TclError:
            pass

    def _restore_tail_again(self) -> None:
        if not self._follow_tail:
            return
        try:
            self.text.yview_moveto(1.0)
        except tk.TclError:
            pass

    # ------------------------------ запись ------------------------------

    def _begin_write(self) -> None:
        self.text.configure(state="normal")

    def _end_write(self) -> None:
        self.text.configure(state="disabled")
        self._schedule_view_restore()

    def _put(self, text: str, *tags: str) -> None:
        self.text.insert("end-1c", text, tags)

    def _block_gap(self, rule: bool = False) -> None:
        if not self._has_content:
            self._has_content = True
            return
        self._put("\n", "gap")
        if rule:
            self._put("\n", "rule")
            self._put("\n", "gap")

    def _insert_markdown(self, content: str, base_tag: str) -> None:
        for tag, piece in markdown.parse(content):
            tags = (base_tag, *tag.split()) if tag else (base_tag,)
            self._put(piece, *tags)

    def _mark(self, name: str, gravity: str = "left") -> None:
        self.text.mark_set(name, "end-1c")
        self.text.mark_gravity(name, gravity)
        self._marks.append(name)

    def _replace_marked_text(self, start_mark: str, end_mark: str, value: str, *tags: str) -> None:
        start = self.text.index(start_mark)
        self.text.delete(start_mark, end_mark)
        self.text.mark_set(start_mark, start)
        self.text.mark_gravity(start_mark, "left")
        self.text.mark_set(end_mark, start)
        self.text.mark_gravity(end_mark, "right")  # на время вставки — чтобы метка оказалась после текста
        self.text.insert(start, value, tags)
        self.text.mark_gravity(end_mark, "left")

    def _hover_link(self, tag: str, on_color: str | None = None) -> None:
        self.text.tag_bind(tag, "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind(tag, "<Leave>", lambda e: self.text.configure(cursor=""))

    # ------------------------------ сообщения ------------------------------

    def add_message(self, role: str, text: str):
        role = role if role in ROLE_TITLES else "assistant"
        text = str(text)
        self._counter += 1
        number = self._counter
        self._begin_write()
        if role == "system":
            self._block_gap()
            self._put(text.strip(), "sys")
            self._put("\n", "sys")
            self._end_write()
            return None
        self._block_gap(rule=(role == "user"))
        self._put(f"●  {ROLE_TITLES[role]}", "hdr", f"hdr_{role}")
        if role == "assistant":
            self._raw[number] = text
            copy_tag = f"__chat_copy_{number}"
            self._card_tags.append(copy_tag)
            self._put("      ", "hdr")
            self._mark(f"__chat_copy_{number}_s")
            self._put("Копировать", "hdr", "hdr_copy", copy_tag)
            self._mark(f"__chat_copy_{number}_e")
            self.text.tag_bind(copy_tag, "<ButtonRelease-1>", lambda e, n=number: self._copy_message(n))
            self._hover_link(copy_tag)
        self._put("\n", "hdr")
        if role == "assistant":
            self._insert_markdown(text, "body")
        elif role == "error":
            self._put(text.rstrip("\n"), "body", "body_error")
        else:
            self._put(text.rstrip("\n"), "body")
        self._put("\n", "body")
        self._end_write()
        return None

    def _copy_message(self, number: int) -> None:
        if self.text.tag_ranges("sel"):
            return
        raw = self._raw.get(number)
        if raw is None:
            return
        self.clipboard_clear()
        self.clipboard_append(raw)
        self._set_copy_label(number, "Скопировано ✓", self.theme.c["ok"])
        self.after(1400, lambda: self._set_copy_label(number, "Копировать", None))

    def _set_copy_label(self, number: int, label: str, color: str | None) -> None:
        tag = f"__chat_copy_{number}"
        try:
            self.text.tag_configure(tag, foreground=color or self.theme.c["muted"])
            self.text.configure(state="normal")
            self._replace_marked_text(f"__chat_copy_{number}_s", f"__chat_copy_{number}_e", label, "hdr", "hdr_copy", tag)
            self.text.configure(state="disabled")
        except tk.TclError:
            pass

    def _open_url(self, event) -> None:
        if self.text.tag_ranges("sel"):
            return
        try:
            index = self.text.index(f"@{event.x},{event.y}")
            found = self.text.tag_prevrange("url", f"{index}+1c")
            if found and self.text.compare(found[0], "<=", index) and self.text.compare(index, "<", found[1]):
                webbrowser.open(self.text.get(found[0], found[1]))
        except tk.TclError:
            pass

    # ------------------------------ раскрывающиеся карточки ------------------------------

    def _append_expandable(self, kind: str, title: str, summary: str, body: str, status: str = "",
                           status_color: str | None = None, tool_name: str | None = None,
                           tool_args: str = "") -> dict[str, Any]:
        c, th = self.theme.c, self.theme
        self._counter += 1
        prefix = f"__chat_{kind}_{self._counter}"
        head_tag, body_tag, status_tag = f"{prefix}_head", f"{prefix}_body", f"{prefix}_status"
        self._card_tags.extend((head_tag, body_tag, status_tag))
        names = {key: f"{prefix}_{key}" for key in ("arrow_s", "arrow_e", "st_s", "st_e", "bd_s", "bd_e")}

        self._begin_write()
        self._block_gap()
        head_color = c["reasoning"] if kind == "reasoning" else c["muted"]
        self.text.tag_configure(head_tag, foreground=head_color, font=th.font_small, spacing1=th.px(2), spacing3=th.px(2))
        self.text.tag_configure(status_tag, foreground=status_color or c["warn"], font=th.font_small)
        self.text.tag_configure(body_tag, elide=True, font=th.font_mono_small, foreground=c["code_fg"],
                                lmargin1=th.px(22), lmargin2=th.px(22), rmargin=th.px(10), spacing1=th.px(2))

        self._mark(names["arrow_s"])
        self._put("▸ ", head_tag, "arrow")
        self._mark(names["arrow_e"])
        icon = "✦ " if kind == "reasoning" else "⚙ "
        self._put(f"{icon}{title}", head_tag, "tool_name")
        if summary:
            self._put(f"   {summary}", head_tag, "tool_sum")
        self._put("   ·   ", head_tag)
        self._mark(names["st_s"])
        self._put(status, head_tag, status_tag)
        self._mark(names["st_e"])
        self._put("\n", head_tag)

        self._mark(names["bd_s"])
        self._put(body, body_tag)
        self._mark(names["bd_e"])
        self._put("\n", body_tag)

        record: dict[str, Any] = {
            "name": tool_name, "args": tool_args, "head_tag": head_tag, "body_tag": body_tag,
            "status_tag": status_tag, "expanded": False, **names,
        }
        self.text.tag_bind(head_tag, "<ButtonRelease-1>", lambda e, r=record: self._toggle(r))
        self._hover_link(head_tag)
        self._tool_records.append(record)
        self._end_write()
        return record

    def _toggle(self, record: dict[str, Any]) -> None:
        if self.text.tag_ranges("sel"):
            return
        self._set_expanded(record, not record["expanded"])

    def _set_expanded(self, record: dict[str, Any], expanded: bool) -> None:
        if record["expanded"] == expanded:
            return
        record["expanded"] = expanded
        try:
            self.text.configure(state="normal")
            self.text.tag_configure(record["body_tag"], elide=not expanded)
            self._replace_marked_text(record["arrow_s"], record["arrow_e"], "▾ " if expanded else "▸ ",
                                      record["head_tag"], "arrow")
            self.text.configure(state="disabled")
        except tk.TclError:
            return
        self._schedule_view_restore()

    def add_reasoning(self, text: str) -> None:
        self._append_expandable("reasoning", "Рассуждения модели", "", clip(text, 8000), status="развернуть")

    def add_tool_call(self, name: str, args: Any):
        record = self._append_expandable(
            "tool", name, summarize_args(args),
            f"Аргументы:\n{clip(format_arguments(args), 6000)}\n\nРезультат появится после завершения.",
            status="выполняется…", status_color=self.theme.c["warn"], tool_name=name, tool_args=format_arguments(args),
        )
        self._pending_tools.append(record)
        return record

    def finish_tool(self, name: str, result: Any, error: bool = False) -> None:
        c = self.theme.c
        record = next((item for item in reversed(self._pending_tools) if item.get("name") == name), None)
        if record is None:
            record = self.add_tool_call(name, {})
        if record in self._pending_tools:
            self._pending_tools.remove(record)
        self._begin_write()
        status = "ошибка" if error else "готово ✓"
        self.text.tag_configure(record["status_tag"], foreground=c["danger"] if error else c["ok"])
        self._replace_marked_text(record["st_s"], record["st_e"], status, record["head_tag"], record["status_tag"])
        label = "Ошибка" if error else "Результат"
        body = f"Аргументы:\n{clip(record.get('args', ''), 6000)}\n\n{label}:\n{clip(format_result(result), 10000)}"
        self._replace_marked_text(record["bd_s"], record["bd_e"], body, record["body_tag"])
        self._end_write()
        if error:
            self._set_expanded(record, True)

    def add_tool_history(self, name: str, content: str) -> None:
        self._append_expandable("tool", name, "", clip(content, 10000), status="из истории",
                                status_color=self.theme.c["muted"], tool_name=name, tool_args="")

    # ------------------------------ пустой экран ------------------------------

    def show_welcome(self, title: str, subtitle: str, suggestions: list[str], suggests: int, on_pick: Callable[[str], None],
                     notices: list[tuple[str, Callable[[], None] | None]] | None = None) -> None:
        self._begin_write()
        self._has_content = True
        self._put(title + "\n", "hero")
        self._put(subtitle + "\n", "hero_sub")
        for index, (note, action) in enumerate(notices or []):
            tag = f"__chat_note_{index}"
            self._card_tags.append(tag)
            self._put(f"⚠  {note}\n", "note", tag)
            if action is not None:
                self.text.tag_bind(tag, "<ButtonRelease-1>", lambda e, a=action: a())
                self._hover_link(tag)
        self._put("Можно начать с:\n", "hero_label")
        # for index, suggestion in enumerate(suggestions):
        #     tag = f"__chat_suggest_{index}"
        #     self._card_tags.append(tag)
        #     self._put(f"→  {suggestion}\n", "suggest", tag)
        #     self.text.tag_bind(tag, "<ButtonRelease-1>", lambda e, s=suggestion: on_pick(s))
        #     self._hover_link(tag)
        picked = random.sample(suggestions, min(len(suggestions), suggests))
        for index, suggestion in enumerate(picked):
            tag = f"__chat_suggest_{index}"
            self._card_tags.append(tag)
            self._put(f"→  {suggestion}\n", "suggest", tag)
            self.text.tag_bind(tag, "<ButtonRelease-1>", lambda e, s=suggestion: on_pick(s))
            self._hover_link(tag)
        self.welcome_visible = True
        self._end_write()

    # ------------------------------ очистка ------------------------------

    def clear(self) -> None:
        self._cancel_restore()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        if self._card_tags:
            self.text.tag_delete(*self._card_tags)
        if self._marks:
            self.text.mark_unset(*self._marks)
        self._card_tags.clear()
        self._marks.clear()
        self._raw.clear()
        self._has_content = False
        self.welcome_visible = False
        self._pending_tools.clear()
        self._tool_records.clear()
        self._counter = 0
        self._set_follow(True)

    def destroy(self) -> None:
        self._cancel_restore()
        super().destroy()


def format_result(value: Any) -> str:
    from .widgets import pretty
    return pretty(value)