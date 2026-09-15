# -*- coding: utf-8 -*-
"""独立 Toplevel 弹窗：关于、免责声明、健康自检、推送历史和迁移向导。"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from ..backup import backup_user_data, restore_user_data
from ..constants import APP_COPYRIGHT, APP_NAME
from ..health import HEALTH_ERROR, HEALTH_OK, HEALTH_WARN, run_health_checks
from ..push import clear_push_history, load_push_history
from ..runtime import _log_dir
from .theme import FONT, STATUS_RED


def show_about(ctx) -> None:
    tk = ctx.tk
    root = ctx.root
    BG, CARD = ctx.BG, ctx.CARD
    PRIMARY, PRIMARY_D = ctx.PRIMARY, ctx.PRIMARY_D
    GRAY, DARK, P = ctx.GRAY, ctx.DARK, ctx.P
    messagebox = ctx.messagebox
    win = tk.Toplevel(root)
    win.title("关于本工具")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()
    box = tk.Frame(win, bg=CARD, highlightbackground=P["border"],
                   highlightthickness=1, bd=0)
    box.pack(padx=20, pady=20, fill="both", expand=True)
    tk.Label(box, text=APP_NAME, bg=CARD, fg=DARK,
             font=(FONT, 15, "bold")).pack(anchor="w", padx=20,
                                            pady=(18, 2))
    from ..runtime import APP_VERSION
    tk.Label(box, text=f"版本 v{APP_VERSION}", bg=CARD, fg=PRIMARY,
             font=(FONT, 10, "bold")).pack(anchor="w", padx=20)
    for line in (
        "支持平台：TraeWork CN / 腾讯 WorkBuddy",
        "功能：多账号批量签到 · 推送通知 · 历史统计 · 自动任务",
        "",
        "数据与隐私：",
        f"· 所有账号、密钥、历史仅保存在本机：{_log_dir()}",
        "· 凭据使用 Windows DPAPI 加密，不上传任何第三方服务器",
        "· 本工具不收集、不上传任何个人信息",
        "",
        "免责声明：本工具为个人学习用途的免费开源工具，",
        "自动化签到可能违反对应平台的服务条款，使用风险",
        "（包括但不限于账号受限）由使用者自行承担。",
        "",
        APP_COPYRIGHT,
    ):
        tk.Label(box, text=line or " ", bg=CARD,
                 fg=(GRAY if line.startswith(("·", "支持", "功能"))
                     else DARK),
                 font=(FONT, 9), justify="left", anchor="w",
                 wraplength=460).pack(anchor="w", padx=20)
    row = tk.Frame(box, bg=CARD)
    row.pack(fill="x", padx=20, pady=(14, 18))

    def _open_data_dir():
        try:
            os.startfile(str(_log_dir()))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开数据目录：{e}",
                                 parent=win)

    tk.Button(row, text="打开数据目录", bg=P["seg"], fg=DARK,
              font=(FONT, 9), relief="flat", cursor="hand2", bd=0,
              activebackground=P["seg_active"], padx=12, pady=5,
              command=_open_data_dir).pack(side="left")
    tk.Button(row, text="知道了", bg=PRIMARY, fg="white",
              font=(FONT, 9, "bold"), relief="flat", cursor="hand2",
              bd=0, activebackground=PRIMARY_D, activeforeground="white",
              padx=18, pady=5, command=win.destroy).pack(side="right")
    win.update_idletasks()
    win.geometry(f"+{root.winfo_rootx() + 80}"
                 f"+{root.winfo_rooty() + 80}")
    win.wait_window()


def show_disclaimer_dialog(ctx) -> bool:
    """首次启动风险声明弹窗：返回用户是否同意。"""
    from ..runtime import APP_VERSION
    tk, root = ctx.tk, ctx.root
    BG, CARD, PRIMARY, PRIMARY_D, GRAY, DARK, P = (
        ctx.BG, ctx.CARD, ctx.PRIMARY, ctx.PRIMARY_D, ctx.GRAY, ctx.DARK, ctx.P
    )
    win = tk.Toplevel(root)
    win.title("首次使用 · 风险与隐私声明")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()
    state = {"agree": None}

    def _close():
        if state["agree"] is None:
            state["agree"] = False
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _close)
    box = tk.Frame(win, bg=CARD, highlightbackground=P["border"],
                   highlightthickness=1, bd=0)
    box.pack(padx=18, pady=18)
    tk.Label(box, text="欢迎使用每日签到助手", bg=CARD, fg=DARK,
             font=(FONT, 14, "bold")).pack(anchor="w", padx=22, pady=(18, 4))
    tk.Label(box, text=f"版本 v{APP_VERSION} · {APP_COPYRIGHT}",
             bg=CARD, fg=GRAY, font=(FONT, 9)).pack(anchor="w", padx=22)
    lines = (
        "",
        "使用前请知悉并确认：",
        "1. 本工具为个人学习用途的免费开源工具，与 TraeWork、",
        "    腾讯 WorkBuddy 官方无关；自动化签到可能违反对应平台",
        "    的服务条款，账号受限等风险由使用者自行承担。",
        "2. 本工具直接调用平台官方接口完成签到，不经过任何第三方",
        "    服务器；账号凭据使用 Windows DPAPI 加密，仅保存在本机。",
        f"3. 数据目录：{_log_dir()}",
        "    卸载程序不会删除该目录，换电脑可用内置备份 / 换机向导迁移。",
        "4. 建议合理使用（每日一次即可），不要高频请求。",
    )
    for line in lines:
        tk.Label(box, text=line or " ", bg=CARD,
                 fg=(DARK if line and line[0].isdigit() else GRAY),
                 font=(FONT, 9), justify="left", anchor="w",
                 wraplength=470).pack(anchor="w", padx=22)
    row = tk.Frame(box, bg=CARD)
    row.pack(fill="x", padx=22, pady=(16, 18))

    def _agree():
        state["agree"] = True
        win.destroy()

    def _disagree():
        state["agree"] = False
        win.destroy()

    tk.Button(row, text="不同意并退出", bg=P["seg"], fg=DARK,
              font=(FONT, 9), relief="flat", bd=0, cursor="hand2",
              activebackground=P["seg_active"], padx=12, pady=6,
              command=_disagree).pack(side="left")
    tk.Button(row, text="我已了解并同意", bg=PRIMARY, fg="white",
              font=(FONT, 9, "bold"), relief="flat", bd=0, cursor="hand2",
              activebackground=PRIMARY_D, activeforeground="white",
              padx=16, pady=6, command=_agree).pack(side="right")
    win.update_idletasks()
    win.geometry(f"{max(win.winfo_reqwidth(), 520)}x{win.winfo_reqheight()}"
                 f"+{root.winfo_rootx() + 40}+{root.winfo_rooty() + 60}")
    win.wait_window()
    return state["agree"] is True


def show_health(ctx) -> None:
    tk, ttk, root = ctx.tk, ctx.ttk, ctx.root
    BG, CARD, PRIMARY, PRIMARY_D, GREEN, GRAY, DARK, P = (
        ctx.BG, ctx.CARD, ctx.PRIMARY, ctx.PRIMARY_D, ctx.GREEN, ctx.GRAY, ctx.DARK, ctx.P
    )
    meta = {
        HEALTH_OK: ("✓", GREEN, "正常"),
        HEALTH_WARN: ("⚠", "#c98a12", "需关注"),
        HEALTH_ERROR: ("✗", "#d33b3b", "异常"),
    }
    win = tk.Toplevel(root)
    win.title("健康自检")
    win.configure(bg=BG)
    win.transient(root)
    win.geometry("560x520")
    win.minsize(520, 460)
    head = tk.Frame(win, bg=BG)
    head.pack(fill="x", padx=18, pady=(16, 4))
    tk.Label(head, text="健康自检", bg=BG, fg=DARK,
             font=(FONT, 14, "bold")).pack(side="left")
    summary_var = tk.StringVar(value="正在检查…")
    tk.Label(head, textvariable=summary_var, bg=BG, fg=GRAY,
             font=(FONT, 9)).pack(side="right")

    canvas_wrap = tk.Frame(win, bg=BG)
    canvas_wrap.pack(fill="both", expand=True, padx=18, pady=(6, 6))
    canvas = tk.Canvas(canvas_wrap, bg=BG, highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(canvas_wrap, orient="vertical",
                        command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    list_frame = tk.Frame(canvas, bg=BG)
    canvas_window = canvas.create_window((0, 0), window=list_frame,
                                         anchor="nw")

    def _on_configure(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(canvas_window, width=canvas.winfo_width())

    list_frame.bind("<Configure>", _on_configure)
    canvas.bind("<Configure>", _on_configure)

    def _on_wheel(e):
        try:
            canvas.yview_scroll(int(-e.delta / 120), "units")
        except tk.TclError:
            pass

    canvas.bind("<Enter>",
                lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
    canvas.bind("<Leave>",
                lambda _e: canvas.unbind_all("<MouseWheel>"))
    win.bind("<Destroy>",
             lambda _e: canvas.unbind_all("<MouseWheel>"), add="+")

    foot = tk.Frame(win, bg=BG)
    foot.pack(fill="x", padx=18, pady=(0, 14))
    rerun_btn = tk.Button(foot, text="重新检查", bg=PRIMARY, fg="white",
                          font=(FONT, 9, "bold"), relief="flat", bd=0,
                          activebackground=PRIMARY_D, activeforeground="white",
                          cursor="hand2", padx=16, pady=6)
    rerun_btn.pack(side="right")
    tk.Button(foot, text="关闭", bg=P["seg"], fg=DARK,
              font=(FONT, 9), relief="flat", bd=0, cursor="hand2",
              activebackground=P["seg_active"], padx=14, pady=6,
              command=win.destroy).pack(side="right", padx=(0, 8))

    def _clear_children(parent):
        for child in parent.winfo_children():
            child.destroy()

    def _render(results):
        _clear_children(list_frame)
        counts = {HEALTH_OK: 0, HEALTH_WARN: 0, HEALTH_ERROR: 0}
        for item in results:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            icon, color, _label = meta[item["status"]]
            cardf = tk.Frame(list_frame, bg=CARD,
                             highlightbackground=P["border"],
                             highlightthickness=1, bd=0)
            cardf.pack(fill="x", pady=(0, 8))
            top_row = tk.Frame(cardf, bg=CARD)
            top_row.pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(top_row, text=icon, bg=CARD, fg=color,
                     font=(FONT, 12, "bold"), width=2).pack(side="left")
            tk.Label(top_row, text=item["title"], bg=CARD, fg=DARK,
                     font=(FONT, 10, "bold")).pack(side="left")
            tk.Label(top_row, text=meta[item["status"]][2],
                     bg=CARD, fg=color,
                     font=(FONT, 8, "bold")).pack(side="right")
            tk.Label(cardf, text=item["detail"], bg=CARD, fg=GRAY,
                     font=(FONT, 8), justify="left", anchor="w",
                     wraplength=480).pack(anchor="w", padx=(38, 12))
            if item.get("hint"):
                tk.Label(cardf, text="建议：" + item["hint"], bg=CARD,
                         fg=color, font=(FONT, 8), justify="left",
                         anchor="w", wraplength=480).pack(
                    anchor="w", padx=(38, 12), pady=(2, 10))
            else:
                tk.Frame(cardf, bg=CARD, height=8).pack()
        if counts[HEALTH_ERROR]:
            summary_var.set(
                f"{counts[HEALTH_ERROR]} 项异常 · "
                f"{counts[HEALTH_WARN]} 项需关注 · "
                f"{counts[HEALTH_OK]} 项正常")
        elif counts[HEALTH_WARN]:
            summary_var.set(
                f"{counts[HEALTH_WARN]} 项需关注 · "
                f"{counts[HEALTH_OK]} 项正常")
        else:
            summary_var.set(f"全部 {counts[HEALTH_OK]} 项正常")

    state = {"running": False}

    def _run():
        if state["running"]:
            return
        state["running"] = True
        rerun_btn.config(state="disabled", text="检查中…")
        summary_var.set("正在检查客户端、网络与任务…")
        _clear_children(list_frame)
        tk.Label(list_frame, text="检测大约需要几秒，请稍候…",
                 bg=BG, fg=GRAY, font=(FONT, 9)).pack(pady=30)

        def worker():
            try:
                results = run_health_checks()
            except Exception as e:
                results = [{"key": "fatal", "title": "健康自检",
                            "status": HEALTH_ERROR,
                            "detail": f"自检流程异常：{e}", "hint": ""}]

            def done():
                if not win.winfo_exists():
                    return
                _render(results)
                rerun_btn.config(state="normal", text="重新检查")
                state["running"] = False
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    rerun_btn.config(command=_run)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.update_idletasks()
    win.geometry(f"+{root.winfo_rootx() + 100}"
                 f"+{root.winfo_rooty() + 60}")
    _run()


def show_push_history(ctx) -> None:
    tk, root, messagebox = ctx.tk, ctx.root, ctx.messagebox
    BG, CARD, GREEN, GRAY, DARK, P = ctx.BG, ctx.CARD, ctx.GREEN, ctx.GRAY, ctx.DARK, ctx.P
    win = tk.Toplevel(root)
    win.title("消息推送历史")
    win.configure(bg=BG)
    win.transient(root)
    win.geometry("560x480")
    win.minsize(480, 360)
    try:
        win.grab_set()
    except Exception:
        pass
    tk.Label(win, text="消息推送历史（仅记录标题、渠道与结果，不含正文与密钥）",
             bg=BG, fg=GRAY, font=(FONT, 9), anchor="w"
             ).pack(fill="x", padx=14, pady=(12, 6))
    box = tk.Frame(win, bg=CARD)
    box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
    inner = tk.Frame(box, bg=CARD)
    inner.pack(fill="both", expand=True)
    items = load_push_history(limit=200)
    if not items:
        tk.Label(inner, text="暂无推送记录。\n开启推送后，测试消息与每日签到结果都会记录在这里。",
                 bg=CARD, fg=GRAY, font=(FONT, 10), justify="left"
                 ).pack(anchor="w", padx=14, pady=14)
    else:
        for it in items:
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", padx=10, pady=3)
            ok_col = GREEN if it.get("ok") else STATUS_RED
            tk.Label(row, text="✓" if it.get("ok") else "✗",
                     bg=CARD, fg=ok_col, font=(FONT, 10, "bold"),
                     width=2).pack(side="left")
            meta = (f"{it.get('ts', '')}　[{it.get('kind') or '其他'}]"
                    f"　{'、'.join(it.get('channels') or []) or '—'}")
            tk.Label(row, text=meta, bg=CARD, fg=GRAY,
                     font=(FONT, 8), anchor="w").pack(fill="x")
            title = str(it.get("title") or "")
            if len(title) > 60:
                title = title[:59] + "…"
            tk.Label(row, text=title, bg=CARD, fg=DARK,
                     font=(FONT, 9), anchor="w").pack(fill="x")
            detail = str(it.get("detail") or "")
            if detail:
                if len(detail) > 90:
                    detail = detail[:89] + "…"
                tk.Label(row, text=detail, bg=CARD, fg=GRAY,
                         font=(FONT, 8), anchor="w", wraplength=500,
                         justify="left").pack(fill="x")

    def on_clear():
        if not messagebox.askyesno(
                APP_NAME, "确定清空全部推送历史吗？此操作不可撤销。",
                parent=win):
            return
        clear_push_history()
        win.destroy()

    foot = tk.Frame(win, bg=BG)
    foot.pack(fill="x", padx=14, pady=(0, 12))
    tk.Button(foot, text="清空历史", bg=P["del_bg"], fg=P["del_txt"],
              font=(FONT, 9), relief="flat", cursor="hand2",
              activebackground=P["del_bg_a"], padx=10, pady=3, bd=0,
              command=on_clear).pack(side="left")
    tk.Button(foot, text="关闭", bg=P["btn_gray"], fg=DARK,
              font=(FONT, 9), relief="flat", cursor="hand2",
              activebackground=P["btn_gray_a"], padx=14, pady=3, bd=0,
              command=win.destroy).pack(side="right")


def show_migration(ctx, on_data_changed=None) -> None:
    """换机迁移向导：4 步引导，内嵌备份/恢复入口。"""
    tk, root, messagebox, filedialog = ctx.tk, ctx.root, ctx.messagebox, ctx.filedialog
    BG, PRIMARY, PRIMARY_D, GREEN, GRAY, DARK, P = ctx.BG, ctx.PRIMARY, ctx.PRIMARY_D, ctx.GREEN, ctx.GRAY, ctx.DARK, ctx.P
    win = tk.Toplevel(root)
    win.title("换机迁移向导")
    win.configure(bg=BG)
    win.transient(root)
    win.geometry("600x520")
    win.minsize(540, 460)
    try:
        win.grab_set()
    except Exception:
        pass

    head = tk.Frame(win, bg=BG)
    head.pack(fill="x", padx=18, pady=(16, 4))
    title_var = tk.StringVar()
    tk.Label(head, textvariable=title_var, bg=BG, fg=DARK,
             font=(FONT, 13, "bold"), anchor="w").pack(anchor="w")
    step_var = tk.StringVar()
    tk.Label(head, textvariable=step_var, bg=BG, fg=GRAY,
             font=(FONT, 9), anchor="w").pack(anchor="w", pady=(2, 0))

    content = tk.Frame(win, bg=BG)
    content.pack(fill="both", expand=True, padx=18, pady=8)

    nav = tk.Frame(win, bg=BG)
    nav.pack(fill="x", padx=18, pady=(0, 14))
    prev_btn = tk.Button(nav, text="上一步", bg=P["btn_gray"], fg=DARK,
                         font=(FONT, 9), relief="flat", cursor="hand2",
                         activebackground=P["btn_gray_a"],
                         padx=14, pady=4, bd=0)
    prev_btn.pack(side="left")
    next_btn = tk.Button(nav, text="下一步", bg=PRIMARY, fg="white",
                         font=(FONT, 9, "bold"), relief="flat", cursor="hand2",
                         activebackground=PRIMARY_D, activeforeground="white",
                         padx=18, pady=4, bd=0)
    next_btn.pack(side="right")
    close_btn = tk.Button(nav, text="关闭", bg=P["btn_gray"], fg=DARK,
                          font=(FONT, 9), relief="flat", cursor="hand2",
                          activebackground=P["btn_gray_a"],
                          padx=14, pady=4, bd=0, command=win.destroy)
    close_btn.pack(side="right", padx=(0, 8))

    state = {"step": 0, "backup_path": ""}

    def _para(parent, text, color=None, bold=False):
        tk.Label(parent, text=text, bg=BG, fg=color or DARK,
                 font=(FONT, 10, "bold" if bold else "normal"),
                 justify="left", anchor="w", wraplength=540
                 ).pack(anchor="w", pady=3)

    def _warn_box(parent, text):
        box = tk.Frame(parent, bg=P["warn_bg"], highlightthickness=0)
        box.pack(fill="x", pady=8)
        tk.Label(box, text=text, bg=P["warn_bg"], fg=P["warn_txt"],
                 font=(FONT, 9, "bold"), justify="left", anchor="w",
                 wraplength=512, padx=12, pady=10).pack(anchor="w")

    def _ok_box(parent, text):
        box = tk.Frame(parent, bg=P["ok_bg"], highlightthickness=0)
        box.pack(fill="x", pady=8)
        tk.Label(box, text=text, bg=P["ok_bg"], fg=GREEN,
                 font=(FONT, 9), justify="left", anchor="w",
                 wraplength=512, padx=12, pady=10).pack(anchor="w")

    def _do_backup_in_wizard():
        try:
            default_name = f"签到助手备份_{datetime.now().strftime('%Y%m%d')}.zip"
            path = filedialog.asksaveasfilename(
                parent=win, title="把备份保存到 U 盘 / 网盘 / 非系统盘",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name, defaultextension=".zip",
                filetypes=[("备份压缩包", "*.zip")])
            if not path:
                return
            info = backup_user_data(path)
            state["backup_path"] = path
            n = sum(1 for v in info.values() if v)
            _ok_box(content, f"已生成备份（含 {n} 类数据）：\n{path}\n"
                             "请确认该文件已放到 U 盘或网盘，可在新电脑访问。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"备份失败：{e}", parent=win)

    def _do_restore_in_wizard():
        try:
            path = filedialog.askopenfilename(
                parent=win, title="选择从旧电脑带来的备份压缩包",
                filetypes=[("备份压缩包", "*.zip"), ("所有文件", "*.*")])
            if not path:
                return
            res = restore_user_data(path)
            state["restored"] = "、".join(res["restored"])
            _ok_box(content, f"已恢复：{state['restored']}\n"
                             "设置即时生效，建议关闭后重新打开本工具。")
            if on_data_changed is not None:
                on_data_changed()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"恢复失败：{e}", parent=win)

    steps = [
        {
            "title": "第 1 步（旧电脑）：确认账号可手动登录",
            "sub": "迁移前最重要的准备",
            "render": lambda: (
                _warn_box(content, "重要：账号与推送凭据经 Windows DPAPI 加密，"
                                  "绑定旧电脑的当前用户，恢复到新电脑后需重新登录验证，"
                                  "无法直接解密使用。"),
                _para(content, "请在旧电脑上逐一确认："),
                _para(content, "1. TraeWork CN、腾讯 WorkBuddy 客户端均可正常打开并已登录；"),
                _para(content, "2. 你记得各账号的登录方式（手机号 / 邮箱 / 扫码）；"),
                _para(content, "3. Server酱 / PushPlus / 企业微信 / 钉钉 的推送密钥可在对应平台重新获取。"),
                _para(content, "建议先在旧电脑完成一次手动签到，确认账号状态正常后再继续。",
                      color=GRAY),
            ),
        },
        {
            "title": "第 2 步（旧电脑）：备份并拷出数据",
            "sub": "一键打包账号、设置、历史与推送记录",
            "render": lambda: (
                _para(content, "点击下方按钮生成备份压缩包，并保存到 U 盘、移动硬盘"
                              "或网盘（不要只放在旧电脑桌面）："),
                tk.Button(content, text="① 生成备份包（另存为…）",
                          bg=P["ok_bg"], fg=GREEN, font=(FONT, 10, "bold"),
                          relief="flat", cursor="hand2",
                          activebackground=P["ok_bg_a"],
                          padx=14, pady=6, bd=0,
                          command=_do_backup_in_wizard).pack(anchor="w", pady=6),
                _para(content, "备份包含：accounts.json、settings.json、"
                              "checkin_history.json、push_history.json。", color=GRAY),
                _warn_box(content, "再次提醒：凭据密文随备份带走，但只能在"
                                  "「同一台电脑同一用户」下解密；换机后必须重新登录。"),
            ),
        },
        {
            "title": "第 3 步（新电脑）：安装并恢复",
            "sub": "在新电脑完成安装后恢复数据",
            "render": lambda: (
                _para(content, "在新电脑上："),
                _para(content, "1. 安装 TraeWork CN 与腾讯 WorkBuddy 客户端；"),
                _para(content, "2. 把本工具（exe 或源码）放到新电脑，先启动一次；"),
                _para(content, "3. 把备份压缩包拷到新电脑本地磁盘（U 盘内也可直接选）；"),
                _para(content, "4. 点击下方按钮选择备份包恢复（当前同名文件会自动另存"
                              "为 .restore-bak）："),
                tk.Button(content, text="② 选择备份包并恢复",
                          bg=P["note_bg"], fg=P["note_txt"],
                          font=(FONT, 10, "bold"), relief="flat", cursor="hand2",
                          padx=14, pady=6, bd=0,
                          command=_do_restore_in_wizard).pack(anchor="w", pady=6),
            ),
        },
        {
            "title": "第 4 步（新电脑）：逐账号重新登录确认",
            "sub": "完成迁移的最后一步",
            "render": lambda: (
                _para(content, "恢复后请逐个账号完成："),
                _para(content, "1. 在客户端登录对应账号，点「重登」按钮可一键唤起引导；"),
                _para(content, "2. 登录后在本工具点「保存当前登录账号」刷新凭据快照；"),
                _para(content, "3. 在「推送设置」中重新填写各渠道密钥并发一条测试推送；"),
                _para(content, "4. 确认「开机自动签到」计划任务时间（换机后需重新开启）；"),
                _para(content, "5. 手动执行一次「全部账号签到」验证全流程。"),
                _ok_box(content, "签到历史与推送记录会原样保留；新凭据写入后，"
                                "自动签到与微信推送即恢复正常。"),
            ),
        },
    ]

    def render_step():
        for w in content.winfo_children():
            w.destroy()
        st = steps[state["step"]]
        title_var.set(st["title"])
        step_var.set(f"步骤 {state['step'] + 1} / {len(steps)}")
        st["render"]()
        prev_btn.config(state=("disabled" if state["step"] == 0 else "normal"))
        next_btn.config(text=("完成" if state["step"] == len(steps) - 1 else "下一步"))

    def go_next():
        if state["step"] == len(steps) - 1:
            win.destroy()
            return
        if state["step"] == 1 and not state.get("backup_path"):
            if not messagebox.askyesno(
                    "确认跳过备份", "尚未在本向导中生成备份包。\n"
                                  "如果你已在「备份」按钮中自行备份，请选「是」继续；"
                                  "否则建议选「否」先生成备份。", parent=win):
                return
        state["step"] += 1
        render_step()

    def go_prev():
        if state["step"] > 0:
            state["step"] -= 1
            render_step()

    prev_btn.config(command=go_prev)
    next_btn.config(command=go_next)
    render_step()
