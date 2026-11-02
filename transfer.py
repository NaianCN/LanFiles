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
import base64
import hashlib
import hmac
import ipaddress
import secrets
import signal
import sqlite3
import stat
from collections import deque
from dataclasses import dataclass
from http.cookies import SimpleCookie, CookieError
import json
import os
import re
import shutil
import socket
import subprocess
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
DISK_CHECK_INTERVAL = 64 * 1024 * 1024  # 上传中每写入这么多字节复查一次磁盘余量
DOMAIN_RE = re.compile(r"[0-9]{4}")  # 域号：4 位数字（含前导 0）

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
        with tempfile.TemporaryFile(dir=target) as f:
            f.write(b"ok")
            f.flush()
        return os.path.abspath(target)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 持久化登记、身份、资源限制
# ---------------------------------------------------------------------------
SESSION_TTL = 7 * 24 * 3600
AUTH_NONCE_TTL = 120.0         # 登录随机数有效期（秒）
AUTH_FAILURE_WINDOW = 300.0    # 密码错误计数窗口（秒）
AUTH_FAILURES_PER_IP = 5
AUTH_FAILURES_GLOBAL = 50
NAME_MEMORY_TTL = 30 * 24 * 3600  # 按来源 IP 记住的名字保留多久
COOKIE_NAME = "lanfiles_session"
ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Limits:
    max_file_size: int = 10 * 1024**3
    spool_quota: int = 20 * 1024**3
    max_uploads: int = 4
    uploads_per_device: int = 3
    min_free_space: int = 1024**3
    max_connections: int = 64
    max_devices: int = 256
    max_transfers: int = 1024
    io_timeout: float = 30.0
    upload_timeout: float = 6 * 3600.0
    register_per_ip: int = 10
    register_global: int = 100

    def __post_init__(self):
        for name in ("max_file_size", "spool_quota", "min_free_space"):
            if getattr(self, name) < 0:
                raise ValueError(name + " 必须非负")
        for name in ("max_uploads", "uploads_per_device", "max_connections", "max_devices", "max_transfers",
                     "io_timeout", "upload_timeout", "register_per_ip", "register_global"):
            if getattr(self, name) <= 0:
                raise ValueError(name + " 必须大于零")


class APIError(Exception):
    def __init__(self, status, message, code=None):
        self.status, self.message, self.code = status, message, code
        super().__init__(message)


