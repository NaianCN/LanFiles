#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网传文件（LanFiles）—— 桌面 App 客户端
深度参考 iOS 设计语言（分组卡片、系统蓝、圆角胶囊与微交互）
保证二维码在最小窗口状态下也 100% 完整清晰显示。
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
        self.root.title("局域网传文件 · LanFiles")
        # 宽裕的默认尺寸，并设置 560x520 紧凑最小尺寸（即便缩至最小，二维码也绝对完整显示）
        self.root.geometry("740x670")
        self.root.minsize(560, 520)
        self.root.configure(bg=COLOR_BG)

        # 状态与配置
        self.server_port = find_available_port(8000)
        self.server_process = None
        self.spool_dir = resolve_spool_dir()
        self.primary_ip, self.other_ips = get_lan_ips()
        self.qr_image = None
        self.log_queue = queue.Queue()
        self._is_closing = False
        self.logs_expanded = True

        # 定位 transfer.py
        self.transfer_script = self._find_transfer_script()

        # 构建界面
        self._build_ui()

        # 启动后台服务
        self.start_server()
        self.root.after(100, self._process_log_queue)

        # 快捷键与退出处理
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Command-q>", lambda e: self.on_close())
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

    def _build_ui(self):
        self.main_container = tk.Frame(self.root, bg=COLOR_BG, padx=14, pady=12)
        self.main_container.pack(fill=tk.BOTH, expand=True)

        # ===================================================================
        # 1. iOS 顶部导航大标题栏
        # ===================================================================
        header = tk.Frame(self.main_container, bg=COLOR_BG)
        header.pack(fill=tk.X, pady=(0, 8))

        title_left = tk.Frame(header, bg=COLOR_BG)
        title_left.pack(side=tk.LEFT, fill=tk.Y)

        title_lbl = tk.Label(
            title_left,
            text="局域网传文件",
            font=(FONT_FAMILY, 22, "bold"),
            bg=COLOR_BG,
            fg=COLOR_TEXT
        )
        title_lbl.pack(anchor="w")

        subtitle_lbl = tk.Label(
            title_left,
            text=("同 Wi-Fi 浏览器互传 · 单文件 %.1f GiB · 总量 %.1f GiB · 并发 %d" % (
                self.limits.max_file_size / 1024**3, self.limits.spool_quota / 1024**3, self.limits.max_uploads)),
            font=(FONT_FAMILY, 12),
            bg=COLOR_BG,
            fg=COLOR_SECONDARY
        )
        subtitle_lbl.pack(anchor="w", pady=(1, 0))

        # 状态指示胶囊
        self.status_pill = tk.Frame(
            header,
            bg=COLOR_GREEN_BG,
            highlightbackground=COLOR_GREEN,
            highlightthickness=1,
            padx=9,
            pady=4
        )
        self.status_pill.pack(side=tk.RIGHT, pady=2)

        self.status_dot = tk.Label(
            self.status_pill,
            text="●",
            fg=COLOR_GREEN,
            bg=COLOR_GREEN_BG,
            font=("Arial", 11)
        )
        self.status_dot.pack(side=tk.LEFT, padx=(0, 4))

        self.status_text = tk.Label(
            self.status_pill,
            text=f"服务运行中 : {self.server_port}",
            fg=COLOR_GREEN,
            bg=COLOR_GREEN_BG,
            font=(FONT_FAMILY, 11, "bold")
        )
        self.status_text.pack(side=tk.LEFT)

        # ===================================================================
        # 2. Section 1: 连接地址与二维码卡片（核心展示）
        # ===================================================================
        tk.Label(
            self.main_container,
            text="连接地址 · 手机扫码或浏览器访问",
            font=(FONT_FAMILY, 12, "bold"),
            bg=COLOR_BG,
            fg=COLOR_HEADER
        ).pack(anchor="w", pady=(2, 6))

        card1 = tk.Frame(
            self.main_container,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=14,
            pady=12
        )
        card1.pack(fill=tk.X, pady=(0, 10))

        # --- 二维码容器：布局在左侧，永久保证 100% 完整显示，无论窗口缩小到什么程度都绝不被遮挡 ---
        self.qr_container = tk.Frame(
            card1,
            bg="#FAFAFA",
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=10,
            pady=8
        )
        self.qr_container.pack(side=tk.LEFT, anchor="center", padx=(0, 14), fill=tk.NONE, expand=False)

        self.qr_label = tk.Label(self.qr_container, bg="#FFFFFF")
        self.qr_label.pack()

        tk.Label(
            self.qr_container,
            text="📷 手机扫码直达",
            font=(FONT_FAMILY, 10),
            bg="#FAFAFA",
            fg=COLOR_SECONDARY
        ).pack(pady=(4, 0))

        # --- 右侧：网址卡片与操作按钮 ---
        right_body = tk.Frame(card1, bg=COLOR_CARD)
        right_body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 网址高亮条（iOS 柔和蓝底框）
        self.url_box = tk.Frame(
            right_body,
            bg=COLOR_BLUE_BG,
            highlightbackground=COLOR_BLUE,
            highlightthickness=1.5,
            padx=10,
            pady=8
        )
        self.url_box.pack(fill=tk.X, pady=(0, 8))

        self.url_var = tk.StringVar(value=f"http://{self.primary_ip}:{self.server_port}")
        self.url_display = tk.Label(
            self.url_box,
            textvariable=self.url_var,
            font=(MONO_FAMILY, 17, "bold"),
            fg="#0051C7",
            bg=COLOR_BLUE_BG,
            cursor="hand2"
        )
        self.url_display.pack(anchor="w")
        self.url_display.bind("<Button-1>", lambda e: self.copy_url())

        # 按钮栏（iOS 胶囊按钮）
        btn_bar = tk.Frame(right_body, bg=COLOR_CARD)
        btn_bar.pack(fill=tk.X, pady=(0, 6))

        self.copy_btn = IOSButton(
            btn_bar,
            text="📋 复制网址",
            command=self.copy_url,
            bg_color=COLOR_BLUE,
            fg_color="#FFFFFF",
            hover_bg=COLOR_BLUE_HOVER,
            active_bg=COLOR_BLUE_ACTIVE,
            padx=11,
            pady=5
        )
        self.copy_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.open_browser_btn = IOSButton(
            btn_bar,
            text="🌐 浏览器打开",
            command=self.open_in_browser,
            bg_color=COLOR_GRAY_BTN,
            fg_color=COLOR_BLUE,
            hover_bg=COLOR_GRAY_HOVER,
            padx=11,
            pady=5
        )
        self.open_browser_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.refresh_ip_btn = IOSButton(
            btn_bar,
            text="🔄 刷新",
            command=self.refresh_ips,
            bg_color=COLOR_GRAY_BTN,
            fg_color=COLOR_TEXT,
            hover_bg=COLOR_GRAY_HOVER,
            padx=9,
            pady=5
        )
        self.refresh_ip_btn.pack(side=tk.LEFT)

        # 备用地址栏
        self.other_frame = tk.Frame(right_body, bg=COLOR_CARD)
        self.other_frame.pack(fill=tk.X, pady=(2, 0))
        self._render_other_ips()

        # 提示小字
        tk.Label(
            right_body,
            text="💡 手机连入同一 Wi-Fi 后，用相机扫码或浏览器打开",
            font=(FONT_FAMILY, 11),
            bg=COLOR_CARD,
            fg=COLOR_SECONDARY
        ).pack(anchor="w", pady=(4, 0))

        # ===================================================================
        # 3. Section 2: 服务设置与中转目录（iOS TableView 风格卡片）
        # ===================================================================
        tk.Label(
            self.main_container,
            text="服务设置与中转目录",
            font=(FONT_FAMILY, 12, "bold"),
            bg=COLOR_BG,
            fg=COLOR_HEADER
        ).pack(anchor="w", pady=(2, 6))

        card2 = tk.Frame(
            self.main_container,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=14,
            pady=8
        )
        card2.pack(fill=tk.X, pady=(0, 10))

        # Row 1: 端口配置与启停
        row1 = tk.Frame(card2, bg=COLOR_CARD)
        row1.pack(fill=tk.X, pady=4)

        tk.Label(
            row1,
            text="监听端口：",
            font=(FONT_FAMILY, 12),
            bg=COLOR_CARD,
            fg=COLOR_TEXT
        ).pack(side=tk.LEFT)

        self.port_var = tk.StringVar(value=str(self.server_port))
        self.port_entry = tk.Entry(
            row1,
            textvariable=self.port_var,
            width=6,
            font=(FONT_FAMILY, 12),
            highlightbackground=COLOR_BORDER,
            relief="solid",
            bd=1
        )
        self.port_entry.pack(side=tk.LEFT, padx=(2, 8))

        self.restart_btn = IOSButton(
            row1,
            text="更改并重启",
            command=self.change_port_and_restart,
            bg_color=COLOR_GRAY_BTN,
            fg_color=COLOR_TEXT,
            hover_bg=COLOR_GRAY_HOVER,
            padx=8,
            pady=3,
            font=(FONT_FAMILY, 11)
        )
        self.restart_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.toggle_btn = IOSButton(
            row1,
            text="⏹ 停止服务",
            command=self.toggle_server,
            bg_color=COLOR_RED,
            fg_color="#FFFFFF",
            hover_bg=COLOR_RED_HOVER,
            padx=9,
            pady=3,
            font=(FONT_FAMILY, 11, "bold")
        )
        self.toggle_btn.pack(side=tk.LEFT)

        # 分割线（iOS 行间分割线）
        tk.Frame(card2, height=1, bg=COLOR_BORDER).pack(fill=tk.X, pady=5)

        # Row 2: 中转文件存放目录
        row2 = tk.Frame(card2, bg=COLOR_CARD)
        row2.pack(fill=tk.X, pady=4)

        tk.Label(
            row2,
            text="文件存放：",
            font=(FONT_FAMILY, 12),
            bg=COLOR_CARD,
            fg=COLOR_TEXT
        ).pack(side=tk.LEFT)

        self.spool_var = tk.StringVar(value=self.spool_dir)
        spool_entry = tk.Entry(
            row2,
            textvariable=self.spool_var,
            font=(MONO_FAMILY, 11),
            fg=COLOR_SECONDARY,
            state="readonly",
            highlightbackground=COLOR_BORDER,
            relief="solid",
            bd=1
        )
        spool_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 8))

        self.open_spool_btn = IOSButton(
            row2,
            text="📂 访达中打开",
            command=self.open_spool_directory,
            bg_color=COLOR_BLUE,
            fg_color="#FFFFFF",
            hover_bg=COLOR_BLUE_HOVER,
            padx=9,
            pady=3,
            font=(FONT_FAMILY, 11, "bold")
        )
        self.open_spool_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.change_spool_btn = IOSButton(
            row2,
            text="更换...",
            command=self.change_spool_directory,
            bg_color=COLOR_GRAY_BTN,
            fg_color=COLOR_TEXT,
            hover_bg=COLOR_GRAY_HOVER,
            padx=8,
            pady=3,
            font=(FONT_FAMILY, 11)
        )
        self.change_spool_btn.pack(side=tk.LEFT)

        # ===================================================================
        # 4. Section 3: 运行日志（可折叠式 iOS 卡片）
        # ===================================================================
        log_header = tk.Frame(self.main_container, bg=COLOR_BG)
        log_header.pack(fill=tk.X, pady=(2, 4))

        self.log_title_lbl = tk.Label(
            log_header,
            text="▼ 运行日志与传输动态",
            font=(FONT_FAMILY, 12, "bold"),
            bg=COLOR_BG,
            fg=COLOR_HEADER,
            cursor="hand2"
        )
        self.log_title_lbl.pack(side=tk.LEFT)
        self.log_title_lbl.bind("<Button-1>", lambda e: self.toggle_logs())

        IOSButton(
            log_header,
            text="清空日志",
            command=self.clear_logs,
            bg_color=COLOR_GRAY_BTN,
            fg_color=COLOR_SECONDARY,
            hover_bg=COLOR_GRAY_HOVER,
            padx=6,
            pady=2,
            font=(FONT_FAMILY, 10)
        ).pack(side=tk.RIGHT)

        self.card3 = tk.Frame(
            self.main_container,
            bg="#1E1E1E",
            highlightbackground=COLOR_BORDER,
            highlightthickness=1
        )
        self.card3.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            self.card3,
            bg="#1E1E1E",
            fg="#D4D4D4",
            insertbackground="#FFFFFF",
            font=(MONO_FAMILY, 11),
            relief="flat",
            wrap=tk.WORD,
            padx=8,
            pady=6
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(self.card3, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=scrollbar.set)

        # 初始刷新二维码
        self._update_qr()

    def toggle_logs(self):
        """折叠/展开运行日志面板。"""
        if self.logs_expanded:
            self.card3.pack_forget()
            self.log_title_lbl.configure(text="▶ 运行日志与传输动态 (点击展开)")
            self.logs_expanded = False
        else:
            self.card3.pack(fill=tk.BOTH, expand=True)
            self.log_title_lbl.configure(text="▼ 运行日志与传输动态")
            self.logs_expanded = True

    def _render_other_ips(self):
        for child in self.other_frame.winfo_children():
            child.destroy()

        if not self.other_ips:
            return

        tk.Label(
            self.other_frame,
            text="备用地址：",
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_SECONDARY
        ).pack(side=tk.LEFT)

        for ip in self.other_ips:
            alt_url = f"http://{ip}:{self.server_port}"
            tk.Label(
                self.other_frame,
                text=alt_url,
                font=(MONO_FAMILY, 10),
                bg=COLOR_CARD,
                fg=COLOR_TEXT
            ).pack(side=tk.LEFT, padx=(2, 4))

            IOSButton(
                self.other_frame,
                text="复制",
                command=lambda u=alt_url: self.copy_text(u),
                bg_color=COLOR_GRAY_BTN,
                fg_color=COLOR_TEXT,
                hover_bg=COLOR_GRAY_HOVER,
                padx=5,
                pady=1,
                font=(FONT_FAMILY, 9)
            ).pack(side=tk.LEFT, padx=(0, 6))

    def _update_qr(self):
        current_url = self.url_var.get()
        if not current_url or not generate_qr_photo:
            return

        try:
            # 使用 130px 高清二维码，紧凑锐利，在最小窗口也 100% 完整显示
            img = generate_qr_photo(self.root, current_url, target_size=130)
            if img:
                self.qr_image = img
                self.qr_label.configure(image=self.qr_image, width=img.width(), height=img.height())
        except Exception as e:
            self._log(f"[App] 二维码生成异常: {e}\n")

    def copy_url(self):
        url = self.url_var.get()
        self.copy_text(url)

        orig_text = "📋 复制网址"
        self.copy_btn.configure(text="✓ 已复制！", bg=COLOR_GREEN)

        def _restore():
            if not self._is_closing:
                self.copy_btn.configure(text=orig_text, bg=COLOR_BLUE)

        self.root.after(1800, _restore)

    def copy_text(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self._log(f"[App] 已复制到剪贴板: {text}\n")

    def open_in_browser(self):
        url = self.url_var.get()
        self._log(f"[App] 打开浏览器: {url}\n")
        webbrowser.open(url)

    def refresh_ips(self):
        self._log("[App] 正在重新扫描局域网 IP...\n")
        self.primary_ip, self.other_ips = get_lan_ips()
        new_url = f"http://{self.primary_ip}:{self.server_port}"
        self.url_var.set(new_url)
        self._render_other_ips()
        self._update_qr()
        self._log(f"[App] IP 扫描完成，主地址: {new_url}\n")

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
        self._log(f"[App] 在访达中打开目录: {target}\n")

    def change_spool_directory(self):
        new_dir = filedialog.askdirectory(initialdir=self.spool_dir, title="选择接收文件存放目录")
        if new_dir:
            probe_dir = resolve_spool_dir(new_dir)
            if probe_dir == os.path.abspath(new_dir):
                self.spool_dir = probe_dir
                self.spool_var.set(probe_dir)
                self._log(f"[App] 中转目录已更新为: {probe_dir}\n")
                if self.is_server_running():
                    self.restart_server()
            else:
                messagebox.showwarning("无写入权限", f"选取的目录不可写或无权限访问，已保持原目录：\n{self.spool_dir}")

    def change_port_and_restart(self):
        val = self.port_var.get().strip()
        if not val.isdigit() or not (1024 <= int(val) <= 65535):
            messagebox.showerror("端口无效", "请输入有效的端口号（1024 ~ 65535 之间）")
            return
        new_port = int(val)
        if not check_port_available(new_port) and new_port != self.server_port:
            messagebox.showwarning("端口已被占用", f"端口 {new_port} 已被占用，请尝试其他端口。")
            return
        self.server_port = new_port
        self.url_var.set(f"http://{self.primary_ip}:{self.server_port}")
        self._render_other_ips()
        self._update_qr()
        self.restart_server()

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
            "--min-free-space", str(self.limits.min_free_space),
        ]

        self._log(f"[App] 正在启动 LanFiles 服务...\n")
        self._log(f"[App] 执行命令: {' '.join(cmd)}\n")

        try:
            self.server_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=os.path.dirname(self.transfer_script) or None
            )
        except Exception as e:
            self._log(f"[App 错误] 启动服务失败: {e}\n")
            messagebox.showerror("启动失败", f"无法启动服务进程: {e}")
            self._update_server_status_ui(False)
            return

        threading.Thread(target=self._reader_thread, daemon=True).start()

        time.sleep(0.3)
        if self.server_process.poll() is not None:
            self._log(f"[App 错误] 服务启动后立即退出，请查看日志中的端口、目录锁或数据库错误。\n")
            self._update_server_status_ui(False)
        else:
            self._update_server_status_ui(True)
            self._log(f"[App 成功] 服务已在端口 {self.server_port} 成功运行。\n")

    def stop_server(self):
        if not self.server_process:
            self._update_server_status_ui(False)
            return

        self._log("[App] 正在停止服务...\n")
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
        if running:
            self.status_pill.configure(bg=COLOR_GREEN_BG, highlightbackground=COLOR_GREEN)
            self.status_dot.configure(text="●", fg=COLOR_GREEN, bg=COLOR_GREEN_BG)
            self.status_text.configure(
                text=f"服务运行中 : {self.server_port}",
                fg=COLOR_GREEN,
                bg=COLOR_GREEN_BG
            )
            self.toggle_btn.set_theme(COLOR_RED, "#FFFFFF", COLOR_RED_HOVER)
            self.toggle_btn.configure(text="⏹ 停止服务")
        else:
            self.status_pill.configure(bg=COLOR_RED_BG, highlightbackground=COLOR_RED)
            self.status_dot.configure(text="●", fg=COLOR_RED, bg=COLOR_RED_BG)
            self.status_text.configure(
                text="服务已停止",
                fg=COLOR_RED,
                bg=COLOR_RED_BG
            )
            self.toggle_btn.set_theme(COLOR_GREEN, "#FFFFFF", "#2DB84D")
            self.toggle_btn.configure(text="▶️ 启动服务")

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
                line = self.log_queue.get_nowait()
                self._append_log_text(line)
            except queue.Empty:
                break

        if not self._is_closing:
            try:
                self.root.after(150, self._process_log_queue)
            except Exception:
                pass

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
