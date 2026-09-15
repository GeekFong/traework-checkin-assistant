# -*- coding: utf-8 -*-
"""崩溃转存：独立 crash log 与诊断包内的崩溃文件。

迁移自旧单文件脚本式测试 test_t24.py。
红线：
  - 经 data_home 夹具把崩溃目录重定向到临时目录，绝不触碰真实 crashes/；
  - 不真正弹窗，只覆盖底层转存/列举/清理能力与全局钩子落盘。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import zipfile

import pytest

from trae_checkin import backup, crashhandlers as ch, runtime


@pytest.fixture
def boom():
    """返回一次诱发异常的 (type, value, traceback)。"""
    try:
        raise ValueError("单元测试诱发的崩溃")
    except ValueError:
        return sys.exc_info()


def _install_hooks_no_popup():
    ch.install_exception_hooks(popup=False)


# ───────────────────────── 写入 / 列举 / 清理 ─────────────────────────

def test_dump_file_created_with_stack(data_home, boom):
    path = ch.write_crash_dump(*boom, "主线程")
    assert path is not None
    assert path.is_file()
    assert path.name.startswith("crash-")
    assert path.name.endswith(".log")
    text = path.read_text(encoding="utf-8")
    assert "单元测试诱发的崩溃" in text
    assert "ValueError" in text
    assert "主线程" in text
    assert runtime.APP_VERSION in text


def test_dump_lives_under_home_crashes_dir(data_home, boom):
    path = ch.write_crash_dump(*boom, "后台线程 x")
    assert path.parent == data_home / "crashes"


def test_list_returns_newest_first(data_home, boom):
    paths = []
    base = time.time() - 100
    for i in range(3):
        p = ch.write_crash_dump(*boom, f"位置{i}")
        # 用 mtime 模拟先后，避免实睡
        p.touch()
        import os
        os.utime(p, (base + i, base + i))
        paths.append(p)
    listed = ch.list_crash_dumps()
    assert [p.name for p in listed] == [p.name for p in reversed(paths)]


def test_keep_only_recent_10(data_home, boom):
    import os
    first = None
    base = time.time() - 100
    for i in range(12):
        p = ch.write_crash_dump(*boom, f"位置{i}")
        os.utime(p, (base + i, base + i))
        if first is None:
            first = p
    dumps = ch.list_crash_dumps(limit=100)
    assert len(dumps) == 10
    assert first.name not in [p.name for p in dumps]


def test_clear_removes_all(data_home, boom):
    for _ in range(3):
        ch.write_crash_dump(*boom, "位置")
    n = ch.clear_crash_dumps()
    assert n == 3
    assert ch.list_crash_dumps() == []


def test_list_missing_dir_returns_empty(data_home):
    assert ch.list_crash_dumps() == []


# ───────────────────────── 全局钩子集成 ─────────────────────────

def test_sys_excepthook_writes_dump(data_home, boom):
    _install_hooks_no_popup()
    prev = sys.excepthook
    try:
        sys.excepthook(*boom)
    finally:
        sys.excepthook = prev
    dumps = ch.list_crash_dumps()
    assert len(dumps) == 1
    assert "单元测试诱发的崩溃" in dumps[0].read_text(encoding="utf-8")


def test_thread_excepthook_writes_dump(data_home, boom):
    _install_hooks_no_popup()
    prev_hook = threading.excepthook
    et, ev, tb = boom
    t = threading.Thread(target=lambda: None, name="t24-worker")
    args = threading.ExceptHookArgs((et, ev, tb, t))
    try:
        threading.excepthook(args)
    finally:
        threading.excepthook = prev_hook
    dumps = ch.list_crash_dumps()
    assert len(dumps) == 1
    assert "t24-worker" in dumps[0].read_text(encoding="utf-8")


def test_keyboard_interrupt_not_dumped(data_home):
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        et, ev, tb = sys.exc_info()
    # _log_uncaught 对 Ctrl+C 直接交回默认钩子；非交互调用下不产生崩溃文件
    ch._log_uncaught(et, ev, tb, "主线程")
    assert ch.list_crash_dumps() == []


def test_dump_writer_never_raises(data_home, boom):
    # 崩溃目录落在一个普通文件之下，mkdir 必失败，函数应静默返回 None
    blocker = data_home / "afile"
    blocker.write_text("x", encoding="utf-8")
    import unittest.mock as mock
    with mock.patch.object(ch, "CRASH_DIR", blocker / "crashes"):
        assert ch.write_crash_dump(*boom, "位置") is None


# ───────────────────────── 诊断包内崩溃文件 ─────────────────────────

def test_bundle_contains_crash_files_and_count(data_home, boom):
    ch.write_crash_dump(*boom, "主线程")
    zip_path = data_home / "diag.zip"
    summary = backup.export_diagnostic_bundle(str(zip_path))
    assert summary["crash_dumps"] == 1
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        crash_names = [n for n in names if n.startswith("crashes/")]
        assert len(crash_names) == 1
        info = json.loads(zf.read("diagnostic_info.json").decode("utf-8"))
        assert info["crash_dumps"] == 1
        body = zf.read(crash_names[0]).decode("utf-8")
        assert "单元测试诱发的崩溃" in body


def test_bundle_without_crashes_reports_zero(data_home):
    zip_path = data_home / "diag2.zip"
    summary = backup.export_diagnostic_bundle(str(zip_path))
    assert summary["crash_dumps"] == 0
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert not any(n.startswith("crashes/") for n in names)