class Store:
    """所有登记、空间预留及读者计数都在同一个锁内更新。"""
    def __init__(self, spool_dir, limits=None):
        self.limits = limits or Limits()
        self.root = os.path.abspath(spool_dir)
        self.state_dir = os.path.join(self.root, ".lanfiles")
        self.blob_dir = os.path.join(self.state_dir, "files")
        for directory in (self.state_dir, self.blob_dir):
            if os.path.islink(directory):
                raise RuntimeError("中转状态目录不能是符号链接")
            os.makedirs(directory, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._readers = {}
        self._written = {}
        self._checked = {}
        self._seen = {}
        self._registrations = deque()
        self._instance = None
        self.db = None
        self.closed = False
        try:
            lock_path = os.path.join(self.state_dir, "instance.lock")
            if os.path.islink(lock_path):
                raise RuntimeError("实例锁不能是符号链接")
            self._instance = open(lock_path, "a+b")
            if os.name == "nt":
                import msvcrt
                self._instance.seek(0, os.SEEK_END)
                if self._instance.tell() == 0:
                    self._instance.write(b"\0")
                    self._instance.flush()
                self._instance.seek(0)
                try:
                    msvcrt.locking(self._instance.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise RuntimeError(self._busy_message(None)) from None
            else:
                import fcntl
                try:
                    fcntl.flock(self._instance.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._instance.seek(0)
                    holder = self._instance.read(16).decode("ascii", "replace").strip()
                    raise RuntimeError(self._busy_message(holder if holder.isdigit() else None)) from None
                # 记下持锁进程号，下一个实例启动失败时可以告诉用户是谁占用了目录。
                self._instance.seek(0)
                self._instance.truncate()
                self._instance.write(str(os.getpid()).encode("ascii"))
                self._instance.flush()
            db_path = os.path.join(self.state_dir, "state.sqlite3")
            if os.path.islink(db_path):
                raise RuntimeError("状态数据库不能是符号链接")
            self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("状态数据库损坏，请保留文件并从备份恢复")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("不支持的状态数据库版本")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, secret_hash TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, domain TEXT NOT NULL, addr TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    transfer_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    sender TEXT NOT NULL, sender_name TEXT NOT NULL, target TEXT,
                    domain TEXT NOT NULL, filename TEXT NOT NULL,
                    expected INTEGER NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS names (
                    addr TEXT PRIMARY KEY, name TEXT NOT NULL, updated REAL NOT NULL
                );
                PRAGMA user_version=1;
            ''')
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO settings VALUES ('instance_id',?)", (uuid.uuid4().hex,))
            instance_id = self.db.execute("SELECT value FROM settings WHERE key='instance_id'").fetchone()[0]
            if not ID_RE.fullmatch(instance_id):
                raise RuntimeError("状态数据库含非法实例 ID")
            # Cookie 不按端口隔离：用持久化实例 ID 隔离不同中转目录，同时支持改端口。
            self.cookie_name = COOKIE_NAME + "_" + instance_id
            self.instance_id = instance_id
            self.recover()
        except Exception:
            self.close()
            raise

    def _busy_message(self, pid):
        return "中转目录 %s 已被另一个 LanFiles 服务占用%s。请先关闭那个服务（其他窗口或终端），或换一个目录" % (
            self.root, "（进程 %s）" % pid if pid else "")

    def file_path(self, tid, partial=False):
        if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
            raise RuntimeError("非法的中转文件登记 ID")
        return os.path.join(self.blob_dir, tid + (".part" if partial else ""))

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            if self.db is not None:
                self.db.close()
            # 关闭文件描述符会释放 flock / Windows 字节锁。
            if self._instance is not None:
                self._instance.close()

    def _device(self, did):
        row = self.db.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if row is None or row["expires"] <= time.time():
            return None
        dev = dict(row)
        dev["last_seen"] = self._seen.get(did, dev["last_seen"])
        return dev

    def authenticate(self, token):
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._lock:
            row = self.db.execute("SELECT id FROM devices WHERE secret_hash=?", (digest,)).fetchone()
            dev = self._device(row[0]) if row else None
            if dev:
                now = time.time()
                self._seen[dev["id"]] = now
                if now - dev["last_seen"] >= 60 or now - self.db.execute(
                        "SELECT last_seen FROM devices WHERE id=?", (dev["id"],)).fetchone()[0] >= 60:
                    with self.db:
                        self.db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, dev["id"]))
                dev["last_seen"] = now
            return dev

    def register(self, token, name, domain, addr):
        with self._lock:
            now = time.time()
            while self._registrations and self._registrations[0][0] <= now - 60:
                self._registrations.popleft()
            if len(self._registrations) >= self.limits.register_global or sum(
                    ip == addr for _, ip in self._registrations) >= self.limits.register_per_ip:
                raise APIError(429, "注册过于频繁，请稍后重试")
            self._registrations.append((now, addr))
            dev = self.authenticate(token)
            new_token = None
            with self.db:
                self.db.execute("DELETE FROM devices WHERE expires<=?", (now,))
                if dev:
                    self.db.execute("UPDATE devices SET name=?,domain=?,addr=?,last_seen=? WHERE id=?",
                                    (name or dev["name"], domain if domain is not None else dev["domain"],
                                     addr, now, dev["id"]))
                    did = dev["id"]
                else:
                    if self.db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= self.limits.max_devices:
                        raise APIError(429, "设备登记已达上限，请等待旧身份过期")
                    did, new_token = uuid.uuid4().hex, secrets.token_urlsafe(32)
                    if not name:
                        # 浏览器没带名字（清过数据、换了浏览器）时，沿用该 IP 上次用的名字。
                        row = self.db.execute("SELECT name FROM names WHERE addr=? AND updated>?",
                                              (addr, now - NAME_MEMORY_TTL)).fetchone()
                        name = row[0] if row else random_name()
                    self.db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?)", (
                        did, hashlib.sha256(new_token.encode("ascii")).hexdigest(),
                        name, domain or "", addr, now, now + SESSION_TTL, now))
                self.db.execute("INSERT OR REPLACE INTO names VALUES (?,?,?)",
                                (addr, name or dev["name"], now))
            self._seen[did] = now
            return self._device(did), new_token

    def online_devices(self, did):
        with self._lock:
            dev = self._device(did)
            if not dev:
                raise APIError(401, "设备身份已失效，请重新连接")
            now = time.time()
            return [{"id": row["id"], "name": row["name"],
                     "online": now - self._seen.get(row["id"], row["last_seen"]) <= OFFLINE_TTL}
                    for row in self.db.execute("SELECT * FROM devices WHERE domain=? AND id<>? AND expires>?",
                                               (dev["domain"], did, now))]

    def all_devices(self):
        """本机管理视图：所有房间的有效设备。"""
        with self._lock:
            now = time.time()
            return [{"id": row["id"], "name": row["name"], "domain": row["domain"], "addr": row["addr"],
                     "online": now - self._seen.get(row["id"], row["last_seen"]) <= OFFLINE_TTL}
                    for row in self.db.execute("SELECT * FROM devices WHERE expires>? ORDER BY created", (now,))]

    def delete_device(self, did):
        """注销设备身份并清理发给它的私发文件；在线设备会以新身份自动重连。"""
        with self._lock:
            if not self._device(did):
                raise APIError(404, "设备不存在")
            for row in list(self.db.execute(
                    "SELECT transfer_id FROM transfers WHERE target=? AND state='ready'", (did,))):
                self._mark_delete(row[0])
                self._delete(row[0])
            with self.db:
                self.db.execute("DELETE FROM devices WHERE id=?", (did,))
            self._seen.pop(did, None)
            return {"ok": True}

    def apply_password(self, password):
        """访问密码变化（含首次启用）时注销全部旧身份；返回被注销的数量。"""
        tag = hmac.new(password.encode("utf-8"), self.instance_id.encode("ascii"), "sha256").hexdigest() if password else ""
        with self._lock:
            self.cleanup()  # 先回收过期身份，下面只处理有效身份
            row = self.db.execute("SELECT value FROM settings WHERE key='password_tag'").fetchone()
            removed = 0
            if tag and (row is None or row[0] != tag):
                for (did,) in list(self.db.execute("SELECT id FROM devices")):
                    self.delete_device(did)
                    removed += 1
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('password_tag',?)", (tag,))
            return removed

    def _used(self):
        return self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='uploading' THEN expected ELSE size END),0) FROM transfers").fetchone()[0]

    def _unwritten(self):
        return sum(max(0, row["expected"] - self._written.get(row["transfer_id"], 0))
                   for row in self.db.execute("SELECT transfer_id,expected FROM transfers WHERE state='uploading'"))

    def begin_upload(self, did, target, domain, filename, total):
        with self._lock:
            self.cleanup()
            dev = self._device(did)
            if not dev:
                raise APIError(401, "设备身份已失效")
            if bool(target) == bool(domain):
                raise APIError(400, "to（私发）与 domain（广播）必须二选一")
            if target:
                receiver = self._device(target)
                if not receiver:
                    raise APIError(404, "目标设备不存在")
                if receiver["domain"] != dev["domain"]:
                    raise APIError(403, "目标设备不在你的域内")
                if time.time() - receiver["last_seen"] > OFFLINE_TTL:
                    raise APIError(410, "目标设备已离线")
            elif not DOMAIN_RE.fullmatch(domain):
                raise APIError(400, "域号需为 4 位数字")
            elif dev["domain"] != domain:
                raise APIError(403, "你不在该域内")
            if total > self.limits.max_file_size:
                raise APIError(413, "文件超过单文件上限")
            active = list(self.db.execute("SELECT sender FROM transfers WHERE state='uploading'"))
            if len(active) >= self.limits.max_uploads or sum(
                    row[0] == did for row in active) >= self.limits.uploads_per_device:
                raise APIError(429, "上传并发已满，请稍后重试")
            if self.db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] >= self.limits.max_transfers:
                raise APIError(429, "传输登记已达上限")
            if self._used() + total > self.limits.spool_quota:
                raise APIError(507, "中转空间配额不足，请移除文件或等待过期清理")
            if shutil.disk_usage(self.blob_dir).free < self._unwritten() + total + self.limits.min_free_space:
                raise APIError(507, "磁盘可用空间不足，已保留安全余量")
            tid = uuid.uuid4().hex
            with self.db:
                self.db.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    tid, "uploading", did, dev["name"], target or None, domain or "",
                    filename, total, 0, time.time()))
            self._written[tid] = 0
            return tid

    def write_chunk(self, tid, file, chunk):
        # 余量按区间抽查、写盘不持全局锁：逐块 statvfs + 查库会让并发上传互相等待。
        with self._lock:
            done = self._written[tid]
            last = self._checked.get(tid)
            if last is None or done - last >= DISK_CHECK_INTERVAL:
                if shutil.disk_usage(self.blob_dir).free < self._unwritten() + self.limits.min_free_space:
                    raise APIError(507, "磁盘可用空间不足")
                self._checked[tid] = done
        view = memoryview(chunk)
        while view:
            written = file.write(view)
            if not written:
                raise OSError("磁盘写入未取得进展")
            view = view[written:]
        with self._lock:
            self._written[tid] += len(chunk)

    def finish_upload(self, tid):
        with self._lock:
            row = self.db.execute("SELECT * FROM transfers WHERE transfer_id=? AND state='uploading'", (tid,)).fetchone()
            if row is None or self._written.get(tid) != row["expected"]:
                raise APIError(400, "文件内容不完整，未投递")
            os.replace(self.file_path(tid, True), self.file_path(tid))
            with self.db:
                self.db.execute("UPDATE transfers SET state='ready',size=expected,created=? WHERE transfer_id=?", (time.time(), tid))
            self._written.pop(tid, None)
            self._checked.pop(tid, None)
            return {"transfer_id": tid, "size": row["expected"], "filename": row["filename"],
                    "kind": "direct" if row["target"] else "domain"}

    def _mark_delete(self, tid):
        size = 0
        for path in (self.file_path(tid), self.file_path(tid, True)):
            try:
                size += os.lstat(path).st_size
            except FileNotFoundError:
                pass
        with self.db:
            self.db.execute("UPDATE transfers SET state='deleting',size=? WHERE transfer_id=?", (size, tid))
        self._written.pop(tid, None)
        self._checked.pop(tid, None)

    def _delete(self, tid):
        if self._readers.get(tid):
            return False
        for path in (self.file_path(tid), self.file_path(tid, True)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                return False
        with self.db:
            self.db.execute("DELETE FROM transfers WHERE transfer_id=?", (tid,))
        return True

    def cancel_upload(self, tid):
        with self._lock:
            self._mark_delete(tid)
            self._delete(tid)

    def cleanup(self):
        with self._lock:
            now = time.time()
            for row in list(self.db.execute("SELECT transfer_id FROM transfers WHERE state='ready' AND created<=?", (now-FILE_TTL,))):
                self._mark_delete(row[0])
            for row in list(self.db.execute("SELECT transfer_id FROM transfers WHERE state='deleting'")):
                self._delete(row[0])
            with self.db:
                self.db.execute("DELETE FROM devices WHERE expires<=?", (now,))
                self.db.execute("DELETE FROM names WHERE updated<=?", (now - NAME_MEMORY_TTL,))
            expired_seen = [did for did in self._seen if self._device(did) is None]
            for did in expired_seen:
                self._seen.pop(did, None)

    def recover(self):
        with self._lock:
            known = set()
            for row in list(self.db.execute("SELECT * FROM transfers")):
                tid = row["transfer_id"]
                self.file_path(tid)  # 校验磁盘路径之前先校验持久化 ID。
                known.add(tid)
                if row["state"] == "uploading" or (row["state"] == "ready" and not os.path.isfile(self.file_path(tid))):
                    self._mark_delete(tid)
                elif row["state"] not in ("ready", "deleting"):
                    raise RuntimeError("状态数据库含未知传输状态")
            for entry in os.scandir(self.blob_dir):
                tid = entry.name.removesuffix(".part")
                if ID_RE.fullmatch(tid) and tid not in known and entry.is_file(follow_symlinks=False):
                    # 只触碰本工具专用目录里的已知命名文件。
                    try:
                        os.unlink(entry.path)
                    except OSError as exc:
                        raise RuntimeError("无法回收孤立中转文件: " + entry.name) from exc
            self.cleanup()

    def inbox(self, did):
        with self._lock:
            self.cleanup()
            dev = self._device(did)
            if not dev:
                raise APIError(401, "设备身份已失效")
            return [{"transfer_id": row["transfer_id"], "kind": "direct" if row["target"] else "domain",
                     "filename": row["filename"], "size": row["size"], "from_name": row["sender_name"]}
                    for row in self.db.execute(
                        "SELECT * FROM transfers WHERE state='ready' AND (target=? OR (target IS NULL AND domain=? AND domain<>'' AND sender<>?)) ORDER BY created",
                        (did, dev["domain"], did))]

    def files(self, did):
        """网页管理视图；旧 inbox 继续只返回可领取文件。"""
        with self._lock:
            self.cleanup()
            dev = self._device(did)
            if not dev:
                raise APIError(401, "设备身份已失效")
            result = {"inbox": [], "outbox": []}
            rows = self.db.execute(
                "SELECT * FROM transfers WHERE state IN ('ready','deleting') AND "
                "(target=? OR (target IS NULL AND sender=?) OR "
                "(state='ready' AND target IS NULL AND domain=? AND domain<>'')) ORDER BY created",
                (did, did, dev["domain"]))
            for row in rows:
                own_broadcast = row["target"] is None and row["sender"] == did
                item = {"transfer_id": row["transfer_id"],
                        "kind": "direct" if row["target"] else "domain",
                        "filename": row["filename"], "size": row["size"],
                        "from_name": row["sender_name"], "domain": row["domain"],
                        "created": row["created"], "state": row["state"],
                        "can_delete": row["target"] == did or own_broadcast}
                result["outbox" if own_broadcast else "inbox"].append(item)
            return result

    def acquire_download(self, tid, did):
        with self._lock:
            self.cleanup()
            row = self.db.execute("SELECT * FROM transfers WHERE transfer_id=? AND state='ready'", (tid,)).fetchone()
            if row is None:
                raise APIError(404, "文件不存在或已被清理")
            dev = self._device(did)
            if not dev:
                raise APIError(401, "设备身份已失效")
            if row["target"]:
                if row["target"] != did:
                    raise APIError(403, "你不是该文件的收件人")
            elif not row["domain"] or row["domain"] != dev["domain"]:
                raise APIError(403, "你不在该域内，无权下载")
            path = self.file_path(tid)
            try:
                if os.path.islink(path):
                    raise OSError("拒绝符号链接")
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size != row["size"]:
                    os.close(fd)
                    raise OSError("文件类型或长度不符")
                file = os.fdopen(fd, "rb")
            except OSError:
                self._mark_delete(tid)
                self._delete(tid)
                raise APIError(404, "中转文件缺失或损坏")
            self._readers[tid] = self._readers.get(tid, 0) + 1
            return dict(row), file

    def release_download(self, tid):
        with self._lock:
            count = self._readers.get(tid, 1) - 1
            if count:
                self._readers[tid] = count
            else:
                self._readers.pop(tid, None)
            row = self.db.execute("SELECT state FROM transfers WHERE transfer_id=?", (tid,)).fetchone()
            if row and row[0] == "deleting":
                self._delete(tid)

    def ack(self, tid, did):
        with self._lock:
            self.cleanup()
            row = self.db.execute("SELECT * FROM transfers WHERE transfer_id=? AND state IN ('ready','deleting')", (tid,)).fetchone()
            if row is None:
                raise APIError(404, "文件不存在")
            if (row["target"] or row["sender"]) != did:
                raise APIError(403, "只有私发收件人或广播发送者才能移除")
            self._mark_delete(tid)
            deleted = self._delete(tid)
            return {"ok": True, "pending": not deleted}


def log_transfer(action, filename, size, who, seconds):
    """传输动态只写文件名、设备名、大小和速度，不含 Cookie 或请求 URL。"""
    mb = size / 1e6
    speed = " · %.1f MB/s" % (mb / seconds) if seconds >= 0.05 else ""
    print("[%s] %s %s（%.1f MB）%s · %.1f 秒%s" % (
        time.strftime("%H:%M:%S"), action, filename, mb, who, seconds, speed), flush=True)


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def random_name():
    return "%s%s" % (secrets.choice(ADJECTIVES), secrets.choice(NOUNS))


def sanitize_filename(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:200] or "file"


class LanFilesServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, store, password=None, allow_hosts=()):
        self.store = store
        self.password = password or None
        # 只接受 IP 字面量、localhost 和 --allow-host 指定的名字作为 Host，防止 DNS 重绑定。
        self.allowed_names = {"localhost"} | {h.strip().lower() for h in allow_hosts if h.strip()}
        self._auth_lock = threading.Lock()
        self._nonces = {}
        self._auth_failures = deque()
        self.stop_event = threading.Event()
        self._permits = threading.BoundedSemaphore(store.limits.max_connections)
        self._connections = set()
        self._connections_lock = threading.Lock()
        self._cleanup_thread = None
        # 只有本机（回环或本机网卡地址）发起的请求可以管理设备。
        self.admin_addrs = _collect_ips() | {"127.0.0.1", "::1"}
        try:
            super().__init__(address, Handler)
        except Exception:
            store.close()
            raise
        removed = store.apply_password(self.password)
        if removed:
            print("访问密码已变更，已注销 %d 个旧设备身份" % removed, flush=True)
        self._cleanup_thread = threading.Thread(target=self._cleanup, daemon=True)
        self._cleanup_thread.start()

    def host_allowed(self, host):
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else ""
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        name = name.lower().rstrip(".")
        return bool(name) and (_is_ip(name) or name in self.allowed_names)

    def issue_challenge(self):
        with self._auth_lock:
            now = time.monotonic()
            for nonce in [n for n, exp in self._nonces.items() if exp <= now]:
                del self._nonces[nonce]
            while len(self._nonces) >= 256:
                del self._nonces[next(iter(self._nonces))]
            nonce = secrets.token_hex(16)
            self._nonces[nonce] = now + AUTH_NONCE_TTL
            return nonce

    def verify_access(self, auth, addr):
        """挑战应答：浏览器只发送 HMAC(密码, 一次性随机数)，密码本身不经过网络。"""
        with self._auth_lock:
            now = time.monotonic()
            while self._auth_failures and self._auth_failures[0][0] <= now - AUTH_FAILURE_WINDOW:
                self._auth_failures.popleft()
            if len(self._auth_failures) >= AUTH_FAILURES_GLOBAL or sum(
                    ip == addr for _, ip in self._auth_failures) >= AUTH_FAILURES_PER_IP:
                raise APIError(429, "密码错误次数过多，请 5 分钟后再试")
            if auth is None:
                raise APIError(401, "需要访问密码", "password_required")
            if not isinstance(auth, dict) or set(auth) != {"nonce", "proof"} or not all(
                    isinstance(v, str) and len(v) <= 128 for v in auth.values()):
                raise APIError(400, "密码验证格式无效")
            expires = self._nonces.pop(auth["nonce"], None)
            expected = hmac.new(self.password.encode("utf-8"), auth["nonce"].encode("ascii", "replace"),
                                "sha256").hexdigest()
            if expires is None or expires <= now or not hmac.compare_digest(expected, auth["proof"].lower()):
                self._auth_failures.append((now, addr))
                raise APIError(401, "访问密码错误", "bad_password")

    def handle_error(self, request, client_address):
        # 客户端断开等属于正常网络噪声，不打印堆栈。
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def _cleanup(self):
        while not self.stop_event.wait(CLEANUP_INTERVAL):
            try:
                self.store.cleanup()
            except Exception as exc:
                print("[清理失败，稍后重试] %s" % exc, file=sys.stderr, flush=True)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.store.limits.io_timeout)
        return request, address

    def process_request(self, request, address):
        if not self._permits.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            with self._connections_lock:
                self._connections.discard(request)
            self._permits.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self._connections_lock:
                self._connections.discard(request)
            self._permits.release()

    def server_close(self):
        self.stop_event.set()
        with self._connections_lock:
            connections = list(self._connections)
        for request in connections:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().server_close()  # 等待处理器释放文件和空间后才能关数据库。
        if self._cleanup_thread is not None:
            self._cleanup_thread.join()
        self.store.close()


def create_server(host, port, spool_dir, limits=None, password=None, allow_hosts=()):
    return LanFilesServer((host, port), Store(spool_dir, limits), password, allow_hosts)


def parse_size(value):
    match = re.fullmatch(r"(\d+)(B|KiB|MiB|GiB)?", str(value), re.I)
    if not match:
        raise argparse.ArgumentTypeError("容量需为非负整数，可带 KiB/MiB/GiB 单位")
    multipliers = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
    size = int(match[1]) * multipliers.get((match[2] or "b").lower(), 1)
    if size > 2**63 - 1:
        raise argparse.ArgumentTypeError("容量超出支持范围")
    return size


def add_limit_arguments(parser):
    parser.add_argument("--max-file-size", type=parse_size, default=10*1024**3)
    parser.add_argument("--spool-quota", type=parse_size, default=20*1024**3)
    parser.add_argument("--max-uploads", type=int, default=4)
    parser.add_argument("--uploads-per-device", type=int, default=3)
    parser.add_argument("--min-free-space", type=parse_size, default=1024**3)


def limits_from_args(args):
    return Limits(max_file_size=args.max_file_size, spool_quota=args.spool_quota,
                  max_uploads=args.max_uploads, uploads_per_device=args.uploads_per_device,
                  min_free_space=args.min_free_space)


# ---------------------------------------------------------------------------
# HTTP 处理：身份和请求边界统一入口
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LanFiles/2.0"
    protocol_version = "HTTP/1.1"

    body_consumed = False

    @property
    def store(self):
        return self.server.store

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status, body, content_type="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200, extra=None):
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8", extra)

    def _json_error(self, status, message):
        self._json({"error": message}, status)

    def _query(self):
        parsed = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
        if any(len(values) != 1 for values in qs.values()):
            raise APIError(400, "查询参数不能重复")
        return parsed.path, qs

    def _length(self, required=False):
        if self.headers.get_all("Transfer-Encoding"):
            raise APIError(400, "不支持 Transfer-Encoding，请发送 Content-Length")
        values = self.headers.get_all("Content-Length", [])
        if not values:
            if required:
                raise APIError(400, "缺少 Content-Length")
            return 0
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,19}", values[0]):
            raise APIError(400, "Content-Length 必须为非负整数且不能重复")
        return int(values[0])

    def _token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            name = self.store.cookie_name
            return cookie[name].value if name in cookie else ""
        except CookieError:
            return ""

    def _identity(self, qs):
        dev = self.store.authenticate(self._token())
        if not dev:
            raise APIError(401, "设备身份已失效，请重新连接")
        for key in ("device_id", "from"):
            if key in qs and qs[key][0] != dev["id"]:
                raise APIError(403, "请求身份与会话不符", "identity_mismatch")
        return dev

    def _origin(self):
        origin = self.headers.get_all("Origin", [])
        hosts = self.headers.get_all("Host", [])
        if origin and (len(origin) != 1 or origin[0] != "http://" + hosts[0]):
            raise APIError(403, "拒绝跨站修改请求")

    def _drain_body(self):
        """拒绝请求前读掉不大的请求体，否则关闭连接会重置 TCP，客户端看不到错误原因。"""
        if self.body_consumed:
            return
        self.body_consumed = True
        try:
            remaining = self._length()
        except APIError:
            return
        if remaining > 1024 * 1024:
            return
        try:
            self.connection.settimeout(2)
            while remaining > 0:
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def handle_expect_100(self):
        # 不先答应接收未经校验的文件；客户端可以直接发送或等待最终响应。
        self._json_error(417, "不支持 Expect: 100-continue，请直接发送请求")
        return False

    def _dispatch(self, method):
        try:
            path, qs = self._query()
            self._length()  # 所有路由都拒绝有歧义的 HTTP framing。
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise APIError(400, "需要唯一 Host 请求头")
            if not self.server.host_allowed(hosts[0]):
                raise APIError(400, "不接受的访问地址，请用 IP 访问")
            if method == "POST":
                self._origin()
            if method == "GET" and path == "/":
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8", {"Content-Security-Policy": INDEX_CSP})
            if method == "GET" and path == "/favicon.ico":
                return self._send(204, b"")
            if method == "GET" and path == "/api/auth/challenge":
                required = bool(self.server.password)
                return self._json({"required": required, "nonce": self.server.issue_challenge() if required else None})
            if method == "POST" and path == "/api/register":
                return self._api_register()
            if path.startswith("/api/admin/"):
                if not self._is_admin():
                    raise APIError(403, "仅限运行服务的电脑管理设备")
                if method == "GET" and path == "/api/admin/devices":
                    return self._json(self.store.all_devices())
                if method == "POST" and path.startswith("/api/admin/delete-device/"):
                    return self._json(self.store.delete_device(path[len("/api/admin/delete-device/"):]))
                raise APIError(404, "未找到该路径")
            dev = self._identity(qs)
            if method == "GET" and path == "/api/session":
                return self._json(self._profile(dev))
            if method == "GET" and path == "/api/devices":
                return self._json(self.store.online_devices(dev["id"]))
            if method == "GET" and path == "/api/files":
                return self._json(self.store.files(dev["id"]))
            if method == "GET" and path == "/api/inbox":
                return self._json(self.store.inbox(dev["id"]))
            if method == "POST" and path == "/api/send":
                return self._api_send(qs, dev)
            if method == "GET" and path.startswith("/api/download/"):
                return self._api_download(path[len("/api/download/"):], dev)
            if method == "POST" and path.startswith("/api/ack/"):
                return self._json(self.store.ack(path[len("/api/ack/"):], dev["id"]))
            self._json_error(404, "未找到该路径")
        except APIError as exc:
            self._drain_body()
            if exc.code:
                self._json({"error": exc.message, "code": exc.code}, exc.status)
            else:
                self._json_error(exc.status, exc.message)
        except (ValueError, UnicodeError):
            self._json_error(400, "请求格式无效")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except (sqlite3.Error, OSError) as exc:
            print("[请求失败] %s" % exc, file=sys.stderr, flush=True)
            self._json_error(503, "状态或文件暂时不可用，请稍后重试")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass  # 不记录 Cookie 或请求 URL。

    def _api_register(self):
        length = self._length()
        if length > 16 * 1024:
            raise APIError(413, "注册 JSON 不能超过 16 KiB")
        self.body_consumed = True
        try:
            raw = self.rfile.read(length)
        except socket.timeout:
            raise APIError(408, "注册请求超时")
        if len(raw) != length:
            raise APIError(400, "注册请求内容不完整")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError):
            raise APIError(400, "请求体不是有效 JSON")
        if not isinstance(data, dict) or set(data) - {"name", "domain", "auth"}:
            raise APIError(400, "注册只接受 name、domain 和 auth，不能指定设备 ID")
        name, domain = data.get("name"), data.get("domain")
        if name is not None and (not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name)):
            raise APIError(400, "设备名称必须为不超过 80 字符的字符串")
        if domain is not None and (not isinstance(domain, str) or (domain and not DOMAIN_RE.fullmatch(domain))):
            raise APIError(400, "域号需为空或 4 位数字")
        if self.server.password and not self.store.authenticate(self._token()):
            self.server.verify_access(data.get("auth"), self.client_address[0])
        dev, token = self.store.register(self._token(), name, domain, self.client_address[0])
        extra = {}
        if token:
            extra["Set-Cookie"] = "%s=%s; Max-Age=%d; HttpOnly; SameSite=Strict; Path=/" % (
                self.store.cookie_name, token, SESSION_TTL)
        self._json(dict(self._profile(dev), new_identity=bool(token)), extra=extra)

    def _is_admin(self):
        return self.client_address[0] in self.server.admin_addrs

    def _profile(self, dev):
        return {"device_id": dev["id"], "name": dev["name"], "domain": dev["domain"],
                "admin": self._is_admin(),
                "limits": {"max_file_size": self.store.limits.max_file_size,
                           "spool_quota": self.store.limits.spool_quota,
                           "max_uploads": self.store.limits.max_uploads,
                           "uploads_per_device": self.store.limits.uploads_per_device}}

    def _api_send(self, qs, dev):
        total = self._length(required=True)
        tid = self.store.begin_upload(dev["id"], qs.get("to", [""])[0],
                                      qs.get("domain", [""])[0],
                                      sanitize_filename(qs.get("name", [""])[0]), total)
        self.body_consumed = True  # 上传开始读取后不再兜底读取
        complete = False
        started = time.monotonic()
        deadline = started + self.store.limits.upload_timeout
        try:
            path = self.store.file_path(tid, True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb", buffering=0) as file:
                remaining = total
                while remaining:
                    available_time = deadline - time.monotonic()
                    if available_time <= 0:
                        raise APIError(408, "上传超过最长允许时间")
                    self.connection.settimeout(min(self.store.limits.io_timeout, available_time))
                    try:
                        chunk = self.rfile.read1(min(CHUNK_SIZE, remaining))
                    except socket.timeout:
                        raise APIError(408, "上传读取超时")
                    if not chunk:
                        raise APIError(400, "文件内容不完整，未投递")
                    if self.server.stop_event.is_set():
                        raise APIError(503, "服务正在停止")
                    self.store.write_chunk(tid, file, chunk)
                    remaining -= len(chunk)
                file.flush()
                os.fsync(file.fileno())
            result = self.store.finish_upload(tid)
            complete = True
            self._json(result)
            to = qs.get("to", [""])[0]
            receiver = self.store.all_devices() if to else []
            target = next((d["name"] for d in receiver if d["id"] == to), "?") if to else "房间 #" + dev["domain"]
            log_transfer("上传", result["filename"], result["size"], "%s → %s" % (dev["name"], target),
                         time.monotonic() - started)
        except OSError:
            raise APIError(507, "文件写入失败，未投递")
        finally:
            if not complete:
                self.store.cancel_upload(tid)

    def _api_download(self, tid, dev):
        row, file = self.store.acquire_download(tid, dev["id"])
        try:
            fallback = row["filename"].encode("ascii", "replace").decode("ascii").replace('"', "_") or "download"
            disposition = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (fallback, urllib.parse.quote(row["filename"]))
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(row["size"]))
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            started = time.monotonic()
            # sendfile 走内核零拷贝；不支持的平台（如 Windows）自动退回普通 send。
            self.connection.sendfile(file)
            log_transfer("下载", row["filename"], row["size"], "→ " + dev["name"], time.monotonic() - started)
        finally:
            file.close()
            self.store.release_download(tid)


# ---------------------------------------------------------------------------
# 内嵌前端
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>局域网传文件</title>
<style>
:root {
  --bg: #F2F2F7;
  --card: #FFFFFF;
  --separator: #E5E5EA;
  --text: #1C1C1E;
  --secondary: #8E8E93;
  --header: #6D6D72;
  --accent: #007AFF;
  --accent-soft: rgba(0, 122, 255, 0.10);
  --purple: #AF52DE;
  --green: #34C759;
  --red: #FF3B30;
  --orange: #FF9500;
  --fill: rgba(118, 118, 128, 0.12);
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 16px rgba(0,0,0,.04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000;
    --card: #1C1C1E;
    --separator: #38383A;
    --text: #F2F2F7;
    --secondary: #8E8E93;
    --header: #98989F;
    --accent: #0A84FF;
    --accent-soft: rgba(10, 132, 255, 0.18);
    --purple: #BF5AF2;
    --green: #30D158;
    --red: #FF453A;
    --orange: #FF9F0A;
    --fill: rgba(118, 118, 128, 0.24);
    --shadow: none;
  }
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
.app { max-width: 1120px; margin: 0 auto; padding: 16px 16px 32px; }

/* 顶栏 */
.topbar { display: flex; align-items: center; gap: 12px; padding: 4px 4px 14px; flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.logo { width: 36px; height: 36px; border-radius: 10px; flex: none; background: linear-gradient(135deg, #34AADC, #007AFF); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 18px; }
.brand h1 { font-size: 22px; font-weight: 700; margin: 0; white-space: nowrap; }
.status-pill { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 6px 12px; border-radius: 999px; background: var(--card); box-shadow: var(--shadow); font-size: 13px; color: var(--secondary); }
.status-pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--orange); flex: none; }
.status-pill.ok .dot { background: var(--green); }
.status-pill.err { color: var(--red); }
.status-pill.err .dot { background: var(--red); }
#connection-status { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.notice { font-size: 13px; color: var(--red); background: var(--card); border-radius: 12px; padding: 10px 14px; margin: 0 0 14px; box-shadow: var(--shadow); }

/* 两栏 */
.layout { display: grid; gap: 20px; grid-template-columns: minmax(0, 1fr); align-items: start; }
@media (min-width: 900px) { .layout { grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); } }
.col { display: grid; gap: 20px; min-width: 0; }

/* 卡片与区块 */
.card { background: var(--card); border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 6px; margin: 0 0 8px; min-height: 22px; }
.section-head h2 { font-size: 13px; font-weight: 600; color: var(--header); margin: 0; letter-spacing: .3px; }
.section-head .hint { font-size: 12px; color: var(--secondary); text-align: right; }
.section-head.collapsible { cursor: pointer; user-select: none; }
.caret { display: inline-block; width: 14px; color: var(--secondary); }
.dev-del { color: var(--red); font-size: 13px; padding: 6px 4px; flex: none; }

/* 我的身份 + 房间 */
.me-row { display: flex; align-items: center; gap: 12px; padding: 14px 16px; }
.me-field { flex: 1; min-width: 0; }
.me-field label { display: block; font-size: 12px; color: var(--secondary); margin-bottom: 4px; }
.name-box { display: flex; gap: 6px; align-items: center; }
.name-box input {
  flex: 1; min-width: 0; padding: 8px 12px; font-size: 16px; font-weight: 600; border: none; border-radius: 10px;
  background: var(--fill); color: var(--text); outline: none; font-family: inherit;
}
.name-box input:focus, .room-box input:focus { box-shadow: 0 0 0 2px var(--accent); }
.icon-btn { width: 36px; height: 36px; border-radius: 10px; background: var(--fill); font-size: 18px; flex: none; display: inline-flex; align-items: center; justify-content: center; }
.icon-btn:active { opacity: .6; }
.room-row { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-top: 0.5px solid var(--separator); flex-wrap: wrap; }
.room-box { display: flex; align-items: center; gap: 8px; }
.room-box input {
  width: 92px; padding: 7px 0; font-size: 18px; letter-spacing: 5px; text-align: center; font-family: ui-monospace, "SF Mono", Menlo, monospace;
  border: none; border-radius: 10px; background: var(--fill); color: var(--text); outline: none;
}
.domain-status { flex: 1; font-size: 13px; color: var(--secondary); min-width: 150px; }
.domain-status b { color: var(--accent); }
.pill { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; background: var(--fill); color: var(--secondary); }

/* 列表 */
.ios-list { list-style: none; margin: 0; padding: 0; }
.ios-row { display: flex; align-items: center; gap: 12px; padding: 11px 16px; min-height: 58px; }
.ios-row + .ios-row { border-top: 0.5px solid var(--separator); }
.ios-row.empty { justify-content: center; color: var(--secondary); font-size: 14px; min-height: 64px; }

/* 设备 */
.device { cursor: pointer; transition: background .15s ease; }
.device:hover { background: var(--fill); }
.device.offline { cursor: default; }
.device.offline .avatar, .device.offline .dmeta, .device.offline .check { opacity: .45; }
.device.offline:hover { background: none; }
.device.selected, .device.selected:hover { background: var(--accent-soft); }
.device.selected .dname { color: var(--accent); }
.avatar {
  position: relative; width: 40px; height: 40px; border-radius: 50%; flex: none;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 17px; font-weight: 600;
}
.avatar.lg { width: 48px; height: 48px; font-size: 20px; }
.adot { position: absolute; right: -1px; bottom: -1px; width: 12px; height: 12px; border-radius: 50%; background: var(--green); border: 2px solid var(--card); }
.adot.off { background: #C7C7CC; }
.dmeta { flex: 1; min-width: 0; }
.dname { font-size: 16px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dsub { font-size: 12px; color: var(--secondary); margin-top: 2px; }
.check { width: 22px; height: 22px; border-radius: 50%; border: 1.5px solid var(--separator); flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 13px; font-weight: 700; }
.device.selected .check { background: var(--accent); border-color: var(--accent); }

/* 发送 */
.send-card { padding: 14px; }
.send-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 2px 10px; }
.send-head h3 { margin: 0; font-size: 15px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dropzone {
  border: 1.5px dashed color-mix(in srgb, var(--accent) 45%, transparent); border-radius: 12px; padding: 22px 14px; text-align: center;
  color: var(--secondary); cursor: pointer; transition: background .15s, border-color .15s;
}
.dropzone:hover { background: var(--accent-soft); }
.dropzone.drag { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
.dropzone.purple { border-color: color-mix(in srgb, var(--purple) 45%, transparent); }
.dropzone.purple:hover, .dropzone.purple.drag { background: color-mix(in srgb, var(--purple) 10%, transparent); border-color: var(--purple); }
.dropzone .big { font-size: 26px; line-height: 1; }
.dropzone b { display: block; color: var(--text); margin: 8px 0 3px; font-size: 15px; font-weight: 600; }
.dropzone small { font-size: 12px; color: var(--secondary); }
.send-hint { padding: 22px 16px; text-align: center; color: var(--secondary); font-size: 14px; line-height: 1.6; }

/* 上传记录 */
.sends { list-style: none; margin: 0; padding: 0; }
.sends:empty { display: none; }
.sends + .sends:not(:empty) { border-top: 0.5px solid var(--separator); }
.send-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 10px; align-items: center; padding: 11px 16px; }
.send-item + .send-item { border-top: 0.5px solid var(--separator); }
.send-item .nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 500; }
.send-item .st { font-size: 12px; color: var(--secondary); text-align: right; white-space: nowrap; }
.send-item .st.ok { color: var(--green); }
.send-item .st.err { color: var(--red); white-space: normal; overflow-wrap: anywhere; }
.bar { grid-column: 1 / -1; height: 4px; border-radius: 2px; background: var(--fill); overflow: hidden; }
.bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .15s linear; }
.send-item.done .bar > i { background: var(--green); }
.send-item.failed .bar > i { background: var(--red); }
.send-item .sp { grid-column: 1 / -1; font-size: 12px; color: var(--secondary); font-variant-numeric: tabular-nums; }
.upload-total { font-variant-numeric: tabular-nums; }

/* 文件 */
.file-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 10px 16px; border-bottom: 0.5px solid var(--separator); flex-wrap: wrap; }
.file-toolbar label { display: flex; align-items: center; gap: 8px; font-size: 14px; cursor: pointer; }
.file-select, #select-all-files { width: 18px; height: 18px; flex: none; accent-color: var(--accent); cursor: pointer; margin: 0; }
.sub-head { padding: 10px 16px 4px; font-size: 12px; font-weight: 600; color: var(--header); display: flex; justify-content: space-between; gap: 8px; }
.sub-head span { font-weight: 400; color: var(--secondary); }
.sub-head.split { border-top: 0.5px solid var(--separator); margin-top: 4px; }
.ficon { width: 40px; height: 40px; border-radius: 10px; flex: none; background: var(--accent-soft); display: flex; align-items: center; justify-content: center; font-size: 19px; }
.fmeta { flex: 1; min-width: 0; }
.fname { font-size: 15px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fsub { font-size: 12px; color: var(--secondary); margin-top: 3px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.tag { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; }
.tag.direct { background: var(--accent-soft); color: var(--accent); }
.tag.domain { background: color-mix(in srgb, var(--purple) 14%, transparent); color: var(--purple); }
.tag.pending { background: color-mix(in srgb, var(--orange) 16%, transparent); color: var(--orange); }
.file-actions { display: flex; gap: 8px; align-items: center; flex: none; }
.file-result { font-size: 13px; color: var(--secondary); margin: 8px 6px 0; line-height: 1.5; overflow-wrap: anywhere; }
.file-result:empty { display: none; }
.file-note, .file-error { font-size: 12px; margin-top: 4px; line-height: 1.4; overflow-wrap: anywhere; }
.file-note { color: var(--secondary); }
.file-error { color: var(--red); }

/* 按钮 */
button { cursor: pointer; font-family: inherit; border: none; background: none; font-size: 15px; color: var(--accent); padding: 0; }
button:disabled { opacity: .45; cursor: not-allowed; }
.btn { padding: 7px 14px; border-radius: 10px; background: var(--accent); color: #fff; font-weight: 600; font-size: 14px; white-space: nowrap; }
.btn:active { opacity: .6; }
.btn.ghost { background: var(--fill); color: var(--accent); }
.btn.destructive { background: transparent; color: var(--red); padding: 7px 4px; }
.link-btn { color: var(--accent); font-size: 14px; flex: none; }
.link-btn.destructive { color: var(--red); }
a.dl { text-decoration: none; padding: 6px 12px; border-radius: 999px; background: var(--accent-soft); font-weight: 600; font-size: 13px; }
a.dl:active { opacity: .6; }
.file-actions .link-btn.destructive { font-size: 13px; padding: 6px 4px; }

.footer { font-size: 12px; color: var(--secondary); text-align: center; margin-top: 24px; line-height: 1.6; }

@media (max-width: 480px) {
  .app { padding: 12px 12px 28px; }
  .brand h1 { font-size: 20px; }
  .ios-row { padding: 10px 12px; gap: 10px; }
  .file-item .ficon { width: 34px; height: 34px; font-size: 17px; }
  .file-item .fname { font-size: 14px; }
  .file-actions { flex-direction: column; gap: 4px; }
  .file-toolbar, .me-row, .room-row, .send-item, .sub-head { padding-left: 12px; padding-right: 12px; }
}
#broadcast-hint { font-size: 12px; color: var(--secondary); }
.small-link { font-size: 13px; }

/* 访问密码 */
.login { position: fixed; inset: 0; z-index: 10; background: var(--bg); display: flex; align-items: center; justify-content: center; padding: 16px; }
.login-card { width: 100%; max-width: 340px; background: var(--card); border-radius: 16px; box-shadow: var(--shadow); padding: 24px 20px; text-align: center; }
.login-card .lock { font-size: 34px; }
.login-card h2 { font-size: 18px; margin: 10px 0 16px; }
.login-card input { width: 100%; padding: 10px 12px; font-size: 16px; border: none; border-radius: 10px; background: var(--fill); color: var(--text); outline: none; font-family: inherit; }
.login-card input:focus { box-shadow: 0 0 0 2px var(--accent); }
.login-card .btn { width: 100%; margin-top: 12px; padding: 10px; font-size: 15px; }
.login-error { color: var(--red); font-size: 13px; margin-top: 10px; min-height: 18px; }
.hidden { display: none !important; }
</style>
</head>
<body>
<div class="login hidden" id="login" role="dialog" aria-labelledby="login-title">
  <form class="login-card" id="login-form">
    <div class="lock">🔒</div>
    <h2 id="login-title">输入访问密码</h2>
    <input type="password" id="access-password" autocomplete="current-password" aria-label="访问密码">
    <button class="btn" type="submit">进入</button>
    <div class="login-error" id="login-error" role="alert"></div>
  </form>
</div>
<div class="app">
  <header class="topbar">
    <div class="brand"><div class="logo">⇄</div><h1>局域网传文件</h1></div>
    <div class="status-pill" id="status-pill"><span class="dot"></span><span id="connection-status" role="status">正在连接…</span></div>
  </header>
  <div id="identity-notice" role="alert" class="notice hidden"></div>

  <div class="layout">
    <div class="col">
      <section>
        <div class="section-head"><h2>我的设备</h2></div>
        <div class="card">
          <div class="me-row">
            <div class="avatar lg" id="me-avatar">?</div>
            <div class="me-field">
              <label for="my-name">我的名字</label>
              <div class="name-box">
                <input id="my-name" maxlength="80" placeholder="我的名字" autocomplete="off">
                <button class="icon-btn" id="name-dice" title="换一个随机名字" aria-label="换一个随机名字">🎲</button>
              </div>
            </div>
          </div>
          <div class="room-row">
            <div class="room-box">
              <input id="domain-input" maxlength="4" inputmode="numeric" pattern="[0-9]*" placeholder="房间号" aria-label="4 位房间号">
              <button class="btn" id="domain-join">加入</button>
              <button class="btn ghost hidden" id="domain-leave">退出</button>
            </div>
            <div class="domain-status" id="domain-status"></div>
          </div>
        </div>
      </section>

      <section>
        <div class="section-head collapsible" id="devices-head" role="button" tabindex="0" aria-expanded="true">
          <h2><span class="caret" id="devices-caret">▾</span><span id="devices-title">在线设备</span></h2><span class="hint" id="devices-hint"></span>
        </div>
        <div class="card" id="devices-card"><ul class="ios-list" id="device-list"><li class="ios-row empty">正在发现设备…</li></ul></div>
      </section>

      <section>
        <div class="section-head"><h2>发送</h2></div>
        <div class="card">
          <div class="send-hint" id="send-hint">选择设备后发送文件</div>
          <div class="send-card hidden" id="session-panel">
            <div class="send-head">
              <h3 id="peer-name">发给 …</h3>
              <button class="link-btn destructive" id="deselect-btn">取消选择</button>
            </div>
            <div class="dropzone" id="dropzone">
              <div class="big">📤</div>
              <b>拖拽文件到这里，或点击选择</b>
            </div>
            <input type="file" id="file-input" multiple class="hidden">
          </div>
          <div class="send-card hidden" id="broadcast-panel">
            <div class="send-head"><h3>发给房间内所有人</h3><span class="hint" id="broadcast-hint"></span></div>
            <div class="dropzone purple" id="bdropzone">
              <div class="big">📢</div>
              <b>拖拽文件到这里，或点击选择</b>
            </div>
            <input type="file" id="bfile-input" multiple class="hidden">
          </div>
        </div>
      </section>

      <section class="hidden" id="upload-progress">
        <div class="section-head">
          <h2>上传记录 <span class="upload-total" id="upload-total"></span></h2>
          <button class="link-btn small-link" id="clear-sends">清除已完成</button>
        </div>
        <div class="card">
          <ul class="sends" id="send-list"></ul>
          <ul class="sends" id="bsend-list"></ul>
        </div>
      </section>
    </div>

    <div class="col">
      <section aria-label="文件删除管理">
        <div class="section-head"><h2>文件</h2></div>
        <div class="card">
          <div class="file-toolbar">
            <label><input type="checkbox" id="select-all-files" aria-label="选择全部可删除文件">全选可删除</label>
            <button class="btn destructive" id="delete-selected" disabled>删除所选（0）</button>
          </div>
          <div class="sub-head">收到的文件</div>
          <ul class="ios-list" id="inbox-list"><li class="ios-row empty">暂无文件</li></ul>
          <div class="sub-head split">我发出的房间共享</div>
          <ul class="ios-list" id="outbox-list"><li class="ios-row empty">暂无自己发出的房间共享</li></ul>
        </div>
        <div id="delete-result" role="status" aria-live="polite" class="file-result"></div>
      </section>
    </div>
  </div>

  <div class="footer">
    <div id="limits-summary"></div>
  </div>
</div>

<script>
(function () {
  localStorage.removeItem("lanfiles_device_id");
  var LS_DOMAIN = "lanfiles_domain";
  var LS_NAME = "lanfiles_name";
  var NAME_WORDS = __NAME_WORDS__;
  var me = { id: "", name: "", domain: localStorage.getItem(LS_DOMAIN) || "" };
  var selectedId = null;
  var devices = [];
  var inbox = [];
  var outbox = [];
  var selectedFiles = new Set();
  var deleteErrors = new Map();
  var deleteBusy = false;
  var identityGeneration = 0;
  var fileRevision = 0;

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
  var fmtTime = function (s) {
    s = Math.max(0, Math.round(s));
    if (s >= 3600) return Math.floor(s / 3600) + " 小时 " + Math.floor(s % 3600 / 60) + " 分";
    if (s >= 60) return Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
    return s + " 秒";
  };
  var AV_COLORS = ["#6366f1", "#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#14b8a6"];
  function avatarColor(name) {
    var h = 0, i;
    for (i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return AV_COLORS[h % AV_COLORS.length];
  }
  function fileIcon(name) {
    var ext = (String(name).split(".").pop() || "").toLowerCase();
    if (/^(png|jpe?g|gif|webp|heic|bmp|svg|tiff?)$/.test(ext)) return "🖼️";
    if (/^(mp4|mov|mkv|avi|webm|m4v|flv)$/.test(ext)) return "🎬";
    if (/^(mp3|wav|flac|aac|m4a|ogg)$/.test(ext)) return "🎵";
    if (/^(zip|rar|7z|tar|gz|bz2|xz|dmg|iso)$/.test(ext)) return "🗜️";
    if (ext === "pdf") return "📕";
    if (/^(xlsx?|csv|numbers)$/.test(ext)) return "📊";
    if (/^(pptx?|key)$/.test(ext)) return "📽️";
    if (/^(apk|exe|msi|pkg|app)$/.test(ext)) return "📦";
    return "📄";
  }
  // ---- 访问密码：纯 JS 的 HMAC-SHA256（HTTP 页面不能用 WebCrypto），只发送对一次性随机数的签名 ----
  var SHA_K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  function sha256(bytes) {
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var len = bytes.length, total = ((len + 9 + 63) >> 6) << 6;
    var m = new Uint8Array(total); m.set(bytes); m[len] = 0x80;
    var dv = new DataView(m.buffer), w = new Uint32Array(64), i;
    dv.setUint32(total - 4, (len * 8) >>> 0); dv.setUint32(total - 8, Math.floor(len / 536870912));
    for (var o = 0; o < total; o += 64) {
      for (i = 0; i < 16; i++) w[i] = dv.getUint32(o + i * 4);
      for (i = 16; i < 64; i++) {
        var x = w[i - 15], y = w[i - 2];
        var s0 = (x >>> 7 | x << 25) ^ (x >>> 18 | x << 14) ^ (x >>> 3);
        var s1 = (y >>> 17 | y << 15) ^ (y >>> 19 | y << 13) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (i = 0; i < 64; i++) {
        var t1 = (h + ((e >>> 6 | e << 26) ^ (e >>> 11 | e << 21) ^ (e >>> 25 | e << 7)) + ((e & f) ^ (~e & g)) + SHA_K[i] + w[i]) | 0;
        var t2 = (((a >>> 2 | a << 30) ^ (a >>> 13 | a << 19) ^ (a >>> 22 | a << 10)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0; H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
    }
    var out = new Uint8Array(32), odv = new DataView(out.buffer);
    for (i = 0; i < 8; i++) odv.setUint32(i * 4, H[i] >>> 0);
    return out;
  }
  function hmacHex(key, message) {
    var enc = new TextEncoder(), k = enc.encode(key), i;
    if (k.length > 64) k = sha256(k);
    var block = new Uint8Array(64); block.set(k);
    var msg = enc.encode(message), inner = new Uint8Array(64 + msg.length), outer = new Uint8Array(96);
    for (i = 0; i < 64; i++) { inner[i] = block[i] ^ 0x36; outer[i] = block[i] ^ 0x5c; }
    inner.set(msg, 64); outer.set(sha256(inner), 64);
    return Array.from(sha256(outer), function (v) { return (v < 16 ? "0" : "") + v.toString(16); }).join("");
  }
  // 扫码链接把密码放在 # 后面：不会发给服务器，读取后立即从地址栏清除。
  var accessKey = "";
  (function () {
    var match = /(?:^#|&)k=([^&]*)/.exec(location.hash);
    if (match) {
      try { accessKey = decodeURIComponent(match[1]); } catch (_) {}
      history.replaceState(null, "", location.pathname + location.search);
    }
  })();
  var waitingForPassword = false;
  function showLogin(message) {
    waitingForPassword = true;
    $("login").classList.remove("hidden");
    $("login-error").textContent = message || "";
    $("access-password").value = "";
    setTimeout(function () { $("access-password").focus(); }, 0);
  }
  $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var value = $("access-password").value;
    if (!value) { $("login-error").textContent = "请输入密码"; return; }
    accessKey = value; waitingForPassword = false;
    $("login-error").textContent = "正在验证…";
    schedulePoll(0);
  });

  function savedName() {
    try { var n = localStorage.getItem(LS_NAME) || ""; return n.length <= 80 ? n : ""; } catch (_) { return ""; }
  }
  function saveName(n) { try { localStorage.setItem(LS_NAME, n); } catch (_) {} }
  function randomName() {
    var pick = function (list) { return list[Math.floor(Math.random() * list.length)]; };
    return pick(NAME_WORDS[0]) + pick(NAME_WORDS[1]);
  }
  function renderMe(name) {
    var av = $("me-avatar");
    av.textContent = (name || "?").charAt(0);
    av.style.background = name ? avatarColor(name) : "var(--secondary)";
  }

  var registration = null;
  var retryDelay = 1500;
  var pollTimer = null;
  var queue = [];
  var activeUploads = 0;
  var connected = false;
  var identityWasLost = false;
  var nameSyncAt = 0;

  function status(message, error) {
    $("connection-status").textContent = message;
    $("status-pill").className = "status-pill" + (error ? " err" : (connected ? " ok" : ""));
  }
  function api(path, options) {
    return fetch(path, Object.assign({ credentials: "same-origin" }, options || {}))
      .then(async function (r) {
        var data;
        try { data = await r.json(); } catch (_) { throw new Error("服务响应无效"); }
        if (!r.ok) { var e = new Error(data.error || "请求失败"); e.status = r.status; e.code = data.code; throw e; }
        return data;
      });
  }
  function showIdentityNotice() {
    $("identity-notice").textContent = "已建立新身份；旧身份的私发文件无法自动恢复。";
    $("identity-notice").classList.remove("hidden");
  }
  function applyProfile(d) {
    if (me.id && me.id !== d.device_id) {
      identityGeneration++; fileRevision++; selectedFiles.clear(); deleteErrors.clear();
      inbox = []; outbox = [];
      showIdentityNotice();
    }
    if ((me.id && me.id !== d.device_id) || me.domain !== (d.domain || "")) {
      selectedId = null;
      $("session-panel").classList.add("hidden");
    }
    me.id = d.device_id; me.name = d.name; me.domain = d.domain || ""; me.admin = !!d.admin;
    localStorage.setItem(LS_DOMAIN, me.domain);
    // 名字以本浏览器保存的为准：新身份（Cookie 丢失、其他标签页重建）会被改回固定名字。
    var fixed = savedName();
    if (!fixed) saveName(me.name);
    else if (fixed !== me.name && Date.now() - nameSyncAt > 10000) {
      nameSyncAt = Date.now();
      updateProfile({ name: fixed });
    }
    if (document.activeElement !== $("my-name")) { $("my-name").value = fixed || me.name; renderMe(fixed || me.name); }
    renderDomain();
    if (d.limits) {
      me.maxFileSize = d.limits.max_file_size;
      me.uploadSlots = Math.max(1, d.limits.uploads_per_device || 1);
      $("limits-summary").textContent = "单文件上限 " + fmtSize(d.limits.max_file_size) +
        " · 文件保留 1 小时";
    }
  }
  function clearStale() {
    identityWasLost = identityWasLost || !!me.id;
    connected = false;
    identityGeneration++; fileRevision++; selectedFiles.clear(); deleteErrors.clear();
    me.id = ""; devices = []; inbox = []; outbox = []; selectedId = null;
    $("session-panel").classList.add("hidden");
    renderDevices(); renderInbox();
  }
  function register(profile) {
    if (registration) {
      // 已有注册在途时，资料修改排在其后，避免被合并丢失。
      if (!profile) return registration;
      return registration.catch(function () {}).then(function () { return register(profile); });
    }
    var hadIdentity = !!me.id;
    var body = profile || { domain: me.domain };
    if (!profile && savedName()) body.name = savedName();
    var generation = identityGeneration;
    var send = function () {
      return api("/api/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    };
    registration = send().catch(async function (e) {
      if (e.code !== "password_required") throw e;
      if (!accessKey) { showLogin(); throw e; }
      var challenge = await api("/api/auth/challenge");
      body.auth = { nonce: challenge.nonce, proof: hmacHex(accessKey, challenge.nonce) };
      try { return await send(); } catch (e2) {
        if (e2.code === "bad_password" || e2.status === 429) { accessKey = ""; showLogin(e2.message); }
        throw e2;
      } finally { delete body.auth; }
    }).then(function (d) {
      $("login").classList.add("hidden");
      if (generation !== identityGeneration) return null;
      applyProfile(d);
      if (d.new_identity && (hadIdentity || identityWasLost)) showIdentityNotice();
      identityWasLost = false;
      return d;
    }).finally(function () { registration = null; });
    return registration;
  }
  function schedulePoll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, delay);
  }
  async function poll() {
    clearTimeout(pollTimer);
    var requestGeneration = identityGeneration;
    try {
      if (!me.id) { await register(); requestGeneration = identityGeneration; }
      // Cookie 被同浏览器的标签页共享，服务器档案是身份与房间的唯一真值。
      var profile = await api("/api/session");
      if (requestGeneration !== identityGeneration) return schedulePoll(0);
      applyProfile(profile);
      requestGeneration = identityGeneration;
      var revision = fileRevision, generation = identityGeneration;
      var lists = await Promise.all([api("/api/devices"), api("/api/files")]);
      if (generation !== identityGeneration) return schedulePoll(0);
      devices = lists[0];
      if (revision === fileRevision) { inbox = lists[1].inbox; outbox = lists[1].outbox; }
      renderDevices(); renderInbox(); retryDelay = 1500; connected = true;
      drainQueue();
      status("已连接", false);
    } catch (e) {
      if (requestGeneration !== identityGeneration) return schedulePoll(0);
      connected = false;
      if (waitingForPassword) { status("需要访问密码", true); return; }  // 等用户输入后再继续
      if (e.status === 401) {
        clearStale();
        status("身份已失效，正在重新连接…", true);
        try {
          await register();
          status("已建立新身份；旧身份文件无法自动恢复。", true);
        } catch (registrationError) {
          if (waitingForPassword) { status("需要访问密码", true); return; }
          status(registrationError.message + "，稍后重试", true);
        }
      } else {
        status("连接中断：" + e.message + "，正在重试…", true);
      }
      retryDelay = Math.min(retryDelay * 2, 15000);
    }
    schedulePoll(retryDelay);
  }

  function renderDomain() {
    if (document.activeElement !== $("domain-input")) $("domain-input").value = me.domain;
    var st = $("domain-status");
    var leave = $("domain-leave");
    var join = $("domain-join");
    var bp = $("broadcast-panel");
    if (me.domain) {
      st.innerHTML = '已加入 <b>#' + esc(me.domain) + '</b>';
      join.textContent = "更换";
      leave.classList.remove("hidden");
      bp.classList.remove("hidden");
      $("broadcast-hint").textContent = "房间 #" + me.domain;
    } else {
      st.textContent = "未加入房间";
      join.textContent = "加入";
      leave.classList.add("hidden");
      bp.classList.add("hidden");
    }
    renderSendHint();
  }
  function renderSendHint() {
    var idle = !selectedId && !me.domain;
    $("send-hint").classList.toggle("hidden", !idle);
  }

  function renderDevices() {
    var ul = $("device-list");
    var title = $("devices-title");
    var hint = $("devices-hint");
    ul.innerHTML = "";
    var online = devices.filter(function (d) { return d.online; }).length;
    title.textContent = me.domain ? "房间内设备" : "在线设备";
    hint.textContent = online + " 台在线";
    if (!devices.length) {
      ul.innerHTML = '<li class="ios-row empty">暂无其他设备</li>';
      renderSendHint();
      return;
    }
    devices.slice().sort(function (x, y) { return (y.online - x.online) || x.name.localeCompare(y.name); }).forEach(function (d) {
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
      ds.textContent = d.online ? (d.id === selectedId ? "已选择" : "在线") : "离线";
      meta.appendChild(dn); meta.appendChild(ds);
      var check = document.createElement("span");
      check.className = "check";
      check.textContent = d.id === selectedId ? "✓" : "";
      li.appendChild(av); li.appendChild(meta);
      if (me.admin) {
        var del = document.createElement("button");
        del.className = "dev-del"; del.textContent = "删除"; del.setAttribute("aria-label", "删除设备 " + d.name);
        del.onclick = function (e) { e.stopPropagation(); deleteDevice(d); };
        li.appendChild(del);
      }
      li.appendChild(check);
      if (d.online) li.onclick = function () { selectDevice(d.id, d.name); };
      ul.appendChild(li);
    });
    if (selectedId) {
      var still = devices.some(function (d) { return d.id === selectedId && d.online; });
      if (!still) closeSession();
    }
    renderSendHint();
  }

  function deleteDevice(d) {
    var note = d.online ? "\n该设备在线，删除后会以新身份重新连接。" : "";
    if (!window.confirm("删除设备“" + d.name + "”？发给它的未下载私发文件也会删除。" + note)) return;
    api("/api/admin/delete-device/" + encodeURIComponent(d.id), { method: "POST" }).then(function () {
      if (selectedId === d.id) closeSession();
      schedulePoll(0);
    }).catch(function (e) { status("删除设备失败：" + e.message, true); });
  }

  function deletableFiles() {
    return inbox.concat(outbox).filter(function (t) { return t.can_delete && t.state === "ready"; });
  }
  function renderInbox() {
    var eligible = new Set(deletableFiles().map(function (t) { return t.transfer_id; }));
    selectedFiles.forEach(function (id) { if (!eligible.has(id)) selectedFiles.delete(id); });
    renderFileGroup("inbox-list", inbox, false);
    renderFileGroup("outbox-list", outbox, true);
    var all = $("select-all-files");
    all.checked = eligible.size > 0 && selectedFiles.size === eligible.size;
    all.indeterminate = selectedFiles.size > 0 && selectedFiles.size < eligible.size;
    all.disabled = deleteBusy || !eligible.size;
    $("delete-selected").disabled = deleteBusy || !selectedFiles.size;
    $("delete-selected").textContent = deleteBusy ? "正在删除…" : "删除所选（" + selectedFiles.size + "）";
  }
  function renderFileGroup(listId, items, ownBroadcast) {
    var ul = $(listId);
    ul.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("li"); empty.className = "ios-row empty";
      empty.textContent = ownBroadcast ? "暂无自己发出的房间共享" : "暂无文件";
      ul.appendChild(empty); return;
    }
    items.slice().reverse().forEach(function (t) {
      var pending = t.state === "deleting";
      var li = document.createElement("li");
      li.className = "ios-row file-item" + (ownBroadcast ? " outbox-item" : " inbox-item");
      li.dataset.transferId = t.transfer_id;
      if (t.can_delete) {
        var checkbox = document.createElement("input"); checkbox.type = "checkbox";
        checkbox.className = "file-select"; checkbox.setAttribute("aria-label", "选择 " + t.filename);
        checkbox.checked = selectedFiles.has(t.transfer_id); checkbox.disabled = deleteBusy || pending;
        checkbox.onchange = function () {
          if (checkbox.checked) selectedFiles.add(t.transfer_id); else selectedFiles.delete(t.transfer_id);
          renderInbox();
        };
        li.appendChild(checkbox);
      }
      var icon = document.createElement("div"); icon.className = "ficon"; icon.textContent = fileIcon(t.filename);
      var meta = document.createElement("div"); meta.className = "fmeta";
      var name = document.createElement("div"); name.className = "fname"; name.textContent = t.filename; name.title = t.filename;
      var sub = document.createElement("div"); sub.className = "fsub";
      var tag = document.createElement("span");
      tag.className = "tag " + (pending ? "pending" : (t.kind === "domain" ? "domain" : "direct"));
      tag.textContent = pending ? "等待删除" : (t.kind === "domain" ? "房间共享" : "私发");
      var info = document.createElement("span");
      info.textContent = (ownBroadcast ? "房间 #" + t.domain : "来自 " + t.from_name) + " · " + fmtSize(t.size);
      sub.appendChild(tag); sub.appendChild(info);
      meta.appendChild(name); meta.appendChild(sub);
      if (pending) {
        var waiting = document.createElement("div"); waiting.className = "file-note";
        waiting.textContent = "等待下载结束或重试磁盘删除"; meta.appendChild(waiting);
      }
      if (deleteErrors.has(t.transfer_id)) {
        var error = document.createElement("div"); error.className = "file-error";
        error.textContent = deleteErrors.get(t.transfer_id); meta.appendChild(error);
      }
      var actions = document.createElement("div"); actions.className = "file-actions";
      if (!ownBroadcast && !pending) {
        var a = document.createElement("a"); a.className = "dl link-btn";
        a.href = "/api/download/" + encodeURIComponent(t.transfer_id);
        a.setAttribute("download", t.filename); a.textContent = "下载"; actions.appendChild(a);
      }
      if (t.can_delete) {
        var remove = document.createElement("button"); remove.className = "link-btn destructive";
        remove.textContent = "删除"; remove.disabled = deleteBusy || pending;
        remove.onclick = function () { deleteFiles([t]); }; actions.appendChild(remove);
      }
      li.appendChild(icon); li.appendChild(meta); li.appendChild(actions); ul.appendChild(li);
    });
  }
  function updateLocalFile(id, pending) {
    fileRevision++;
    [inbox, outbox].forEach(function (list) {
      for (var i = list.length - 1; i >= 0; i--) {
        if (list[i].transfer_id === id) {
          if (pending) list[i] = Object.assign({}, list[i], { state: "deleting" });
          else list.splice(i, 1);
        }
      }
    });
    selectedFiles.delete(id); deleteErrors.delete(id);
  }
  async function deleteFiles(items) {
    if (deleteBusy || !items.length) return;
    var targets = items.filter(function (t) { return t.can_delete && t.state === "ready"; })
      .map(function (t) { return { transfer_id: t.transfer_id, filename: t.filename }; });
    if (!targets.length) return;
    var message = targets.length === 1 ? "删除中转文件“" + targets[0].filename + "”？" : "删除所选的 " + targets.length + " 个中转文件？";
    if (!window.confirm(message + "\n删除中转文件不会影响已下载的副本。")) return;
    var identity = me.id, generation = identityGeneration;
    var removed = 0, pending = 0, failed = 0, remaining = 0;
    deleteBusy = true;
    targets.forEach(function (t) { deleteErrors.delete(t.transfer_id); });
    $("delete-result").textContent = "正在删除 " + targets.length + " 个文件…";
    renderInbox();
    try {
      for (var i = 0; i < targets.length; i++) {
        if (identityGeneration !== generation || me.id !== identity) { remaining = targets.length - i; break; }
        var item = targets[i];
        try {
          var result = await api("/api/ack/" + encodeURIComponent(item.transfer_id) + "?device_id=" + encodeURIComponent(identity), { method: "POST" });
          if (result.pending) pending++; else removed++;
          if (identityGeneration === generation) updateLocalFile(item.transfer_id, result.pending);
        } catch (e) {
          if (e.status === 404) {
            removed++;
            if (identityGeneration === generation) updateLocalFile(item.transfer_id, false);
          } else {
            failed++;
            if (identityGeneration === generation) {
              deleteErrors.set(item.transfer_id, e.message || "删除失败，请重试");
              selectedFiles.add(item.transfer_id);
            }
            if (identityGeneration !== generation || me.id !== identity) {
              remaining = targets.length - i - 1; break;
            }
            if (e.status === 401 || e.code === "identity_mismatch") {
              remaining = targets.length - i - 1;
              clearStale(); schedulePoll(0); break;
            }
          }
        }
        renderInbox();
      }
    } finally {
      deleteBusy = false;
      $("delete-result").textContent = "已删除 " + removed + " · 等待删除 " + pending + " · 失败 " + failed +
        (remaining ? " · 未删除 " + remaining + "（身份已失效或变化，请重新选择）" : "");
      renderInbox(); schedulePoll(0);
    }
  }
  $("select-all-files").onchange = function () {
    if (deleteBusy) return;
    selectedFiles.clear();
    if (this.checked) deletableFiles().forEach(function (t) { selectedFiles.add(t.transfer_id); });
    renderInbox();
  };
  $("delete-selected").onclick = function () {
    deleteFiles(deletableFiles().filter(function (t) { return selectedFiles.has(t.transfer_id); }));
  };

  function selectDevice(id, name) {
    if (id === selectedId) { closeSession(); return; }
    selectedId = id;
    $("peer-name").textContent = "发给 " + name;
    $("session-panel").classList.remove("hidden");
    renderDevices();
  }
  function closeSession() {
    selectedId = null;
    $("session-panel").classList.add("hidden");
    renderDevices();
  }

  // ---- 上传：按服务器给的每设备并发数并行，显示速度与剩余时间 ----
  var uploading = new Set();
  function renderUploadTotal() {
    var speed = 0;
    uploading.forEach(function (u) { speed += u.speed || 0; });
    $("upload-total").textContent = uploading.size ? "· " + uploading.size + " 个进行中 · " + fmtSize(speed) + "/s" : (queue.length ? "· " + queue.length + " 个排队" : "");
  }
  function sendFiles(files, listId, urlFn) {
    if (files.length) $("upload-progress").classList.remove("hidden");
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      var li = document.createElement("li");
      li.className = "send-item";
      li.innerHTML = '<span class="nm"></span><span class="st">排队中</span><span class="bar"><i></i></span><span class="sp"></span>';
      li.querySelector(".nm").textContent = file.name;
      li.querySelector(".sp").textContent = fmtSize(file.size);
      $(listId).appendChild(li);
      // 入队时固定目标和房间，之后切换设备不会改变这些文件的去向。
      queue.push({ file: file, url: urlFn(file), li: li });
    }
    renderUploadTotal();
    drainQueue();
  }
  function drainQueue() {
    while (connected && queue.length && activeUploads < (me.uploadSlots || 1)) {
      activeUploads++;
      sendOne(queue.shift()).then(function () { activeUploads--; renderUploadTotal(); drainQueue(); });
    }
  }
  function sendOne(item) {
    return new Promise(function (resolve) {
      var file = item.file, li = item.li;
      var fill = li.querySelector(".bar > i"), st = li.querySelector(".st"), sp = li.querySelector(".sp");
      var xhr = new XMLHttpRequest();
      var finished = false;
      var track = { speed: 0, loaded: 0, at: performance.now(), start: performance.now() };
      function finish(error) {
        if (finished) return;
        finished = true;
        uploading.delete(track);
        var secs = (performance.now() - track.start) / 1000;
        if (error) { st.textContent = "失败：" + error; st.className = "st err"; li.classList.add("failed"); li.title = error; sp.textContent = fmtSize(file.size); status(file.name + "：" + error, true); }
        else {
          fill.style.width = "100%"; st.textContent = "已上传"; st.className = "st ok"; li.classList.add("done");
          li.title = "已上传到中转，等待收件人下载";
          sp.textContent = fmtSize(file.size) + (secs > 0.5 ? " · 平均 " + fmtSize(file.size / secs) + "/s · 用时 " + fmtTime(secs) : "");
        }
        renderUploadTotal();
        resolve();
      }
      if (me.maxFileSize !== undefined && file.size > me.maxFileSize) { finish("文件超过单文件上限"); return; }
      st.textContent = "上传中";
      uploading.add(track);
      renderUploadTotal();
      xhr.open("POST", item.url); xhr.withCredentials = true;
      xhr.timeout = 6 * 3600 * 1000 + 30000;
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        var now = performance.now(), dt = (now - track.at) / 1000;
        if (dt >= 0.4) {
          var inst = (e.loaded - track.loaded) / dt;
          track.speed = track.speed ? track.speed * 0.6 + inst * 0.4 : inst;
          track.loaded = e.loaded; track.at = now;
          var left = track.speed > 0 ? (e.total - e.loaded) / track.speed : 0;
          sp.textContent = fmtSize(e.loaded) + " / " + fmtSize(e.total) + " · " + fmtSize(track.speed) + "/s" + (left >= 1 ? " · 剩余 " + fmtTime(left) : "");
          renderUploadTotal();
        }
        fill.style.width = (e.loaded / e.total * 100) + "%";
        st.textContent = Math.floor(e.loaded / e.total * 100) + "%";
        if (e.loaded === e.total) st.textContent = "保存中…";
      };
      xhr.onload = function () {
        var d = {};
        try { d = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300 && d.size === file.size && d.transfer_id) finish();
        else {
          finish(d.error || "上传未确认，请检查文件完整性");
          if (xhr.status === 401) { clearStale(); schedulePoll(0); }
        }
      };
      xhr.onerror = function () { connected = false; finish("连接中断，文件未确认投递，请重试"); schedulePoll(0); };
      xhr.ontimeout = function () { connected = false; finish("上传超时，请重试"); schedulePoll(0); };
      xhr.onabort = function () { finish("上传已取消"); };
      xhr.send(file);
    });
  }
  $("clear-sends").onclick = function () {
    document.querySelectorAll(".send-item.done").forEach(function (li) { li.remove(); });
    if (!document.querySelector(".send-item")) $("upload-progress").classList.add("hidden");
  };

  function directUrl(file) {
    return "/api/send?to=" + encodeURIComponent(selectedId) + "&name=" + encodeURIComponent(file.name);
  }
  function broadcastUrl(file) {
    return "/api/send?domain=" + encodeURIComponent(me.domain) + "&name=" + encodeURIComponent(file.name);
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

  function updateProfile(profile) {
    return register(profile).then(function () { schedulePoll(0); }).catch(function (e) {
      status("更新失败：" + e.message, true);
      renderDomain();
      if (e.status === 401) { clearStale(); schedulePoll(0); }
    });
  }
  function setName(v) {
    v = v.trim();
    if (!v || v.length > 80 || /[\x00-\x1f]/.test(v)) { $("my-name").value = savedName() || me.name; status("名称需为 1 至 80 个字符", true); return; }
    $("my-name").value = v;
    saveName(v); renderMe(v);
    if (v !== me.name) updateProfile({ name: v });
  }
  $("my-name").addEventListener("change", function () { setName(this.value); });
  $("my-name").addEventListener("input", function () { renderMe(this.value.trim()); });
  $("my-name").addEventListener("keydown", function (e) { if (e.key === "Enter") this.blur(); });
  $("name-dice").addEventListener("click", function () { setName(randomName()); });
  $("domain-join").addEventListener("click", function () {
    var v = $("domain-input").value.trim();
    if (v && !/^[0-9]{4}$/.test(v)) { status("房间号需为 4 位数字", true); return; }
    closeSession(); updateProfile({ domain: v });
  });
  $("domain-input").addEventListener("keydown", function (e) { if (e.key === "Enter") $("domain-join").click(); });
  $("domain-leave").addEventListener("click", function () {
    closeSession(); updateProfile({ domain: "" });
  });
  $("deselect-btn").addEventListener("click", closeSession);
  function setDevicesCollapsed(collapsed) {
    $("devices-card").classList.toggle("hidden", collapsed);
    $("devices-caret").textContent = collapsed ? "▸" : "▾";
    $("devices-head").setAttribute("aria-expanded", String(!collapsed));
    try { localStorage.setItem("lanfiles_devices_collapsed", collapsed ? "1" : ""); } catch (_) {}
  }
  $("devices-head").addEventListener("click", function () { setDevicesCollapsed(!$("devices-card").classList.contains("hidden")); });
  $("devices-head").addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.click(); } });
  try { setDevicesCollapsed(localStorage.getItem("lanfiles_devices_collapsed") === "1"); } catch (_) {}

  setupDrop("dropzone", "file-input", "send-list", directUrl, true);
  setupDrop("bdropzone", "bfile-input", "bsend-list", broadcastUrl, false);

  renderMe(savedName());
  if (savedName()) $("my-name").value = savedName();
  poll();
})();
</script>
</body>
</html>"""
# 前端“随机名字”按钮与服务端使用同一组词。
INDEX_HTML = INDEX_HTML.replace("__NAME_WORDS__", json.dumps([ADJECTIVES, NOUNS], ensure_ascii=False))


def _inline_hash(tag):
    body = re.search(r"<%s>(.*?)</%s>" % (tag, tag), INDEX_HTML, re.S).group(1)
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")


# 只允许页面自带的这一段脚本和样式；不能被嵌入其他网站的框架。
INDEX_CSP = ("default-src 'none'; script-src %s; style-src %s; connect-src 'self'; img-src 'self'; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'") % (_inline_hash("script"), _inline_hash("style"))


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _collect_ips():
    """收集本机所有 IPv4 接口地址。"""
    ips = set()
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2", "en3", "bridge0", "p2p0"):
            try:
                res = subprocess.run(["ipconfig", "getifaddr", iface],
                                     capture_output=True, text=True, timeout=1)
                ip = res.stdout.strip()
                if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    ips.add(ip)
            except Exception:
                pass
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
    parser.add_argument("--password", default=None,
                        help="访问密码（更推荐用环境变量 LANFILES_PASSWORD，避免出现在进程列表）")
    parser.add_argument("--allow-host", action="append", default=[], metavar="名称",
                        help="额外允许的访问域名（默认只接受 IP 和 localhost）")
    parser.add_argument("--exit-with-parent", action="store_true", help=argparse.SUPPRESS)
    add_limit_arguments(parser)
    args = parser.parse_args()
    try:
        limits = limits_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    password = args.password if args.password is not None else os.environ.get("LANFILES_PASSWORD", "")
    if len(password) > 128:
        parser.error("访问密码不能超过 128 个字符")

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

    try:
        httpd = create_server(args.host, args.port, SPOOL_DIR, limits, password, args.allow_host)
    except (OSError, sqlite3.Error, RuntimeError) as e:
        print("错误：无法启动状态存储或监听 %s:%s（%s）" % (args.host, args.port, e))
        print("提示：检查端口、中转目录实例锁及数据库；不同实例需指定不同 --dir，可用 --port 更换端口")
        sys.exit(1)

    legacy = [name for name in os.listdir(SPOOL_DIR) if ID_RE.fullmatch(name)]
    if legacy:
        print("提示：发现 %d 个旧版遗留文件，原位保留，请手动处理；无法自动恢复旧收件人。" % len(legacy), flush=True)
    print("容量限制：单文件 %d MiB / 中转总量 %d MiB / 同时上传 %d（每设备 %d） / 保留空闲 %d MiB" % (
        limits.max_file_size // 1024**2, limits.spool_quota // 1024**2,
        limits.max_uploads, limits.uploads_per_device, limits.min_free_space // 1024**2), flush=True)
    primary, others = local_ipv4() if args.host == "0.0.0.0" else (args.host, [])

    fw_hint = "防火墙允许本程序（Windows 勾选专用网络）"

    lines = ["=" * 60,
             "  局域网传文件已启动",
             "  在其他设备的浏览器打开：",
             "    ★ http://%s:%d" % (primary, args.port)]
    if others:
        lines.append("  备用地址：")
        for ip in others:
            lines.append("    http://%s:%d" % (ip, args.port))
    if password:
        lines.append("  已启用访问密码")
    lines += ["  中转目录：%s" % SPOOL_DIR,
              "  连不上：确认同一网段、用 http://、%s" % fw_hint,
              "=" * 60]
    print("\n".join(lines), flush=True)
    def request_stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, request_stop)
    if args.exit_with_parent:
        # 桌面版被强制退出或崩溃时，服务不会留在后台继续占用端口和目录。
        parent = os.getppid()

        def watch_parent():
            while not httpd.stop_event.wait(2):
                if os.getppid() != parent:
                    print("桌面程序已退出，服务随之停止。", flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
