# -*- coding: utf-8 -*-
"""GUI 主题色板与整树换色逻辑。"""

from __future__ import annotations

from ..settings import load_settings, save_theme_preference


FONT = "Microsoft YaHei UI"

PALETTES = {
    "light": {
        "bg": "#f4f6fb", "card": "#ffffff", "primary": "#2f6bff",
        "primary_d": "#1f56e0", "green": "#1a9e5c", "gray": "#6b7280",
        "dark": "#1f2937", "border": "#e3e8f0", "seg": "#dfe5f0",
        "seg_active": "#cfd7ea", "row": "#f7f9fd", "btn_gray": "#eef1f7",
        "btn_gray_a": "#dde3ee", "hint": "#b6bdca", "footer": "#9aa3b2",
        "csv_bg": "#eef3ff", "csv_bg_a": "#dde7ff", "blue_txt": "#2f6bff",
        "info_bg": "#e8f4ff", "info_bg_a": "#d2e9ff", "info_txt": "#1172b8",
        "diag_bg": "#f3f0ff", "diag_bg_a": "#e6e0ff", "diag_txt": "#6d4aff",
        "warn_bg": "#fff4e5", "warn_bg_a": "#ffe7c2", "warn_txt": "#c7771f",
        "del_bg": "#fdecec", "del_bg_a": "#f9d6d6", "del_txt": "#c0392b",
        "note_bg": "#eef3fb", "note_txt": "#2c5fa8",
        "ok_bg": "#eafaf1", "ok_bg_a": "#d4f2e0",
        "entry_bg": "#ffffff", "entry_fg": "#1f2937",
        "grid": "#e9edf5", "axis": "#c7cedb", "heat_none": "#eef1f6",
        "tip_bg": "#1f2937", "tip_fg": "#ffffff",
    },
    "dark": {
        "bg": "#15181f", "card": "#1e232c", "primary": "#5b8cff",
        "primary_d": "#3f6fe0", "green": "#34c47a", "gray": "#9aa4b2",
        "dark": "#e6eaf0", "border": "#333a47", "seg": "#2a303b",
        "seg_active": "#3a4354", "row": "#262c37", "btn_gray": "#2c333f",
        "btn_gray_a": "#39424f", "hint": "#6b7482", "footer": "#7c8594",
        "csv_bg": "#22304a", "csv_bg_a": "#2d3f60", "blue_txt": "#8ab0ff",
        "info_bg": "#1f3145", "info_bg_a": "#2a415c", "info_txt": "#6fb6ef",
        "diag_bg": "#2e2a45", "diag_bg_a": "#3b3558", "diag_txt": "#a994ff",
        "warn_bg": "#3d2f17", "warn_bg_a": "#4e3c1d", "warn_txt": "#e0a53d",
        "del_bg": "#40242a", "del_bg_a": "#522e35", "del_txt": "#f08a8a",
        "note_bg": "#243247", "note_txt": "#8ab0dd",
        "ok_bg": "#1d3a2a", "ok_bg_a": "#264b36",
        "entry_bg": "#232a35", "entry_fg": "#e6eaf0",
        "grid": "#2c333f", "axis": "#46505f", "heat_none": "#2a303b",
        "tip_bg": "#e6eaf0", "tip_fg": "#15181f",
    },
}

# 语义状态色在两套主题下都保持醒目，不参与 token 重映射。
STATUS_RED, STATUS_AMBER = "#e0533d", "#e0a53d"
HEAT_OK, HEAT_PARTIAL, HEAT_FAIL = "#22c55e", "#f59e0b", "#ef4444"


def initial_theme_name() -> str:
    """读取持久化主题偏好，异常或非法值时回退浅色。"""
    name = "light"
    try:
        name = load_settings().get("theme", "light")
        if name not in PALETTES:
            name = "light"
    except Exception:
        pass
    return name


