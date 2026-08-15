# 局域网传文件（LanFiles）

> 单文件、零依赖的局域网文件互传工具。一台电脑运行，同网段的任意设备（手机 / 平板 / 电脑）用浏览器打开地址即可互传，**无需安装任何东西**。
>
> A single-file, zero-dependency LAN file transfer tool. Run it on one computer, and any device (phone / tablet / PC) on the same network opens the address in a browser to transfer files — **nothing to install**.

纯 Python 标准库实现，跨平台（macOS / Windows ），iOS 风格界面。
Pure Python standard library, cross-platform (macOS / Windows ), iOS-style UI.

---

## 简体中文

### ✨ 功能特性

- **单文件、零依赖**：整个程序就是一个 `transfer.py`，只用 Python 标准库。
- **浏览器即设备**：每个打开页面的浏览器标签页自动成为一个“设备”，无需装 App。
- **1 对 1 私发**：在设备列表点选某台设备，拖拽文件发给它（大文件流式传输、中文文件名）。
- **域号（房间）**：输入 4 位数字进入房间，同域设备互相可见、可私发、可「域内广播」给所有人。
- **域内广播**：A 发文件到域，域内所有人都能在收件箱看到并下载。
- **iOS 风格界面**：浅色、系统蓝、分组列表，选中设备有明显 ✓。
- **无鉴权**：面向可信局域网，完全开放、即开即用。

### 🚀 快速开始

```bash
python3 transfer.py
```

终端会打印可访问地址，例如：

```
★ http://192.168.5.10:8000
```

让同一局域网下的其他设备用浏览器打开这个地址即可。

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8000` | 监听端口（被占用时换一个，如 `--port 9000`） |
| `--name` | 随机 | 本机显示名 |
| `--dir` | `~/Downloads/lanfiles/` | 中转文件存放目录 |

```bash
python3 transfer.py --port 9000 --name 客厅电脑 --dir ~/lanfiles
```

### 🧩 域号（房间）

- 页面顶部输入 **4 位数字**（如 `1234`）→ 点「加入」，同域号的设备进入同一房间，只互相可见。
- **1 对 1 私发**：点选某台设备 → 拖文件发给它（与普通模式一致）。
- **域内广播**：加入域后出现「发给域内所有人」，拖入文件后，域内所有成员都能下载（约 1 小时后自动清理）。
- 不同域号、以及无域设备与域内设备之间**互相隔离**。

### 🧠 工作原理

1. 每台浏览器标签页打开页面时自动注册为一个“设备”（身份存于 `localStorage`）。
2. 页面每 1.5 秒轮询一次：获取在线设备列表 + 自己收到的文件。
3. 发送：选中目标设备 → 原始字节流上传到中枢 → 目标设备页面出现“收到文件” → 点「下载」保存。
4. 中转文件投递后（点「移除」）删除，长时间无人领取（默认 1 小时）自动清理。

### 📁 项目结构

```
.
├── transfer.py        # 核心程序（后端 + 内嵌前端，单文件）
├── test_transfer.py   # 全链路自测（unittest）
├── LICENSE            # MIT 许可证
└── README.md
```

### 🧪 测试

```bash
python3 -m unittest test_transfer -v
```

覆盖：注册/上线、双向互发、大文件流式传输、中文文件名、文件名清洗、域隔离、域内广播、下载权限、错误路径（403/404/410）、地址选择等。

### ❓ 其他设备连不上？

1. **确认程序还在运行**（终端窗口没关、没按 Ctrl+C）——最常见原因。
2. **同一路由器 / 网段**：设备与电脑 IP 同网段（如 `192.168.5.x`）；手机用蜂窝/访客网络连不上。
3. **用 `http://` 开头**，不要用 `https://`。
4. **关闭路由器的 AP/客户端/无线隔离**。
5. **防火墙放行**：Windows 首次弹窗勾选「专用网络」允许；macOS 允许 Python 接受传入连接。
6. **地址是 `172.x.x.1` 这类虚拟网卡地址**（WSL2/Hyper-V/虚拟机）：改用终端「本机/备用地址」里与你设备同网段的那个。

