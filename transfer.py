#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网传文件 —— 单文件、零依赖的局域网文件互传工具。

用法：
    python3 transfer.py            # 默认 0.0.0.0:8000
    python3 transfer.py --port 9000
    python3 transfer.py --name 客厅电脑

然后让同网段的任意设备（手机 / 平板 / 电脑）用浏览器打开终端打印的地址即可。
每个打开的浏览器标签页就是一个“设备”，选中某个设备即可双向传文件。

仅使用 Python 标准库，无需 pip install 任何东西。
"""

import argparse
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
OFFLINE_TTL = 10.0       # 多少秒没有心跳视为离线
FILE_TTL = 3600.0        # 未投递文件保留多少秒后被清理
CLEANUP_INTERVAL = 60.0  # 清理线程扫描间隔（秒）
CHUNK_SIZE = 1024 * 1024 # 读写分块大小（1MB）
DOMAIN_RE = re.compile(r"^\d{4}$")  # 域号：4 位数字（含前导 0）

ADJECTIVES = ["晴空", "快乐", "安静", "勇敢", "温柔", "机灵", "好奇", "闪电", "微风", "星辰",
              "薄荷", "柠檬", "海盐", "山茶", "松果", "云朵", "萤火", "麦浪", "雨滴", "晨光"]
NOUNS = ["小鹿", "海豚", "松鼠", "白兔", "熊猫", "狐狸", "刺猬", "燕子", "猫咪", "小狗",
         "企鹅", "猫头鹰", "河马", "斑马", "鲸鱼", "鹦鹉", "乌龟", "袋鼠", "仓鼠", "蜜蜂"]


def default_spool_dir():
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, "Downloads", "lanfiles")
    return os.path.join(tempfile.gettempdir(), "lanfiles")


def fallback_spool_dirs():
    """下载文件夹不可用时的备用中转目录（放在用户目录下，比系统临时目录更易找）。"""
    dirs = []
    home = os.path.expanduser("~")
    if home and home != "~":
        dirs.append(os.path.join(home, "lanfiles"))
    dirs.append(os.path.join(tempfile.gettempdir(), "lanfiles"))
    return dirs


def ensure_spool_dir(target):
    """创建并校验目录可写（含真实写测试，能捕获 macOS 对“下载”文件夹的 TCC 拦截）。"""
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".write-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return os.path.abspath(target)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
class Registry:
    """线程安全的设备 / 传输登记表。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.devices = {}    # device_id -> {"name": str, "last_seen": float, "addr": str, "domain": str}
        self.transfers = {}  # transfer_id -> {"from": str, "from_name": str, "to": str|None,
                             #                  "kind": "direct"|"domain", "domain": str|None,
                             #                  "filename": str, "size": int,
                             #                  "path": str, "created": float}

    # ---- 设备 ----
    def upsert_device(self, device_id, name=None, domain=None, addr=""):
        with self._lock:
            now = time.time()
            dev = self.devices.get(device_id)
            if dev is None:
                dev = {"name": name or random_name(), "last_seen": now,
                       "addr": addr, "domain": domain or ""}
                self.devices[device_id] = dev
            else:
                if name:
                    dev["name"] = name
                if domain is not None:
                    dev["domain"] = domain
                dev["last_seen"] = now
                dev["addr"] = addr or dev.get("addr", "")
            return dict(dev)

    def touch_device(self, device_id):
        with self._lock:
            dev = self.devices.get(device_id)
            if dev:
                dev["last_seen"] = time.time()
            return bool(dev)

    def get_device(self, device_id):
        with self._lock:
            dev = self.devices.get(device_id)
            return dict(dev) if dev else None

    def online_devices(self, except_id=None, domain=""):
        now = time.time()
        with self._lock:
            out = []
            for did, dev in self.devices.items():
                if did == except_id:
                    continue
                if dev.get("domain", "") != domain:
                    continue
                online = (now - dev["last_seen"]) <= OFFLINE_TTL
                out.append({"id": did, "name": dev["name"], "online": online})
            return out

    def is_online(self, device_id):
        with self._lock:
            dev = self.devices.get(device_id)
            if not dev:
                return False
            return (time.time() - dev["last_seen"]) <= OFFLINE_TTL

    # ---- 传输 ----
    def add_transfer(self, transfer):
        with self._lock:
            self.transfers[transfer["transfer_id"]] = transfer

    def get_transfer(self, transfer_id):
        with self._lock:
            t = self.transfers.get(transfer_id)
            return dict(t) if t else None

    def inbox(self, device_id, domain=""):
        with self._lock:
            items = []
            for t in self.transfers.values():
                if t.get("kind", "direct") == "domain":
                    if domain and t.get("domain") == domain and t.get("from") != device_id:
                        items.append(t)
                else:
                    if t.get("to") == device_id:
                        items.append(t)
            items.sort(key=lambda t: t["created"])
            return [{"transfer_id": t["transfer_id"],
                     "kind": t.get("kind", "direct"),
                     "filename": t["filename"],
                     "size": t["size"],
                     "from_name": t["from_name"]} for t in items]

    def remove_transfer(self, transfer_id):
        with self._lock:
            return self.transfers.pop(transfer_id, None)

    def expired_transfers(self, ttl):
        now = time.time()
        with self._lock:
            expired = [tid for tid, t in self.transfers.items()
                       if now - t["created"] > ttl]
            for tid in expired:
                self.transfers.pop(tid, None)
            return expired


