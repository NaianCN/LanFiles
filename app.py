#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网传文件（LanFiles）—— 桌面 App：启动服务、显示连接地址与二维码、管理设备。
"""

import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import List, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox
from transfer import Limits, add_limit_arguments, limits_from_args

# 导入同目录的 QR 生成器
try:
    from qr_core import generate_qr_photo
except ImportError:
    generate_qr_photo = None

# 字体与配色规范（iOS Human Interface Guidelines 参考）
FONT_FAMILY = "PingFang SC"
MONO_FAMILY = "Menlo"

COLOR_BG = "#F2F2F7"          # iOS 分组表格背景
COLOR_CARD = "#FFFFFF"        # iOS 白色卡片
COLOR_BORDER = "#E5E5EA"      # iOS 细分割线
COLOR_BLUE = "#007AFF"        # iOS 系统蓝
COLOR_BLUE_HOVER = "#0062D6"
COLOR_BLUE_ACTIVE = "#004FB8"
COLOR_BLUE_BG = "#F0F7FF"     # 地址框浅蓝底色
COLOR_GREEN = "#34C759"       # iOS 系统绿
COLOR_GREEN_BG = "#E8F8EE"
COLOR_RED = "#FF3B30"         # iOS 系统红
COLOR_RED_HOVER = "#D70015"
COLOR_RED_BG = "#FEECEC"
COLOR_GRAY_BTN = "#E5E5EA"    # iOS 浅灰按钮背景
COLOR_GRAY_HOVER = "#D1D1D6"
COLOR_TEXT = "#1C1C1E"        # 主要文字
COLOR_SECONDARY = "#8E8E93"   # 次要说明文字
COLOR_HEADER = "#6D6D72"      # 分组小标题文字


class IOSButton(tk.Label):
    """iOS 风格交互按钮（支持自定义背景、悬停变色与点击动画）。"""
    def __init__(self, parent, text, command=None,
                 bg_color=COLOR_BLUE, fg_color="#FFFFFF",
                 hover_bg=COLOR_BLUE_HOVER, active_bg=COLOR_BLUE_ACTIVE,
                 font=(FONT_FAMILY, 11, "bold"),
                 padx=11, pady=5, **kwargs):
        super().__init__(
            parent, text=text, bg=bg_color, fg=fg_color,
            font=font, padx=padx, pady=pady, cursor="hand2",
            relief="flat", **kwargs
        )
        self.cmd = command
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.hover_bg = hover_bg
        self.active_bg = active_bg
        self.enabled = True

        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, e):
        if self.enabled:
            self.configure(bg=self.hover_bg)

    def _on_leave(self, e):
        if self.enabled:
            self.configure(bg=self.bg_color)

    def _on_press(self, e):
        if self.enabled:
            self.configure(bg=self.active_bg)

    def _on_release(self, e):
        if self.enabled:
            self.configure(bg=self.hover_bg)
            if self.cmd:
                self.cmd()

    def set_theme(self, bg_color, fg_color, hover_bg=None, active_bg=None):
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.hover_bg = hover_bg or bg_color
        self.active_bg = active_bg or hover_bg or bg_color
        self.configure(bg=bg_color, fg=fg_color)


def check_port_available(port: int) -> bool:
    """检查指定端口是否可用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_available_port(preferred: int = 8000) -> int:
    """自动寻找空闲端口。"""
    if check_port_available(preferred):
        return preferred
    for p in (8001, 8002, 8080, 8888, 9000, 9001):
        if check_port_available(p):
            return p
    return preferred


