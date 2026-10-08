# -*- coding: utf-8 -*-
"""transfer.py 的全链路自测：起一个临时端口服务，跑通注册/互发/收件/下载/移除/清理。"""

import http.client
import hashlib
import json
import os
import shutil
import tempfile
import socket
import sqlite3
import subprocess
import sys
import argparse
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path
from dataclasses import replace
import time
from unittest import mock
import threading
import unittest
import urllib.parse

import transfer


class TestTransfer(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="lanfiles-test-")
        self.cookies = {}
        self.server = transfer.create_server("127.0.0.1", 0, self.tmpdir,
            transfer.Limits(min_free_space=0, register_per_ip=100, register_global=1000))
        self.store = self.server.store
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":0.02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 工具 ----------
    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = dict(headers or {})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        identity = (qs.get("device_id") or qs.get("from") or [""])[0]
        if identity in self.cookies and "Cookie" not in request_headers:
            request_headers["Cookie"] = self.cookies[identity]
        conn.request(method, path, body=body, headers=request_headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def register(self, device_id=None, name=None, domain=None):
        body = {}
        if device_id:
            body["device_id"] = device_id
        if name:
            body["name"] = name
        if domain is not None:
            body["domain"] = domain
        status, headers, data = self.req(
            "POST", "/api/register", json.dumps(body).encode(),
            {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        device = json.loads(data)
        self.cookies[device["device_id"]] = headers.get("Set-Cookie", "").split(";", 1)[0]
        return device

    def devices(self, device_id):
        status, _, data = self.req("GET", "/api/devices?device_id=%s" % device_id)
        self.assertEqual(status, 200)
        return json.loads(data)

    def inbox(self, device_id):
        status, _, data = self.req("GET", "/api/inbox?device_id=%s" % device_id)
        self.assertEqual(status, 200)
        return json.loads(data)

    def send(self, from_id, to_id, filename, content):
        url = "/api/send?from=%s&to=%s&name=%s" % (
            from_id, to_id, urllib.parse.quote(filename))
        return self.req("POST", url, content)

    def send_domain(self, from_id, domain, filename, content):
        url = "/api/send?from=%s&domain=%s&name=%s" % (
            from_id, domain, urllib.parse.quote(filename))
        return self.req("POST", url, content)

    # ---------- 用例 ----------
    def files(self, device_id):
        status,_,data=self.req("GET","/api/files?device_id="+device_id)
        self.assertEqual(status,200)
        return json.loads(data)

    def test_visual_file_listing_permissions_and_room_exit(self):
        a,b,c=self.register(name="A",domain="1234"),self.register(name="B",domain="1234"),self.register(name="C",domain="1234")
        _,_,data=self.send(a["device_id"],b["device_id"],"私发.txt",b"direct")
        direct=json.loads(data)["transfer_id"]
        _,_,data=self.send_domain(a["device_id"],"1234","共享.txt",b"shared")
        shared=json.loads(data)["transfer_id"]
        received=self.files(b["device_id"])
        direct_row=next(x for x in received["inbox"] if x["transfer_id"]==direct)
        shared_row=next(x for x in received["inbox"] if x["transfer_id"]==shared)
        self.assertTrue(direct_row["can_delete"])
        self.assertFalse(shared_row["can_delete"])
        self.assertEqual(direct_row["state"],"ready")
        self.assertNotIn(direct,[x["transfer_id"] for x in self.files(c["device_id"])["inbox"]])
        self.assertEqual(self.files(a["device_id"])["outbox"][0]["transfer_id"],shared)
        self.req("POST","/api/register",b'{"domain":"9999"}',{"Cookie":self.cookies[a["device_id"]]})
        own=self.files(a["device_id"])["outbox"]
        self.assertEqual(own[0]["domain"],"1234")
        self.assertEqual(own[0]["filename"],"共享.txt")
        self.assertTrue(own[0]["can_delete"])
        self.assertEqual(self.req("POST","/api/ack/"+shared+"?device_id="+c["device_id"])[0],403)
        self.assertEqual(self.req("POST","/api/ack/"+shared+"?device_id="+a["device_id"])[0],200)
        self.assertEqual(self.files(a["device_id"])["outbox"],[])

    def test_visual_file_listing_pending_delete_and_restart(self):
        a,b=self.register(name="A",domain="1234"),self.register(name="B",domain="1234")
        _,_,data=self.send(a["device_id"],b["device_id"],"busy.bin",b"busy")
        tid=json.loads(data)["transfer_id"]
        row,file=self.store.acquire_download(tid,b["device_id"])
        try:
            result=json.loads(self.req("POST","/api/ack/"+tid+"?device_id="+b["device_id"])[2])
            self.assertTrue(result["pending"])
            pending=self.files(b["device_id"])["inbox"]
            self.assertEqual(pending[0]["state"],"deleting")
            self.assertEqual(self.inbox(b["device_id"]),[])
            self.assertEqual(self.files(a["device_id"])["inbox"],[])
        finally: file.close();self.store.release_download(tid)
        self.assertEqual(self.files(b["device_id"])["inbox"],[])
        _,_,data=self.send_domain(a["device_id"],"1234","busy-shared.bin",b"shared")
        tid=json.loads(data)["transfer_id"]
        with mock.patch("transfer.os.unlink",side_effect=PermissionError("busy")):
            self.req("POST","/api/ack/"+tid+"?device_id="+a["device_id"])
            self.server.shutdown();self.server.server_close();self.thread.join()
            self.server=transfer.create_server("127.0.0.1",0,self.tmpdir,transfer.Limits(min_free_space=0))
            self.store=self.server.store;self.port=self.server.server_address[1]
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":0.02},daemon=True);self.thread.start()
            self.assertEqual(self.files(a["device_id"])["outbox"][0]["state"],"deleting")
            self.assertEqual(self.files(b["device_id"])["inbox"],[])
        self.store.cleanup()
        self.assertEqual(self.files(a["device_id"])["outbox"],[])
        self.assertFalse(os.path.exists(self.store.file_path(tid)))

    def test_repeated_pending_delete_reports_pending_and_checks_owner(self):
        a,b=self.register(name="A"),self.register(name="B")
        _,_,data=self.send(a["device_id"],b["device_id"],"busy",b"busy")
        tid=json.loads(data)["transfer_id"]
        _,file=self.store.acquire_download(tid,b["device_id"])
        try:
            self.req("POST","/api/ack/"+tid+"?device_id="+b["device_id"])
            status,_,data=self.req("POST","/api/ack/"+tid+"?device_id="+b["device_id"])
            self.assertEqual(status,200)
            self.assertTrue(json.loads(data)["pending"])
            self.assertEqual(self.req("POST","/api/ack/"+tid+"?device_id="+a["device_id"])[0],403)
        finally:file.close();self.store.release_download(tid)

    def test_visual_files_requires_cookie_and_rejects_impersonation(self):
        a,b=self.register(name="A"),self.register(name="B")
        self.assertEqual(self.req("GET","/api/files")[0],401)
        status,_,data=self.req("GET","/api/files?device_id="+a["device_id"],headers={"Cookie":self.cookies[b["device_id"]]})
        self.assertEqual(status,403)
        self.assertEqual(json.loads(data).get("code"),"identity_mismatch")

    def test_private_cookie_required(self):
        victim = self.register(name="Victim")
        status, _, _ = self.req("GET", "/api/inbox?device_id=" + victim["device_id"],
                                headers={"Cookie": ""})
        self.assertEqual(status, 401)

    def test_public_id_cannot_impersonate(self):
        a, b, attacker = self.register(name="A"), self.register(name="B"), self.register(name="C")
        _, _, data = self.send(a["device_id"], b["device_id"], "secret.bin", b"private")
        tid = json.loads(data)["transfer_id"]
        headers = {"Cookie": self.cookies[attacker["device_id"]]}
        for method, path in [("GET", "/api/inbox?device_id=" + b["device_id"]),
                             ("GET", "/api/download/"+tid+"?device_id="+b["device_id"]),
                             ("POST", "/api/ack/"+tid+"?device_id="+b["device_id"])]:
            with self.subTest(path=path):
                self.assertEqual(self.req(method, path, headers=headers)[0], 403)
        self.assertEqual(self.req("POST", "/api/register", json.dumps({"device_id":b["device_id"]}),
                                  {"Content-Type":"application/json", **headers})[0], 400)
        self.assertEqual(self.req("GET", "/api/download/"+tid+"?device_id="+b["device_id"])[2], b"private")

    def test_cookie_flags_and_safe_profile(self):
        status, headers, data = self.req("POST", "/api/register", b'{"name":"A"}')
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("token", json.loads(data))

    def test_registration_validation_and_origin(self):
        for value in [[], 4, {"name": {}}, {"name": "x"*81}, {"domain":"1234\n"}]:
            with self.subTest(value=value):
                self.assertEqual(self.req("POST", "/api/register", json.dumps(value))[0], 400)
        self.assertEqual(self.req("POST", "/api/register", b" "*(16*1024+1))[0], 413)
        self.assertEqual(self.req("POST", "/api/register", b"{}", {"Origin":"http://evil.example"})[0], 403)

    def raw_request(self, raw):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(raw)
            conn.shutdown(socket.SHUT_WR)
            result = b""
            while True:
                data = conn.recv(65536)
                if not data: break
                result += data
        return result

    def test_short_upload_not_published(self):
        a, b = self.register(name="A"), self.register(name="B")
        request = ("POST /api/send?from=%s&to=%s&name=partial.bin HTTP/1.1\r\n"
                   "Host: localhost\r\nCookie: %s\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc") % (
                       a["device_id"], b["device_id"], self.cookies[a["device_id"]])
        response = self.raw_request(request.encode())
        self.assertIn(b" 400 ", response.split(b"\r\n",1)[0])
        self.assertEqual(self.inbox(b["device_id"]), [])

    def test_invalid_upload_lengths(self):
        a,b=self.register(name="A"),self.register(name="B")
        for length in ["-1", "junk", "1\r\nContent-Length: 1"]:
            with self.subTest(length=length):
                raw=("POST /api/send?from=%s&to=%s HTTP/1.1\r\nHost: localhost\r\nCookie: %s\r\n"
                     "Content-Length: %s\r\nConnection: close\r\n\r\nx") % (
                         a["device_id"], b["device_id"], self.cookies[a["device_id"]], length)
                self.assertIn(b" 400 ",self.raw_request(raw.encode()).split(b"\r\n",1)[0])
        self.assertEqual(self.inbox(b["device_id"]), [])

    def test_full_flow_chinese_filename(self):
        a = self.register(name="设备A")
        b = self.register(name="设备B")

        # A 的视角能看到 B 在线
        ids = [d["id"] for d in self.devices(a["device_id"])]
        self.assertIn(b["device_id"], ids)

        content = "hello 中文 🎉".encode("utf-8")
        fname = "测试 文件.txt"
        status, _, data = self.send(a["device_id"], b["device_id"], fname, content)
        self.assertEqual(status, 200)
        tid = json.loads(data)["transfer_id"]

        # B 的收件箱
        inbox = self.inbox(b["device_id"])
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["filename"], fname)
        self.assertEqual(inbox[0]["size"], len(content))

        # 下载内容一致
        status, headers, data = self.req(
            "GET", "/api/download/%s?device_id=%s" % (tid, b["device_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(data, content)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("%E6%B5%8B%E8%AF%95", headers.get("Content-Disposition", ""))  # 中文文件名编码

        # 移除
        status, _, _ = self.req(
            "POST", "/api/ack/%s?device_id=%s" % (tid, b["device_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(self.inbox(b["device_id"]), [])
        self.assertFalse(os.path.exists(self.store.file_path(tid)))

    def test_bidirectional(self):
        a = self.register(name="A")
        b = self.register(name="B")
        # A -> B
        self.assertEqual(self.send(a["device_id"], b["device_id"], "a2b.bin", b"AAA")[0], 200)
        # B -> A（反向）
        self.assertEqual(self.send(b["device_id"], a["device_id"], "b2a.bin", b"BBB")[0], 200)
        self.assertEqual(len(self.inbox(b["device_id"])), 1)
        self.assertEqual(len(self.inbox(a["device_id"])), 1)

    def test_large_file(self):
        a = self.register(name="A")
        b = self.register(name="B")
        content = os.urandom(10 * 1024 * 1024)  # 10MB
        status, _, data = self.send(a["device_id"], b["device_id"], "big.bin", content)
        self.assertEqual(status, 200)
        tid = json.loads(data)["transfer_id"]
        status, _, data = self.req(
            "GET", "/api/download/%s?device_id=%s" % (tid, b["device_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         hashlib.sha256(content).hexdigest())

    def test_filename_sanitized(self):
        a = self.register(name="A")
        b = self.register(name="B")
        status, _, data = self.send(a["device_id"], b["device_id"], "../../evil.txt", b"x")
        self.assertEqual(status, 200)
        inbox = self.inbox(b["device_id"])
        self.assertEqual(inbox[0]["filename"], "evil.txt")

    def test_errors(self):
        a = self.register(name="A")
        b = self.register(name="B")
        # 发送给不存在的设备 -> 404
        status, _, _ = self.send(a["device_id"], "nope", "f.bin", b"x")
        self.assertEqual(status, 404)
        # 发送给离线设备 -> 410（把 OFFLINE_TTL 压到 0 强制离线）
        c = self.register(name="C")
        old_ttl = transfer.OFFLINE_TTL
        transfer.OFFLINE_TTL = 0
        try:
            import time
            time.sleep(0.01)
            status, _, _ = self.send(a["device_id"], c["device_id"], "f.bin", b"x")
            self.assertEqual(status, 410)
        finally:
            transfer.OFFLINE_TTL = old_ttl
        # 下载：非收件人 -> 403
        status, _, data = self.send(a["device_id"], b["device_id"], "secret.bin", b"s")
        tid = json.loads(data)["transfer_id"]
        status, _, _ = self.req("GET", "/api/download/%s?device_id=%s" % (tid, a["device_id"]))
        self.assertEqual(status, 403)
        # 不存在的传输 -> 404
        status, _, _ = self.req("GET", "/api/download/deadbeef?device_id=%s" % b["device_id"])
        self.assertEqual(status, 404)

    def test_default_spool_dir(self):
        from unittest import mock
        # HOME 正常时：落在下载文件夹下的 lanfiles 子目录
        self.assertTrue(transfer.default_spool_dir().endswith(
            os.path.join("Downloads", "lanfiles")))
        # HOME 不可用时：回退到系统临时目录下的 lanfiles
        with mock.patch("os.path.expanduser", return_value="~"):
            self.assertTrue(transfer.default_spool_dir().endswith("lanfiles"))

    def test_ensure_spool_dir(self):
        # 正常创建：返回绝对路径、目录存在、探测文件已清理
        base = tempfile.mkdtemp(prefix="lanfiles-ensure-")
        try:
            target = os.path.join(base, "sub", "dir")
            self.assertEqual(transfer.ensure_spool_dir(target), os.path.abspath(target))
            self.assertTrue(os.path.isdir(target))
            self.assertEqual(os.listdir(target), [])
        finally:
            shutil.rmtree(base, ignore_errors=True)
        # 父路径是文件：返回 None
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        try:
            self.assertIsNone(transfer.ensure_spool_dir(os.path.join(f.name, "x")))
        finally:
            os.remove(f.name)

    def test_choose_addresses(self):
        # 复现原 bug：同时存在虚拟网卡 172.21.208.1 与真实局域网 192.168.5.10，
        # 默认路由接口 IP 为 192.168.5.10 → 主地址必须是 192.168.5.10。
        ips = {"172.21.208.1", "192.168.5.10", "127.0.0.1"}
        primary, others = transfer.choose_addresses(ips, "192.168.5.10")
        self.assertEqual(primary, "192.168.5.10")
        self.assertIn("172.21.208.1", others)
        self.assertEqual(others[-1], "127.0.0.1")  # 回环置底

        # 默认路由缺失时兜底：选真实局域网地址而非虚拟地址
        primary, _ = transfer.choose_addresses(ips, None)
        self.assertEqual(primary, "192.168.5.10")

        # 默认路由 IP 不在集合中（罕见）→ 兜底
        primary, _ = transfer.choose_addresses(ips, "10.0.0.1")
        self.assertEqual(primary, "192.168.5.10")

        # 仅回环
        primary, others = transfer.choose_addresses({"127.0.0.1"}, None)
        self.assertEqual(primary, "127.0.0.1")
        self.assertEqual(others, [])

        # 空集合
        primary, others = transfer.choose_addresses(set(), None)
        self.assertEqual(primary, "127.0.0.1")
        self.assertEqual(others, [])

    def test_domain_scoping(self):
        a = self.register(name="A", domain="1234")
        b = self.register(name="B", domain="1234")
        c = self.register(name="C")            # 无域
        d = self.register(name="D", domain="9999")
        # 同域可见
        ids_a = [x["id"] for x in self.devices(a["device_id"])]
        self.assertIn(b["device_id"], ids_a)
        self.assertNotIn(c["device_id"], ids_a)
        self.assertNotIn(d["device_id"], ids_a)
        # 无域设备看不到域内设备
        ids_c = [x["id"] for x in self.devices(c["device_id"])]
        self.assertNotIn(a["device_id"], ids_c)

    def test_domain_broadcast(self):
        a = self.register(name="A", domain="1234")
        b = self.register(name="B", domain="1234")
        c = self.register(name="C")            # 无域
        content = b"hello domain"
        status, _, data = self.send_domain(a["device_id"], "1234", "共享.txt", content)
        self.assertEqual(status, 200)
        tid = json.loads(data)["transfer_id"]

        # 同域 B 收件箱可见，标记 kind=domain
        inbox_b = self.inbox(b["device_id"])
        self.assertEqual(len(inbox_b), 1)
        self.assertEqual(inbox_b[0]["kind"], "domain")
        self.assertEqual(inbox_b[0]["filename"], "共享.txt")
        # 发送者 A 自己的收件箱不出现自己发的广播
        self.assertEqual(self.inbox(a["device_id"]), [])
        # 无域 C 不可见
        self.assertEqual(self.inbox(c["device_id"]), [])

        # 同域 B 可下载，内容一致
        status, _, data = self.req(
            "GET", "/api/download/%s?device_id=%s" % (tid, b["device_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(data, content)
        # 无域 C 下载 -> 403
        status, _, _ = self.req(
            "GET", "/api/download/%s?device_id=%s" % (tid, c["device_id"]))
        self.assertEqual(status, 403)
        # 非发送者 B 移除 -> 403
        status, _, _ = self.req(
            "POST", "/api/ack/%s?device_id=%s" % (tid, b["device_id"]))
        self.assertEqual(status, 403)
        # 发送者 A 移除 -> 200，文件清理
        status, _, _ = self.req(
            "POST", "/api/ack/%s?device_id=%s" % (tid, a["device_id"]))
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(self.store.file_path(tid)))

    def test_handler_write_and_rename_failures_release_reservation(self):
        a,b=self.register(name="A"),self.register(name="B")
        for operation in ["write_chunk", "replace"]:
            with self.subTest(operation=operation):
                target=mock.patch.object(self.store,"write_chunk",side_effect=OSError("write failure")) if operation=="write_chunk" else mock.patch("transfer.os.replace",side_effect=OSError("rename failure"))
                with target:
                    self.assertEqual(self.send(a["device_id"],b["device_id"],"failed.bin",b"abc")[0],507)
                self.assertEqual(self.inbox(b["device_id"]),[])
                self.assertEqual(os.listdir(self.store.blob_dir),[])
                status,_,data=self.send(a["device_id"],b["device_id"],"next.bin",b"abc")
                self.assertEqual(status,200)
                tid=json.loads(data)["transfer_id"]
                self.req("POST","/api/ack/"+tid+"?device_id="+b["device_id"])

    def test_zero_byte_and_upload_limits(self):
        a,b=self.register(name="A"),self.register(name="B")
        self.store.limits = replace(self.store.limits, max_file_size=3, spool_quota=4)
        self.assertEqual(self.send(a["device_id"],b["device_id"],"empty.bin",b"")[0],200)
        self.assertEqual(self.send(a["device_id"],b["device_id"],"large.bin",b"1234")[0],413)
        self.assertEqual(self.send(a["device_id"],b["device_id"],"ok.bin",b"123")[0],200)
        self.assertEqual(self.send(a["device_id"],b["device_id"],"full.bin",b"12")[0],507)
        self.assertEqual([x["size"] for x in self.inbox(b["device_id"])],[0,3])

    def test_two_instances_keep_independent_browser_cookies(self):
        jar=http.cookiejar.CookieJar()
        client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        def register_at(port):
            req=urllib.request.Request("http://127.0.0.1:%d/api/register" % port,data=b"{}")
            with client.open(req,timeout=2) as response: return json.load(response)["device_id"]
        def profile_at(port):
            try:
                with client.open("http://127.0.0.1:%d/api/session" % port,timeout=2) as response:
                    return response.status,json.load(response)
            except urllib.error.HTTPError as e:
                try: return e.code,json.load(e)
                finally: e.close()
        original=register_at(self.port)
        with tempfile.TemporaryDirectory(prefix="lanfiles-second-") as other:
            server=transfer.create_server("127.0.0.1",0,other,transfer.Limits(min_free_space=0))
            thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True); thread.start()
            try:
                second=register_at(server.server_address[1])
                status,profile=profile_at(self.port)
                self.assertEqual(status,200)
                self.assertEqual(profile["device_id"],original)
                status,profile=profile_at(server.server_address[1])
                self.assertEqual(status,200)
                self.assertEqual(profile["device_id"],second)
            finally: server.shutdown();server.server_close();thread.join()

    def test_session_and_file_survive_restart(self):
        a,b=self.register(name="A"),self.register(name="B")
        _,_,data=self.send(a["device_id"],b["device_id"],"restore.bin",b"restore")
        tid=json.loads(data)["transfer_id"]
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server=transfer.create_server("127.0.0.1",0,self.tmpdir,transfer.Limits(min_free_space=0))
        self.store=self.server.store; self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":0.02},daemon=True)
        self.thread.start()
        self.assertEqual(self.inbox(b["device_id"])[0]["transfer_id"],tid)
        self.assertEqual(self.req("GET","/api/download/"+tid+"?device_id="+b["device_id"])[2],b"restore")
        status,_,data=self.req("POST","/api/register",b'{"name":"Renamed"}',
                              {"Cookie":self.cookies[b["device_id"]]})
        self.assertEqual(status,200); self.assertEqual(json.loads(data)["device_id"],b["device_id"])

    def test_transfer_encoding_and_expect_rejected(self):
        a,b=self.register(name="A"),self.register(name="B")
        for headers,status in [("Transfer-Encoding: chunked",400),("Expect: 100-continue\r\nContent-Length: 1",417)]:
            raw=("POST /api/send?to=%s HTTP/1.1\r\nHost: localhost\r\nCookie: %s\r\n%s\r\nConnection: close\r\n\r\n0\r\n\r\n") % (
                b["device_id"], self.cookies[a["device_id"]],headers)
            self.assertIn((" %d " % status).encode(),self.raw_request(raw.encode()).split(b"\r\n",1)[0])
        self.assertEqual(self.inbox(b["device_id"]),[])

    def test_upload_idle_and_total_deadlines(self):
        a,b=self.register(name="A"),self.register(name="B")
        self.store.limits=replace(self.store.limits,io_timeout=0.15,upload_timeout=0.25)
        with socket.create_connection(("127.0.0.1",self.port),timeout=2) as c:
            raw=("POST /api/send?to=%s HTTP/1.1\r\nHost: localhost\r\nCookie: %s\r\nContent-Length: 10\r\n\r\n") % (b["device_id"],self.cookies[a["device_id"]])
            c.sendall(raw.encode())
            self.assertIn(b" 408 ",c.recv(65536).split(b"\r\n",1)[0])
        # 不断滴入字节绕不过总时限。
        with socket.create_connection(("127.0.0.1",self.port),timeout=2) as c:
            c.sendall(raw.encode())
            started=time.monotonic()
            for _ in range(6):
                try: c.sendall(b"x")
                except OSError: break
                time.sleep(0.07)
            response=c.recv(65536)
            self.assertIn(b" 408 ",response.split(b"\r\n",1)[0])
            self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.inbox(b["device_id"]),[])
        self.assertEqual(os.listdir(self.store.blob_dir),[])

    def test_expired_session_replaced_not_claimed(self):
        a=self.register(name="A")
        with self.store.db:
            self.store.db.execute("UPDATE devices SET expires=0 WHERE id=?",(a["device_id"],))
        headers={"Cookie":self.cookies[a["device_id"]]}
        self.assertEqual(self.req("GET","/api/inbox",headers=headers)[0],401)
        status,response_headers,data=self.req("POST","/api/register",b"{}",headers)
        self.assertEqual(status,200)
        self.assertNotEqual(json.loads(data)["device_id"],a["device_id"])
        self.assertIn("HttpOnly",response_headers["Set-Cookie"])

    def test_same_session_cannot_cross_rooms_and_profile_shared(self):
        a,b=self.register(name="A",domain="1234"),self.register(name="B",domain="1234")
        c=self.register(name="C",domain="9999")
        _,_,data=self.send_domain(a["device_id"],"1234","x",b"room")
        tid=json.loads(data)["transfer_id"]
        headers={"Cookie":self.cookies[c["device_id"]]}
        self.assertEqual(self.req("GET","/api/download/"+tid,headers=headers)[0],403)
        headers={"Cookie":self.cookies[b["device_id"]]}
        self.req("POST","/api/register",b'{"domain":"9999"}',headers)
        self.assertEqual(json.loads(self.req("GET","/api/session",headers=headers)[2])["domain"],"9999")
        self.assertEqual(self.req("GET","/api/download/"+tid,headers=headers)[0],403)

    def test_duplicate_query_ids_rejected(self):
        a=self.register(name="A")
        self.assertEqual(self.req("GET","/api/inbox?device_id=%s&device_id=%s" % (a["device_id"],a["device_id"]))[0],400)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="lanfiles-store-")
        self.limits=transfer.Limits(max_file_size=100,spool_quota=200,min_free_space=0,register_per_ip=100,register_global=1000)
        self.store=transfer.Store(self.tmp.name,self.limits)
        self.a,self.token_a=self.store.register("","A","1234","local")
        self.b,self.token_b=self.store.register("","B","1234","local")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def upload(self,content=b"abc",domain=False):
        tid=self.store.begin_upload(self.a["id"],"" if domain else self.b["id"],"1234" if domain else "","x.bin",len(content))
        with open(self.store.file_path(tid,True),"wb",buffering=0) as f:
            self.store.write_chunk(tid,f,content)
        self.store.finish_upload(tid)
        return tid

    def assert_api_error(self,status,call):
        with self.assertRaises(transfer.APIError) as caught: call()
        self.assertEqual(caught.exception.status,status)

    def test_reservations_are_atomic_and_released(self):
        self.store.limits=replace(self.limits,spool_quota=30)
        c,_=self.store.register("","C","1234","local")
        barrier=threading.Barrier(2); results=[]
        def reserve(dev):
            barrier.wait()
            try: results.append(self.store.begin_upload(dev,"","1234","x",20))
            except transfer.APIError as e: results.append(e.status)
        threads=[threading.Thread(target=reserve,args=(d,)) for d in [self.a["id"],c["id"]]]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(507),1)
        tid=next(x for x in results if isinstance(x,str)); self.store.cancel_upload(tid)
        replacement=self.store.begin_upload(self.a["id"],"","1234","x",20)
        self.store.cancel_upload(replacement)

    def test_upload_slots_and_transfer_count(self):
        self.store.limits=replace(self.limits,max_uploads=1,max_transfers=1)
        tid=self.store.begin_upload(self.a["id"],self.b["id"],"","x",1)
        self.assert_api_error(429,lambda:self.store.begin_upload(self.a["id"],self.b["id"],"","x",1))
        self.assert_api_error(429,lambda:self.store.begin_upload(self.b["id"],self.a["id"],"","x",1))
        self.store.cancel_upload(tid)
        self.upload()
        self.assert_api_error(429,lambda:self.store.begin_upload(self.a["id"],self.b["id"],"","x",1))

    def test_partial_disk_write_is_completed_before_publication(self):
        tid=self.store.begin_upload(self.a["id"],self.b["id"],"","x",3)
        with open(self.store.file_path(tid,True),"wb",buffering=0) as file:
            class ShortWriter:
                def write(self, data): return file.write(data[:1])
            self.store.write_chunk(tid,ShortWriter(),b"abc")
        self.store.finish_upload(tid)
        row, file=self.store.acquire_download(tid,self.b["id"])
        try: self.assertEqual(file.read(),b"abc")
        finally: file.close(); self.store.release_download(tid)

    def test_disk_floor_and_write_error_release(self):
        disk=shutil._ntuple_diskusage(100,99,1)
        self.store.limits=replace(self.limits,min_free_space=2)
        with mock.patch("transfer.shutil.disk_usage",return_value=disk):
            self.assert_api_error(507,lambda:self.store.begin_upload(self.a["id"],self.b["id"],"","x",1))
        self.store.limits=self.limits
        tid=self.store.begin_upload(self.a["id"],self.b["id"],"","x",1)
        with open(self.store.file_path(tid,True),"wb",buffering=0) as f:
            with mock.patch("transfer.shutil.disk_usage",side_effect=OSError("disk error")):
                with self.assertRaises(OSError): self.store.write_chunk(tid,f,b"x")
        self.store.cancel_upload(tid)
        self.assertEqual(self.store.inbox(self.b["id"]),[])
        self.assertEqual(os.listdir(self.store.blob_dir),[])
        self.upload()

    def test_reader_delays_delete_and_keeps_quota(self):
        tid=self.upload(); row,file=self.store.acquire_download(tid,self.b["id"])
        result=self.store.ack(tid,self.b["id"])
        self.assertTrue(result["pending"])
        self.assertTrue(os.path.exists(self.store.file_path(tid)))
        self.assertEqual(self.store.inbox(self.b["id"]),[])
        self.assert_api_error(404,lambda:self.store.acquire_download(tid,self.b["id"]))
        self.store.limits=replace(self.limits,spool_quota=3)
        self.assert_api_error(507,lambda:self.store.begin_upload(self.a["id"],self.b["id"],"","next",1))
        self.assertEqual(file.read(),b"abc"); file.close(); self.store.release_download(tid)
        self.assertFalse(os.path.exists(self.store.file_path(tid)))
        self.upload()

    def test_delete_failure_retries(self):
        tid=self.upload()
        with mock.patch("transfer.os.unlink",side_effect=PermissionError("busy")):
            self.assertTrue(self.store.ack(tid,self.b["id"])["pending"])
            self.assertEqual(self.store.inbox(self.b["id"]),[])
        self.assertTrue(os.path.exists(self.store.file_path(tid)))
        self.store.cleanup()
        self.assertFalse(os.path.exists(self.store.file_path(tid)))

    def test_expiry_is_checked_at_read_and_restart(self):
        tid=self.upload()
        with self.store.db:
            self.store.db.execute("UPDATE transfers SET created=0 WHERE transfer_id=?",(tid,))
        self.assert_api_error(404,lambda:self.store.acquire_download(tid,self.b["id"]))
        self.assertFalse(os.path.exists(self.store.file_path(tid)))
        tid=self.upload()
        with self.store.db: self.store.db.execute("UPDATE transfers SET created=0 WHERE transfer_id=?",(tid,))
        self.store.close(); self.store=transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(self.store.inbox(self.b["id"]),[])
        self.assertFalse(os.path.exists(self.store.file_path(tid)))

    def test_recovery_cleans_partial_and_preserves_legacy(self):
        tid=self.store.begin_upload(self.a["id"],self.b["id"],"","partial",10)
        with open(self.store.file_path(tid,True),"wb") as f: f.write(b"abc")
        # 崩溃发生在重命名后、发布事务前。
        os.replace(self.store.file_path(tid,True),self.store.file_path(tid))
        legacy=os.path.join(self.tmp.name,"a"*32)
        with open(legacy,"wb") as f: f.write(b"legacy")
        self.store.close(); self.store=transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(self.store.inbox(self.b["id"]),[])
        self.assertEqual(os.listdir(self.store.blob_dir),[])
        self.assertEqual(Path(legacy).read_bytes(),b"legacy")

    def test_single_instance_lock_and_corrupt_database(self):
        with self.assertRaises(OSError): transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(self.store.authenticate(self.token_a)["id"],self.a["id"])
        self.store.close()
        Path(self.tmp.name,".lanfiles","state.sqlite3").write_bytes(b"corrupt database")
        with self.assertRaises(sqlite3.DatabaseError): transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(Path(self.tmp.name,".lanfiles","state.sqlite3").read_bytes(),b"corrupt database")

    def test_rate_limits_and_device_capacity(self):
        self.store.limits=replace(self.limits,register_per_ip=1,register_global=3,max_devices=3)
        self.store.register("","C","","other")
        self.assert_api_error(429,lambda:self.store.register("","D","","other"))
        self.assert_api_error(429,lambda:self.store.register("","D","","another"))
        # 分离容量与限速控制。
        self.store.limits=replace(self.limits,max_devices=3)
        self.assert_api_error(429,lambda:self.store.register("","D","","new"))

    def test_credentials_not_stored_in_plaintext(self):
        self.store.close()
        data=Path(self.tmp.name,".lanfiles","state.sqlite3").read_bytes()
        self.assertNotIn(self.token_a.encode(),data)
        self.store=transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(self.store.authenticate(self.token_a)["id"],self.a["id"])

    def test_force_killed_writer_recovers(self):
        self.store.close()
        code='''import sys,time,json,transfer
s=transfer.Store(sys.argv[1],transfer.Limits(min_free_space=0))
a,_=s.register('', 'writer', '1234', 'child')
t=s.begin_upload(a['id'],'','1234','partial.bin',10)
f=open(s.file_path(t,True),'wb',buffering=0); s.write_chunk(t,f,b'abc')
print('ready',flush=True); time.sleep(60)
'''
        proc=subprocess.Popen([sys.executable,"-c",code,self.tmp.name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),"ready")
            proc.kill(); proc.wait(timeout=3)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close(); proc.stderr.close()
        self.store=transfer.Store(self.tmp.name,self.limits)
        self.assertEqual(os.listdir(self.store.blob_dir),[])
        self.upload()

    def test_capacity_parser(self):
        self.assertEqual(transfer.parse_size("10GiB"),10737418240)
        self.assertEqual(transfer.parse_size("2MiB"),2097152)
        self.assertEqual(transfer.parse_size("0"),0)
        for value in ["-1", "1.5GiB", "999999999999999999999GiB"]:
            with self.assertRaises(argparse.ArgumentTypeError): transfer.parse_size(value)


class TestConnectionLimit(unittest.TestCase):
    def test_limit_before_thread_creation_and_release(self):
        with tempfile.TemporaryDirectory(prefix="lanfiles-conn-") as tmp:
            server=transfer.create_server("127.0.0.1",0,tmp,transfer.Limits(min_free_space=0,max_connections=1,io_timeout=0.25))
            thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.02},daemon=True); thread.start()
            address=server.server_address
            try:
                with socket.create_connection(address,timeout=2) as occupied:
                    occupied.sendall(b"GET / HTTP/1.1\r\n")
                    deadline=time.monotonic()+1
                    while not server._connections and time.monotonic()<deadline: time.sleep(0.005)
                    with socket.create_connection(address,timeout=2) as other:
                        other.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                        self.assertIn(b" 503 ",other.recv(1024).split(b"\r\n",1)[0])
                deadline=time.monotonic()+1
                while server._connections and time.monotonic()<deadline: time.sleep(0.005)
                conn=http.client.HTTPConnection(*address,timeout=2)
                try:
                    conn.request("GET","/"); self.assertEqual(conn.getresponse().status,200)
                finally: conn.close()
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
