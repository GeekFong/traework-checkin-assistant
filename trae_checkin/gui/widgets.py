# -*- coding: utf-8 -*-
"""可复用的 Tkinter 小控件工厂。"""

from __future__ import annotations


def make_card(ctx, parent):
    """创建统一的卡片容器。"""
    return ctx.tk.Frame(
        parent,
        bg=ctx.CARD,
        highlightbackground=ctx.P["border"],
        highlightthickness=1,
        bd=0,
    )


def clear_children(widget) -> None:
    """销毁容器内全部直接子控件。"""
    for child in widget.winfo_children():
        child.destroy()
