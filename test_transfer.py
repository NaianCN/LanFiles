# -*- coding: utf-8 -*-
"""transfer.py 的全链路自测：起一个临时端口服务，跑通注册/互发/收件/下载/移除/清理。"""

import http.client
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse

import transfer


class TestTransfer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="lanfiles-test-")
        transfer.SPOOL_DIR = cls.tmpdir
        cls.server = transfer.ThreadingHTTPServer(("127.0.0.1", 0), transfer.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ---------- 工具 ----------
    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
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
        status, _, data = self.req(
            "POST", "/api/register", json.dumps(body).encode(),
            {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        return json.loads(data)

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
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, tid)))

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
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, tid)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