REGISTRY = Registry()
SPOOL_DIR = default_spool_dir()


def random_name():
    import random
    return "%s%s" % (random.choice(ADJECTIVES), random.choice(NOUNS))


def device_type(user_agent):
    ua = (user_agent or "").lower()
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "android" in ua:
        return "Android"
    if "windows" in ua:
        return "Windows"
    if "macintosh" in ua or "mac os" in ua:
        return "Mac"
    if "linux" in ua:
        return "Linux"
    return "设备"


def sanitize_filename(name):
    """去除路径分隔符与控制字符，防止路径穿越。"""
    name = (name or "").replace("\\", "/")
    name = os.path.basename(name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.strip()
    if not name:
        name = "file"
    if len(name) > 200:
        name = name[:200]
    return name


def cleanup_loop():
    """后台线程：定期清理超过保留期的未投递文件。"""
    while True:
        try:
            for tid in REGISTRY.expired_transfers(FILE_TTL):
                _safe_unlink_by_id(tid)
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL)


def _safe_unlink_by_id(tid):
    # 清理时按 transfer_id 推测文件路径（文件保存在 SPOOL_DIR/<transfer_id>）
    p = os.path.join(SPOOL_DIR, tid)
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LanFiles/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def _send(self, status, body, content_type="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _json_error(self, status, message):
        self._json({"error": message}, status)

    def _query(self):
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        length = int(length)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # ---------- 路由 ----------
    def do_GET(self):
        path, qs = self._query()
        if path == "/":
            self._serve_index()
        elif path == "/favicon.ico":
            self._send(204, b"")
        elif path == "/api/devices":
            self._api_devices(qs)
        elif path == "/api/inbox":
            self._api_inbox(qs)
        elif path.startswith("/api/download/"):
            self._api_download(path, qs)
        else:
            self._json_error(404, "未找到该路径")

    def do_POST(self):
        path, qs = self._query()
        if path == "/api/register":
            self._api_register()
        elif path == "/api/send":
            self._api_send(qs)
        elif path.startswith("/api/ack/"):
            self._api_ack(path, qs)
        else:
            self._json_error(404, "未找到该路径")

    def log_message(self, fmt, *args):
        # 保持终端输出简洁
        pass

    # ---------- 页面 ----------
    def _serve_index(self):
        self._send(200, INDEX_HTML, "text/html; charset=utf-8")

    # ---------- API ----------
    def _api_register(self):
        data = {}
        try:
            raw = self._read_body()
            if raw:
                data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json_error(400, "请求体不是有效 JSON")

        device_id = data.get("device_id") or uuid.uuid4().hex
        name = data.get("name") or None
        domain = data.get("domain")
        if domain is not None:
            domain = str(domain)
            if domain != "" and not DOMAIN_RE.match(domain):
                return self._json_error(400, "域号需为 4 位数字")
        addr = self.client_address[0] if self.client_address else ""
        dev = REGISTRY.upsert_device(device_id, name, domain, addr)
        self._json({"device_id": device_id, "name": dev["name"], "domain": dev["domain"]})

    def _api_devices(self, qs):
        device_id = qs.get("device_id", [""])[0]
        if not device_id or not REGISTRY.touch_device(device_id):
            return self._json_error(401, "未知设备，请先注册")
        dev = REGISTRY.get_device(device_id)
        self._json(REGISTRY.online_devices(except_id=device_id, domain=dev.get("domain", "")))

    def _api_send(self, qs):
        from_id = qs.get("from", [""])[0]
        to_id = qs.get("to", [""])[0]
        domain = qs.get("domain", [""])[0]
        filename = qs.get("name", [""])[0]
        if not from_id:
            return self._json_error(400, "缺少 from 参数")
        if bool(to_id) == bool(domain):
            return self._json_error(400, "to（1对1）与 domain（广播）必须二选一")

        sender = REGISTRY.get_device(from_id)
        if not sender:
            return self._json_error(401, "未知发送方")

        kind = "direct"
        target = None
        if to_id:
            target_dev = REGISTRY.get_device(to_id)
            if not target_dev:
                return self._json_error(404, "目标设备不存在")
            if target_dev.get("domain", "") != sender.get("domain", ""):
                return self._json_error(403, "目标设备不在你的域内")
            if not REGISTRY.is_online(to_id):
                return self._json_error(410, "目标设备已离线")
            target = to_id
        else:
            if not DOMAIN_RE.match(domain):
                return self._json_error(400, "域号需为 4 位数字")
            if sender.get("domain", "") != domain:
                return self._json_error(403, "你不在该域内")
            kind = "domain"

        length = self.headers.get("Content-Length")
        if length is None:
            return self._json_error(400, "缺少文件内容（Content-Length）")

        filename = sanitize_filename(filename)
        transfer_id = uuid.uuid4().hex
        tmp_path = os.path.join(SPOOL_DIR, transfer_id)
        size = 0
        try:
            total = int(length)
            with open(tmp_path, "wb") as f:
                remaining = total
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
                    remaining -= len(chunk)
        except OSError:
            _safe_unlink_by_id(transfer_id)
            return self._json_error(507, "写入失败（磁盘空间不足或目录不可写）")

        REGISTRY.add_transfer({
            "transfer_id": transfer_id,
            "kind": kind,
            "from": from_id,
            "from_name": sender["name"],
            "to": target,
            "domain": domain if kind == "domain" else None,
            "filename": filename,
            "size": size,
            "path": tmp_path,
            "created": time.time(),
        })
        self._json({"transfer_id": transfer_id, "size": size,
                    "filename": filename, "kind": kind})

    def _api_inbox(self, qs):
        device_id = qs.get("device_id", [""])[0]
        if not device_id or not REGISTRY.touch_device(device_id):
            return self._json_error(401, "未知设备，请先注册")
        dev = REGISTRY.get_device(device_id)
        self._json(REGISTRY.inbox(device_id, domain=dev.get("domain", "")))

    def _api_download(self, path, qs):
        transfer_id = path[len("/api/download/"):].strip()
        device_id = qs.get("device_id", [""])[0]
        if not transfer_id:
            return self._json_error(400, "缺少 transfer_id")

        transfer = REGISTRY.get_transfer(transfer_id)
        if not transfer:
            return self._json_error(404, "文件不存在或已被清理")
        dev = REGISTRY.get_device(device_id)
        if not dev:
            return self._json_error(401, "未知设备，请先注册")
        if transfer.get("kind", "direct") == "domain":
            if not transfer.get("domain") or dev.get("domain", "") != transfer["domain"]:
                return self._json_error(403, "你不在该域内，无权下载")
        else:
            if transfer.get("to") != device_id:
                return self._json_error(403, "你不是该文件的收件人")

        file_path = transfer["path"]
        if not os.path.exists(file_path):
            REGISTRY.remove_transfer(transfer_id)
            return self._json_error(404, "文件不存在或已被清理")

        fallback = transfer["filename"].encode("ascii", "replace").decode("ascii").replace('"', "_") or "download"
        disposition = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
            fallback, urllib.parse.quote(transfer["filename"]))

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(transfer["size"]))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK_SIZE)

    def _api_ack(self, path, qs):
        transfer_id = path[len("/api/ack/"):].strip()
        device_id = qs.get("device_id", [""])[0]
        if not transfer_id:
            return self._json_error(400, "缺少 transfer_id")

        transfer = REGISTRY.get_transfer(transfer_id)
        if not transfer:
            return self._json_error(404, "文件不存在")
        dev = REGISTRY.get_device(device_id)
        if not dev:
            return self._json_error(401, "未知设备，请先注册")
        if transfer.get("kind", "direct") == "domain":
            if transfer.get("from") != device_id:
                return self._json_error(403, "只有发送者才能移除域共享文件")
        else:
            if transfer.get("to") != device_id:
                return self._json_error(403, "你不是该文件的收件人")

        REGISTRY.remove_transfer(transfer_id)
        try:
            os.remove(transfer["path"])
        except OSError:
            pass
        self._json({"ok": True})


