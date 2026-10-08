"""Exercise real Tk widgets and child processes using a temporary spool and loopback host."""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from transfer import Limits

with tempfile.TemporaryDirectory(prefix="lanfiles-gui-") as spool:
    app.resolve_spool_dir = lambda desired=None: spool
    app.find_available_port = lambda preferred=8000: 18765
    app.get_lan_ips = lambda: ("127.0.0.1", [])
    original_popen = subprocess.Popen
    launched = []
    def loopback_process(args, **kwargs):
        args = list(args)
        if "--host" in args:
            args[args.index("--host") + 1] = "127.0.0.1"
        launched.append(args)
        return original_popen(args, **kwargs)
    app.subprocess.Popen = loopback_process
    root = tk.Tk()
    client = None
    try:
        client = app.LanFilesApp(root, Limits(max_file_size=2*1024**3,spool_quota=5*1024**3,max_uploads=2,min_free_space=0))
        root.update()
        assert client.is_server_running()
        assert client.qr_image is not None
        args = launched[-1]
        assert args[args.index("--max-file-size")+1] == "2147483648"
        assert args[args.index("--spool-quota")+1] == "5368709120"
        assert args[args.index("--max-uploads")+1] == "2"
        conn = http.client.HTTPConnection("127.0.0.1",client.server_port,timeout=3)
        conn.request("POST","/api/register",b"{}")
        response=conn.getresponse(); cookie=response.getheader("Set-Cookie").split(";",1)[0]
        identity=json.loads(response.read())["device_id"]; conn.close()
        old=client.server_process
        client.restart_server(); root.update()
        assert old.poll() is not None
        assert client.is_server_running()
        conn=http.client.HTTPConnection("127.0.0.1",client.server_port,timeout=3)
        conn.request("GET","/api/session",headers={"Cookie":cookie})
        response=conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["device_id"] == identity
        conn.close()
        root.geometry("560x520"); root.update()
        assert root.winfo_width()>=560 and root.winfo_height()>=520
        print("PASS: Tk widgets, QR, custom limits, full process restart and durable Cookie")
    finally:
        if client: client.on_close()
        else: root.destroy()
