# -*- coding: utf-8 -*-
"""积分趋势图两个历史缺陷的进程内 GUI 冒烟（合并自 trend_fix / trend_layout）。

fix 场景：窗口内仅 1 个积分点（42）。早期 render_trend 在断线收尾时
把只有 1 个点（2 个扁平坐标）的 pts 交给 create_line，触发
"TclError: wrong # coordinates: expected at least 4, got 2"。
防护为 len(pts) >= 4 才画线；初始 30 天渲染与点击「近 90 天」
二次渲染均不应中断，提示文字应正常绘制。

layout 场景：最近 5 天中 3 天带不同积分（40/120/180）。早期边距
常量 BY 误写成 24，绘图区仅 12px 高，网格与折线全挤在顶部。
修复后 BY = H - 24 = 146，本用例直接测量 Canvas 图元坐标做几何断言。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from _gui_helpers import collect_texts, find_button_contains, run_gui_steps

pytestmark = pytest.mark.gui

# 与 gui/app.py render_trend 常量保持一致
W, H = 520, 170
LX, RX, TY, BY = 38, 10, 20, H - 24


def _write_history(days: dict) -> None:
    from trae_checkin import history

    history.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history.HISTORY_FILE.write_text(
        json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")


def _record(platform, username, note, credits):
    from trae_checkin.constants import PLATFORM_LABELS

    return {
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "username": username,
        "note": note,
        "ok": True,
        "already": False,
        "suspicious": False,
        "credits": credits,
        "message": "签到成功",
        "time": "08:00",
    }


def _find_trend_canvas(root):
    """定位积分趋势 Canvas：请求尺寸 520x170（映射前用 req 尺寸兜底）。"""
    found = []

    def walk(widget):
        for ch in widget.winfo_children():
            if ch.winfo_class() == "Canvas":
                try:
                    cw = ch.winfo_width()
                    chh = ch.winfo_height()
                    if cw <= 1 or chh <= 1:  # 未映射时回退到请求尺寸
                        cw, chh = ch.winfo_reqwidth(), ch.winfo_reqheight()
                    if chh == H and 400 <= cw <= 600 and len(ch.find_all()) >= 10:
                        found.append(ch)
                        return
                except Exception:
                    pass
            walk(ch)

    walk(root)
    return found[0] if found else None


def _measure(cv):
    """返回水平网格线 y 集合、数据点中心坐标、日期刻度 y 集合。"""
    grid_ys, dot_centers, date_ys = [], [], []
    for oid in cv.find_all():
        t = cv.type(oid)
        c = cv.coords(oid)
        if t == "line" and len(c) == 4:
            x1, y1, x2, y2 = c
            if abs(y1 - y2) < 1 and (x2 - x1) > 400:  # 水平网格线
                grid_ys.append(round(y1, 1))
        elif t == "oval" and len(c) == 4:
            x1, y1, x2, y2 = c
            dot_centers.append((round((x1 + x2) / 2, 1),
                                round((y1 + y2) / 2, 1)))
        elif t == "text" and len(c) == 2:
            if re.match(r"^\d{2}-\d{2}$", str(cv.itemcget(oid, "text"))):
                date_ys.append(round(c[1], 1))
    return grid_ys, dot_centers, date_ys


def _assert_layout(cv, errors, tag):
    grid_ys, dots, date_ys = _measure(cv)
    # 5 条水平网格线，纵向应铺满 126px 绘图区（缺陷版仅 12px）
    if len(grid_ys) < 5:
        errors.append(f"{tag}：应有 5 条网格线，实际 {len(grid_ys)}")
        return
    span = max(grid_ys) - min(grid_ys)
    if not span > 100:
        errors.append(f"{tag}：网格线纵向跨度应 >100px，实际 {span:.0f}px")
    if abs(min(grid_ys) - TY) > 3:
        errors.append(f"{tag}：网格顶边应≈{TY}，实际 {min(grid_ys):.0f}")
    if abs(max(grid_ys) - BY) > 3:
        errors.append(f"{tag}：网格底边应≈{BY}，实际 {max(grid_ys):.0f}")
    # 数据点必须落在绘图区纵向范围内（缺陷版全挤在 y≈12~24 顶部）
    if len(dots) < 3:
        errors.append(f"{tag}：应至少 3 个数据点，实际 {len(dots)}")
    else:
        cy = [d[1] for d in dots]
        if not (min(cy) >= TY - 4 and max(cy) <= BY + 4):
            errors.append(
                f"{tag}：数据点 y 应在 [{TY},{BY}] 内，实际 "
                f"{min(cy):.0f}~{max(cy):.0f}")
        if max(cy) - min(cy) <= 40:
            errors.append(
                f"{tag}：不同积分数据点应纵向分层，实际跨度 "
                f"{max(cy) - min(cy):.0f}px")
    # X 轴日期刻度应在底边下方（≈ BY+12=158）
    if date_ys and min(date_ys) <= BY + 4:
        errors.append(
            f"{tag}：日期刻度应位于 X 轴下方（>{BY + 4}），"
            f"实际 {min(date_ys):.0f}")


def test_trend_single_point_fix(data_home, monkeypatch):
    """单点历史：30/90 天两次渲染都不触发 create_line 坐标错误。"""
    from trae_checkin import history
    from trae_checkin.constants import PLAT_TRAEWORK

    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")

    today = datetime.now().strftime("%Y-%m-%d")
    ident = history.history_identity(PLAT_TRAEWORK, "smoke_user")
    _write_history({today: {ident: _record(
        PLAT_TRAEWORK, "smoke_user", "单点冒烟", 42)}})

    # 数据层自检：30/90 天窗口都只有 1 个非 None 点
    for d in (30, 90):
        tr = history.history_credit_trend(d)
        assert tr["has_data"], f"{d} 天窗口应判定有数据"
        nonnull = [p for p in tr["series"][0]["points"] if p is not None]
        assert len(nonnull) == 1, f"{d} 天窗口应恰好 1 个积分点，实际 {len(nonnull)}"

    errors: list[str] = []

    def step_start(root):
        state = {"tries": 0}

        def verify():
            try:
                texts = collect_texts(root)
                if not any("积分趋势" in t for t in texts):
                    errors.append("积分趋势区应已渲染（标题存在）")
                if not any("悬停数据点" in t for t in texts):
                    errors.append("趋势提示文字应已绘制（说明 create_line 未中断渲染）")
            except Exception as e:
                errors.append(f"收尾断言异常：{type(e).__name__}: {e}")
            finally:
                root.destroy()

        def poll():
            # 能进入事件循环本身就说明初始（30 天单点）渲染未抛异常
            btn = find_button_contains(root, "90")
            if btn is not None:
                try:
                    btn.invoke()  # 同步走回调，渲染异常会冒泡到 Tk 兜底
                except Exception as e:
                    errors.append(f"点击近 90 天触发渲染异常：{type(e).__name__}: {e}")
                root.after(200, verify)
                return
            state["tries"] += 1
            if state["tries"] >= 40:
                errors.append("超时：应能找到近 90 天按钮")
                root.destroy()
            else:
                root.after(100, poll)

        poll()

    rc = run_gui_steps([(200, step_start)], errors, quit_ms=8000)
    assert rc == 0
    assert errors == []


def test_trend_layout_not_collapsed(data_home, monkeypatch):
    """三点历史：绘图区不坍缩，30/90 天几何布局均正确。"""
    from trae_checkin import history
    from trae_checkin.constants import PLAT_TRAEWORK

    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")

    today = datetime.now().date()
    ident = history.history_identity(PLAT_TRAEWORK, "smoke_layout")
    days = {}
    for back, credits in ((1, 40), (3, 120), (4, 180)):
        d = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        days[d] = {ident: _record(
            PLAT_TRAEWORK, "smoke_layout", "布局冒烟", credits)}
    _write_history(days)

    tr = history.history_credit_trend(30)
    nonnull = [p for p in tr["series"][0]["points"] if p is not None]
    assert len(nonnull) == 3, f"30 天窗口应有 3 个积分点，实际 {len(nonnull)}"

    errors: list[str] = []

    def step_start(root):
        state = {"tries": 0}

        def step_90d():
            cv = _find_trend_canvas(root)
            if cv is None:
                errors.append("90 天：应找到趋势画布")
            else:
                _assert_layout(cv, errors, "90d")
            root.destroy()

        def poll():
            root.update_idletasks()
            cv = _find_trend_canvas(root)
            if cv is None:
                state["tries"] += 1
                if state["tries"] >= 40:
                    errors.append("超时：30 天趋势画布未渲染")
                    root.destroy()
                else:
                    root.after(100, poll)
                return
            _assert_layout(cv, errors, "30d")
            btn = find_button_contains(root, "90")
            if btn is None:
                errors.append("应能找到近 90 天按钮")
                root.destroy()
                return
            try:
                btn.invoke()
            except Exception as e:
                errors.append(f"点击近 90 天异常：{type(e).__name__}: {e}")
            root.after(300, step_90d)

        poll()

    rc = run_gui_steps([(200, step_start)], errors, quit_ms=8000)
    assert rc == 0
    assert errors == []
