# -*- coding: utf-8 -*-
"""全局异常钩子测试：sys/threading/Tk 回调与弹窗节流。

迁移自旧单文件 test_t5.py。所有崩溃转存通过 data_home 夹具重定向到临时目录，
绝不写入真实数据目录。
"""
import sys
import threading
import time
from unittest import mock

import pytest

from trae_checkin import crashhandlers as ch


class FakeLog:
    def __init__(self):
        self.lines = []

    def error(self, *a):
        self.lines.append(" ".join(str(x) for x in a))

    def warning(self, *a):
        self.lines.append("W:" + " ".join(str(x) for x in a))

    def info(self, *a):
        pass


@pytest.fixture
def fake_log(monkeypatch):
    fl = FakeLog()
    monkeypatch.setattr(ch, "log", fl)
    return fl


@pytest.fixture
def hooks_restored():
    """安装钩子并在用例结束后恢复系统默认链，避免污染其他测试。"""
    orig_sys = sys.excepthook
    orig_thread = getattr(threading, "excepthook", None)
    ch.install_exception_hooks(popup=False)
    yield
    sys.excepthook = orig_sys
    if hasattr(threading, "excepthook"):
        if orig_thread is not None:
            threading.excepthook = orig_thread
        else:
            del threading.excepthook


def test_hooks_installed(hooks_restored):
    assert sys.excepthook is not sys.__excepthook__
    if hasattr(threading, "excepthook"):
        assert threading.excepthook is not getattr(
            threading, "__excepthook__", None)


def test_main_thread_exception_logged(hooks_restored, fake_log):
    try:
        raise ValueError("boom-main")
    except ValueError:
        et, ev, tb = sys.exc_info()
        ch._log_uncaught(et, ev, tb, "主线程", popup=False)
    assert any("boom-main" in x and "主线程" in x for x in fake_log.lines)


def test_background_thread_exception_logged(hooks_restored, fake_log):
    t = threading.Thread(
        target=lambda: (_ for _ in ()).throw(RuntimeError("boom-thread")),
        name="TestWorker")
    t.start()
    t.join()
    time.sleep(0.2)
    assert any("boom-thread" in x and "TestWorker" in x
               for x in fake_log.lines)


def test_background_thread_no_popup(hooks_restored):
    popup_calls = []
    try:
        raise RuntimeError("boom-bg")
    except RuntimeError:
        et, ev, tb = sys.exc_info()
        with mock.patch.object(ch, "_show_error_dialog",
                               side_effect=lambda *a: popup_calls.append(a)):
            ch._log_uncaught(et, ev, tb, "后台线程 x", popup=False)
    assert popup_calls == []


def test_keyboard_interrupt_goes_to_default(hooks_restored):
    default_called = []
    with mock.patch.object(sys, "__excepthook__",
                           side_effect=lambda *a: default_called.append(1)):
        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            et, ev, tb = sys.exc_info()
            ch._log_uncaught(et, ev, tb, "主线程")
    assert default_called == [1]


def test_tk_callback_exception_captured():
    tkinter = pytest.importorskip("tkinter")
    root = tkinter.Tk()
    root.withdraw()
    try:
        ch.install_tk_error_handler(root, popup=False)
        caught = []
        with mock.patch.object(ch, "_log_uncaught",
                               side_effect=lambda *a, **k: caught.append(a)):
            def bad_callback():
                raise ValueError("boom-callback")
            root.after(0, bad_callback)
            root.update()
            root.update()
        assert len(caught) == 1
        assert caught[0][1] is not None
        assert str(caught[0][1]) == "boom-callback"
    finally:
        root.destroy()


def test_error_dialog_throttled():
    tkinter = pytest.importorskip("tkinter")
    shown = []

    class FakeMB:
        @staticmethod
        def showerror(*a, **k):
            shown.append(a)

    class FakeTk:
        def __init__(self):
            self.dead = False

        def withdraw(self):
            pass

        def attributes(self, *a):
            pass

        def destroy(self):
            self.dead = True

    ch._error_popups.clear()
    with mock.patch.object(tkinter, "Tk", FakeTk), \
            mock.patch("tkinter.messagebox.showerror", FakeMB.showerror):
        for _ in range(3):
            try:
                raise ValueError("x")
            except ValueError:
                ch._show_error_dialog(*sys.exc_info(), "界面操作")
            time.sleep(0.01)
    assert len(shown) == 1
