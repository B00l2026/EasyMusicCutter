"""Small desktop helpers without additional runtime dependencies."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


class HoverText:
    def __init__(self, widget, text: Callable[[], str]):
        self.widget, self.text = widget, text
        self.timer = None
        self.window = None
        widget.bind('<Enter>', self._enter, add=True)
        widget.bind('<Leave>', self._hide, add=True)
        widget.bind('<ButtonPress>', self._hide, add=True)
        widget.bind('<Destroy>', self._hide, add=True)

    def _enter(self, _event=None):
        self._hide()
        self.timer = self.widget.after(550, self._show)

    def _show(self):
        self.timer = None
        text = self.text()
        if not text:
            return
        self.window = window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        x = min(self.widget.winfo_rootx(), max(0, self.widget.winfo_screenwidth() - 520))
        y = min(self.widget.winfo_rooty() + self.widget.winfo_height() + 4, self.widget.winfo_screenheight() - 110)
        window.wm_geometry(f'+{x}+{y}')
        ttk.Label(window, text=text, wraplength=500, padding=9, relief='solid', borderwidth=1).pack()

    def _hide(self, _event=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window:
            if self.window.winfo_exists():
                self.window.destroy()
            self.window = None
