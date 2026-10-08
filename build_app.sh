#!/bin/bash
# ==============================================================================
# LanFiles (局域网传文件) macOS 桌面 App 打包脚本
# 自动生成原生 .icns 高清图标与标准 macOS .app 应用程序包
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="LanFiles.app"
FINAL_BUNDLE_DIR="$SCRIPT_DIR/$APP_NAME"
BUILD_TMP="$(mktemp -d "${TMPDIR:-/tmp}/lanfiles-build.XXXXXX")"
trap 'rm -rf "$BUILD_TMP"' EXIT
BUNDLE_DIR="$BUILD_TMP/$APP_NAME"
CONTENTS_DIR="$BUNDLE_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
ICONSET_DIR="$BUILD_TMP/AppIcon.iconset"
ICON_SVG="$BUILD_TMP/icon.svg"

echo "==> 1. 准备临时构建目录（旧应用保持可用）..."
rm -rf "$BUNDLE_DIR"
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"
mkdir -p "$MACOS_DIR"
mkdir -p "$RESOURCES_DIR"

echo "==> 2. 准备 macOS 应用图标..."
if [ -f "$SCRIPT_DIR/assets/AppIcon.icns" ]; then
    cp "$SCRIPT_DIR/assets/AppIcon.icns" "$RESOURCES_DIR/AppIcon.icns"
else
cat << 'EOF' > "$ICON_SVG"
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#2196F3"/>
      <stop offset="100%" stop-color="#0056D2"/>
    </linearGradient>
    <filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="24" stdDeviation="30" flood-opacity="0.25"/>
    </filter>
  </defs>

  <!-- macOS 规范圆角外框 -->
  <rect x="64" y="64" width="896" height="896" rx="200" fill="url(#bgGrad)" filter="url(#shadow)"/>

  <!-- 发光内圆 -->
  <circle cx="512" cy="512" r="320" fill="none" stroke="#FFFFFF" stroke-width="20" stroke-dasharray="16 24" opacity="0.4"/>
  <circle cx="512" cy="512" r="230" fill="none" stroke="#FFFFFF" stroke-width="26" opacity="0.6"/>

  <!-- 文件折角图标 -->
  <path d="M372 260 H552 L672 380 V740 H372 Z" fill="#FFFFFF" opacity="0.95"/>
  <path d="M552 260 V380 H672 Z" fill="#D0E4FF"/>

  <!-- 传输箭头符号 (双向互传) -->
  <path d="M512 440 L512 630" stroke="#0066FF" stroke-width="36" stroke-linecap="round"/>
  <path d="M440 512 L512 440 L584 512" stroke="#0066FF" stroke-width="36" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
  
  <!-- Wi-Fi / 局域网扩散波纹 -->
  <path d="M392 790 C430 760, 594 760, 632 790" stroke="#FFFFFF" stroke-width="28" stroke-linecap="round" fill="none"/>
  <circle cx="512" cy="830" r="18" fill="#FFFFFF"/>
</svg>
EOF

# 使用 sips 生成多规格 PNG
sips -s format png "$ICON_SVG" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null 2>&1
sips -z 512 512 "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null 2>&1
sips -z 512 512 "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null 2>&1
sips -z 256 256 "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null 2>&1
sips -z 256 256 "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null 2>&1
sips -z 128 128 "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null 2>&1
sips -z 64 64   "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null 2>&1
sips -z 32 32   "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null 2>&1
sips -z 32 32   "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null 2>&1
sips -z 16 16   "$ICONSET_DIR/icon_512x512@2x.png" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null 2>&1

iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES_DIR/AppIcon.icns"
rm -rf "$ICONSET_DIR" "$ICON_SVG"
fi

echo "==> 3. 复制核心程序源码到 App Resources..."
cp "$SCRIPT_DIR/app.py" "$RESOURCES_DIR/app.py"
cp "$SCRIPT_DIR/qr_core.py" "$RESOURCES_DIR/qr_core.py"
cp "$SCRIPT_DIR/transfer.py" "$RESOURCES_DIR/transfer.py"
cp "$SCRIPT_DIR/LICENSE" "$RESOURCES_DIR/LICENSE"
cp "$SCRIPT_DIR/AUTHORS.md" "$RESOURCES_DIR/AUTHORS.md"

echo "==> 4. 生成 Info.plist 元数据..."
cat << 'EOF' > "$CONTENTS_DIR/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleDisplayName</key>
    <string>局域网传文件</string>
    <key>CFBundleExecutable</key>
    <string>LanFiles</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.lanfiles.macos</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>LanFiles</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.2.0</string>
    <key>CFBundleVersion</key>
    <string>1.2.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
</dict>
</plist>
EOF

echo "==> 5. 生成启动引导脚本..."
cat << 'EOF' > "$MACOS_DIR/LanFiles"
#!/bin/bash
# LanFiles 启动引导：严格选择具备现代图形渲染 (Tk >= 8.6 / 9.0) 的 Python 3 解释器

check_python() {
    local bin="$1"
    if [ -x "$bin" ]; then
        if "$bin" -c "import tkinter as tk; exit(0 if tk.TkVersion >= 8.6 else 1)" 2>/dev/null; then
            echo "$bin"
            return 0
        fi
    fi
    return 1
}

PYTHON_BIN=""

# 1. 优先检查官方 Python.framework (3.14, 3.13, 3.12, 3.11, Current)
for p in \
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"; do
    if [ -x "$p" ]; then
        FOUND="$(check_python "$p")"
        if [ -n "$FOUND" ]; then
            PYTHON_BIN="$FOUND"
            break
        fi
    fi
done

# 2. 检查 Homebrew 与常见本地安装路径
if [ -z "$PYTHON_BIN" ]; then
    for p in \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3"; do
        if [ -x "$p" ]; then
            FOUND="$(check_python "$p")"
            if [ -n "$FOUND" ]; then
                PYTHON_BIN="$FOUND"
                break
            fi
        fi
    done
fi

# 3. 检查系统 PATH 中的 python3 (同样严格过滤，拒绝已被系统弃用且无法渲染文字的 Tk 8.5)
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        FOUND="$(check_python "$(command -v python3)")"
        if [ -n "$FOUND" ]; then
            PYTHON_BIN="$FOUND"
        fi
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    osascript -e 'display alert "未检测到支持现代界面的 Python 3" message "macOS 自带的 Python 3.9 (Tk 8.5) 已被系统废弃且无法正常渲染文字。请安装官方 Python 3.11+ 或通过 Homebrew 安装 Python。"'
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
export TK_SILENCE_DEPRECATION=1
exec "$PYTHON_BIN" "$APP_DIR/app.py" "$@"
EOF

chmod +x "$MACOS_DIR/LanFiles"

# 只有所有文件准备完毕后才替换交付包。
rm -rf "$FINAL_BUNDLE_DIR"
mv "$BUNDLE_DIR" "$FINAL_BUNDLE_DIR"
echo "=========================================================="
echo "  构建成功！"
echo "  应用位置: $FINAL_BUNDLE_DIR"
echo "  运行方式: 双击打开，或终端执行 open $APP_NAME"
echo "=========================================================="
