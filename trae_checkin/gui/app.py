# -*- coding: utf-8 -*-
"""tkinter 图形界面。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
import os
import sys
import threading
from typing import Optional
from ..accounts import delete_account, list_accounts, save_current_account, set_account_enabled, set_account_note
from ..backup import backup_user_data, export_diagnostic_bundle, restore_user_data
from ..constants import APP_NAME, PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY
from ..crashhandlers import clear_crash_dumps, install_exception_hooks, install_tk_error_handler, list_crash_dumps
from ..history import TREND_MAX_LINES, account_status_lookup, export_history_csv, history_calendar, history_credit_trend, history_identity, history_monthly_stats, history_recent_rows, history_summary, load_history, record_checkin_history
from ..launcher import find_install_exes, find_wb_install_exes, get_app_data_dir, launch_app, launch_platform_app
from ..push import push_wechat
from ..reports import _is_relogin_failure, build_checkin_report
from ..runtime import APP_VERSION, LOG_FILE, _log_dir, _run, log, resource_path
from ..scheduler import create_scheduled_task, delete_scheduled_task, task_exists
from ..service import run_batch_checkin
from ..settings import PUSH_CHANNELS, PUSH_CHANNEL_LABELS, SETTINGS_VERSION, accept_disclaimer, load_settings, save_settings_dict, save_theme_preference
from ..platforms.traework import is_logged_in, run_checkin
from ..platforms.workbuddy import run_wb_checkin_live, wb_find_auth_file, wb_is_logged_in
from .dialogs import (
    show_about,
    show_disclaimer_dialog as open_disclaimer_dialog,
    show_health,
    show_migration,
    show_push_history,
)
from .state import GuiContext
from .theme import (
    FONT,
    HEAT_FAIL,
    HEAT_OK,
    HEAT_PARTIAL,
    PALETTES,
    STATUS_AMBER,
    STATUS_RED,
    ThemeManager,
    initial_theme_name,
)
from .widgets import make_card


def run_gui() -> int:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog

    _initial_theme = initial_theme_name()
    P = dict(PALETTES[_initial_theme])
    current_theme = {"name": _initial_theme}

    BG = P["bg"]
    CARD = P["card"]
    PRIMARY = P["primary"]
    PRIMARY_D = P["primary_d"]
    GREEN = P["green"]
    GRAY = P["gray"]
    DARK = P["dark"]

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("600x760")
    root.configure(bg=BG)
    root.minsize(560, 640)

    # 窗口图标：打包后取 exe 内嵌图标；源码态取仓库 assets/app.ico；失败用 Tk 默认图标
    try:
        if getattr(sys, "frozen", False):
            root.iconbitmap(default=sys.executable)
        else:
            _ico = resource_path("app.ico")
            if _ico.is_file():
                root.iconbitmap(default=str(_ico))
    except Exception:
        log.debug("窗口图标加载失败，使用默认图标", exc_info=True)

    # 全局异常兜底：后台线程写日志，界面回调异常写日志并弹窗提示
    install_exception_hooks(popup=False)
    install_tk_error_handler(root, popup=True)

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass

    theme = ThemeManager(root, style, _initial_theme)
    ctx = GuiContext(
        tk=tk,
        ttk=ttk,
        messagebox=messagebox,
        filedialog=filedialog,
        simpledialog=simpledialog,
        root=root,
        style=style,
        theme=theme,
    )

    def _sync_theme_colors() -> None:
        nonlocal BG, CARD, PRIMARY, PRIMARY_D, GREEN, GRAY, DARK
        P.update(theme.palette)
        colors = theme.colors
        BG, CARD = colors["BG"], colors["CARD"]
        PRIMARY, PRIMARY_D = colors["PRIMARY"], colors["PRIMARY_D"]
        GREEN, GRAY, DARK = colors["GREEN"], colors["GRAY"], colors["DARK"]
        current_theme["name"] = theme.name

    def apply_theme(name: str, persist: bool = True):
        """整树换色；动态区域在渲染函数定义后通过回调重绘。"""
        theme.apply(name, persist=False)
        _sync_theme_colors()
        if persist:
            save_theme_preference(name)

    def toggle_theme():
        apply_theme(theme.toggle())
        try:
            theme_btn.config(
                text="☀ 浅色" if current_theme["name"] == "dark" else "🌙 深色")
        except Exception:
            pass

    def card(parent) -> tk.Frame:
        return make_card(ctx, parent)

    def on_show_about():
        show_about(ctx)

    def show_disclaimer_dialog() -> bool:
        """首次启动风险声明弹窗：返回用户是否同意（同意才能进入主界面）。"""
        return open_disclaimer_dialog(ctx)

    def on_show_health():
        show_health(ctx)

    # 顶部：第一行放功能按钮，第二行独占放标题与副标题。
    # 若把长副标题与右侧按钮放在同一行，Label 会被挤压、内部居中导致
    # 文字左右两端被裁切，因此这里刻意拆成两行。
    toolbar = tk.Frame(root, bg=BG)
    toolbar.pack(fill="x", padx=24, pady=(22, 0))
    theme_btn = tk.Button(
        toolbar, text="☀ 浅色" if _initial_theme == "dark" else "🌙 深色",
        command=toggle_theme, bg=CARD, fg=DARK, relief="flat", bd=0,
        activebackground=P["seg"], activeforeground=DARK,
        font=(FONT, 10), padx=10, pady=4, cursor="hand2")
    theme_btn.pack(side="right", anchor="e")
    about_btn = tk.Button(
        toolbar, text="关于", command=on_show_about, bg=CARD, fg=DARK,
        relief="flat", bd=0, activebackground=P["seg"],
        activeforeground=DARK, font=(FONT, 10), padx=10, pady=4,
        cursor="hand2")
    about_btn.pack(side="right", anchor="e", padx=(0, 6))
    health_btn = tk.Button(
        toolbar, text="健康自检", command=on_show_health, bg=CARD, fg=DARK,
        relief="flat", bd=0, activebackground=P["seg"],
        activeforeground=DARK, font=(FONT, 10), padx=10, pady=4,
        cursor="hand2")
    health_btn.pack(side="right", anchor="e", padx=(0, 6))

    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=24, pady=(8, 6))
    tk.Label(header, text=APP_NAME, bg=BG, fg=DARK,
             font=(FONT, 19, "bold")).pack(anchor="w")
    tk.Label(header, text="TraeWork CN / 腾讯 WorkBuddy · 多账号批量签到 · 推送微信 · 开机补签",
             bg=BG, fg=GRAY, font=(FONT, 10)).pack(anchor="w", pady=(2, 0))

    # 平台切换
    plat_row = tk.Frame(root, bg=BG)
    plat_row.pack(fill="x", padx=24, pady=(8, 0))
    platform_var = tk.StringVar(value=PLAT_TRAEWORK)
    tk.Label(plat_row, text="当前平台：", bg=BG, fg=DARK,
             font=(FONT, 10)).pack(side="left")
    plat_seg = tk.Frame(plat_row, bg=P["seg"], highlightthickness=0, bd=0)
    plat_seg.pack(side="left")

    def on_platform_change(*_args):
        cur = platform_var.get()
        for pf, btn in plat_buttons.items():
            if pf == cur:
                btn.config(bg=PRIMARY, fg="white", activebackground=PRIMARY_D,
                           activeforeground="white", relief="flat")
            else:
                btn.config(bg=P["seg"], fg=DARK, activebackground=P["seg_active"],
                           activeforeground=DARK, relief="flat")
        result_var.set("")
        try:
            update_accounts_hint()
        except Exception:
            pass
        stop_waiting()
        threading.Thread(target=refresh_ui, daemon=True).start()

    plat_buttons = {}
    for pf, label in ((PLAT_TRAEWORK, "TraeWork CN"), (PLAT_WORKBUDDY, "WorkBuddy")):
        active = pf == platform_var.get()
        b = tk.Radiobutton(
            plat_seg, text=label, variable=platform_var, value=pf,
            indicatoron=0, bg=(PRIMARY if active else P["seg"]),
            fg=("white" if active else DARK), selectcolor=PRIMARY,
            activebackground=(PRIMARY_D if active else P["seg_active"]),
            activeforeground="white",
            font=(FONT, 9, "bold"), bd=0, padx=16, pady=4, cursor="hand2")
        b.pack(side="left")
        plat_buttons[pf] = b
    platform_var.trace_add("write", on_platform_change)

    # 可滚动内容区
    outer = tk.Frame(root, bg=BG)
    outer.pack(fill="both", expand=True, padx=(24, 12), pady=10)
    canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0, width=536)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    body = tk.Frame(canvas, bg=BG)
    body_id = canvas.create_window((0, 0), window=body, anchor="nw", width=536)

    def _on_body_config(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_config(e):
        canvas.itemconfigure(body_id, width=e.width)

    body.bind("<Configure>", _on_body_config)
    canvas.bind("<Configure>", _on_canvas_config)

    def _on_wheel(e):
        canvas.yview_scroll(int(-e.delta / 120), "units")

    def _bind_wheel(_e=None):
        canvas.bind_all("<MouseWheel>", _on_wheel)

    def _unbind_wheel(_e=None):
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)

    # ── 状态卡片 ──
    status_card = card(body)
    status_card.pack(fill="x")
    st_dot = tk.Label(status_card, text="●", bg=CARD, fg=GRAY,
                      font=(FONT, 14))
    st_dot.grid(row=0, column=0, padx=(18, 6), pady=16, sticky="n")
    st_text = tk.Label(status_card, text="正在检测环境…", bg=CARD, fg=DARK,
                       font=(FONT, 12, "bold"), justify="left", anchor="w")
    st_text.grid(row=0, column=1, sticky="w", pady=(16, 0))
    st_sub = tk.Label(status_card, text="", bg=CARD, fg=GRAY,
                      font=(FONT, 9), justify="left", anchor="w", wraplength=420)
    st_sub.grid(row=1, column=1, sticky="w", pady=(0, 16))
    status_card.grid_columnconfigure(1, weight=1)

    # ── 登录引导卡片（默认隐藏）──
    login_card = card(body)
    login_title = tk.Label(login_card, text="需要先登录 TraeWork CN",
                           bg=CARD, fg=DARK, font=(FONT, 12, "bold"),
                           anchor="w", justify="left")
    login_title.pack(fill="x", padx=18, pady=(16, 4))
    login_hint = tk.Label(login_card, text="", bg=CARD, fg=GRAY,
                          font=(FONT, 9), justify="left", anchor="w",
                          wraplength=470)
    login_hint.pack(fill="x", padx=18)

    btn_row = tk.Frame(login_card, bg=CARD)
    btn_row.pack(fill="x", padx=18, pady=14)
    waiting_var = tk.StringVar(value="")

    def set_status(dot_color: str, title: str, sub: str = ""):
        st_dot.config(fg=dot_color)
        st_text.config(text=title)
        st_sub.config(text=sub)

    login_poll = {"on": False}

    def stop_waiting():
        login_poll["on"] = False
        waiting_var.set("")
        for b in btn_row.winfo_children():
            try:
                b.config(state="normal")
            except Exception:
                pass

    def _start_polling(hint: str):
        login_poll["on"] = True
        waiting_var.set(hint)
        for b in btn_row.winfo_children():
            try:
                b.config(state="disabled")
            except Exception:
                pass
        poll_login(90)

    def poll_login(times: int):
        if not login_poll["on"]:
            return
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            ok_logged, _ = wb_is_logged_in()
            label = "WorkBuddy"
        else:
            ok_logged, _ = is_logged_in()
            label = "TraeWork CN"
        if ok_logged:
            stop_waiting()
            messagebox.showinfo(APP_NAME, f"检测到 {label} 登录成功！")
            refresh_ui()
            return
        if times <= 0:
            stop_waiting()
            messagebox.showinfo(APP_NAME, "暂未检测到登录，可稍后点击「重新检测」。")
            return
        root.after(2000, lambda: poll_login(times - 1))

    def on_open_app():
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            exes = find_wb_install_exes()
            if not exes:
                messagebox.showwarning(APP_NAME, "未能自动找到 WorkBuddy，请先安装腾讯 WorkBuddy 桌面端。")
                return
            if launch_app(exes[0]):
                _start_polling("已打开 WorkBuddy，请在窗口中完成登录，登录后此处会自动继续…（最多等待 3 分钟）")
            else:
                messagebox.showerror(APP_NAME, "无法启动 WorkBuddy，请手动打开后登录。")
            return
        exes = find_install_exes()
        chosen: Optional[Path] = exes[0] if exes else None
        if not chosen:
            messagebox.showwarning(APP_NAME, "未能自动找到 TraeWork CN，请先安装客户端。")
            return
        if launch_app(chosen):
            _start_polling("已打开应用，请在弹出的窗口中登录，登录后此处会自动继续…（最多等待 3 分钟）")
        else:
            messagebox.showerror(APP_NAME, "无法启动应用，请手动打开 TraeWork CN 登录。")

    def on_manual_exe():
        pf = platform_var.get()
        name = "WorkBuddy" if pf == PLAT_WORKBUDDY else "TraeWork CN"
        path = filedialog.askopenfilename(
            title=f"请选择 {name} 主程序（.exe）",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            if launch_app(Path(path)):
                _start_polling(f"已启动所选程序，请完成 {name} 登录…")

    open_btn = tk.Button(btn_row, text="打开并登录", bg=PRIMARY,
                         fg="white", font=(FONT, 10, "bold"), relief="flat",
                         activebackground=PRIMARY_D, activeforeground="white",
                         cursor="hand2", padx=14, pady=7, bd=0,
                         command=lambda: threading.Thread(target=on_open_app,
                                                          daemon=True).start())
    open_btn.pack(side="left")
    tk.Button(btn_row, text="手动选择程序位置", bg="#eef1f7", fg=DARK,
              font=(FONT, 9), relief="flat", cursor="hand2", padx=12, pady=7,
              bd=0, command=on_manual_exe).pack(side="left", padx=(10, 0))
    tk.Label(login_card, textvariable=waiting_var, bg=CARD, fg=PRIMARY,
             font=(FONT, 9), wraplength=470, justify="left", anchor="w").pack(
        fill="x", padx=18, pady=(0, 8))

    # ── 操作卡片（登录后显示）──
    action_card = card(body)
    ac_title = tk.Label(action_card, text="签到", bg=CARD, fg=DARK,
                        font=(FONT, 12, "bold"), anchor="w")
    ac_title.pack(fill="x", padx=18, pady=(16, 8))
    result_var = tk.StringVar(value="")
    tk.Label(action_card, textvariable=result_var, bg=CARD, fg=DARK,
             font=(FONT, 10), justify="left", anchor="w",
             wraplength=470).pack(fill="x", padx=18)

    op_row = tk.Frame(action_card, bg=CARD)
    op_row.pack(fill="x", padx=18, pady=14)
    checkin_btn = tk.Button(op_row, text="立即签到", bg=PRIMARY, fg="white",
                            font=(FONT, 10, "bold"), relief="flat",
                            activebackground=PRIMARY_D, activeforeground="white",
                            cursor="hand2", padx=16, pady=7, bd=0)
    checkin_btn.pack(side="left")
    refresh_btn = tk.Button(op_row, text="重新检测", bg="#eef1f7", fg=DARK,
                            font=(FONT, 9), relief="flat", cursor="hand2",
                            padx=12, pady=7, bd=0)
    refresh_btn.pack(side="left", padx=(10, 0))

    # ── 多账号管理卡片 ──
    accounts_card = card(body)
    accounts_card.pack(fill="x", pady=(14, 0))
    tk.Label(accounts_card, text="多账号批量签到", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    accounts_hint_var = tk.StringVar(value="")
    tk.Label(accounts_card, textvariable=accounts_hint_var,
             bg=CARD, fg=GRAY, font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)

    acc_list_frame = tk.Frame(accounts_card, bg=CARD)
    acc_list_frame.pack(fill="x", padx=18, pady=(10, 4))
    accounts_state_var = tk.StringVar(value="尚未保存任何账号")
    tk.Label(accounts_card, textvariable=accounts_state_var, bg=CARD, fg=DARK,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)

    acc_row = tk.Frame(accounts_card, bg=CARD)
    acc_row.pack(fill="x", padx=18, pady=12)
    save_acc_btn = tk.Button(acc_row, text="保存当前登录账号", bg=PRIMARY,
                             fg="white", font=(FONT, 10, "bold"), relief="flat",
                             activebackground=PRIMARY_D, activeforeground="white",
                             cursor="hand2", padx=14, pady=7, bd=0)
    save_acc_btn.pack(side="left")
    batch_btn = tk.Button(acc_row, text="全部账号签到", bg=GREEN, fg="white",
                          font=(FONT, 10, "bold"), relief="flat",
                          activebackground="#15804c", activeforeground="white",
                          cursor="hand2", padx=14, pady=7, bd=0)
    batch_btn.pack(side="left", padx=(10, 0))
    batch_result_var = tk.StringVar(value="")
    tk.Label(accounts_card, textvariable=batch_result_var, bg=CARD, fg=DARK,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18, pady=(0, 4))
    # 登录失效时按平台出现的"一键打开重登"按钮容器
    batch_guide_frame = tk.Frame(accounts_card, bg=CARD)
    batch_guide_frame.pack(fill="x", padx=18, pady=(0, 12))

    # ── 签到历史卡片 ──
    history_card = card(body)
    history_card.pack(fill="x", pady=(14, 0))
    hist_head = tk.Frame(history_card, bg=CARD)
    hist_head.pack(fill="x", padx=18, pady=(16, 2))
    tk.Label(hist_head, text="签到历史与统计", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(side="left")

    def on_export_csv():
        try:
            n = len(load_history().get("days", {}))
            if not n:
                messagebox.showinfo(APP_NAME, "暂无历史记录可导出，完成一次签到后再来。")
                return
            default_name = f"签到历史_{datetime.now().strftime('%Y%m%d')}.csv"
            path = filedialog.asksaveasfilename(
                title="导出签到历史",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name,
                defaultextension=".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
            if not path:
                return
            count = export_history_csv(path)
            messagebox.showinfo(
                APP_NAME, f"已导出 {count} 条签到记录到：\n{path}\n\n"
                          "CSV 采用 UTF-8 编码，可直接用 Excel 打开。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"导出失败：{e}")

    export_btn = tk.Button(hist_head, text="导出 CSV", bg="#eef3ff", fg=PRIMARY,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           activebackground="#dde7ff", padx=10, pady=3, bd=0,
                           command=on_export_csv)
    export_btn.pack(side="right")

    def on_backup_data():
        try:
            default_name = f"签到助手备份_{datetime.now().strftime('%Y%m%d')}.zip"
            path = filedialog.asksaveasfilename(
                title="备份账号与设置",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name, defaultextension=".zip",
                filetypes=[("备份压缩包", "*.zip")])
            if not path:
                return
            info = backup_user_data(path)
            bits = []
            bits.append("账号 " + ("已备份" if info["accounts"] else "无"))
            bits.append("设置 " + ("已备份" if info["settings"] else "无"))
            bits.append("历史 " + ("已备份" if info["history"] else "无"))
            bits.append("推送记录 " + ("已备份" if info.get("push_log") else "无"))
            messagebox.showinfo(
                APP_NAME, "备份完成：\n" + "，".join(bits) +
                f"\n\n文件：{path}\n\n"
                "重要提示：账号与推送凭据经 Windows DPAPI 加密，\n"
                "该备份只能在本机同一 Windows 用户下恢复；\n"
                "换机或重装系统前请先在各客户端确认可手动登录。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"备份失败：{e}")

    def on_restore_data():
        if not messagebox.askyesno(
                APP_NAME, "恢复会用备份包覆盖当前的账号、设置与历史\n"
                          "（当前文件会自动另存为 .restore-bak）。\n\n"
                          "确定选择备份文件并恢复吗？"):
            return
        try:
            path = filedialog.askopenfilename(
                title="选择备份压缩包",
                initialdir=str(Path.home() / "Desktop"),
                filetypes=[("备份压缩包", "*.zip"), ("所有文件", "*.*")])
            if not path:
                return
            res = restore_user_data(path)
            names = "、".join(res["restored"])
            messagebox.showinfo(
                APP_NAME, f"已恢复：{names}\n\n设置即时生效；如界面显示异常，"
                          "关闭后重新打开本工具即可。")
            render_accounts()
            render_history()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"恢复失败：{e}")

    def on_export_diag():
        try:
            default_name = f"签到助手诊断_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
            path = filedialog.asksaveasfilename(
                title="导出诊断包",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name, defaultextension=".zip",
                filetypes=[("诊断压缩包", "*.zip")])
            if not path:
                return
            info = export_diagnostic_bundle(path)
            messagebox.showinfo(
                APP_NAME, f"诊断包已导出：\n{path}\n\n"
                          f"含版本/环境、{info['accounts']} 个账号概况、"
                          f"{info['history_days']} 天历史概况与最近日志。\n"
                          "凭据已脱敏，可安全发送给他人协助排查。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"诊断包导出失败：{e}")

    def on_show_push_history():
        show_push_history(ctx)

    push_hist_btn = tk.Button(hist_head, text="推送记录", bg="#e8f4ff",
                              fg="#1172b8", font=(FONT, 9), relief="flat",
                              cursor="hand2", activebackground="#d2e9ff",
                              padx=8, pady=3, bd=0,
                              command=on_show_push_history)
    push_hist_btn.pack(side="right", padx=(0, 6))

    diag_btn = tk.Button(hist_head, text="诊断包", bg="#f3f0ff", fg="#6d4aff",
                         font=(FONT, 9), relief="flat", cursor="hand2",
                         activebackground="#e6e0ff", padx=8, pady=3, bd=0,
                         command=on_export_diag)
    diag_btn.pack(side="right", padx=(0, 6))
    restore_btn = tk.Button(hist_head, text="恢复", bg="#fff4e5", fg="#c7771f",
                            font=(FONT, 9), relief="flat", cursor="hand2",
                            activebackground="#ffe7c2", padx=8, pady=3, bd=0,
                            command=on_restore_data)
    restore_btn.pack(side="right", padx=(0, 6))
    backup_btn = tk.Button(hist_head, text="备份", bg="#eafaf1", fg=GREEN,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           activebackground="#d4f2e0", padx=8, pady=3, bd=0,
                           command=on_backup_data)
    backup_btn.pack(side="right", padx=(0, 6))

    def on_show_migration():
        def _refresh_after_restore():
            render_accounts()
            render_history()

        show_migration(ctx, on_data_changed=_refresh_after_restore)

    migrate_btn = tk.Button(hist_head, text="换机迁移", bg=P["note_bg"],
                            fg=P["note_txt"], font=(FONT, 9), relief="flat",
                            cursor="hand2", activebackground=P["seg"],
                            padx=8, pady=3, bd=0, command=on_show_migration)
    migrate_btn.pack(side="right", padx=(0, 6))

    history_summary_var = tk.StringVar(value="")
    tk.Label(history_card, textvariable=history_summary_var,
             bg=CARD, fg=DARK, font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)
    history_frame = tk.Frame(history_card, bg=CARD)
    history_frame.pack(fill="x", padx=18, pady=(10, 14))

    # ── 消息通知卡片（多渠道） ──
    notify_card = card(body)
    notify_card.pack(fill="x", pady=(14, 0))
    tk.Label(notify_card, text="签到结果消息推送（可同时开启多个渠道）",
             bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    tk.Label(notify_card,
             text="勾选要使用的渠道并填写凭据，保存后每日所有账号的签到结果会汇总为一条消息，"
                  "同时发往所有已勾选渠道；某个渠道发送失败不影响其他渠道。凭据经 Windows "
                  "DPAPI 加密后仅保存在本机。",
             bg=CARD, fg=GRAY, font=(FONT, 9), justify="left", anchor="w",
             wraplength=500).pack(fill="x", padx=18)

    nrow0 = tk.Frame(notify_card, bg=CARD)
    nrow0.pack(fill="x", padx=18, pady=(12, 4))
    push_enabled_var = tk.IntVar(value=0)
    tk.Checkbutton(nrow0, text="开启消息推送", variable=push_enabled_var,
                   bg=CARD, fg=DARK, font=(FONT, 10, "bold"), selectcolor=CARD,
                   activebackground=CARD, bd=0).pack(side="left")
    only_failures_var = tk.IntVar(value=0)
    tk.Checkbutton(nrow0, text="仅在有账号失败时推送", variable=only_failures_var,
                   bg=CARD, fg=GRAY, font=(FONT, 9), selectcolor=CARD,
                   activebackground=CARD, bd=0).pack(side="left", padx=(20, 0))

    # 每个渠道一行：勾选框 + 名称 + 凭据输入；群机器人额外显示加签密钥输入
    ch_enable_vars: dict[str, tk.IntVar] = {}
    ch_key_vars: dict[str, tk.StringVar] = {}
    ch_secret_vars: dict[str, tk.StringVar] = {}
    ch_secret_labels: dict[str, tk.Label] = {}
    for ch in PUSH_CHANNELS:
        row = tk.Frame(notify_card, bg=CARD)
        row.pack(fill="x", padx=18, pady=3)
        var = tk.IntVar(value=0)
        ch_enable_vars[ch] = var
        tk.Checkbutton(row, text=PUSH_CHANNEL_LABELS[ch], variable=var,
                       bg=CARD, fg=DARK, font=(FONT, 9, "bold"), width=16,
                       anchor="w", selectcolor=CARD, activebackground=CARD,
                       bd=0).pack(side="left")
        key_var = tk.StringVar(value="")
        ch_key_vars[ch] = key_var
        cred_hint = "Webhook 地址" if ch in ("wecom", "dingtalk") else "Key / Token"
        tk.Entry(row, textvariable=key_var, font=(FONT, 9), width=34
                 ).pack(side="left", padx=(2, 8))
        secret_var = tk.StringVar(value="")
        ch_secret_vars[ch] = secret_var
        sec_lbl = tk.Label(row, text="加签密钥：", bg=CARD, fg=GRAY,
                           font=(FONT, 9))
        ch_secret_labels[ch] = sec_lbl
        sec_lbl.pack(side="left")
        tk.Entry(row, textvariable=secret_var, font=(FONT, 9), width=18
                 ).pack(side="left")
        if ch not in ("wecom", "dingtalk"):
            sec_lbl.pack_forget()
        tk.Label(row, text=cred_hint, bg=CARD, fg="#b6bdca",
                 font=(FONT, 8)).pack(side="left", padx=(4, 0))

    nrow4 = tk.Frame(notify_card, bg=CARD)
    nrow4.pack(fill="x", padx=18, pady=(8, 4))
    save_key_btn = tk.Button(nrow4, text="保存推送设置", bg=PRIMARY, fg="white",
                             font=(FONT, 9, "bold"), relief="flat",
                             activebackground=PRIMARY_D, activeforeground="white",
                             cursor="hand2", padx=12, pady=6, bd=0)
    save_key_btn.pack(side="left")
    test_push_btn = tk.Button(nrow4, text="发送测试消息（发往所有已勾选渠道）",
                              bg="#eef1f7", fg=DARK,
                              font=(FONT, 9), relief="flat", cursor="hand2",
                              padx=12, pady=6, bd=0)
    test_push_btn.pack(side="left", padx=(10, 0))
    getkey_btn = tk.Button(nrow4, text="如何获取凭据？", bg=BG, fg=PRIMARY,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           padx=6, pady=6, bd=0)
    getkey_btn.pack(side="left")
    push_state_var = tk.StringVar(value="")
    tk.Label(notify_card, textvariable=push_state_var, bg=CARD, fg=GRAY,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=500).pack(fill="x", padx=18, pady=(0, 12))

    # ── 自动任务卡片 ──
    task_card = card(body)
    task_card.pack(fill="x", pady=(14, 0))
    tk.Label(task_card, text="每日自动签到", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    task_state_var = tk.StringVar(value="检测中…")
    tk.Label(task_card, textvariable=task_state_var, bg=CARD, fg=GRAY,
             font=(FONT, 9), anchor="w", justify="left").pack(fill="x", padx=18)

    trow = tk.Frame(task_card, bg=CARD)
    trow.pack(fill="x", padx=18, pady=12)
    tk.Label(trow, text="每天", bg=CARD, fg=DARK, font=(FONT, 10)).pack(side="left")
    time_var = tk.StringVar(value="09:00")
    time_entry = tk.Entry(trow, textvariable=time_var, width=6,
                          font=(FONT, 10), justify="center")
    time_entry.pack(side="left", padx=6)
    tk.Label(trow, text="自动签到（24小时制，如 09:00；修改时间后点击下方按钮生效）",
             bg=CARD, fg=GRAY, font=(FONT, 9)).pack(side="left")

    trow2 = tk.Frame(task_card, bg=CARD)
    trow2.pack(fill="x", padx=18, pady=(0, 16))
    enable_task_btn = tk.Button(trow2, text="开启 / 更新签到时间", bg=GREEN, fg="white",
                                font=(FONT, 10, "bold"), relief="flat",
                                activebackground="#15804c", activeforeground="white",
                                cursor="hand2", padx=14, pady=7, bd=0)
    enable_task_btn.pack(side="left")
    disable_task_btn = tk.Button(trow2, text="关闭自动签到", bg="#eef1f7", fg=DARK,
                                 font=(FONT, 9), relief="flat", cursor="hand2",
                                 padx=12, pady=7, bd=0)
    disable_task_btn.pack(side="left", padx=(10, 0))

    # 底部：打开日志
    bottom = tk.Frame(root, bg=BG)
    bottom.pack(fill="x", padx=24, pady=(0, 14))

    def open_log():
        try:
            os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_NAME, f"日志位置：\n{LOG_FILE}")

    tk.Button(bottom, text="打开运行日志", bg=BG, fg=GRAY, font=(FONT, 8),
              relief="flat", bd=0, cursor="hand2",
              command=open_log).pack(side="left")
    tk.Label(bottom, text=f"客户端版本 {APP_VERSION}", bg=BG, fg="#9aa3b2",
             font=(FONT, 8)).pack(side="right")

    # ── 交互逻辑 ──

    def show_only(frame: tk.Frame):
        login_card.pack_forget()
        action_card.pack_forget()
        if frame is not None:
            # before=accounts_card 保证登录/操作卡始终位于多账号卡之前
            frame.pack(fill="x", pady=(14, 0), before=accounts_card)

    def do_checkin():
        checkin_btn.config(state="disabled", text="签到中…")
        result_var.set("正在与服务器通信，请稍候…")

        def worker():
            pf = platform_var.get()
            if pf == PLAT_WORKBUDDY:
                r = run_wb_checkin_live(status_only=False)
            else:
                r = run_checkin(status_only=False)
            if r.get("platform") is None:
                r["platform"] = pf
                r["platform_label"] = PLATFORM_LABELS.get(pf, pf)
            record_checkin_history([r])

            def done():
                checkin_btn.config(state="normal", text="立即签到")
                if r.get("ok") and r.get("suspicious"):
                    # 防假成功（t23）：流程 200 但缺成功信号，用警示图标区别于正常 ✓
                    result_var.set("⚠ " + r.get("message", "需人工确认"))
                elif r.get("ok"):
                    result_var.set("✓ " + r.get("message", "签到成功"))
                else:
                    result_var.set("✗ " + r.get("message", "签到失败"))
                refresh_task_state()
                render_history()
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    checkin_btn.config(command=do_checkin)
    refresh_btn.config(command=lambda: threading.Thread(target=refresh_ui,
                                                        daemon=True).start())

    def refresh_task_state():
        exists = task_exists()
        if exists:
            task_state_var.set("● 已开启：每天到点自动后台签到，关机错过会在开机后补签")
            enable_task_btn.config(state="normal")
            disable_task_btn.config(state="normal")
        else:
            task_state_var.set("○ 未开启：开启后将每天自动签到，无需手动操作")
            enable_task_btn.config(state="normal")
            disable_task_btn.config(state="normal")

    def on_enable_task():
        t = time_var.get().strip()
        enable_task_btn.config(state="disabled")
        task_state_var.set("正在创建定时任务…")

        def worker():
            ok_create, msg = create_scheduled_task(t)

            def done():
                refresh_task_state()
                if ok_create:
                    messagebox.showinfo(APP_NAME, msg + "\n关机错过时开机会自动补签。")
                else:
                    messagebox.showerror(APP_NAME, msg)
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def on_disable_task():
        disable_task_btn.config(state="disabled")

        def worker():
            delete_scheduled_task()
            root.after(0, lambda: (refresh_task_state(),
                                   messagebox.showinfo(APP_NAME, "已关闭每日自动签到。")))

        threading.Thread(target=worker, daemon=True).start()

    enable_task_btn.config(command=on_enable_task)
    disable_task_btn.config(command=on_disable_task)

    # ── 多账号交互 ──

    HINT_BY_PLATFORM = {
        PLAT_TRAEWORK: "TraeWork 客户端同一时间只能登录一个账号。请在客户端登录某账号后，"
                       "点击「保存当前登录账号」；切换账号登录后再次保存，即可收集多个账号。",
        PLAT_WORKBUDDY: "请先在 WorkBuddy 桌面端登录某账号，再点击「保存当前登录账号」；"
                        "退出后换另一个账号登录，再次保存即可收集多个 WorkBuddy 账号。",
    }

    def update_accounts_hint():
        pf = platform_var.get()
        label = PLATFORM_LABELS.get(pf, "")
        save_acc_btn.config(text=f"保存当前{label}账号")
        accounts_hint_var.set(HINT_BY_PLATFORM.get(pf, "") +
                              "全部已保存账号（含两个平台）会统一批量签到，无需保持登录。")

    def guide_relogin(pf: str, who: str = ""):
        """一键打开对应平台客户端，并给出重新登录→刷新凭据→重试的分步引导。"""
        label = PLATFORM_LABELS.get(pf, "客户端")
        ok, info = launch_platform_app(pf)
        if ok:
            log.info(f"已为账号 {who} 打开 {label} 客户端：{info}")
            messagebox.showinfo(
                APP_NAME,
                f"已打开 {label} 客户端（{info}）。\n\n"
                f"请按以下步骤刷新账号「{who or label}」：\n"
                f"1. 在客户端里重新登录该账号，登录成功后保持在线约 10 秒；\n"
                f"2. 回到本助手，顶部切换到{label}页签并点「保存当前登录账号」；\n"
                f"3. 点「全部账号签到」验证。\n\n"
                "提示：同一客户端同时只能登录一个账号，多个账号需逐个切换处理。")
        else:
            log.warning(f"打开 {label} 客户端失败：{info}")
            if messagebox.askyesno(
                    APP_NAME,
                    f"{info}\n\n是否手动选择 {label}.exe 的位置？"):
                path = filedialog.askopenfilename(
                    title=f"请选择 {label} 主程序（.exe）",
                    filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
                if path and launch_app(Path(path)):
                    messagebox.showinfo(
                        APP_NAME,
                        f"已启动所选程序。请登录账号后，\n"
                        "回到本助手点「保存当前登录账号」再重新签到。")

    def render_accounts():
        for w in acc_list_frame.winfo_children():
            w.destroy()
        accs = list_accounts()
        if not accs:
            accounts_state_var.set("还没有账号，按下面 4 步完成第一次签到：")
            batch_btn.config(state="disabled")
            update_accounts_hint()
            guide = tk.Frame(acc_list_frame, bg=P["row"],
                             highlightbackground=P["border"], highlightthickness=1)
            guide.pack(fill="x", pady=(2, 6))
            steps = (
                ("①", "打开客户端并登录",
                 "启动 TraeWork CN / WorkBuddy 桌面端，完成登录后保持在线约 10 秒"),
                ("②", "保存当前登录账号",
                 "回到本助手点下方「保存当前登录账号」，凭据经 DPAPI 加密保存到本机"),
                ("③", "配置微信推送（可选）",
                 "在「推送设置」里填好 Server酱 / 企业微信等密钥，签到结果自动推送到微信"),
                ("④", "签到并开启自动任务",
                 "点「全部账号签到」验证成功后，到「自动任务」开启每日定时自动签到"),
            )
            for num, step_title, step_desc in steps:
                srow = tk.Frame(guide, bg=P["row"])
                srow.pack(fill="x", padx=14, pady=(12, 0))
                badge = tk.Label(srow, text=num, bg=PRIMARY, fg="white",
                                 font=(FONT, 10, "bold"), width=2, height=1)
                badge.pack(side="left", anchor="n")
                txt = tk.Frame(srow, bg=P["row"])
                txt.pack(side="left", padx=(10, 0), fill="x", expand=True)
                tk.Label(txt, text=step_title, bg=P["row"], fg=DARK,
                         font=(FONT, 10, "bold"), anchor="w").pack(anchor="w")
                tk.Label(txt, text=step_desc, bg=P["row"], fg=GRAY,
                         font=(FONT, 8), anchor="w", wraplength=440,
                         justify="left").pack(anchor="w")
            btnrow = tk.Frame(guide, bg=P["row"])
            btnrow.pack(fill="x", padx=14, pady=14)

            def _empty_open_client():
                pf = platform_var.get()
                label = PLATFORM_LABELS.get(pf, "客户端")
                ok, info = launch_platform_app(pf)
                if ok:
                    messagebox.showinfo(
                        APP_NAME,
                        f"已打开 {label} 客户端（{info}）。\n\n"
                        "登录成功并保持在线约 10 秒后，\n"
                        "回到本助手点「保存当前登录账号」。", parent=root)
                else:
                    messagebox.showwarning(
                        "未找到客户端",
                        f"{info}\n\n请确认已安装 {label} 桌面端；也可以手动启动客户端、"
                        "登录后再回来保存账号。", parent=root)

            tk.Button(btnrow, text="立即打开客户端（第 ① 步）", bg=PRIMARY,
                      fg="white", font=(FONT, 9, "bold"), relief="flat", bd=0,
                      activebackground=PRIMARY_D, activeforeground="white",
                      cursor="hand2", padx=14, pady=6,
                      command=_empty_open_client).pack(side="left")
            return
        active_n = sum(1 for a in accs if a.get("enabled", True))
        if active_n == len(accs):
            accounts_state_var.set(f"已保存 {len(accs)} 个账号（跨平台统一批量签到）：")
        else:
            accounts_state_var.set(
                f"已保存 {len(accs)} 个账号，其中 {active_n} 个参与签到"
                "（取消勾选可临时停用）：")
        batch_btn.config(state="normal" if active_n else "disabled")
        update_accounts_hint()
        status_map = account_status_lookup(90)
        for i, a in enumerate(accs):
            row = tk.Frame(acc_list_frame, bg=P["row"],
                           highlightbackground=P["border"], highlightthickness=1)
            row.pack(fill="x", pady=3)
            info = tk.Frame(row, bg=P["row"])
            info.pack(side="left", fill="x", expand=True, padx=10, pady=6)
            uname = a.get('display_name') or a['username']
            note = (a.get("note") or "").strip()
            title = f"[{a.get('platform_label') or ''}] " + (
                f"{note}（{uname}）" if note else uname)
            name_color = DARK if a.get("enabled", True) else GRAY
            tk.Label(info, text=title, bg=P["row"], fg=name_color,
                     font=(FONT, 10, "bold"), anchor="w").pack(anchor="w")
            sub_parts = [f"地区 {a.get('region') or 'CN'}"]
            st = status_map.get(history_identity(a.get("platform", ""), uname))
            if st:
                if st.get("streak"):
                    sub_parts.append(f"连续 {st['streak']} 天")
                if st.get("last_date"):
                    sub_parts.append(f"上次签到 {st['last_date']} {st.get('last_time', '')}".rstrip())
            if a.get("saved_at"):
                sub_parts.append(f"保存于 {a['saved_at']}")
            if not a.get("enabled", True):
                sub_parts.append("已停用（不参与批量/自动签到）")
            tk.Label(info, text="　·　".join(sub_parts), bg=P["row"], fg=GRAY,
                     font=(FONT, 8), anchor="w").pack(anchor="w")

            def _toggle(on_var, key=a["key"]):
                set_account_enabled(key, bool(on_var.get()))
                render_accounts()

            enable_var = tk.IntVar(value=1 if a.get("enabled", True) else 0)
            tk.Checkbutton(row, text="参与签到", variable=enable_var,
                           bg=P["row"], fg=DARK, font=(FONT, 8),
                           selectcolor=P["row"], activebackground=P["row"],
                           bd=0, command=lambda v=enable_var: _toggle(v)
                           ).pack(side="right", padx=(0, 4), pady=6)

            def _edit_note(key=a["key"], name=uname, cur=note):
                val = simpledialog.askstring(
                    "账号备注", f"为账号「{name}」设置备注名（留空清除）：",
                    initialvalue=cur, parent=root)
                if val is not None:
                    set_account_note(key, val.strip())
                    render_accounts()

            tk.Button(row, text="备注", bg=P["note_bg"], fg=P["note_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      padx=10, pady=3, bd=0, command=_edit_note).pack(
                side="right", padx=(0, 2))

            def _relogin(pf=a.get("platform", PLAT_TRAEWORK), name=title):
                guide_relogin(pf, name)

            tk.Button(row, text="重登", bg=P["warn_bg"], fg=P["warn_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      activebackground=P["warn_bg_a"],
                      padx=10, pady=3, bd=0, command=_relogin).pack(
                side="right", padx=(0, 2))

            def _del(key=a["key"], name=uname):
                if messagebox.askyesno(APP_NAME, f"确定删除账号「{name}」的保存凭证吗？\n"
                                                 "（不会影响客户端登录，仅删除本工具的快照）"):
                    delete_account(key)
                    render_accounts()

            tk.Button(row, text="删除", bg=P["del_bg"], fg=P["del_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      activebackground=P["del_bg_a"],
                      padx=10, pady=3, bd=0, command=_del).pack(
                side="right", padx=10)

    def render_history():
        for w in history_frame.winfo_children():
            w.destroy()
        try:
            summ = history_summary(30)
            rows = history_recent_rows(14)
        except Exception as e:
            history_summary_var.set("历史记录读取失败。")
            tk.Label(history_frame, text=str(e), bg=CARD, fg=GRAY,
                     font=(FONT, 9), anchor="w").pack(anchor="w")
            return
        accs = summ.get("accounts") or []
        if not accs:
            history_summary_var.set("暂无历史记录。完成一次签到后，这里会展示连续天数与近 30 天成功率。")
            return
        head_parts = []
        if summ.get("today_total"):
            head_parts.append(f"今日 {summ['today_ok']}/{summ['today_total']} 个账号成功")
        else:
            head_parts.append("今日尚无签到记录")
        head_parts.append("近 30 天：")
        history_summary_var.set("　".join(head_parts))
        # 账号统计条
        stat_frame = tk.Frame(history_frame, bg=CARD)
        stat_frame.pack(fill="x")
        for a in accs:
            name = a["username"]
            short = name if len(name) <= 14 else name[:13] + "…"
            txt = (f"[{a['platform_label']}] {short}　连续 {a['streak']} 天　"
                   f"成功率 {a['rate']}%（{a['success']}/{a['total']}）")
            color = GREEN if a["rate"] >= 90 else (STATUS_AMBER if a["rate"] >= 50
                                                   else STATUS_RED)
            tk.Label(stat_frame, text=txt, bg=CARD, fg=color,
                     font=(FONT, 9, "bold"), anchor="w").pack(anchor="w", pady=1)
        # 近 90 天签到热力图（两行 × 15 周，GitHub 风格）
        try:
            tk.Label(history_frame, text="近 90 天签到热力图", bg=CARD, fg=GRAY,
                     font=(FONT, 9, "bold"), anchor="w").pack(
                anchor="w", pady=(10, 2))
            cal = history_calendar(90)
            dates_desc = sorted(cal.keys(), reverse=True)  # 今天 → 90 天前
            weeks = [dates_desc[i:i + 7] for i in range(0, 90, 7)]
            CELL, GAP = 13, 3
            HEAT_COLORS = {"all_ok": HEAT_OK, "partial": HEAT_PARTIAL,
                           "all_fail": HEAT_FAIL, "none": P["heat_none"]}
            HEAT_DESC = {"all_ok": "全部成功", "partial": "部分失败",
                         "all_fail": "全部失败", "none": "无记录"}
            cols = max(len(w) for w in weeks)
            rows_n = len(weeks)
            cv_w = cols * (CELL + GAP) + 4
            cv_h = rows_n * (CELL + GAP) + 4
            heat = tk.Canvas(history_frame, width=cv_w, height=cv_h,
                             bg=CARD, highlightthickness=0)
            heat.pack(anchor="w")
            tip_var = tk.StringVar(value="悬停色块可查看当天详情")
            tk.Label(history_frame, textvariable=tip_var, bg=CARD, fg=GRAY,
                     font=(FONT, 8), anchor="w").pack(anchor="w")

            def _date_items(d):
                cell = cal.get(d)
                if not cell:
                    return []
                out = []
                for it in cell.get("items", []):
                    nm = it.get("note") or it.get("username") or "?"
                    if it.get("ok") and it.get("suspicious"):
                        icon = "⚠ "
                    elif it.get("ok"):
                        icon = "✓ "
                    else:
                        icon = "✗ "
                    out.append(icon + str(nm))
                return out

            for r, week in enumerate(weeks):
                for c, d in enumerate(week):
                    cell = cal[d]
                    x0 = 2 + c * (CELL + GAP)
                    y0 = 2 + r * (CELL + GAP)
                    rid = heat.create_rectangle(
                        x0, y0, x0 + CELL, y0 + CELL,
                        fill=HEAT_COLORS[cell["state"]], outline="", width=0)
                    detail = "、".join(_date_items(d))

                    def _enter(_e, dd=d, cc=cell, dx=detail):
                        if cc["total"]:
                            tip_var.set(f"{dd}　{HEAT_DESC[cc['state']]}"
                                        f"（{cc['success']}/{cc['total']}）　{dx}")
                        else:
                            tip_var.set(f"{dd}　无签到记录")

                    def _leave(_e):
                        tip_var.set("悬停色块可查看当天详情")
                    heat.tag_bind(rid, "<Enter>", _enter)
                    heat.tag_bind(rid, "<Leave>", _leave)

            legend = tk.Frame(history_frame, bg=CARD)
            legend.pack(anchor="w", pady=(2, 0))
            for st_key in ("all_ok", "partial", "all_fail", "none"):
                tk.Label(legend, text="  ", bg=HEAT_COLORS[st_key],
                         font=(FONT, 8), padx=6).pack(side="left", padx=(8, 2))
                tk.Label(legend, text=HEAT_DESC[st_key], bg=CARD, fg=GRAY,
                         font=(FONT, 8)).pack(side="left")
        except Exception as e:
            log.warning(f"热力图渲染失败：{e}")

        # 积分趋势折线图（近 30 / 90 天，最多 6 条线）
        try:
            tk.Label(history_frame, text="积分趋势（签到后积分余额）",
                     bg=CARD, fg=GRAY, font=(FONT, 9, "bold"),
                     anchor="w").pack(anchor="w", pady=(10, 2))
            trend_seg = tk.Frame(history_frame, bg=CARD)
            trend_seg.pack(anchor="w")
            trend_days_var = tk.IntVar(value=30)
            trend_box = tk.Frame(history_frame, bg=CARD)
            trend_box.pack(fill="x")

            def render_trend():
                for w in trend_box.winfo_children():
                    w.destroy()
                trend = history_credit_trend(trend_days_var.get())
                if not trend.get("has_data"):
                    tk.Label(trend_box, text="历史记录中暂无积分数据，无法绘制趋势。",
                             bg=CARD, fg=GRAY, font=(FONT, 8),
                             anchor="w").pack(anchor="w")
                    return
                W, H = 520, 170
                # BY 为 X 轴（绘图区底边），必须靠近画布底部；
                # 早期版本误写成 24，绘图区仅 12px 高，刻度与折线全挤在顶部
                LX, RX, TY, BY = 38, 10, 20, H - 24
                dates = trend["dates"]
                n = len(dates)
                max_c = trend["max_credit"]
                # 整齐刻度（取 1/2/5×10^k）且保证 vmax >= max_c：
                # 既让刻度显示为 50/100/150…，又避免数据点画出绘图区顶边
                raw_step = max(1, (max_c + 3) // 4)
                unit = 1
                while raw_step > unit * 5:
                    unit *= 10
                step = unit * 5
                for cand in (unit, unit * 2, unit * 5):
                    if cand >= raw_step:
                        step = cand
                        break
                vmax = step * 4
                cv = tk.Canvas(trend_box, width=W, height=H, bg=CARD,
                               highlightthickness=0)
                cv.pack(anchor="w")
                grid_col = P["grid"]
                axis_col = P["axis"]
                for g in range(5):
                    v = step * g
                    yy = BY + (TY - BY) * v // vmax
                    cv.create_line(LX, yy, W - RX, yy, fill=grid_col)
                    cv.create_text(LX - 6, yy, text=str(v), anchor="e",
                                   fill=GRAY, font=(FONT, 7))
                cv.create_line(LX, TY, LX, BY, fill=axis_col)
                cv.create_line(LX, BY, W - RX, BY, fill=axis_col)
                # X 轴日期刻度（30 天：每 5 天；90 天：每 15 天）
                tick_step = 5 if n <= 45 else 15
                for i in range(0, n, tick_step):
                    xx = LX + (W - RX - LX) * i // (n - 1)
                    cv.create_text(xx, BY + 12, text=dates[i][5:],
                                   anchor="n", fill=GRAY, font=(FONT, 7))
                seg_ids = []

                def x_of(i):
                    return LX + (W - RX - LX) * i // (n - 1)

                def y_of(v):
                    return BY + (TY - BY) * v // vmax

                for s in trend["series"]:
                    pts, prev = [], None
                    for i, v in enumerate(s["points"]):
                        if v is None:
                            # pts 为扁平坐标（每点 2 个数字），至少 2 个点
                            # （4 个数字）才能画线，单点只保留数据点圆点
                            if len(pts) >= 4:
                                cv.create_line(pts, fill=s["color"], width=2,
                                               capstyle="round", joinstyle="round")
                            pts = []
                            prev = None
                            continue
                        pts.extend([x_of(i), y_of(v)])
                        prev = (x_of(i), y_of(i), v, i)
                    if len(pts) >= 4:
                        cv.create_line(pts, fill=s["color"], width=2,
                                       capstyle="round", joinstyle="round")
                    # 数据点（悬停命中区）
                    for i, v in enumerate(s["points"]):
                        if v is None:
                            continue
                        cx, cy = x_of(i), y_of(v)
                        dot = cv.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                             fill=s["color"], outline=CARD)
                        seg_ids.append(dot)
                        lbl = s["label"]

                        def _enter(_e, cx=cx, cy=cy, v=v, dd=dates[i],
                                   lbl=lbl):
                            tip_var2.set(f"{dd}　{lbl}：{v} 积分")
                            cv.coords(tip_bg, cx + 6, cy - 22,
                                      cx + 6 + 9 * len(tip_var2.get()) // 2 + 10,
                                      cy - 4)
                            cv.coords(tip_txt, cx + 11, cy - 13)
                            cv.itemconfigure(tip_bg, state="normal")
                            cv.itemconfigure(tip_txt, state="normal")

                        def _leave(_e):
                            tip_var2.set("")
                            cv.itemconfigure(tip_bg, state="hidden")
                            cv.itemconfigure(tip_txt, state="hidden")
                        cv.tag_bind(dot, "<Enter>", _enter)
                        cv.tag_bind(dot, "<Leave>", _leave)
                tip_var2 = tk.StringVar(value="")
                tip_bg = cv.create_rectangle(0, 0, 0, 0, fill=P["tip_bg"],
                                             outline="", state="hidden")
                tip_txt = cv.create_text(0, 0, text="", anchor="w",
                                         fill=P["tip_fg"], font=(FONT, 8),
                                         state="hidden")
                cv.create_text(LX, 4, anchor="nw",
                               text="悬停数据点查看详情（缺失日期不断线补画）",
                               fill=GRAY, font=(FONT, 7))
                # 图例
                lg = tk.Frame(trend_box, bg=CARD)
                lg.pack(anchor="w", pady=(2, 0))
                for s in trend["series"]:
                    item = tk.Frame(lg, bg=CARD)
                    item.pack(side="left", padx=(0, 12))
                    tk.Label(item, text="  ", bg=s["color"],
                             font=(FONT, 8)).pack(side="left")
                    tk.Label(item, text=s["label"], bg=CARD, fg=DARK,
                             font=(FONT, 8)).pack(side="left", padx=(3, 0))
                if trend.get("truncated"):
                    tk.Label(trend_box,
                             text=f"账号较多，仅展示最近有记录的 {TREND_MAX_LINES} 个账号。",
                             bg=CARD, fg=GRAY, font=(FONT, 8),
                             anchor="w").pack(anchor="w")

            def _seg_flush():
                for b in (btn30, btn90):
                    active = b["days"] == trend_days_var.get()
                    b["w"].config(bg=PRIMARY if active else P["seg"],
                                  fg="#ffffff" if active else DARK,
                                  relief="flat", cursor="hand2",
                                  font=(FONT, 8, "bold" if active else "normal"))

            for label, days in (("近 30 天", 30), ("近 90 天", 90)):
                b = tk.Button(trend_seg, text=label, bd=0, padx=10, pady=2,
                              bg=P["seg"], fg=DARK, font=(FONT, 8),
                              command=lambda d=days: (
                                  trend_days_var.set(d), _seg_flush(),
                                  render_trend()))
                b.pack(side="left", padx=(0, 6))
                if days == 30:
                    btn30 = {"w": b, "days": days}
                else:
                    btn90 = {"w": b, "days": days}
            _seg_flush()
            render_trend()
        except Exception as e:
            log.warning(f"积分趋势渲染失败：{e}")

        # 近 3 个月月度统计
        try:
            monthly = history_monthly_stats(3)
            month_rows = [m for m in monthly if m.get("accounts")]
            if month_rows:
                tk.Label(history_frame, text="月度统计", bg=CARD, fg=GRAY,
                         font=(FONT, 9, "bold"), anchor="w").pack(
                    anchor="w", pady=(10, 2))
                month_box = tk.Frame(history_frame, bg=CARD)
                month_box.pack(fill="x")
                for mrow in month_rows:
                    line = tk.Frame(month_box, bg=CARD)
                    line.pack(fill="x", pady=1)
                    tk.Label(line, text=mrow["month"], bg=CARD, fg=GRAY,
                             font=(FONT, 8), width=9, anchor="w").pack(side="left")
                    for ma in mrow["accounts"]:
                        nm = ma["username"]
                        nm = nm if len(nm) <= 8 else nm[:7] + "…"
                        mcol = GREEN if ma["rate"] >= 90 else (
                            STATUS_AMBER if ma["rate"] >= 50 else STATUS_RED)
                        tk.Label(line,
                                 text=f"[{ma['platform_label']}] {nm} "
                                      f"{ma['success']}/{ma['total']}（{ma['rate']}%）",
                                 bg=CARD, fg=mcol, font=(FONT, 8),
                                 padx=6).pack(side="left")
        except Exception as e:
            log.warning(f"月度统计渲染失败：{e}")
        # 近 14 天明细
        tk.Label(history_frame, text="近 14 天明细", bg=CARD, fg=GRAY,
                 font=(FONT, 9, "bold"), anchor="w").pack(anchor="w",
                                                           pady=(10, 2))
        list_box = tk.Frame(history_frame, bg=CARD)
        list_box.pack(fill="x")
        weekday_cn = "周一 周二 周三 周四 周五 周六 周日".split()
        for row in rows:
            try:
                dt = datetime.strptime(row["date"], "%Y-%m-%d")
                date_txt = dt.strftime("%m-%d") + " " + weekday_cn[dt.weekday()]
            except Exception:
                date_txt = row["date"]
            line = tk.Frame(list_box, bg=CARD)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=date_txt, bg=CARD, fg=GRAY,
                     font=(FONT, 8), width=10, anchor="w").pack(side="left")
            for it in row["items"]:
                mark = "✓" if it.get("ok") else "✗"
                col = GREEN if it.get("ok") else STATUS_RED
                nm = it.get("username") or "?"
                nm = nm if len(nm) <= 10 else nm[:9] + "…"
                tag = f"{mark} {nm}"
                tk.Label(line, text=tag, bg=CARD, fg=col,
                         font=(FONT, 8), padx=6).pack(side="left")

    def _on_theme_changed():
        # 换肤回调：先同步闭包内的 P/BG/CARD 等颜色，再重绘两处动态区域，
        # 各自异常隔离，单区渲染失败不影响另一区（与旧整树换色行为一致）。
        _sync_theme_colors()
        try:
            render_accounts()
        except Exception:
            pass
        try:
            render_history()
        except Exception:
            pass

    theme.on_change = _on_theme_changed

    def on_save_account():
        pf = platform_var.get()
        label = PLATFORM_LABELS.get(pf, "")
        save_acc_btn.config(state="disabled", text="保存中…")

        def worker():
            ok, msg = save_current_account(pf)

            def done():
                save_acc_btn.config(state="normal")
                update_accounts_hint()
                if ok:
                    messagebox.showinfo(APP_NAME, f"已保存{label}账号：{msg}\n\n"
                                                 f"如需添加更多账号，请在{label}客户端"
                                                 "退出并登录另一个账号后，再次点击保存。")
                    render_accounts()
                else:
                    messagebox.showwarning(APP_NAME, "保存失败：" + msg)
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def render_batch_guides(results: list[dict]):
        """批量签到后，为登录失效的平台渲染"一键打开重登"按钮。"""
        for w in batch_guide_frame.winfo_children():
            w.destroy()
        # 按平台聚合失效账号（仅引导可识别为登录态问题的失败）
        bad: dict[str, list[str]] = {}
        for r in results:
            if not _is_relogin_failure(r):
                continue
            pf = r.get("platform", PLAT_TRAEWORK)
            who = r.get("note") or r.get("username") or "未知账号"
            bad.setdefault(pf, []).append(who)
        if not bad:
            return
        tk.Label(batch_guide_frame,
                 text="检测到登录态失效，可一键打开客户端处理：",
                 bg=CARD, fg=P["warn_txt"], font=(FONT, 9, "bold"),
                 anchor="w").pack(anchor="w")
        grow = tk.Frame(batch_guide_frame, bg=CARD)
        grow.pack(fill="x", pady=(4, 0))
        for pf, names in bad.items():
            label = PLATFORM_LABELS.get(pf, "客户端")
            shown = "、".join(names[:3]) + ("…" if len(names) > 3 else "")

            def _open(pf=pf, shown=shown):
                guide_relogin(pf, shown)

            tk.Button(grow, text=f"打开{label}重登（{len(names)} 个账号）",
                      bg=P["warn_bg"], fg=P["warn_txt"], font=(FONT, 9),
                      relief="flat", cursor="hand2", padx=10, pady=4, bd=0,
                      activebackground=P["warn_bg_a"], command=_open
                      ).pack(side="left", padx=(0, 8))

    def on_batch_checkin():
        accs = [a for a in list_accounts() if a.get("enabled", True)]
        if not accs:
            messagebox.showinfo(APP_NAME,
                                "没有可签到的账号：请先保存账号，或勾选账号的「参与签到」。")
            return
        batch_btn.config(state="disabled", text="签到中…")
        save_acc_btn.config(state="disabled")
        batch_result_var.set(f"正在为 {len(accs)} 个账号逐个签到，请稍候…")

        def worker():
            results = run_batch_checkin(accs, status_only=False)
            record_checkin_history(results)

            def done():
                batch_btn.config(state="normal", text="全部账号签到")
                save_acc_btn.config(state="normal")
                ok_n = sum(1 for r in results if r.get("ok"))
                susp_n = sum(1 for r in results
                             if r.get("ok") and r.get("suspicious"))
                head = f"完成：{ok_n}/{len(results)} 个账号成功"
                if susp_n:
                    head += f"，其中 {susp_n} 个未抓到成功信号，需人工确认（见 ⚠ 行）"
                lines = [head]
                for r in results:
                    if r.get("ok") and r.get("suspicious"):
                        mark = "⚠"
                    elif r.get("ok"):
                        mark = "✓"
                    else:
                        mark = "✗"
                    who = f"[{r.get('platform_label', '')}] {r.get('username')}"
                    lines.append(f"{mark} {who}：{r.get('message')}")
                batch_result_var.set("\n".join(lines))
                render_batch_guides(results)
                render_history()
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    save_acc_btn.config(command=on_save_account)
    batch_btn.config(command=on_batch_checkin)

    # ── 消息推送交互（多渠道） ──

    # 已保存的明文凭据缓存：输入框留空保存时保留原值（凭据不回显）
    saved_creds: dict[str, dict] = {}

    def load_notify_settings():
        s = load_settings()
        saved_creds.clear()
        for ch in PUSH_CHANNELS:
            saved_creds[ch] = dict(s.get("creds", {}).get(ch)
                                   or {"key": "", "secret": ""})
        push_enabled_var.set(1 if s.get("push_enabled") else 0)
        only_failures_var.set(1 if s.get("only_failures") else 0)
        enabled = set(s.get("channels") or [])
        for ch in PUSH_CHANNELS:
            ch_enable_vars[ch].set(1 if ch in enabled else 0)
            # 安全考虑不回显明文；留空保存即沿用已保存凭据
            ch_key_vars[ch].set("")
            ch_secret_vars[ch].set("")
        configured = [PUSH_CHANNEL_LABELS[ch] for ch in PUSH_CHANNELS
                      if saved_creds.get(ch, {}).get("key")]
        if configured:
            push_state_var.set("已配置凭据（本机加密保存，界面不回显）："
                               + "、".join(configured)
                               + "。输入框留空保存可保留原凭据。")
        else:
            push_state_var.set("尚未配置任何推送凭据。")
        return s

    def _collect_settings():
        creds: dict[str, dict] = {}
        enabled_channels: list[str] = []
        for ch in PUSH_CHANNELS:
            old = saved_creds.get(ch) or {"key": "", "secret": ""}
            key = ch_key_vars[ch].get().strip() or old.get("key", "")
            secret = ch_secret_vars[ch].get().strip() or old.get("secret", "")
            creds[ch] = {"key": key, "secret": secret}
            if ch_enable_vars[ch].get():
                enabled_channels.append(ch)
        return {
            "version": SETTINGS_VERSION,
            "push_enabled": bool(push_enabled_var.get()),
            "channels": enabled_channels,
            "only_failures": bool(only_failures_var.get()),
            "creds": creds,
        }

    def _persist_loaded(s: dict):
        saved_creds.clear()
        for ch in PUSH_CHANNELS:
            saved_creds[ch] = dict(s.get("creds", {}).get(ch)
                                   or {"key": "", "secret": ""})
            ch_key_vars[ch].set("")
            ch_secret_vars[ch].set("")

    def on_save_push():
        s = _collect_settings()
        enabled = s["channels"]
        if s["push_enabled"]:
            if not enabled:
                messagebox.showwarning(APP_NAME, "请至少勾选一个推送渠道。")
                return
            missing = [PUSH_CHANNEL_LABELS[ch] for ch in enabled
                       if not s["creds"][ch]["key"]]
            if missing:
                messagebox.showwarning(
                    APP_NAME,
                    "以下已勾选渠道尚未填写凭据：\n" + "、".join(missing))
                return
        try:
            save_settings_dict(s)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败：{e}")
            return
        _persist_loaded(s)
        if s["push_enabled"] and enabled:
            push_state_var.set(
                "设置已保存，推送已开启，发往："
                + "、".join(PUSH_CHANNEL_LABELS[c] for c in enabled) + "。")
        else:
            push_state_var.set("设置已保存，推送当前为关闭状态。")
        messagebox.showinfo(APP_NAME, "推送设置已保存。")

    def on_test_push():
        s = _collect_settings()
        enabled = [c for c in s["channels"] if s["creds"][c]["key"]]
        if not enabled:
            messagebox.showwarning(
                APP_NAME,
                "请先勾选渠道并填写凭据（或先保存已配置的渠道）后再测试。")
            return
        test_push_btn.config(state="disabled", text="发送中…")
        push_state_var.set("正在向 " + "、".join(
            PUSH_CHANNEL_LABELS[c] for c in enabled) + " 发送测试消息…")

        def worker():
            title, content = build_checkin_report([], test=True)
            ok, msg = push_wechat(s, title, content, kind="推送测试")

            def done():
                test_push_btn.config(
                    state="normal", text="发送测试消息（发往所有已勾选渠道）")
                push_state_var.set(("✓ " if ok else "✗ ") + msg +
                                   ("" if ok else "（请检查凭据、加签设置与网络）"))
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def on_get_key():
        import webbrowser
        guides = {
            "serverchan": (
                "https://sct.ftqq.com/",
                "Server酱获取步骤：\n"
                "1. 浏览器打开 sct.ftqq.com，点击「登入」用微信扫码\n"
                "2. 按提示关注「方糖」服务号\n"
                "3. 在「Key & API」页面复制 SCT 开头的 SendKey\n"
                "4. 粘贴回本工具对应输入框，点击「保存推送设置」"),
            "pushplus": (
                "https://www.pushplus.plus/",
                "PushPlus 获取步骤：\n"
                "1. 浏览器打开 pushplus.plus 并用微信登录\n"
                "2. 在「一对一推送」页面复制 token\n"
                "3. 粘贴到输入框并保存\n"
                "注意：PushPlus 需实名后才有推送额度。"),
            "wecom": (
                "",
                "企业微信群机器人获取步骤：\n"
                "1. 在企业微信中打开要接收通知的群聊 → 右上角「…」→「群机器人」\n"
                "2. 点击「添加机器人」，名称可填「签到助手」\n"
                "3. 复制生成的 Webhook 地址"
                "（https://qyapi.weixin.qq.com/...?key=xxxx），整条粘贴到凭据框\n"
                "4. 安全设置二选一：\n"
                "   · 自定义关键词（填写：签到），此时加签密钥留空；\n"
                "   · 或勾选「加签」，把生成的密钥填入「加签密钥」框。\n"
                "5. 点击「保存推送设置」"),
            "dingtalk": (
                "",
                "钉钉群机器人获取步骤：\n"
                "1. 在钉钉中打开要接收通知的群聊 →「群设置」→「机器人」→「添加机器人」\n"
                "2. 选择「自定义」机器人，名称可填「签到助手」\n"
                "3. 安全设置二选一或多选：\n"
                "   · 自定义关键词：签到（本工具消息均含该词）；\n"
                "   · 或勾选「加签」，复制 SEC 开头的密钥填入「加签密钥」框。\n"
                "4. 创建后复制 Webhook 地址"
                "（https://oapi.dingtalk.com/...?access_token=xxxx），整条粘贴\n"
                "5. 点击「保存推送设置」"),
        }
        enabled = [ch for ch in PUSH_CHANNELS if ch_enable_vars[ch].get()]
        chosen = enabled or ["serverchan"]
        parts = []
        for ch in chosen:
            url, guide = guides[ch]
            if url:
                try:
                    webbrowser.open(url)
                except Exception:
                    guide += f"\n（请手动在浏览器打开：{url}）"
            parts.append(guide)
        messagebox.showinfo(APP_NAME, "\n\n".join(parts))

    save_key_btn.config(command=on_save_push)
    test_push_btn.config(command=on_test_push)
    getkey_btn.config(command=on_get_key)

    render_accounts()
    render_history()
    load_notify_settings()

    def _show_status_result(r: dict):
        who = f"[{r.get('platform_label', '')}] {r.get('username', '')}"
        if r.get("ok"):
            if r.get("checked_in"):
                extra = r.get("extra_credits")
                txt = f"✓ 今日已签到（{who}）"
                if r.get("credits") is not None:
                    txt += f"，{r['credits']} 积分"
                    if extra:
                        txt += f"（含额外 {extra}）"
                result_var.set(txt)
            else:
                result_var.set(f"○ 今日尚未签到（{who}），点击「立即签到」领取。")
        else:
            result_var.set("✗ " + r.get("message", "状态查询失败"))

    def refresh_ui():
        stop_waiting()
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            refresh_ui_wb()
        else:
            refresh_ui_trae()

    def refresh_ui_trae():
        # 环境检测
        data_dir = get_app_data_dir()
        if data_dir is None and not find_install_exes():
            set_status(STATUS_RED, "未检测到 TraeWork CN 客户端",
                       "请先下载并安装 TraeWork CN（TRAE SOLO CN）桌面端，安装登录后重新打开本助手。")
            show_only(None)
            refresh_task_state()
            return

        logged_in, reason = is_logged_in()
        if not logged_in:
            exes = find_install_exes()
            login_title.config(text="需要先登录 TraeWork CN")
            if exes:
                set_status(STATUS_AMBER, "已安装，但尚未登录",
                           "点击下方按钮打开 TraeWork CN 并登录账号，助手会自动检测登录结果。")
                login_hint.config(
                    text="步骤：1) 点击「打开并登录」  2) 在应用中完成登录  "
                         "3) 登录成功后本助手自动继续。\n\n"
                         f"检测到程序：{exes[0].name}")
            else:
                set_status(STATUS_AMBER, "尚未登录",
                           "请打开 TraeWork CN 完成登录；若未安装请先安装客户端。")
                login_hint.config(text="未能自动定位程序，可点击「手动选择程序位置」指定主程序。")
            show_only(login_card)
            refresh_task_state()
            return

        # 已登录：查询状态
        login_title.config(text="需要先登录 TraeWork CN")
        set_status(GREEN, "环境正常，已登录 TraeWork CN",
                   "可在下方立即签到；点击「保存当前 TraeWork 账号」可把该账号加入批量签到列表")
        show_only(action_card)
        result_var.set("正在查询今日签到状态…")

        def worker():
            r = run_checkin(status_only=True)
            root.after(0, lambda: (_show_status_result(r), refresh_task_state()))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_ui_wb():
        auth_file = wb_find_auth_file()
        if not auth_file:
            exes = find_wb_install_exes()
            login_title.config(text="需要先登录腾讯 WorkBuddy")
            if exes:
                set_status(STATUS_AMBER, "已安装 WorkBuddy，但尚未登录",
                           "点击下方按钮打开 WorkBuddy 并登录账号，助手会自动检测登录结果。")
                login_hint.config(
                    text="步骤：1) 点击「打开并登录」  2) 在 WorkBuddy 中完成登录  "
                         "3) 登录成功后本助手自动继续。\n\n"
                         f"检测到程序：{exes[0].name}")
            else:
                set_status(STATUS_RED, "未检测到 WorkBuddy 客户端",
                           "请先下载并安装腾讯 WorkBuddy 桌面端（G:\\workb 或默认安装目录），登录后重新检测。")
                login_hint.config(text="未能自动定位 WorkBuddy，可先安装客户端，"
                                       "或点击「手动选择程序位置」指定 WorkBuddy.exe。")
            show_only(login_card)
            refresh_task_state()
            return

        logged_in, reason = wb_is_logged_in()
        if not logged_in:
            login_title.config(text="需要先登录腾讯 WorkBuddy")
            set_status(STATUS_AMBER, "检测到 WorkBuddy，但尚未登录",
                       "请打开 WorkBuddy 完成登录，登录后点击「重新检测」。")
            login_hint.config(
                text=f"凭证文件：{auth_file}\n点击「打开并登录」启动 WorkBuddy 客户端。")
            show_only(login_card)
            refresh_task_state()
            return

        login_title.config(text="需要先登录腾讯 WorkBuddy")
        set_status(GREEN, "环境正常，已登录 WorkBuddy",
                   "可在下方立即签到；点击「保存当前 WorkBuddy 账号」可把该账号加入批量签到列表")
        show_only(action_card)
        result_var.set("正在查询今日签到状态…")

        def worker():
            r = run_wb_checkin_live(status_only=True)
            root.after(0, lambda: (_show_status_result(r), refresh_task_state()))

        threading.Thread(target=worker, daemon=True).start()

    # 深色冷启动：静态控件创建时用的是字面浅色，这里按持久化主题统一着色一次（不回写）
    apply_theme(_initial_theme, persist=False)

    def notify_previous_crashes() -> None:
        """上次运行若留有崩溃转存，启动后提示一次，并提供一键诊断包入口。"""
        try:
            dumps = list_crash_dumps()
            if not dumps:
                return
            latest = dumps[0]
            mtime = datetime.fromtimestamp(
                latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            more = f"\n（共发现 {len(dumps)} 份崩溃记录）" if len(dumps) > 1 else ""
            choice = messagebox.askyesnocancel(
                f"{APP_NAME}上次运行异常退出",
                f"检测到程序在 {mtime} 发生过一次未处理异常。{more}\n\n"
                "「是」：立即导出诊断包（含崩溃堆栈，已脱敏，可发给开发者）\n"
                "「否」：查看后清除崩溃记录，不再提醒\n"
                "「取消」：暂不处理，下次启动继续提醒")
            if choice is None:
                return
            if choice:
                path = filedialog.asksaveasfilename(
                    title="导出诊断包",
                    initialfile=f"traecheckin_diagnostic_"
                                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    defaultextension=".zip",
                    filetypes=[("诊断包", "*.zip")])
                if path:
                    export_diagnostic_bundle(path)
                    messagebox.showinfo(
                        APP_NAME, f"诊断包已导出：\n{path}\n\n"
                                  "其中包含崩溃堆栈与脱敏后的环境信息，"
                                  "可发送给开发者协助排查。")
                    clear_crash_dumps()
            else:
                clear_crash_dumps()
        except Exception as e:
            log.warning(f"崩溃记录提示流程异常：{e}")

    # 首次启动：风险与隐私声明确认；测试可用 TRAESIGN_NO_DISCLAIMER=1 跳过
    if os.environ.get("TRAESIGN_NO_DISCLAIMER") != "1":
        try:
            if not load_settings().get("disclaimer_accepted"):
                root.withdraw()
                if not show_disclaimer_dialog():
                    root.destroy()
                    return 0
                accept_disclaimer()
                root.deiconify()
        except Exception as e:
            log.warning(f"首次启动声明流程异常：{e}")

    # 上次运行若异常退出，启动后提示并可一键导出含崩溃堆栈的诊断包
    if os.environ.get("TRAESIGN_NO_CRASH_NOTICE") != "1":
        root.after(300, notify_previous_crashes)

    # 初次检测放到后台，避免读取/解密阻塞 UI
    root.after(100, lambda: threading.Thread(target=refresh_ui, daemon=True).start())
    root.mainloop()
    return 0