# ---------------------------------------------------------------------------
# 内嵌前端
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>局域网传文件</title>
<style>
:root {
  --bg: #F2F2F7;
  --card: #FFFFFF;
  --separator: #E5E5EA;
  --text: #000000;
  --secondary: #8E8E93;
  --header: #6D6D72;
  --accent: #007AFF;
  --green: #34C759;
  --red: #FF3B30;
  --fill: rgba(118, 118, 128, 0.12);
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
.app { max-width: 680px; margin: 0 auto; padding: 16px; }

/* iOS 大标题 */
.topbar { display: flex; align-items: center; gap: 12px; padding: 8px 4px 4px; }
.topbar h1 { font-size: 30px; font-weight: 700; margin: 0; flex: 1; }
.me-name { display: flex; align-items: center; gap: 8px; }
.me-name label { font-size: 14px; color: var(--secondary); }
.me-name input {
  width: 108px; padding: 8px 12px; font-size: 15px; border: none; border-radius: 10px;
  background: var(--fill); color: var(--text); outline: none;
}

/* 区块 */
.section { margin-bottom: 22px; }
.section-head { display: flex; align-items: center; justify-content: space-between; padding: 0 16px; margin: 0 0 8px; }
.section-head h2 { font-size: 13px; font-weight: 400; color: var(--header); margin: 0; letter-spacing: .2px; }
.section-head .hint { font-size: 12px; color: var(--secondary); }

/* iOS 分组列表：白色圆角容器 + 行间分割线 */
.ios-list {
  list-style: none; margin: 0; padding: 0;
  background: var(--card); border-radius: 10px; overflow: hidden;
}
.ios-row {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  background: var(--card); min-height: 56px;
}
.ios-row + .ios-row { border-top: 0.5px solid var(--separator); }
.ios-row.empty { justify-content: center; color: var(--secondary); font-size: 14px; }

/* 设备行 */
.device { cursor: pointer; transition: background .15s ease; }
.device.offline { opacity: .45; cursor: not-allowed; }
.device.selected { background: rgba(0, 122, 255, 0.08); }
.device.selected .dname { color: var(--accent); }
.avatar {
  position: relative; width: 40px; height: 40px; border-radius: 50%; flex: none;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 17px; font-weight: 600;
}
.adot {
  position: absolute; right: -1px; bottom: -1px; width: 12px; height: 12px;
  border-radius: 50%; background: var(--green); border: 2px solid #fff;
}
.adot.off { background: #C7C7CC; }
.dmeta { flex: 1; min-width: 0; }
.dname { font-size: 17px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dsub { font-size: 13px; color: var(--secondary); margin-top: 2px; }
.check { color: var(--accent); font-size: 20px; font-weight: 700; opacity: 0; flex: none; }
.device.selected .check { opacity: 1; }

/* 收件箱行 */
.ficon { width: 40px; height: 40px; border-radius: 10px; flex: none; background: rgba(0,122,255,.10); color: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 18px; }
.fmeta { flex: 1; min-width: 0; }
.fname { font-size: 17px; font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fsub { font-size: 13px; color: var(--secondary); margin-top: 2px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.tag { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; }
.tag.direct { background: rgba(0,122,255,.12); color: var(--accent); }
.tag.domain { background: rgba(175,82,222,.12); color: #AF52DE; }

/* 按钮（iOS 文本/实心按钮） */
button { cursor: pointer; font-family: inherit; border: none; background: none; font-size: 15px; color: var(--accent); padding: 0; }
.btn { padding: 7px 16px; border-radius: 12px; background: var(--accent); color: #fff; font-weight: 600; }
.btn:active { opacity: .6; }
.btn.ghost { background: transparent; color: var(--accent); }
.btn.destructive { background: transparent; color: var(--red); }
.btn.small { font-size: 14px; }
.link-btn { color: var(--accent); font-size: 15px; flex: none; }
.link-btn.destructive { color: var(--red); }
a.dl { text-decoration: none; }

/* 域号 */
.domain-row { display: flex; gap: 10px; align-items: center; padding: 12px 16px; flex-wrap: wrap; }
.domain-row input {
  width: 96px; padding: 9px 0; font-size: 20px; letter-spacing: 6px; text-align: center;
  border: none; border-radius: 10px; background: var(--fill); color: var(--text); outline: none;
}
.domain-status { flex: 1; font-size: 14px; color: var(--secondary); min-width: 140px; }
.domain-status b { color: var(--accent); }
.pill { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: rgba(118,118,128,.15); color: var(--secondary); }

/* 拖放区 */
.dropzone {
  border: 1.5px dashed rgba(0,122,255,.4); border-radius: 12px; padding: 26px 16px; text-align: center;
  color: var(--secondary); cursor: pointer; background: var(--card); transition: all .15s; margin: 0 0 4px;
}
.dropzone.drag { border-color: var(--accent); background: rgba(0,122,255,.06); color: var(--accent); }
.dropzone .big { font-size: 26px; }
.dropzone b { display: block; color: var(--text); margin: 6px 0 2px; font-size: 15px; font-weight: 600; }
.dropzone small { font-size: 13px; color: var(--secondary); }

/* 发送进度 */
.sends { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
.send-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-radius: 10px; background: var(--card); }
.send-item .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.bar { flex: none; width: 100px; height: 5px; border-radius: 3px; background: rgba(118,118,128,.2); overflow: hidden; }
.bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .1s; }
.send-item .st { flex: none; font-size: 13px; color: var(--secondary); width: 56px; text-align: right; }
.send-item .st.ok { color: var(--green); }
.send-item .st.err { color: var(--red); }

.hidden { display: none !important; }
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <h1>局域网传文件</h1>
    <div class="me-name">
      <label>我是</label>
      <input id="my-name" placeholder="我的名字">
    </div>
  </div>

  <section class="section">
    <div class="section-head"><h2>域号（房间）</h2><span class="hint">同域号才能互相看到</span></div>
    <div class="ios-list">
      <div class="domain-row">
        <input id="domain-input" maxlength="4" inputmode="numeric" pattern="[0-9]*" placeholder="4 位数字">
        <button class="btn small" id="domain-join">加入</button>
        <button class="btn ghost small hidden" id="domain-leave">退出域</button>
        <div class="domain-status" id="domain-status"></div>
      </div>
    </div>
  </section>

  <section class="section">
    <div class="section-head">
      <h2 id="devices-title">在线设备</h2>
      <span class="hint" id="devices-hint"></span>
    </div>
    <ul class="ios-list" id="device-list"><li class="ios-row empty">正在发现设备…</li></ul>
  </section>

  <section class="section hidden" id="session-panel">
    <div class="section-head">
      <h2 id="peer-name">发给 …</h2>
      <button class="link-btn destructive" id="deselect-btn">取消</button>
    </div>
    <div class="dropzone" id="dropzone">
      <div class="big">📤</div>
      <b>拖拽文件到这里，或点击选择</b>
      <small>一对一私发 · 支持多文件、大文件、中文文件名</small>
    </div>
    <input type="file" id="file-input" multiple class="hidden">
    <ul class="sends" id="send-list"></ul>
  </section>

  <section class="section hidden" id="broadcast-panel">
    <div class="section-head"><h2>发给域内所有人</h2><span class="hint" id="broadcast-hint"></span></div>
    <div class="dropzone" id="bdropzone">
      <div class="big">📢</div>
      <b>拖拽文件到这里，或点击选择</b>
      <small id="bdrop-small">域内所有成员都能看到并下载</small>
    </div>
    <input type="file" id="bfile-input" multiple class="hidden">
    <ul class="sends" id="bsend-list"></ul>
  </section>

  <section class="section">
    <div class="section-head"><h2>收到的文件</h2><span class="hint">私发与域共享</span></div>
    <ul class="ios-list" id="inbox-list"><li class="ios-row empty">暂无文件</li></ul>
  </section>
</div>

<script>
(function () {
  var LS_ID = "lanfiles_device_id";
  var LS_DOMAIN = "lanfiles_domain";
  var me = { id: localStorage.getItem(LS_ID) || "", name: "", domain: localStorage.getItem(LS_DOMAIN) || "" };
  var selectedId = null;
  var devices = [];
  var inbox = [];

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var fmtSize = function (n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  };
  var AV_COLORS = ["#6366f1", "#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#14b8a6"];
  function avatarColor(name) {
    var h = 0, i;
    for (i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return AV_COLORS[h % AV_COLORS.length];
  }

  function register() {
    var body = { device_id: me.id, name: me.name, domain: me.domain };
    return fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (d) {
      me.id = d.device_id;
      me.name = d.name;
      me.domain = d.domain || "";
      localStorage.setItem(LS_ID, me.id);
      localStorage.setItem(LS_DOMAIN, me.domain);
      $("my-name").value = d.name;
      renderDomain();
    });
  }

  function poll() {
    if (!me.id) return;
    fetch("/api/devices?device_id=" + encodeURIComponent(me.id))
      .then(function (r) { return r.json(); })
      .then(function (list) { if (Array.isArray(list)) { devices = list; renderDevices(); } })
      .catch(function () {});
    fetch("/api/inbox?device_id=" + encodeURIComponent(me.id))
      .then(function (r) { return r.json(); })
      .then(function (list) { if (Array.isArray(list)) { inbox = list; renderInbox(); } })
      .catch(function () {});
  }

  function renderDomain() {
    $("domain-input").value = me.domain;
    var st = $("domain-status");
    var leave = $("domain-leave");
    var join = $("domain-join");
    var bp = $("broadcast-panel");
    if (me.domain) {
      st.innerHTML = '已加入域 <b>#' + esc(me.domain) + '</b> · 仅同域设备可见';
      join.textContent = "改域";
      leave.classList.remove("hidden");
      bp.classList.remove("hidden");
      $("broadcast-hint").textContent = "域 #" + me.domain;
    } else {
      st.innerHTML = '未加入域 · <span class="pill">公开模式</span>';
      join.textContent = "加入";
      leave.classList.add("hidden");
      bp.classList.add("hidden");
    }
  }

  function renderDevices() {
    var ul = $("device-list");
    var title = $("devices-title");
    var hint = $("devices-hint");
    ul.innerHTML = "";
    if (me.domain) {
      title.textContent = "域内设备";
      hint.textContent = "域 #" + me.domain + " · " + devices.length + " 人";
    } else {
      title.textContent = "在线设备";
      hint.textContent = "公开模式";
    }
    if (!devices.length) {
      ul.innerHTML = '<li class="ios-row empty">还没有其他设备上线</li>';
      return;
    }
    devices.forEach(function (d) {
      var li = document.createElement("li");
      li.className = "ios-row device" + (d.online ? "" : " offline") + (d.id === selectedId ? " selected" : "");
      var av = document.createElement("div");
      av.className = "avatar";
      av.style.background = avatarColor(d.name);
      av.textContent = d.name.charAt(0);
      var adot = document.createElement("span");
      adot.className = "adot" + (d.online ? "" : " off");
      av.appendChild(adot);
      var meta = document.createElement("div");
      meta.className = "dmeta";
      var dn = document.createElement("div");
      dn.className = "dname";
      dn.textContent = d.name;
      var ds = document.createElement("div");
      ds.className = "dsub";
      ds.textContent = d.online ? "在线" : "离线";
      meta.appendChild(dn); meta.appendChild(ds);
      var check = document.createElement("span");
      check.className = "check";
      check.textContent = "✓";
      li.appendChild(av); li.appendChild(meta); li.appendChild(check);
      if (d.online) li.onclick = function () { selectDevice(d.id, d.name); };
      ul.appendChild(li);
    });
    if (selectedId) {
      var still = devices.some(function (d) { return d.id === selectedId && d.online; });
      if (!still) closeSession();
    }
  }

  function renderInbox() {
    var ul = $("inbox-list");
    ul.innerHTML = "";
    if (!inbox.length) {
      ul.innerHTML = '<li class="ios-row empty">暂无文件</li>';
      return;
    }
    inbox.forEach(function (t) {
      var isDomain = t.kind === "domain";
      var li = document.createElement("li");
      li.className = "ios-row inbox-item";
      var ic = document.createElement("div");
      ic.className = "ficon";
      ic.textContent = "📄";
      var meta = document.createElement("div");
      meta.className = "fmeta";
      var fn = document.createElement("div");
      fn.className = "fname";
      fn.textContent = t.filename;
      var fs = document.createElement("div");
      fs.className = "fsub";
      var tag = document.createElement("span");
      tag.className = "tag " + (isDomain ? "domain" : "direct");
      tag.textContent = isDomain ? "域共享" : "私发";
      var info = document.createElement("span");
      info.textContent = t.from_name + " · " + fmtSize(t.size);
      fs.appendChild(tag); fs.appendChild(info);
      meta.appendChild(fn); meta.appendChild(fs);

      var a = document.createElement("a");
      a.className = "dl";
      a.href = "/api/download/" + t.transfer_id + "?device_id=" + encodeURIComponent(me.id);
      a.setAttribute("download", t.filename);
      var btn = document.createElement("button");
      btn.className = "link-btn";
      btn.textContent = "下载";
      a.appendChild(btn);

      li.appendChild(ic); li.appendChild(meta); li.appendChild(a);
      if (!isDomain) {
        var rm = document.createElement("button");
        rm.className = "link-btn destructive";
        rm.textContent = "移除";
        rm.onclick = function () { ack(t.transfer_id); };
        li.appendChild(rm);
      }
      ul.appendChild(li);
    });
  }

  function ack(tid) {
    fetch("/api/ack/" + tid + "?device_id=" + encodeURIComponent(me.id), { method: "POST" })
      .then(function () { poll(); }).catch(function () {});
  }

  function selectDevice(id, name) {
    if (id === selectedId) { closeSession(); return; }
    selectedId = id;
    $("peer-name").textContent = "发给 · " + name;
    $("session-panel").classList.remove("hidden");
    $("send-list").innerHTML = "";
    renderDevices();
  }
  function closeSession() {
    selectedId = null;
    $("session-panel").classList.add("hidden");
    renderDevices();
  }

  function sendFiles(files, listId, urlFn) {
    for (var i = 0; i < files.length; i++) sendOne(files[i], listId, urlFn);
  }
  function sendOne(file, listId, urlFn) {
    var li = document.createElement("li");
    li.className = "send-item";
    li.innerHTML = '<span class="nm"></span><span class="bar"><i></i></span><span class="st">等待</span>';
    li.querySelector(".nm").textContent = file.name;
    $(listId).appendChild(li);
    var fill = li.querySelector(".bar > i");
    var st = li.querySelector(".st");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", urlFn(file));
    xhr.upload.onprogress = function (e) { if (e.lengthComputable) fill.style.width = (e.loaded / e.total * 100) + "%"; };
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        fill.style.width = "100%"; st.textContent = "已发送"; st.className = "st ok";
      } else {
        st.textContent = "失败"; st.className = "st err";
        try { console.error(JSON.parse(xhr.responseText).error); } catch (e) {}
      }
    };
    xhr.onerror = function () { st.textContent = "失败"; st.className = "st err"; };
    xhr.send(file);
  }

  function directUrl(file) {
    return "/api/send?from=" + encodeURIComponent(me.id) + "&to=" + encodeURIComponent(selectedId) + "&name=" + encodeURIComponent(file.name);
  }
  function broadcastUrl(file) {
    return "/api/send?from=" + encodeURIComponent(me.id) + "&domain=" + encodeURIComponent(me.domain) + "&name=" + encodeURIComponent(file.name);
  }

  function setupDrop(id, inputId, listId, urlFn, needSelect) {
    var dz = $(id), input = $(inputId);
    dz.onclick = function () {
      if (needSelect && !selectedId) { alert("请先在设备列表里选择一个在线设备"); return; }
      input.click();
    };
    input.onchange = function () {
      if (needSelect && !selectedId) { alert("请先在设备列表里选择一个在线设备"); return; }
      sendFiles(input.files, listId, urlFn);
      input.value = "";
    };
    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("drag"); });
    });
    dz.addEventListener("drop", function (e) {
      if (needSelect && !selectedId) { alert("请先在设备列表里选择一个在线设备"); return; }
      sendFiles(e.dataTransfer.files, listId, urlFn);
    });
  }

  $("my-name").addEventListener("change", function () {
    var v = this.value.trim();
    if (!v) { this.value = me.name; return; }
    me.name = v;
    register();
  });

  $("domain-join").addEventListener("click", function () {
    var v = $("domain-input").value.trim();
    if (v && !/^\d{4}$/.test(v)) { alert("域号需为 4 位数字"); return; }
    me.domain = v;
    localStorage.setItem(LS_DOMAIN, me.domain);
    selectedId = null; closeSession();
    register().then(poll);
  });
  $("domain-leave").addEventListener("click", function () {
    me.domain = "";
    localStorage.setItem(LS_DOMAIN, "");
    selectedId = null; closeSession();
    register().then(poll);
  });
  $("deselect-btn").addEventListener("click", closeSession);

  setupDrop("dropzone", "file-input", "send-list", directUrl, true);
  setupDrop("bdropzone", "bfile-input", "bsend-list", broadcastUrl, false);

  register().then(function () { poll(); setInterval(poll, 1500); });
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _collect_ips():
    """收集本机所有 IPv4 接口地址。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.add("127.0.0.1")
    return ips


def _default_route_ip():
    """通过 UDP connect 取「默认路由接口」的本机源地址。

    真实网卡才承载默认路由，WSL/Hyper-V/虚拟机等虚拟网卡没有，因此能正确
    排除 172.x.x.1 这类虚拟地址；取不到（无路由/纯隔离网）时返回 None。
    """
    for target in ("8.8.8.8", "1.1.1.1", "114.114.114.114"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return None


def _ip_sort_key(ip):
    """地址权重：常见真实局域网(192.168/10) < 其它(172.16-31、公网) < 链路本地 < 回环。"""
    if ip.startswith("127."):
        return (3, ip)
    if ip.startswith("169.254."):
        return (2, ip)
    if ip.startswith(("192.168.", "10.")):
        return (0, ip)
    return (1, ip)


def choose_addresses(ips, default_route_ip):
    """从接口 IP 集合中选出 (主地址, 其它地址列表)。纯函数，便于测试。"""
    if not ips:
        return ("127.0.0.1", [])
    primary = default_route_ip if (default_route_ip and default_route_ip in ips) else None
    if not primary:
        primary = min((ip for ip in ips if not ip.startswith("127.")),
                      key=_ip_sort_key, default=None)
    if not primary:
        primary = "127.0.0.1"
    others = sorted((ip for ip in ips if ip != primary), key=_ip_sort_key)
    return primary, others


def local_ipv4():
    """返回 (主地址, 其它地址列表)。主地址优先取默认路由接口的真实局域网 IP。"""
    return choose_addresses(_collect_ips(), _default_route_ip())


def _configure_console():
    """Windows 控制台默认非 UTF-8，强制 UTF-8 输出，避免中文乱码或 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _configure_console()
    parser = argparse.ArgumentParser(description="局域网传文件（单文件、零依赖）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，局域网可达）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--name", default="", help="本机显示名（默认随机）")
    parser.add_argument("--dir", default=None,
                        help="中转文件存放目录（默认 ~/Downloads/lanfiles，被系统限制时自动改用 ~/lanfiles）")
    args = parser.parse_args()

    global SPOOL_DIR
    if args.dir is not None:
        SPOOL_DIR = ensure_spool_dir(os.path.abspath(args.dir))
        if SPOOL_DIR is None:
            print("错误：无法创建中转目录 %s（不可写或没有权限）" % args.dir)
            sys.exit(1)
    else:
        candidates = [default_spool_dir()] + fallback_spool_dirs()
        seen = set()
        SPOOL_DIR = None
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            created = ensure_spool_dir(cand)
            if created:
                SPOOL_DIR = created
                break
        if SPOOL_DIR is None:
            print("错误：无法创建任何可写的中转目录，尝试过：%s" % ", ".join(seen))
            sys.exit(1)
        if SPOOL_DIR != candidates[0]:
            print("提示：默认目录 %s 无写入权限（系统可能限制访问“下载”文件夹，如 macOS 隐私权限或 Windows 安全策略），已改用 %s。"
                  % (candidates[0], SPOOL_DIR), flush=True)
            print("      若想用下载文件夹，请授予访问权限后重试，或运行 python3 transfer.py --dir <目录>", flush=True)

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print("错误：无法监听 %s:%s（%s）" % (args.host, args.port, e))
        print("提示：可用 --port 换一个端口，例如 python3 transfer.py --port 9000")
        sys.exit(1)

    httpd.daemon_threads = True
    primary, others = local_ipv4()

    if sys.platform.startswith("win"):
        fw_hint = "    ④ Windows 首次运行若弹防火墙提示，勾选「专用网络」并点「允许访问」"
    elif sys.platform == "darwin":
        fw_hint = "    ④ macOS 若弹防火墙提示，选择「允许」Python 接受传入连接"
    else:
        fw_hint = "    ④ 若系统防火墙拦截，请允许本程序接受传入连接"

    lines = ["=" * 60,
             "  局域网传文件已启动",
             "  请在其他设备的浏览器打开（注意用 http:// 开头）：",
             "    ★ http://%s:%d" % (primary, args.port)]
    if others:
        lines.append("  本机 / 备用地址：")
        for ip in others:
            lines.append("    http://%s:%d" % (ip, args.port))
        lines.append("  （若 ★ 连不上，改用与你设备同网段的备用地址；172.x 且以 .1 结尾的多为虚拟机/WSL 网卡，不是局域网地址。）")
    lines += ["  中转目录：%s" % SPOOL_DIR,
              "  按 Ctrl+C 退出",
              "-" * 60,
              "  提示：其他设备连不上时，先确认本程序仍在运行（此窗口保持打开、不要 Ctrl+C）。",
              "  连不上？逐条排查：",
              "    ① 设备与电脑连同一个路由器/网段（网段需一致，如 192.168.5.x）",
              "    ② 用 http:// 开头，不要用 https://",
              "    ③ 路由器关闭「AP/客户端/无线隔离」",
              fw_hint,
              "=" * 60]
    print("\n".join(lines), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
