from __future__ import annotations

import sys
import tkinter as tk
from typing import Any


class CtkTooltip:
    def __init__(
        self,
        widget: Any,
        text: str,
        *,
        delay_ms: int = 450,
        wraplength: int = 320,
    ) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._show_now, add="+")
        widget.bind("<Button-3>", self._hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def _schedule(self, _event: Any = None) -> None:
        if self.widget is None:
            return
        self._cancel_after()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_after(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._tip is not None or self.widget is None:
            return
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return

        tip = tk.Toplevel(self.widget.winfo_toplevel())
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except Exception:
            pass
        tip.configure(bg="#202633")
        label = tk.Label(
            tip,
            text=self.text,
            justify="left",
            wraplength=self.wraplength,
            background="#202633",
            foreground="#F5F7FA",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 10) if sys.platform == "win32" else None,
        )
        label.pack()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _show_now(self, _event: Any = None) -> None:
        self._cancel_after()
        self._hide()
        self._show()

    def _hide(self, _event: Any = None) -> None:
        self._cancel_after()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _on_destroy(self, _event: Any = None) -> None:
        self._hide()
        self.widget = None


def attach_ctk_tooltip(widget: Any, text: str, *, delay_ms: int = 450, wraplength: int = 320) -> None:
    tooltip = CtkTooltip(widget, text, delay_ms=delay_ms, wraplength=wraplength)
    try:
        setattr(widget, "_ctk_tooltip", tooltip)
    except Exception:
        pass
