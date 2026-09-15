# -*- coding: utf-8 -*-
"""GUI 冒烟测试公共工具（文件名不带 test_ 前缀，不会被 pytest 收集）。

迁移自旧单文件时代的 test_gui_*.py 脚本：
  - run_gui_steps：在真实 Tk 窗口上跑 run_gui，按毫秒时序注入点击/断言，
    回调里的任何异常都会被收集，mainloop 退出后统一抛出；
  - find_buttons / click_button：按文本在控件树里定位按钮。

所有使用方都必须配合 data_home 夹具把数据目录重定向到临时位置，
并设置 TRAESIGN_NO_DISCLAIMER / TRAESIGN_NO_CRASH_NOTICE 跳过弹窗流程。
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional


def find_buttons(widget: tk.Widget,
                 pred: Optional[Callable[[tk.Widget], bool]] = None) -> list:
    """深度遍历控件树，返回 Button 类控件（可再用 pred 过滤）。"""
    out: list = []
    for ch in widget.winfo_children():
        try:
            if ch.winfo_class() == "Button" and (pred is None or pred(ch)):
                out.append(ch)
        except tk.TclError:
            continue
        out.extend(find_buttons(ch, pred))
    return out


def click_button(root: tk.Widget, text: str, errors: list,
                 expect: int = 1) -> Optional[tk.Widget]:
    """按精确文本找按钮并 invoke；数量不符时记录错误。"""
    btns = [b for b in find_buttons(root) if b.cget("text") == text]
    if len(btns) != expect:
        errors.append(f"按钮 {text!r} 找到 {len(btns)} 个（期望 {expect}）")
        return None
    btns[0].invoke()
    return btns[0]


def walk(widget: tk.Widget):
    """深度优先遍历整棵控件树（含起点自身）。"""
    yield widget
    for ch in widget.winfo_children():
        try:
            yield from walk(ch)
        except tk.TclError:
            continue


def find_button(widget: tk.Widget, text: str):
    """按精确文本找第一个 Button，找不到返回 None。"""
    for w in walk(widget):
        try:
            if w.winfo_class() == "Button" and str(w.cget("text")) == text:
                return w
        except tk.TclError:
            continue
    return None


def find_button_contains(widget: tk.Widget, keyword: str):
    """按文本包含关键字找第一个 Button，找不到返回 None。"""
    for w in walk(widget):
        try:
            if w.winfo_class() == "Button" and keyword in str(w.cget("text")):
                return w
        except tk.TclError:
            continue
    return None


def find_toplevel(root: tk.Widget, title: str):
    """按窗口标题找已存在的 Toplevel，找不到返回 None。"""
    for w in root.winfo_children():
        try:
            if w.winfo_class() == "Toplevel" and str(w.title()) == title:
                return w
        except tk.TclError:
            continue
    return None


def collect_texts(widget: tk.Widget) -> list[str]:
    """收集子树内所有 Label 文本，并展开 Canvas 内 create_text 图元文本。

    趋势图/热力图的提示是 Canvas 图元而非 Label，需要 itemcget 才能取到。
    """
    out: list[str] = []

    def _walk(w):
        try:
            cls = w.winfo_class()
        except tk.TclError:
            return
        if cls == "Label":
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
        elif cls == "Canvas":
            try:
                for oid in w.find_all():
                    try:
                        out.append(str(w.itemcget(oid, "text")))
                    except tk.TclError:
                        pass
            except tk.TclError:
                pass
        for ch in w.winfo_children():
            _walk(ch)

    _walk(widget)
    return out


def run_gui_steps(steps: list[tuple[int, Callable[[tk.Tk], None]]],
                  errors: list, quit_ms: Optional[int] = None) -> int:
    """启动 run_gui 并按 (delay_ms, fn) 时序注入动作。

    fn(root) 内抛出的异常会被收集到 errors；mainloop 结束后由调用方断言。
    steps 为空时仅启动并在 quit_ms 后关闭（纯启动/渲染冒烟）。
    """
    from trae_checkin.gui.app import run_gui

    def _guarded(fn):
        # after() 回调以无参方式调用；root 由闭包绑定
        def _wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - 冒烟测试需要收集全部异常
                errors.append(repr(e))
        return _wrap

    original_init = tk.Tk.__init__

    def patched_init(self, *a, **kw):
        original_init(self, *a, **kw)
        root = self
        for delay, fn in steps:
            self.after(delay, _guarded(lambda r=root, f=fn: f(r)))
        if quit_ms is not None:
            self.after(quit_ms, self.destroy)

    tk.Tk.__init__ = patched_init
    try:
        return run_gui()
    finally:
        tk.Tk.__init__ = original_init
