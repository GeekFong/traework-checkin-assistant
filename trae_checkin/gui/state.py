# -*- coding: utf-8 -*-
"""GUI 运行期共享上下文。

历史上所有界面状态都保存在 ``run_gui`` 的嵌套闭包里。该对象把 Tk 根窗口、
主题管理器、控件、Tk 变量与回调集中在一个显式边界内，便于视图模块复用。
"""

from __future__ import annotations


class GuiContext:
    """承载一次 GUI 生命周期内共享的控件、变量与动作。"""

    def __init__(self, *, tk, ttk, messagebox, filedialog, simpledialog,
                 root, style, theme):
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.filedialog = filedialog
        self.simpledialog = simpledialog
        self.root = root
        self.style = style
        self.theme = theme

    @property
    def P(self) -> dict[str, str]:
        return self.theme.palette

    @property
    def FONT(self) -> str:
        from .theme import FONT
        return FONT

    @property
    def BG(self) -> str:
        return self.P["bg"]

    @property
    def CARD(self) -> str:
        return self.P["card"]

    @property
    def PRIMARY(self) -> str:
        return self.P["primary"]

    @property
    def PRIMARY_D(self) -> str:
        return self.P["primary_d"]

    @property
    def GREEN(self) -> str:
        return self.P["green"]

    @property
    def GRAY(self) -> str:
        return self.P["gray"]

    @property
    def DARK(self) -> str:
        return self.P["dark"]

    def after(self, ms: int, callback) -> None:
        self.root.after(ms, callback)