def get_lan_ips() -> Tuple[str, List[str]]:
    """获取本机所有可用局域网 IPv4 地址。"""
    ips = set()

    # macOS 原生快速接口查询
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2", "en3", "bridge0", "p2p0"):
            try:
                res = subprocess.run(
                    ["ipconfig", "getifaddr", iface],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                ip = res.stdout.strip()
                if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    ips.add(ip)
            except Exception:
                pass

    # UDP 路由探针
    for target in ("8.8.8.8", "1.1.1.1", "114.114.114.114"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            ips.add(s.getsockname()[0])
            break
        except Exception:
            pass
        finally:
            s.close()

    # 主机名解析
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass

    def _sort_key(ip):
        if ip.startswith("127."):
            return (3, ip)
        if ip.startswith("169.254."):
            return (2, ip)
        if ip.startswith(("192.168.", "10.")):
            return (0, ip)
        return (1, ip)

    valid_ips = [ip for ip in ips if not ip.startswith("127.")]
    if valid_ips:
        valid_ips.sort(key=_sort_key)
        primary = valid_ips[0]
        others = valid_ips[1:]
    else:
        primary = "127.0.0.1"
        others = []

    return primary, others


SETTINGS_PATH = os.path.expanduser("~/Library/Application Support/LanFiles/settings.json")


def load_settings() -> dict:
    settings = {"password": ""}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("password"), str):
            settings["password"] = data["password"][:128]
    except (OSError, ValueError, AttributeError):
        pass
    return settings


def save_settings(settings: dict):
    """密码以明文保存在仅本人可读的设置文件（0600）中，供下次启动使用。"""
    os.makedirs(os.path.dirname(SETTINGS_PATH), mode=0o700, exist_ok=True)
    fd = os.open(SETTINGS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    os.chmod(SETTINGS_PATH, 0o600)


def short_path(path: str, limit: int = 42) -> str:
    """界面显示用：用户目录写成 ~，过长时只保留末尾。"""
    home = os.path.expanduser("~")
    if home and home != "~" and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def resolve_spool_dir(desired=None) -> str:
    """自动探测并返回首个实际可写的中转目录。"""
    if desired:
        try:
            os.makedirs(desired, exist_ok=True)
            with tempfile.TemporaryFile(dir=desired) as f:
                f.write(b"ok")
                f.flush()
            return os.path.abspath(desired)
        except OSError:
            pass

    home = os.path.expanduser("~")
    candidates = []
    if home and home != "~":
        candidates.append(os.path.join(home, "Downloads", "lanfiles"))
        candidates.append(os.path.join(home, "lanfiles"))
    candidates.append(os.path.join(tempfile.gettempdir(), "lanfiles"))

    for c in candidates:
        try:
            os.makedirs(c, exist_ok=True)
            with tempfile.TemporaryFile(dir=c) as f:
                f.write(b"ok")
                f.flush()
            return os.path.abspath(c)
        except OSError:
            continue

    fallback = os.path.join(tempfile.gettempdir(), "lanfiles")
    os.makedirs(fallback, exist_ok=True)
    return os.path.abspath(fallback)


class LanFilesApp:
    def __init__(self, root: tk.Tk, limits=None):
        self.root = root
        self.limits = limits or Limits()
        self.root.title("局域网传文件")
        self.root.geometry("680x760")
        self.root.minsize(560, 520)
        self.root.configure(bg=COLOR_BG)

        # 状态与配置
        self.server_port = find_available_port(8000)
        self.server_process = None
        self.settings = load_settings()
        self.spool_dir = resolve_spool_dir()
        self.primary_ip, self.other_ips = get_lan_ips()
        self.qr_image = None
        self.log_queue = queue.Queue()
        self._is_closing = False
        self.logs_expanded = False
        self.devices_expanded = True
        self.devices = None          # None = 尚未取得；[] = 没有设备
        self._devices_signature = None
        self._device_fetching = False
        # 本机管理接口走回环地址，绕过系统代理。
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        # 定位 transfer.py
        self.transfer_script = self._find_transfer_script()

        # 构建界面
        self._build_ui()

        # 启动后台服务
        self.start_server()
        self.root.after(100, self._process_log_queue)
        self.root.after(800, self._poll_devices)

        # 快捷键与退出处理
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Command-q>", lambda e: self.on_close())
        # 程序坞或菜单栏的“退出”也要先停止服务。
        self.root.createcommand("tk::mac::Quit", self.on_close)
        self.root.bind("<Command-w>", lambda e: self.on_close())

    def _find_transfer_script(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        for p in (
            os.path.join(base_dir, "transfer.py"),
            os.path.join(base_dir, "..", "transfer.py"),
            os.path.join(base_dir, "Resources", "transfer.py"),
        ):
            if os.path.exists(p):
                return os.path.abspath(p)
        return "transfer.py"

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _card(self, parent, **kwargs):
        options = dict(bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1, padx=14, pady=12)
        options.update(kwargs)
        return tk.Frame(parent, **options)

    def _section_title(self, parent, text):
        return tk.Label(parent, text=text, font=(FONT_FAMILY, 12, "bold"), bg=COLOR_BG, fg=COLOR_HEADER)

    def _gray_button(self, parent, text, command, fg=COLOR_TEXT):
        return IOSButton(parent, text=text, command=command, bg_color=COLOR_GRAY_BTN, fg_color=fg,
                         hover_bg=COLOR_GRAY_HOVER, font=(FONT_FAMILY, 11), padx=10, pady=4)

    def _build_ui(self):
        self.main_container = tk.Frame(self.root, bg=COLOR_BG, padx=16, pady=14)
        self.main_container.pack(fill=tk.BOTH, expand=True)

        # 1. 标题栏 ---------------------------------------------------------
        header = tk.Frame(self.main_container, bg=COLOR_BG)
        header.pack(fill=tk.X, pady=(0, 12))
        tk.Label(header, text="局域网传文件", font=(FONT_FAMILY, 20, "bold"),
                 bg=COLOR_BG, fg=COLOR_TEXT).pack(side=tk.LEFT)

        self.status_pill = tk.Frame(header, bg=COLOR_GREEN_BG, padx=10, pady=3)
        self.status_pill.pack(side=tk.RIGHT)
        self.status_dot = tk.Label(self.status_pill, text="●", fg=COLOR_GREEN, bg=COLOR_GREEN_BG, font=("Arial", 10))
        self.status_dot.pack(side=tk.LEFT, padx=(0, 4))
        self.status_text = tk.Label(self.status_pill, text="", fg=COLOR_GREEN, bg=COLOR_GREEN_BG,
                                    font=(FONT_FAMILY, 11, "bold"))
        self.status_text.pack(side=tk.LEFT)

        # 2. 连接地址 -------------------------------------------------------
        card1 = self._card(self.main_container)
        card1.pack(fill=tk.X, pady=(0, 12))

        self.qr_container = tk.Frame(card1, bg="#FFFFFF")
        self.qr_container.pack(side=tk.LEFT, anchor="center", padx=(0, 16))
        self.qr_label = tk.Label(self.qr_container, bg="#FFFFFF")
        self.qr_label.pack()

        right_body = tk.Frame(card1, bg=COLOR_CARD)
        right_body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.url_var = tk.StringVar(value=self._base_url(self.primary_ip))
        self.url_display = tk.Label(right_body, textvariable=self.url_var, font=(MONO_FAMILY, 18, "bold"),
                                    fg=COLOR_BLUE, bg=COLOR_CARD, cursor="hand2", anchor="w")
        self.url_display.pack(fill=tk.X, pady=(2, 10))
        self.url_display.bind("<Button-1>", lambda e: self.copy_url())

        btn_bar = tk.Frame(right_body, bg=COLOR_CARD)
        btn_bar.pack(fill=tk.X)
        self.copy_btn = IOSButton(btn_bar, text="复制", command=self.copy_url, padx=14, pady=4)
        self.copy_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.open_browser_btn = self._gray_button(btn_bar, "打开", self.open_in_browser, fg=COLOR_BLUE)
        self.open_browser_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.refresh_ip_btn = self._gray_button(btn_bar, "刷新", self.refresh_ips)
        self.refresh_ip_btn.pack(side=tk.LEFT)

        self.other_frame = tk.Frame(right_body, bg=COLOR_CARD)
        self.other_frame.pack(fill=tk.X, pady=(10, 0))
        self._render_other_ips()

        # 3. 设备（可折叠） -------------------------------------------------
        dev_header = tk.Frame(self.main_container, bg=COLOR_BG, cursor="hand2")
        dev_header.pack(fill=tk.X, pady=(0, 6))
        self.devices_title = self._section_title(dev_header, "▾ 设备")
        self.devices_title.configure(cursor="hand2")
        self.devices_title.pack(side=tk.LEFT)
        self.devices_count = tk.Label(dev_header, text="", font=(FONT_FAMILY, 11), bg=COLOR_BG, fg=COLOR_SECONDARY)
        self.devices_count.pack(side=tk.RIGHT)
        for widget in (dev_header, self.devices_title, self.devices_count):
            widget.bind("<Button-1>", lambda e: self.toggle_devices())

        self.devices_card = self._card(self.main_container, padx=0, pady=0)
        self.devices_card.pack(fill=tk.X, pady=(0, 12))
        self.devices_canvas = tk.Canvas(self.devices_card, bg=COLOR_CARD, highlightthickness=0, height=44)
        self.devices_scroll = tk.Scrollbar(self.devices_card, command=self.devices_canvas.yview)
        self.devices_canvas.configure(yscrollcommand=self.devices_scroll.set)
        self.devices_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.devices_inner = tk.Frame(self.devices_canvas, bg=COLOR_CARD)
        self._devices_window = self.devices_canvas.create_window((0, 0), window=self.devices_inner, anchor="nw")
        self.devices_canvas.bind("<Configure>", lambda e: self.devices_canvas.itemconfigure(self._devices_window, width=e.width))
        self.devices_inner.bind("<Configure>", lambda e: self.devices_canvas.configure(scrollregion=self.devices_canvas.bbox("all")))
        self.devices_canvas.bind("<Enter>", lambda e: self.devices_canvas.bind_all("<MouseWheel>", self._on_devices_wheel))
        self.devices_canvas.bind("<Leave>", lambda e: self.devices_canvas.unbind_all("<MouseWheel>"))
        self._render_devices()

        # 4. 设置 -----------------------------------------------------------
        self._section_title(self.main_container, "设置").pack(anchor="w", pady=(0, 6))
        card2 = self._card(self.main_container, pady=8)
        card2.pack(fill=tk.X, pady=(0, 12))

        row1 = tk.Frame(card2, bg=COLOR_CARD)
        row1.pack(fill=tk.X, pady=4)
        tk.Label(row1, text="端口", width=4, anchor="w", font=(FONT_FAMILY, 12), bg=COLOR_CARD, fg=COLOR_TEXT).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.server_port))
        self.port_entry = tk.Entry(row1, textvariable=self.port_var, width=6, font=(MONO_FAMILY, 12),
                                   relief="flat", bg=COLOR_BG, highlightthickness=1,
                                   highlightbackground=COLOR_BORDER, highlightcolor=COLOR_BLUE)
        self.port_entry.pack(side=tk.LEFT, padx=(4, 8), ipady=3)
        self.port_entry.bind("<Return>", lambda e: self.change_port_and_restart())
        self.restart_btn = self._gray_button(row1, "应用", self.change_port_and_restart)
        self.restart_btn.pack(side=tk.LEFT)
        self.toggle_btn = IOSButton(row1, text="停止服务", command=self.toggle_server, bg_color=COLOR_RED,
                                    hover_bg=COLOR_RED_HOVER, active_bg=COLOR_RED_HOVER,
                                    font=(FONT_FAMILY, 11, "bold"), padx=10, pady=4)
        self.toggle_btn.pack(side=tk.RIGHT)

        tk.Frame(card2, height=1, bg=COLOR_BORDER).pack(fill=tk.X, pady=6)

        row2 = tk.Frame(card2, bg=COLOR_CARD)
        row2.pack(fill=tk.X, pady=4)
        tk.Label(row2, text="目录", width=4, anchor="w", font=(FONT_FAMILY, 12), bg=COLOR_CARD, fg=COLOR_TEXT).pack(side=tk.LEFT)
        self.spool_var = tk.StringVar(value=short_path(self.spool_dir))
        self.change_spool_btn = self._gray_button(row2, "更换", self.change_spool_directory)
        self.change_spool_btn.pack(side=tk.RIGHT)
        self.open_spool_btn = self._gray_button(row2, "打开", self.open_spool_directory, fg=COLOR_BLUE)
        self.open_spool_btn.pack(side=tk.RIGHT, padx=(0, 6))
        tk.Label(row2, textvariable=self.spool_var, font=(MONO_FAMILY, 11), bg=COLOR_CARD, fg=COLOR_SECONDARY,
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))

        tk.Frame(card2, height=1, bg=COLOR_BORDER).pack(fill=tk.X, pady=6)
        row3 = tk.Frame(card2, bg=COLOR_CARD)
        row3.pack(fill=tk.X, pady=4)
        tk.Label(row3, text="密码", width=4, anchor="w", font=(FONT_FAMILY, 12), bg=COLOR_CARD, fg=COLOR_TEXT).pack(side=tk.LEFT)
        self.password_var = tk.StringVar(value=self.settings["password"])
        self.password_entry = tk.Entry(row3, textvariable=self.password_var, show="•", width=16, font=(FONT_FAMILY, 12),
                                       relief="flat", bg=COLOR_BG, highlightthickness=1,
                                       highlightbackground=COLOR_BORDER, highlightcolor=COLOR_BLUE)
        self.password_entry.pack(side=tk.LEFT, padx=(4, 8), ipady=3)
        self.password_entry.bind("<Return>", lambda e: self.apply_password())
        self._gray_button(row3, "应用", self.apply_password).pack(side=tk.LEFT)
        self.password_state = tk.Label(row3, text="", font=(FONT_FAMILY, 11), bg=COLOR_CARD, fg=COLOR_SECONDARY)
        self.password_state.pack(side=tk.LEFT, padx=(10, 0))
        self._render_security()

        # 5. 日志（默认折叠） -----------------------------------------------
        log_header = tk.Frame(self.main_container, bg=COLOR_BG)
        log_header.pack(fill=tk.X, pady=(0, 6))
        self.log_title_lbl = self._section_title(log_header, "▸ 日志")
        self.log_title_lbl.configure(cursor="hand2")
        self.log_title_lbl.pack(side=tk.LEFT)
        self.log_title_lbl.bind("<Button-1>", lambda e: self.toggle_logs())
        self.clear_log_btn = tk.Label(log_header, text="清空", font=(FONT_FAMILY, 11), bg=COLOR_BG,
                                      fg=COLOR_SECONDARY, cursor="hand2")
        self.clear_log_btn.bind("<Button-1>", lambda e: self.clear_logs())

        self.card3 = tk.Frame(self.main_container, bg="#1E1E1E")
        self.log_text = tk.Text(self.card3, bg="#1E1E1E", fg="#D4D4D4", insertbackground="#FFFFFF",
                                font=(MONO_FAMILY, 11), relief="flat", wrap=tk.WORD, padx=10, pady=8,
                                highlightthickness=0)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(self.card3, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=scrollbar.set)

        self._update_qr()

    def _base_url(self, ip):
        return "http://%s:%d" % (ip, self.server_port)

    def _share_url(self, url):
        """二维码与复制的链接带上访问密码（在 # 之后，不会发给服务器）。"""
        password = self.settings["password"]
        return url + ("/#k=" + urllib.parse.quote(password, safe="") if password else "")

    def _refresh_address(self):
        self.url_var.set(self._base_url(self.primary_ip))
        self._render_other_ips()
        self._update_qr()
        self._render_security()

    def toggle_logs(self):
        """折叠/展开日志面板。"""
        self.logs_expanded = not self.logs_expanded
        if self.logs_expanded:
            self.card3.pack(fill=tk.BOTH, expand=True)
            self.clear_log_btn.pack(side=tk.RIGHT)
            self.log_text.see(tk.END)
        else:
            self.card3.pack_forget()
            self.clear_log_btn.pack_forget()
        self.log_title_lbl.configure(text=("▾ 日志" if self.logs_expanded else "▸ 日志"))

    def toggle_devices(self):
        self.devices_expanded = not self.devices_expanded
        if self.devices_expanded:
            self.devices_card.pack(fill=tk.X, pady=(0, 12), after=self.devices_title.master)
        else:
            self.devices_card.pack_forget()
        self._update_devices_title()

    def _update_devices_title(self):
        self.devices_title.configure(text=("▾ 设备" if self.devices_expanded else "▸ 设备"))
        if self.devices:
            online = sum(1 for d in self.devices if d["online"])
            self.devices_count.configure(text="%d 台在线 · 共 %d 台" % (online, len(self.devices)))
        else:
            self.devices_count.configure(text="")

    def _render_other_ips(self):
        for child in self.other_frame.winfo_children():
            child.destroy()
        if not self.other_ips:
            return
        tk.Label(self.other_frame, text="备用", font=(FONT_FAMILY, 10), bg=COLOR_CARD,
                 fg=COLOR_SECONDARY).pack(side=tk.LEFT, padx=(0, 6))
        for ip in self.other_ips:
            alt_url = self._base_url(ip)
            link = tk.Label(self.other_frame, text=alt_url, font=(MONO_FAMILY, 11), bg=COLOR_CARD,
                            fg=COLOR_TEXT, cursor="hand2")
            link.pack(side=tk.LEFT, padx=(0, 10))
            link.bind("<Button-1>", lambda e, u=self._share_url(alt_url), w=link: self._copy_feedback(w, u))

    # ------------------------------------------------------------------
    # 设备列表：通过本机管理接口读取与删除
    # ------------------------------------------------------------------
    def _admin_request(self, method, path):
        url = "http://127.0.0.1:%d%s" % (self.server_port, path)
        req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
        with self._http.open(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _poll_devices(self):
        if self._is_closing:
            return
        if not self.is_server_running():
            self._set_devices(None)
        else:
            self._poll_devices_once()
        self.root.after(2000, self._poll_devices)

    def _set_devices(self, devices):
        self.devices = devices
        signature = None if devices is None else tuple(
            (d["id"], d["name"], d["domain"], d["addr"], d["online"]) for d in devices)
        if signature != self._devices_signature:
            self._devices_signature = signature
            self._render_devices()
        self._update_devices_title()

    def _render_devices(self):
        for child in self.devices_inner.winfo_children():
            child.destroy()
        rows = sorted(self.devices or [], key=lambda d: (not d["online"], d["name"]))
        if not rows:
            text = "暂无设备" if self.devices is not None else ("连接中…" if self.is_server_running() else "服务未运行")
            tk.Label(self.devices_inner, text=text, font=(FONT_FAMILY, 11), bg=COLOR_CARD,
                     fg=COLOR_SECONDARY, pady=12).pack(fill=tk.X)
        for index, dev in enumerate(rows):
            if index:
                tk.Frame(self.devices_inner, height=1, bg=COLOR_BORDER).pack(fill=tk.X, padx=(36, 0))
            row = tk.Frame(self.devices_inner, bg=COLOR_CARD, padx=14, pady=7)
            row.pack(fill=tk.X)
            tk.Label(row, text="●", font=("Arial", 11), bg=COLOR_CARD,
                     fg=COLOR_GREEN if dev["online"] else "#C7C7CC").pack(side=tk.LEFT, padx=(0, 8))
            delete = tk.Label(row, text="删除", font=(FONT_FAMILY, 11), bg=COLOR_CARD, fg=COLOR_RED, cursor="hand2")
            delete.pack(side=tk.RIGHT)
            delete.bind("<Button-1>", lambda e, d=dev: self.delete_device(d))
            meta = "%s · %s" % ("房间 #" + dev["domain"] if dev["domain"] else "未加入房间", dev["addr"])
            tk.Label(row, text=meta, font=(FONT_FAMILY, 10), bg=COLOR_CARD,
                     fg=COLOR_SECONDARY).pack(side=tk.RIGHT, padx=(0, 14))
            tk.Label(row, text=dev["name"], font=(FONT_FAMILY, 12, "bold" if dev["online"] else "normal"),
                     bg=COLOR_CARD, fg=COLOR_TEXT if dev["online"] else COLOR_SECONDARY,
                     anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 最多显示 5 行，更多时出现滚动条。
        self.devices_inner.update_idletasks()
        visible = max(1, min(len(rows), 5))
        row_height = self.devices_inner.winfo_reqheight() / max(1, len(rows)) if rows else self.devices_inner.winfo_reqheight()
        self.devices_canvas.configure(height=int(row_height * visible))
        if len(rows) > 5:
            self.devices_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            self.devices_scroll.pack_forget()
            self.devices_canvas.yview_moveto(0)

    def _on_devices_wheel(self, event):
        if self.devices and len(self.devices) > 5:
            self.devices_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def delete_device(self, dev):
        message = "删除设备“%s”？\n发给它的未下载私发文件也会删除。" % dev["name"]
        if dev["online"]:
            message += "\n该设备在线，删除后会以新身份重新连接。"
        if not messagebox.askyesno("删除设备", message, icon="warning"):
            return

        def run():
            try:
                self._admin_request("POST", "/api/admin/delete-device/" + dev["id"])
                self.log_queue.put("[App] 已删除设备 %s\n" % dev["name"])
            except Exception as exc:
                self.log_queue.put("[App 错误] 删除设备失败：%s\n" % exc)
            self.log_queue.put(("refresh", None))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # 地址与目录
    # ------------------------------------------------------------------
    def _update_qr(self):
        current_url = self._share_url(self.url_var.get())
        if not current_url or not generate_qr_photo:
            return
        try:
            img = generate_qr_photo(self.root, current_url, target_size=132)
            if img:
                self.qr_image = img
                self.qr_label.configure(image=self.qr_image, width=img.width(), height=img.height())
        except Exception as e:
            self._log(f"[App] 二维码生成异常: {e}\n")

    def _copy_feedback(self, widget, text):
        self.copy_text(text)
        original = widget.cget("text")
        widget.configure(text="已复制 ✓")
        self.root.after(1500, lambda: (not self._is_closing) and widget.configure(text=original))

    def copy_url(self):
        self.copy_text(self._share_url(self.url_var.get()))
        self.copy_btn.configure(text="已复制 ✓", bg=COLOR_GREEN)

        def _restore():
            if not self._is_closing:
                self.copy_btn.configure(text="复制", bg=COLOR_BLUE)
        self.root.after(1500, _restore)

    def copy_text(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    def open_in_browser(self):
        webbrowser.open(self.url_var.get())

    def refresh_ips(self):
        self.primary_ip, self.other_ips = get_lan_ips()
        self._refresh_address()

    def open_spool_directory(self):
        target = self.spool_dir
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                pass
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            webbrowser.open("file://" + target)

    def change_spool_directory(self):
        new_dir = filedialog.askdirectory(initialdir=self.spool_dir, title="选择中转目录")
        if new_dir:
            probe_dir = resolve_spool_dir(new_dir)
            if probe_dir == os.path.abspath(new_dir):
                self.spool_dir = probe_dir
                self.spool_var.set(short_path(probe_dir))
                self._log(f"[App] 中转目录：{probe_dir}\n")
                if self.is_server_running():
                    self.restart_server()
            else:
                messagebox.showwarning("无写入权限", f"该目录不可写，已保持原目录：\n{self.spool_dir}")

    def _render_security(self):
        if hasattr(self, "password_state"):
            self.password_state.configure(text="已启用" if self.settings["password"] else "未设置")

    def apply_password(self):
        value = self.password_var.get()
        if len(value) > 128 or any(ord(c) < 32 for c in value):
            messagebox.showerror("密码无效", "密码不能超过 128 个字符，也不能包含控制字符")
            return
        if value == self.settings["password"]:
            return
        if value and not messagebox.askyesno("访问密码", "设置新密码后，所有已连接的设备需要重新输入密码或重新扫码。继续吗？"):
            return
        self.settings["password"] = value
        save_settings(self.settings)
        self.restart_server()
        self._refresh_address()

    def change_port_and_restart(self):
        val = self.port_var.get().strip()
        if not val.isdigit() or not (1024 <= int(val) <= 65535):
            messagebox.showerror("端口无效", "请输入 1024 ~ 65535 之间的端口号")
            return
        new_port = int(val)
        if not check_port_available(new_port) and new_port != self.server_port:
            messagebox.showwarning("端口已被占用", f"端口 {new_port} 已被占用，请换一个。")
            return
        self.server_port = new_port
        self.restart_server()
        self._refresh_address()

    # ------------------------------------------------------------------
    # 服务进程
    # ------------------------------------------------------------------
    def is_server_running(self) -> bool:
        return self.server_process is not None and self.server_process.poll() is None

    def start_server(self):
        if self.is_server_running():
            return

        cmd = [
            sys.executable,
            "-u",
            self.transfer_script,
            "--host", "0.0.0.0",
            "--port", str(self.server_port),
            "--dir", self.spool_dir,
            "--max-file-size", str(self.limits.max_file_size),
            "--spool-quota", str(self.limits.spool_quota),
            "--max-uploads", str(self.limits.max_uploads),
            "--uploads-per-device", str(self.limits.uploads_per_device),
            "--min-free-space", str(self.limits.min_free_space),
            "--exit-with-parent",
        ]
        env = dict(os.environ, LANFILES_PASSWORD=self.settings["password"])  # 不放在命令行参数里

        try:
            self.server_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=os.path.dirname(self.transfer_script) or None,
                env=env
            )
        except Exception as e:
            self._log(f"[App 错误] 启动服务失败: {e}\n")
            messagebox.showerror("启动失败", f"无法启动服务进程: {e}")
            self._update_server_status_ui(False)
            return

        threading.Thread(target=self._reader_thread, daemon=True).start()

        time.sleep(0.3)
        if self.server_process.poll() is not None:
            self._log("[App 错误] 服务启动失败，请展开日志查看端口、目录锁或数据库错误。\n")
            self._update_server_status_ui(False)
            if not self.logs_expanded:
                self.toggle_logs()
        else:
            self._update_server_status_ui(True)

    def stop_server(self):
        if not self.server_process:
            self._update_server_status_ui(False)
            return

        proc = self.server_process
        self.server_process = None

        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass

        self._update_server_status_ui(False)
        self._log("[App] 服务已停止。\n")

    def restart_server(self):
        self.stop_server()
        time.sleep(0.3)
        self.start_server()

    def toggle_server(self):
        if self.is_server_running():
            self.stop_server()
        else:
            self.start_server()

    def _update_server_status_ui(self, running: bool):
        fg, bg = (COLOR_GREEN, COLOR_GREEN_BG) if running else (COLOR_RED, COLOR_RED_BG)
        self.status_pill.configure(bg=bg)
        self.status_dot.configure(fg=fg, bg=bg)
        label = "运行中 · %d" % self.server_port
        self.status_text.configure(text=label if running else "已停止", fg=fg, bg=bg)
        if running:
            self.toggle_btn.set_theme(COLOR_RED, "#FFFFFF", COLOR_RED_HOVER)
            self.toggle_btn.configure(text="停止服务")
        else:
            self.toggle_btn.set_theme(COLOR_GREEN, "#FFFFFF", "#2DB84D")
            self.toggle_btn.configure(text="启动服务")
            self._set_devices(None)

    def _reader_thread(self):
        proc = self.server_process
        if not proc or not proc.stdout:
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self.log_queue.put(line)
        except Exception:
            pass
        finally:
            if proc.stdout:
                proc.stdout.close()

    def _process_log_queue(self):
        while not self.log_queue.empty():
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                kind, payload = item
                if kind == "devices":
                    self._device_fetching = False
                    if self.is_server_running() and payload is not None:
                        self._set_devices(payload)
                elif kind == "refresh":
                    self._devices_signature = ()
                    self._device_fetching = False
                    self._poll_devices_once()
            else:
                self._append_log_text(item)

        if not self._is_closing:
            try:
                self.root.after(150, self._process_log_queue)
            except Exception:
                pass

    def _poll_devices_once(self):
        if self.is_server_running() and not self._device_fetching:
            self._device_fetching = True

            def fetch():
                try:
                    result = self._admin_request("GET", "/api/admin/devices")
                except Exception:
                    result = None
                self.log_queue.put(("devices", result))
            threading.Thread(target=fetch, daemon=True).start()

    def _log(self, msg: str):
        self._append_log_text(msg)

    def _append_log_text(self, text: str):
        try:
            self.log_text.insert(tk.END, text)
            self.log_text.see(tk.END)
        except Exception:
            pass

    def clear_logs(self):
        self.log_text.delete("1.0", tk.END)

    def on_close(self):
        self._is_closing = True
        self.stop_server()
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    os.environ["TK_SILENCE_DEPRECATION"] = "1"
    parser = argparse.ArgumentParser(description="LanFiles 桌面客户端")
    add_limit_arguments(parser)
    args = parser.parse_args()
    try:
        limits = limits_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    root = tk.Tk()
    app = LanFilesApp(root, limits)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()


if __name__ == "__main__":
    main()
