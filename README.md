# 局域网传文件（LanFiles）

当前版本：**1.4.1** · 作者：**GPT、Gemini、Claude** · 项目维护：**NaianCN**

[Windows 免安装包](https://raw.githubusercontent.com/NaianCN/LanFiles/main/lanfiles-win.zip) · [macOS 应用包](https://raw.githubusercontent.com/NaianCN/LanFiles/main/lanfiles-macos.zip) · [更新记录](CHANGELOG.md)

在一台电脑上启动服务，同一局域网里的手机、平板和电脑用浏览器打开地址，就能互相发文件：可以私发给某台设备，也可以发到四位数房间里共享。只用 Python 标准库，支持 macOS、Windows 和 Linux，不需要安装任何第三方包。

## 快速开始

| 平台 | 做法 |
|---|---|
| macOS | 解压 `lanfiles-macos.zip`，打开 `LanFiles.app`，用手机扫窗口里的二维码 |
| Windows | 解压 `lanfiles-win.zip`，双击 `start.bat`（已自带 Python 和 SQLite） |
| 任意平台 | 在源码目录运行 `python3 transfer.py` |

启动后终端会显示形如 `http://192.168.x.x:8000` 的地址，在其他设备的浏览器里打开即可。

```bash
python3 transfer.py
```

默认监听 `0.0.0.0:8000`，文件中转到 `~/Downloads/lanfiles`；这个目录不可写时，依次尝试 `~/lanfiles` 和系统临时目录。常用参数：

```bash
python3 transfer.py --host 192.168.5.10 --port 9000 --dir ~/lanfiles \
  --max-file-size 10GiB --spool-quota 20GiB --max-uploads 4 --uploads-per-device 3 --min-free-space 1GiB
```

- 容量参数可写字节数，也可带 `KiB`/`MiB`/`GiB`；同时上传数（总数和每设备）必须大于零。
- 端口被占用、目录已有实例在运行或数据库损坏时，程序会说明原因并退出，不会带病启动。
- 桌面版也可以在源码目录运行 `python3 app.py`，需要带 Tk 8.6 以上的 Python，同样支持上面的容量参数。
- Windows 版的参数直接加在批处理后面，例如 `start.bat --port 9000`。

## 设备与房间

- **身份**：打开网页就自动加入。服务端给每个浏览器分配一个公开的设备 ID 和一个私密凭证；别人只能看到 ID，拿它也冒充不了你。
- **名字**：首次打开随机取名，可以直接改，或点 🎲 换一个。名字存在浏览器里，下次打开照旧；服务端还会按 IP 记住最近用的名字 30 天，所以清了浏览数据、换了浏览器，只要 IP 没变，名字就会自动恢复。建议在路由器里给常用设备固定 IP。
- **会话**：私密凭证只放在 Cookie 里（`HttpOnly; SameSite=Strict; Path=/`），有效 7 天。数据库只存摘要，凭证不会出现在网页、JSON、下载链接或日志里。同一浏览器的多个标签页共用一个身份。
- **私发**：只有收件人能下载和删除。
- **房间**：房间内的成员都能下载广播文件，只有发送者能删除。四位房间号只是分组，**不是密码**，任何连得上服务的设备都能进任何房间。

## 安全

> **只在可信网络里使用。** 传输走 HTTP，不加密。同一网络里能抓包的人可以看到文件内容，也能偷到登录后的 Cookie。访问密码只能挡住"直接打开网页的人"，挡不住抓包。不要把服务端口暴露到公网；默认监听所有 IPv4 网卡，需要限定时用 `--host` 并配合系统防火墙。

### 访问密码（可选）

在桌面版"设置 → 密码"里设置；命令行用环境变量传入，这样密码不会出现在进程列表里：

```bash
LANFILES_PASSWORD='你的密码' python3 transfer.py
```

- 新设备第一次进入要输入密码。桌面版的二维码和"复制"链接会把密码放在 `#k=` 后面，扫码直接进入；`#` 后的内容不会发给服务器，页面读取后立刻从地址栏抹掉。
- 登录用挑战应答：服务器发一个一次性随机数（2 分钟内有效），浏览器只回传 `HMAC-SHA256(密码, 随机数)`，密码本身从不经过网络。
- 同一 IP 5 分钟内输错 5 次（全服务累计 50 次）会被暂时拒绝，返回 `429`。
- 登录后 Cookie 保持 7 天。修改密码或首次启用密码时，所有旧身份都会注销，各设备需要重新输入。
- 桌面版把密码保存在 `~/Library/Application Support/LanFiles/settings.json`，只有本人可读（权限 0600）。

### 其他防护

- **访问地址白名单**：只接受 IP 地址和 `localhost`，防止恶意网站通过 DNS 重绑定访问本服务。想用 `电脑名.local` 之类的域名访问，启动时加 `--allow-host 名称`。
- **内容安全策略**：网页只运行自带的脚本和样式（按哈希放行），不能被其他网站嵌进框架；所有响应都带 `X-Frame-Options: DENY`、`nosniff` 等安全头。
- **跨站请求**：拒绝来自其他网站的修改请求，不开放 CORS。

## 传输

| 项目 | 默认限制 |
|---|---:|
| 单文件 | 10 GiB |
| 已保存文件与上传预留空间 | 合计 20 GiB |
| 同时上传 | 4 个，每设备 3 个 |
| 磁盘安全余量 | 1 GiB |
| HTTP 连接 | 64 个 |
| 登记设备 | 256 个，身份到期后回收 |
| 登记传输 | 1,024 个（含上传中与待删除） |
| 空闲读写超时 | 30 秒 |
| 单次上传最长时间 | 6 小时 |
| 注册及改名 | 每 IP 每分钟 10 次，全服务每分钟 100 次 |

- 网页按排队顺序并行上传，并发数等于每设备上限，实时显示速度和剩余时间。文件入队时就确定了收件人或房间，之后再切换选择不会影响已排队的文件。
- 下载使用 `sendfile` 零拷贝，Windows 上自动改用普通发送。
- "已上传"表示文件已完整存到中转区，不代表对方已经下载。
- 零字节文件可以上传。长度缺失或有误、分块编码、上传中断、超时和写盘失败都不会投递残缺文件。
- 出错时的状态码：文件过大 `413`，数量超限或请求过快 `429`，配额或磁盘不足 `507`，连接过多 `503`。上传失败不会自动重发，以免重复投递。

## 设备管理与日志

- 在运行服务的电脑上（桌面版，或本机浏览器打开网页）可以看到所有设备并删除。只认请求是否来自回环地址或本机网卡地址；其他设备看不到删除按钮，直接调用管理接口会得到 `403`。
- 删除设备会注销它的身份，并清掉发给它、还没下载的私发文件；它发到房间里的文件保留到过期。被删的在线设备会以新身份自动重连，名字按 IP 恢复。
- 设备列表可以折叠，网页会记住折叠状态。
- 服务输出（终端或桌面版的"日志"）会记录每次上传、下载的文件名、设备、大小、用时和速度，不记录 Cookie 和请求地址。

## 删除文件

网页的"文件管理"里可以单个删除，也可以"全选可删除文件"后"删除所选（N）"。单个删除会确认文件名，批量只确认一次。删除的只是中转区里的副本，不影响已经下载到设备上的文件。

- **收到的文件**：私发收件人可以下载和删除；别人发到房间的文件只能下载。
- **我发出的域共享**：列出你发到房间里、还没清理的文件，退出或换了房间也能删除。
- 私发的发送者不能撤回已发给对方的文件。
- 批量删除按确认时选中的文件逐个执行，期间按钮禁用，中途新收到的文件不会被带进去。
- 结果会分别列出已删除、等待删除和失败的数量。失败项显示原因并保持选中，需要你手动重试。
- "等待删除"表示文件还在被下载或磁盘删除失败，此时下载和删除按钮禁用，后台会继续重试，重启后状态依然保留。
- 身份失效或切换时，剩余的删除会停止，选择会清空，不会自动重试。删除请求绑定确认时的设备 ID，身份不符返回 `403` 和 `code: identity_mismatch`。已被其他标签页删除或已过期清理的文件视为删除成功。

## 保存、恢复与升级

数据都放在中转目录下的 `.lanfiles/` 子目录：SQLite（WAL 模式）数据库记录设备和传输，`files/` 存放文件和上传中的临时文件。这个目录归程序管理，请不要手动修改。

- 没人领取的文件在上传完成 1 小时后清理；下载不会自动删除，私发收件人可以手动删。
- 重启后恢复有效身份和未过期的文件，清理没传完的上传和过期文件。删除失败的文件会记下来重试，真正删掉之前仍占用配额。
- 正在被下载的文件被删除时，会立即隐藏并拒绝新的下载，等已有下载结束再真正删除。
- 一个中转目录只能运行一个实例；要运行多个，请使用不同目录。每个目录有自己独立的 Cookie 名，同一台主机不同端口的实例不会互相顶掉身份。
- Cookie 还在时，服务恢复后网页会自动重新同步。清了 Cookie、凭证过期或换了访问地址，就会变成新身份，旧身份的私发文件不会自动转过来，所以请尽量用同一个地址访问。
- 升级时如果在中转目录根部发现旧版留下的 UUID 文件，只会提示并原地保留。旧版没有记录收件人，无法自动恢复归属，需要手动处理。
- 升级或回滚前，请备份程序和 `.lanfiles` 数据。1.1.0 与 1.2.0 的数据库格式相同；更早的内存登记版本读不了新版数据，而且存在旧的安全问题。不要同时运行两个版本。

## HTTP 接口

`POST /api/register` 只接受 `{ "name": "设备名", "domain": "1234" }`。已有有效 Cookie 时更新当前设备，否则新建身份；不接受客户端指定 `device_id`。名字为不超过 80 字符的字符串，JSON 最大 16 KiB。

启用访问密码且没有有效 Cookie 时，还要带上 `"auth": { "nonce": 随机数, "proof": HMAC-SHA256(密码, 随机数) 的十六进制 }`；缺少时返回 `401` 和 `code: password_required`，密码错误时为 `bad_password`。

除注册和 `/api/auth/challenge` 外，其余接口都需要有效 Cookie：

| 接口 | 用途 |
|---|---|
| `GET /api/auth/challenge` | 无需 Cookie。`required` 表示是否启用密码，`nonce` 为一次性随机数 |
| `GET /api/session` | 当前设备信息和容量限制，不含私密凭证 |
| `GET /api/devices` | 同房间的设备列表 |
| `GET /api/inbox` | 当前身份可领取的文件（兼容旧接口） |
| `GET /api/files` | 文件管理视图：收件和发件两组，含本人可管理的待删除项 |
| `POST /api/send?to=公开ID&name=文件名` | 私发，请求体为文件原始字节，需要 Content-Length |
| `POST /api/send?domain=1234&name=文件名` | 发到房间，与 `to` 二选一 |
| `GET /api/download/传输ID` | 下载，浏览器链接会自动带上 Cookie |
| `POST /api/ack/传输ID` | 删除；`pending` 表示还在等待磁盘删除。对待删除项重复操作仍会校验所有者并返回真实状态 |
| `GET /api/admin/devices` | 仅本机：所有设备（含房间、IP、在线状态） |
| `POST /api/admin/delete-device/设备ID` | 仅本机：删除设备 |

- 旧的 `from`、`device_id` 查询参数如果还在发送，必须和 Cookie 身份一致，否则返回 `403`；没有有效 Cookie 返回 `401`。
- 不支持 `Expect: 100-continue`（返回 `417`），直接发送带 Content-Length 的请求即可。

## 开发与测试

```bash
python3 -m unittest test_transfer -v
./build_app.sh
```

- 单元测试只用回环地址和临时目录，覆盖正常互传、身份冒用、HTTP 长度边界、上传中断、超时、配额竞争、持久化、强制终止后恢复、删除重试、连接限制、访问密码和安全响应头。
- `qa/gui_acceptance.py`：验证真实的 Tk 界面、二维码、容量参数和完整的服务重启。
- `qa/browser_acceptance.cjs`、`qa/delete_acceptance.cjs`：浏览器端验收，后者覆盖单个与批量删除、权限、取消确认、部分失败、身份失效和待删除恢复。需要 Playwright 和 Chromium（或本机 Chrome），仅开发时使用：

```bash
npm install --no-save playwright
npx playwright install chromium
```

可用 `LANFILES_PLAYWRIGHT`、`LANFILES_CHROME` 指定安装路径，用 `LANFILES_PYTHON` 指定测试用的 Python。

## 连不上怎么办

1. 确认服务还在运行：终端没有退出，或桌面版显示"运行中"。
2. 确认两台设备在同一网段，用的是终端显示的 `http://` 地址，而不是虚拟网卡的地址。
3. 检查防火墙是否放行了程序和端口（Windows 首次运行时勾选"专用网络"）。
4. 启用了访问密码时，确认输入的密码正确，或直接扫桌面版的二维码。

## English

LanFiles is a zero-dependency LAN file transfer tool built on the Python standard library. Start it on one computer and open the printed address in any browser on the same network to send files directly to a device or share them in a four-digit room. Identity uses a private HttpOnly cookie, so public device IDs grant no authority; an optional access password uses challenge–response login, so the password never crosses the network. SQLite keeps sessions and transfers across restarts, and finished files expire after one hour. Uploads are streamed, size-bounded and only delivered once fully received. Traffic is plain HTTP, so use trusted networks only. The macOS app needs Python with Tk 8.6+; the Windows ZIP bundles Python and SQLite.

## 作者

作者：GPT、Gemini、Claude。项目维护与发布：NaianCN。完整署名见 [AUTHORS.md](AUTHORS.md)。

MIT License，见 [LICENSE](LICENSE)。
