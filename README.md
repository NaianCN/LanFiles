# 局域网传文件（LanFiles）

当前版本：**1.4.1** · 作者：**GPT、Gemini** · 项目维护：**NaianCN**

[Windows 免安装包](https://raw.githubusercontent.com/NaianCN/LanFiles/main/lanfiles-win.zip) · [macOS 应用包](https://raw.githubusercontent.com/NaianCN/LanFiles/main/lanfiles-macos.zip) · [更新记录](CHANGELOG.md)

Python 标准库实现的局域网文件互传工具。电脑启动服务后，其他设备用浏览器打开地址，即可私发文件或在同一房间内共享。支持 macOS、Windows、Linux，不需要安装第三方 Python 包。

## 启动

```bash
python3 transfer.py
```

默认监听 `0.0.0.0:8000`，浏览器使用终端显示的 `http://` 地址。文件默认中转到 `~/Downloads/lanfiles`；该目录不可写时尝试 `~/lanfiles` 和系统临时目录。

```bash
python3 transfer.py --host 192.168.5.10 --port 9000 --dir ~/lanfiles \
  --max-file-size 10GiB --spool-quota 20GiB --max-uploads 4 --uploads-per-device 3 --min-free-space 1GiB
```

容量参数接受非负整数字节数或 `KiB/MiB/GiB`；同时上传数量（总数及每设备）必须大于零。端口或目录不可用、目录已有运行实例、数据库损坏时，会明确报错并停止启动。

macOS 桌面入口为解压 `lanfiles-macos.zip` 后的 `LanFiles.app`，也可在源码目录运行 `python3 app.py`。桌面版需要本机有带 Tk 8.6+ 的 Python，支持相同四个容量参数：

```bash
python3 app.py --max-file-size 2GiB --spool-quota 5GiB --max-uploads 2 --min-free-space 1GiB
```

Windows 免安装包为 `lanfiles-win.zip`，解压后运行 `start.bat`；其 Python 运行时已包含 SQLite。启动参数可在批处理后追加，例如 `start.bat --port 9000`。

## 设备、房间和安全边界

- 打开网页即可加入。服务端为浏览器生成公开设备 ID 和独立随机私密凭证；设备列表仅公开 ID，不能用它冒充他人。
- 首次打开会分配随机名字，可直接修改或点 🎲 换一个；名字保存在本浏览器（localStorage），之后每次打开都沿用，即使 Cookie 失效建立了新身份也会自动改回。服务端还会按设备 IP 记住最近用的名字（保留 30 天）：清除浏览数据、换浏览器或换访问地址后，只要设备 IP 不变，新身份会自动沿用原名字。建议在路由器中给常用设备绑定固定 IP；同一 IP 上的多个浏览器默认同名，可分别修改。
- 私密凭证只通过主机 Cookie 传递：`HttpOnly; SameSite=Strict; Path=/`，有效期自创建起 7 天。数据库仅保存摘要，凭证不会放入网页、JSON、下载 URL 或日志。
- 同一浏览器的多个标签页共享一个设备身份；不同浏览器、隐私窗口或设备通常具有不同身份。
- 私发文件仅收件人可下载和移除。广播文件由当前房间成员下载，只有发送者可移除。
- 四位房间号只用于分组，**不是密码或邀请凭证**，任何能够连接服务的设备都能加入指定房间。
- **仅用于可信网络。** HTTP 不加密；能够监听链路的人可以看到文件、窃取登录后的 Cookie。访问密码只能挡住“直接打开页面的人”，挡不住抓包。默认监听所有 IPv4 网卡；如需限定网卡，用 `--host` 并配置系统防火墙。不要将服务端口暴露到公网。

## 访问密码与其他防护

**访问密码**（可选）：桌面版“设置 → 密码”，或命令行用环境变量（不会出现在进程列表）：

```bash
LANFILES_PASSWORD='你的密码' python3 transfer.py
```

- 新设备首次进入要输入密码；桌面版的二维码和“复制”链接把密码放在 `#k=` 之后，扫码即可进入。`#` 后的内容不会发送到服务器，页面读取后立即从地址栏清除。
- 登录用挑战应答：服务器给出一次性随机数（2 分钟有效），浏览器只发送 `HMAC-SHA256(密码, 随机数)`，密码本身不经过网络。同一 IP 5 分钟内错 5 次（全服务 50 次）返回 `429`。
- 登录后用 Cookie 维持 7 天。修改或首次启用密码会注销全部旧身份，所有设备需重新输入。
- 桌面版把密码保存在 `~/Library/Application Support/LanFiles/settings.json`（权限 0600）。

**其他防护**

- 只接受 IP 地址和 `localhost` 作为访问地址（`Host`），防止 DNS 重绑定把恶意网站指向本服务；如需用其他域名（如 `电脑名.local`）访问，加 `--allow-host 名称`。
- 网页带严格的 `Content-Security-Policy`：只允许页面自带的脚本和样式（按哈希放行），禁止被其他网站嵌入框架；所有响应带 `X-Frame-Options: DENY`、`nosniff` 等安全头。

## 容量和失败处理

| 项目 | 默认限制 |
|---|---:|
| 单文件 | 10 GiB |
| 已保存文件与上传预留空间 | 合计 20 GiB |
| 同时上传 | 4 个，每设备 3 个 |
| 磁盘安全余量 | 1 GiB |
| HTTP 连接 | 64 个 |
| 登记设备 | 256 个，身份到期后回收 |
| 登记传输 | 1,024 个（包括上传中与待删除） |
| 空闲读写超时 | 30 秒 |
| 单次上传最长时间 | 6 小时 |
| 注册及档案修改 | 每 IP 每分钟 10 次，全服务每分钟 100 次 |

网页按入队顺序并行上传多个文件（并发数等于每设备上限），显示实时速度和剩余时间；入队时固定收件人或房间，之后切换选择不会改变已排队文件去向。下载使用 `sendfile` 零拷贝（Windows 自动退回普通发送）。“已上传”表示文件已经完整保存到中转，尚不表示收件人已经下载。

零字节文件允许上传。缺失、重复或非法内容长度、分块请求、短上传、超时和写入失败不会投递残片。超大文件为 `413`，数量或限速为 `429`，配额或磁盘不足为 `507`，连接过载为 `503`。上传失败不自动重发，以免重复投递。

## 保存、恢复和升级

新版中转数据放在所选目录的专用 `.lanfiles/` 子目录：SQLite WAL 数据库保存设备与传输登记，`files/` 存放内容及上传临时文件。该目录属于本工具，勿手动修改数据库或文件。

- 未领取文件保留至上传完成后 1 小时，下载不会自动移除；私发收件人可手动移除。
- 重启会恢复有效身份和未过期文件，清理未完成上传及过期文件。删除失败会保留重试记录，实际删除前继续占用配额。
- 文件正在下载时移除，会立即隐藏并拒绝新下载，已有下载结束后再删除。
- 同一中转目录只允许一个服务实例；如需运行多个实例，使用不同目录；各目录使用独立且持久化的 Cookie 名称，避免同主机不同端口互相覆盖身份。
- 有效 Cookie 保留时，网页会在服务恢复后自动同步。清除 Cookie、凭证过期或改变访问主机/IP 后，可能建立新身份，旧身份的私发文件无法自动转给新身份。恢复时应使用相同主机/IP。
- 首次升级发现旧版根目录中的 UUID 文件时，只提示并原位保留。旧版没有保存收件人，无法自动恢复归属；需要用户手动处理。
- 升级和回滚前备份所用版本及 `.lanfiles` 数据。1.1.0 与 1.2.0 的数据库格式相同；更早的内存登记版本不能读取新版状态，且仍存在旧风险。不要同时运行两个版本。

## 设备管理与传输日志

- 在运行服务的电脑上（桌面版，或本机浏览器访问网页）可以看到所有设备并删除。判断依据是请求来源为回环地址或本机网卡地址，其他设备看不到删除按钮，调用管理接口返回 `403`。
- 删除设备会注销其身份，并清理发给它、尚未下载的私发文件；该设备发出的房间共享保留到过期。在线设备被删除后会以新身份自动重连（名字按 IP 恢复）。
- 设备列表可折叠，网页会记住折叠状态。
- 服务输出（终端或桌面版“日志”）会记录每次上传、下载完成的文件名、设备名、大小、用时和速度，不记录 Cookie 或请求 URL。

## 网页可视化删除

网页的“文件管理”提供“全选可删除文件”和“删除所选（N）”，也可以点每个文件旁的“删除”。单个删除确认文件名，多选只确认一次；删除中转副本不会影响已经下载到设备上的文件。

- “收到的文件”：私发收件人可以下载、选择和删除；其他人发送的广播只有下载入口。
- “我发出的域共享”：列出本浏览器身份发出的所有未清理广播，退出或切换房间后仍能删除。
- 只管理完成上传及等待删除的文件；私发发送者没有撤回收件人文件的权限。
- 删除按固定选择逐个执行。执行期间按钮禁用，后来收到的文件不会被这次批量操作带入。
- 结果分别显示已删除、等待删除和失败数量；失败项目显示原因并保留选择，需要用户明确重试。
- “等待删除”表示仍有下载占用或磁盘删除失败，下载和删除操作禁用，后台会继续重试；重启后也能看到等待状态。
- 身份失效或切换时停止剩余删除，清空旧选择；不会自动重试删除。删除请求绑定确认时的设备 ID，身份不符返回 `403` 和 `code: identity_mismatch`。已被其他标签页删除或过期清理的项目视为操作已完成。

## HTTP API（用于集成）

`POST /api/register` 仅接受 `{ "name": "设备名", "domain": "1234" }`；启用访问密码且没有有效 Cookie 时，还需 `"auth": { "nonce": 随机数, "proof": HMAC-SHA256(密码, 随机数) 的十六进制 }`，缺少时返回 `401` 和 `code: password_required`，错误时为 `bad_password`。已有有效 Cookie 更新当前设备，否则生成新身份；不接受 `device_id`。名称必须为不超过 80 字符的字符串，JSON 最大 16 KiB。

其余接口需要有效 Cookie：

| 接口 | 用途 |
|---|---|
| `GET /api/session` | 当前设备档案和容量限制，不含私密凭证 |
| `GET /api/devices` | 同房间设备列表 |
| `GET /api/inbox` | 当前身份可领取的文件，保持旧接口兼容 |
| `GET /api/files` | 网页管理视图：inbox/outbox 两组，包括本人有权管理的待删除记录 |
| `POST /api/send?to=公开ID&name=文件名` | 私发原始文件字节，需要 Content-Length |
| `POST /api/send?domain=1234&name=文件名` | 房间广播，与 to 二选一 |
| `GET /api/download/传输ID` | 下载，浏览器链接自动带 Cookie |
| `GET /api/auth/challenge` | 无需 Cookie：`required` 表示是否启用访问密码，`nonce` 为一次性随机数 |
| `GET /api/admin/devices` | 本机管理：所有设备（含房间、IP、在线状态） |
| `POST /api/admin/delete-device/设备ID` | 本机管理：删除设备 |
| `POST /api/ack/传输ID` | 移除，`pending` 表示还在等待磁盘删除，重复操作待删除记录仍校验所有者并返回真实状态 |

旧 `from`、`device_id` 查询参数如仍发送，必须与 Cookie 身份一致，否则 `403`；没有有效 Cookie 返回 `401`。浏览器跨站修改请求被拒绝，不开放 CORS。不支持 `Expect: 100-continue`，返回 `417`；直接发送带 Content-Length 的请求即可。

## 检查与构建

```bash
python3 -m unittest test_transfer -v
./build_app.sh
```

测试使用回环地址和临时目录，覆盖正常互传、身份冒用、HTTP 长度边界、短上传、超时、配额竞争、持久化、强制终止恢复、删除重试及连接限制。浏览器验收脚本位于 `qa/browser_acceptance.cjs`，需要本机 Chrome 和 Playwright，仅为开发验证依赖。浏览器测试需要开发环境安装 Playwright 及 Chromium（或已有 Chrome），例如 `npm install --no-save playwright`、`npx playwright install chromium`。用 `LANFILES_PLAYWRIGHT`、`LANFILES_CHROME` 指定安装路径，`LANFILES_PYTHON` 指定测试解释器。`qa/gui_acceptance.py` 验证真实 Tk 控件、二维码、容量参数和完整服务重启。`qa/delete_acceptance.cjs` 验证单个/批量删除、权限、确认取消、部分失败、身份失效和待删除恢复。

连不上时，检查服务仍在运行、设备之间可达、端口和防火墙设置、地址是否属于正确网卡。不要把虚拟网卡地址当作其他设备的连接地址。

## English

A standard-library LAN transfer service with direct delivery and open four-digit room sharing. Browser identity uses a private HttpOnly cookie, while public device IDs carry no authority. SQLite persists sessions and transfers across restarts; completed files expire after one hour. Uploads are streamed, bounded and only published after complete receipt. HTTP is unencrypted: use trusted networks only. The macOS launcher needs Python with modern Tk; the Windows ZIP includes Python and SQLite.

## 作者

作者：GPT、Gemini。项目维护与发布：NaianCN。完整署名见 [AUTHORS.md](AUTHORS.md)。

MIT License — see LICENSE.