### 📦 Windows 免安装版

仓库只保留源码。需要“双击即用、免装 Python”的 Windows 版时，可把 Windows 嵌入式 Python 运行时与 `transfer.py`、启动脚本打包成 zip，或在 GitHub Releases 发布。

### 📄 License

[MIT License](LICENSE) —— 允许自由使用、修改、再分发（含商用），需保留版权声明。

---

## English

### ✨ Features

- **Single file, zero dependencies**: the whole program is one `transfer.py` using only the Python standard library.
- **Browser as device**: every browser tab that opens the page automatically becomes a "device" — no app required.
- **1-to-1 direct send**: pick a device and drag & drop files to it (streamed large files, Chinese filenames supported).
- **Domain (room)**: enter a 4-digit code to join a room; devices in the same room see each other and can send 1-to-1 or broadcast.
- **Domain broadcast**: when A sends a file to the room, everyone in the room can download it.
- **iOS-style UI**: light theme, system blue, grouped list, clear ✓ on the selected device.
- **No auth**: designed for trusted LANs, open and instant.

### 🚀 Quick Start

```bash
python3 transfer.py
```

The terminal prints a reachable address, for example:

```
★ http://192.168.5.10:8000
```

Open that address in a browser on any other device on the same LAN.

Common options:

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8000` | Port (change it if occupied, e.g. `--port 9000`) |
| `--name` | random | Display name of this computer |
| `--dir` | `~/Downloads/lanfiles/` | Staging directory for relayed files |

```bash
python3 transfer.py --port 9000 --name livingroom --dir ~/lanfiles
```

### 🧩 Domain (Room)

- Enter a **4-digit code** (e.g. `1234`) at the top and tap "加入"; devices with the same code join the same room and only see each other.
- **1-to-1 direct send**: pick a device → drag files to it (same as the default mode).
- **Domain broadcast**: after joining, a "发给域内所有人" panel appears; drag files into it and every member can download them (auto-cleaned after ~1 hour).
- Different domains, and no-domain vs. in-domain devices, are **isolated from each other**.

### 🧠 How It Works

1. Each browser tab registers itself as a "device" on page load (identity stored in `localStorage`).
2. The page polls every 1.5 s for the online device list and its own inbox.
3. Send: pick a target device → raw byte stream uploaded to the hub → the target's page shows an incoming file → tap "下载" to save it.
4. Relayed files are deleted after delivery (tap "移除") or after ~1 hour if unclaimed.

### 📁 Project Structure

```
.
├── transfer.py        # core program (backend + embedded frontend, single file)
├── test_transfer.py   # end-to-end tests (unittest)
├── LICENSE            # MIT license
└── README.md
```

### 🧪 Testing

```bash
python3 -m unittest test_transfer -v
```

Covers: registration/online status, bidirectional transfer, large-file streaming, Chinese filenames, filename sanitization, domain isolation, domain broadcast, download permissions, error paths (403/404/410), and address selection.

### ❓ Troubleshooting: Can't Connect

1. **Make sure the program is still running** (the terminal window is open, no Ctrl+C) — the most common cause.
2. **Same router / subnet**: the device and computer must be on the same subnet (e.g. `192.168.5.x`); cellular/guest networks won't work.
3. **Use `http://`, not `https://`.
4. **Disable AP/client/wireless isolation** on your router.
5. **Allow through the firewall**: on Windows tick "Private network" on first prompt; on macOS allow Python to accept incoming connections.
6. **If the address looks like `172.x.x.1`** (a WSL2/Hyper-V/VM virtual adapter): use the same-subnet address listed under "本机/备用地址" instead.

### 📦 Windows Portable Build

This repository keeps only the source. For a "double-click to run, no Python install" Windows build, bundle the Windows embeddable Python runtime with `transfer.py` and a launcher script into a zip, or publish it under GitHub Releases.

### 📄 License

[MIT License](LICENSE) — free to use, modify and redistribute (including commercially), provided the copyright notice is retained.