class ThemeManager:
    """管理当前调色板，并负责把已有 Tk 控件树递归切换到新主题。"""

    def __init__(self, root, style, name: str = "light"):
        self.root = root
        self.style = style
        self.name = name if name in PALETTES else "light"
        self.palette = dict(PALETTES[self.name])
        self._hex_to_token = self._build_hex_map()
        self.on_change = None

    @staticmethod
    def _build_hex_map() -> dict[str, str]:
        light_first = {
            "#f4f6fb": "bg", "#ffffff": "card", "#2f6bff": "primary",
            "#1f56e0": "primary_d", "#1a9e5c": "green", "#6b7280": "gray",
            "#1f2937": "dark", "#e3e8f0": "border", "#dfe5f0": "seg",
            "#cfd7ea": "seg_active", "#f7f9fd": "row", "#eef1f7": "btn_gray",
            "#dde3ee": "btn_gray_a", "#b6bdca": "hint", "#9aa3b2": "footer",
            "#eef3ff": "csv_bg", "#dde7ff": "csv_bg_a",
            "#e8f4ff": "info_bg", "#d2e9ff": "info_bg_a", "#1172b8": "info_txt",
            "#f3f0ff": "diag_bg", "#e6e0ff": "diag_bg_a", "#6d4aff": "diag_txt",
            "#fff4e5": "warn_bg", "#ffe7c2": "warn_bg_a", "#c7771f": "warn_txt",
            "#fdecec": "del_bg", "#f9d6d6": "del_bg_a", "#c0392b": "del_txt",
            "#eef3fb": "note_bg", "#2c5fa8": "note_txt",
            "#eafaf1": "ok_bg", "#d4f2e0": "ok_bg_a",
            "#e9edf5": "grid", "#c7cedb": "axis", "#eef1f6": "heat_none",
            "#15804c": "green",
        }
        # 深色值后写入，保证在两套色值意外相同时以当前换肤需求为准。
        light_first.update({v: k for k, v in PALETTES["dark"].items()})
        return light_first

    @property
    def P(self) -> dict[str, str]:
        return self.palette

    @property
    def colors(self) -> dict[str, str]:
        return {
            "BG": self.palette["bg"],
            "CARD": self.palette["card"],
            "PRIMARY": self.palette["primary"],
            "PRIMARY_D": self.palette["primary_d"],
            "GREEN": self.palette["green"],
            "GRAY": self.palette["gray"],
            "DARK": self.palette["dark"],
        }

    def mapped_color(self, value):
        if not isinstance(value, str):
            return value
        key = value.lower()
        token = self._hex_to_token.get(key)
        return self.palette[token] if token else value

    def configure_widget(self, widget) -> None:
        cls = widget.winfo_class()
        try:
            if cls in ("Frame", "Toplevel", "Canvas"):
                if "bg" in widget.keys():
                    bg = str(widget.cget("bg")).lower()
                    if bg in self._hex_to_token:
                        widget.configure(bg=self.mapped_color(bg))
                if cls == "Frame" and "highlightbackground" in widget.keys():
                    hb = str(widget.cget("highlightbackground")).lower()
                    if hb in self._hex_to_token:
                        widget.configure(highlightbackground=self.mapped_color(hb))
            elif cls in ("Label", "Button"):
                kwargs = {}
                bg = str(widget.cget("bg")).lower()
                if bg in self._hex_to_token:
                    kwargs["bg"] = self.mapped_color(bg)
                fg = str(widget.cget("fg")).lower()
                if fg in self._hex_to_token:
                    kwargs["fg"] = self.mapped_color(fg)
                if "activebackground" in widget.keys():
                    value = str(widget.cget("activebackground")).lower()
                    if value in self._hex_to_token:
                        kwargs["activebackground"] = self.mapped_color(value)
                if "activeforeground" in widget.keys():
                    value = str(widget.cget("activeforeground")).lower()
                    if value in self._hex_to_token:
                        kwargs["activeforeground"] = self.mapped_color(value)
                if "selectcolor" in widget.keys():
                    value = str(widget.cget("selectcolor")).lower()
                    if value in self._hex_to_token:
                        kwargs["selectcolor"] = self.mapped_color(value)
                if kwargs:
                    widget.configure(**kwargs)
            elif cls == "Entry":
                widget.configure(
                    bg=self.palette["entry_bg"],
                    fg=self.palette["entry_fg"],
                    insertbackground=self.palette["entry_fg"],
                    highlightbackground=self.palette["border"],
                )
            elif cls in ("Checkbutton", "Radiobutton"):
                kwargs = {}
                bg = str(widget.cget("bg")).lower()
                if bg in self._hex_to_token:
                    kwargs["bg"] = self.mapped_color(bg)
                fg = str(widget.cget("fg")).lower()
                if fg in self._hex_to_token:
                    kwargs["fg"] = self.mapped_color(fg)
                select = str(widget.cget("selectcolor")).lower()
                if select in self._hex_to_token:
                    kwargs["selectcolor"] = self.mapped_color(select)
                if "activebackground" in widget.keys():
                    value = str(widget.cget("activebackground")).lower()
                    if value in self._hex_to_token:
                        kwargs["activebackground"] = self.mapped_color(value)
                if kwargs:
                    widget.configure(**kwargs)
        except Exception:
            pass

    def walk_and_theme(self, widget) -> None:
        self.configure_widget(widget)
        for child in widget.winfo_children():
            self.walk_and_theme(child)

    def apply(self, name: str, persist: bool = True) -> None:
        if name not in PALETTES:
            name = "light"
        self.name = name
        self.palette.update(PALETTES[name])
        self.root.configure(bg=self.palette["bg"])
        try:
            self.style.configure(
                "Vertical.TScrollbar",
                background=self.palette["seg"],
                troughcolor=self.palette["bg"],
            )
        except Exception:
            pass
        self.walk_and_theme(self.root)
        if self.on_change is not None:
            self.on_change()
        if persist:
            save_theme_preference(name)

    def toggle(self) -> str:
        return "dark" if self.name == "light" else "light"
