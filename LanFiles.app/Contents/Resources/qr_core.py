#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QR 码生成模块（零第三方依赖）。
优先利用 macOS 内置 CoreImage CIFilter 生成高清 PNG 二维码；
若非 macOS 或系统调用失败，自动降级为内置微型纯 Python QR 矩阵生成器。
"""

import json
import os
import subprocess
import sys
import tempfile
import tkinter as tk

# ---------------------------------------------------------------------------
# macOS CoreImage 生成器
# ---------------------------------------------------------------------------
def _generate_qr_macos(text: str, output_path: str, scale: int = 6) -> bool:
    """利用 macOS JXA / CoreImage CIQRCodeGenerator 滤镜生成高清 PNG。"""
    if sys.platform != "darwin":
        return False

    script = """
ObjC.import('Foundation');
ObjC.import('CoreImage');
ObjC.import('AppKit');

var textStr = %s;
var outPath = %s;
var scaleVal = %d;

var filter = $.CIFilter.filterWithName($('CIQRCodeGenerator'));
filter.setValueForKey($(textStr).dataUsingEncoding($.NSUTF8StringEncoding), $('inputMessage'));
filter.setValueForKey($('M'), $('inputCorrectionLevel'));

var ciImage = filter.outputImage;
var transform = $.CGAffineTransformMakeScale(scaleVal, scaleVal);
ciImage = ciImage.imageByApplyingTransform(transform);

var rep = $.NSBitmapImageRep.alloc.initWithCIImage(ciImage);
var data = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
data.writeToFileAtomically($(outPath), true);
""" % (json.dumps(text), json.dumps(output_path), scale)

    try:
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3
        )
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 纯 Python QR 编码器（备用降级方案，适用于无法调用 CoreImage 的环境）
# ---------------------------------------------------------------------------
class _PurePythonQR:
    """轻量级纯 Python 二维码生成器（支持 Byte 模式与中等纠错，适用于短 URL）。"""

    GF256_EXP = [0] * 512
    GF256_LOG = [0] * 256

    @classmethod
    def _init_gf(cls):
        if cls.GF256_EXP[1] != 0:
            return
        x = 1
        for i in range(255):
            cls.GF256_EXP[i] = x
            cls.GF256_LOG[x] = i
            x <<= 1
            if x & 0x100:
                x ^= 0x11D
        for i in range(255, 512):
            cls.GF256_EXP[i] = cls.GF256_EXP[i - 255]

    @classmethod
    def _gf_mul(cls, x, y):
        if x == 0 or y == 0:
            return 0
        return cls.GF256_EXP[cls.GF256_LOG[x] + cls.GF256_LOG[y]]

    @classmethod
    def _rs_poly(cls, nsym):
        g = [1]
        for i in range(nsym):
            g = cls._poly_mul(g, [1, cls.GF256_EXP[i]])
        return g

    @classmethod
    def _poly_mul(cls, p, q):
        r = [0] * (len(p) + len(q) - 1)
        for j in range(len(q)):
            for i in range(len(p)):
                r[i + j] ^= cls._gf_mul(p[i], q[j])
        return r

    @classmethod
    def _rs_encode(cls, data, nsym):
        cls._init_gf()
        gen = cls._rs_poly(nsym)
        res = list(data) + [0] * nsym
        for i in range(len(data)):
            coef = res[i]
            if coef != 0:
                for j in range(len(gen)):
                    res[i + j] ^= cls._gf_mul(gen[j], coef)
        return res[len(data):]

    # 版本参数配置 (版本, 纠错 M): (总码字, 数据码字, 块数)
    VERSIONS = {
        1: (26, 16, 10, 1),
        2: (44, 28, 16, 1),
        3: (70, 44, 26, 1),
        4: (100, 64, 36, 1),
    }

    @classmethod
    def encode_text(cls, text: str):
        """将文本编码为 0/1 模块二维矩阵。"""
        raw = text.encode("utf-8")
        raw_len = len(raw)

        # 选版本
        chosen_ver = None
        for ver in (1, 2, 3, 4):
            total, data_words, ec_words, blocks = cls.VERSIONS[ver]
            # 8-bit byte 模式：模式指示符(4) + 字符计数(8) + 8*len <= data_words * 8
            max_data = data_words - 2
            if raw_len <= max_data:
                chosen_ver = ver
                break
        if chosen_ver is None:
            chosen_ver = 4

        total, data_words, ec_words, blocks = cls.VERSIONS[chosen_ver]
        bits = []
        # Mode indicator: Byte = 0100
        bits.extend([0, 1, 0, 0])
        # Character count: 8 bits
        for b in f"{raw_len:08b}":
            bits.append(int(b))
        # Data
        for byte in raw:
            for b in f"{byte:08b}":
                bits.append(int(b))
        # Terminator: up to 4 zeroes
        rem = data_words * 8 - len(bits)
        bits.extend([0] * min(4, max(0, rem)))
        # Pad to byte boundary
        while len(bits) % 8 != 0:
            bits.append(0)
        # Pad codewords
        pad_bytes = [0xEC, 0x11]
        pad_idx = 0
        while len(bits) < data_words * 8:
            for b in f"{pad_bytes[pad_idx]:08b}":
                bits.append(int(b))
            pad_idx = (pad_idx + 1) % 2

        # Convert to bytes
        data_bytes = []
        for i in range(0, len(bits), 8):
            chunk = bits[i:i + 8]
            data_bytes.append(int("".join(str(x) for x in chunk), 2))

        # Error correction
        ec_bytes = cls._rs_encode(data_bytes, ec_words)
        all_codewords = data_bytes + ec_bytes

        # Build matrix
        size = 17 + 4 * chosen_ver
        matrix = [[None] * size for _ in range(size)]
        reserved = [[False] * size for _ in range(size)]

        def set_func(r, c, val):
            matrix[r][c] = val
            reserved[r][c] = True

        # Finder patterns
        def finder(top, left):
            for r in range(7):
                for c in range(7):
                    if r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4):
                        set_func(top + r, left + c, 1)
                    else:
                        set_func(top + r, left + c, 0)
            # Separators
            for r in range(-1, 8):
                for c in range(-1, 8):
                    rr, cc = top + r, left + c
                    if 0 <= rr < size and 0 <= cc < size and not reserved[rr][cc]:
                        set_func(rr, cc, 0)

        finder(0, 0)
        finder(0, size - 7)
        finder(size - 7, 0)

        # Timing patterns
        for i in range(8, size - 8):
            if not reserved[6][i]:
                set_func(6, i, 1 if i % 2 == 0 else 0)
            if not reserved[i][6]:
                set_func(i, 6, 1 if i % 2 == 0 else 0)

        # Dark module
        set_func(4 * chosen_ver + 9, 8, 1)

        # Alignment patterns for ver 2, 3, 4
        align_pos = {2: [6, 18], 3: [6, 22], 4: [6, 26]}.get(chosen_ver, [])
        if align_pos:
            for r in align_pos:
                for c in align_pos:
                    if reserved[r][c]:
                        continue
                    for dr in range(-2, 3):
                        for dc in range(-2, 3):
                            val = 1 if max(abs(dr), abs(dc)) in (0, 2) else 0
                            set_func(r + dr, c + dc, val)

        # Reserve format info areas
        for i in range(9):
            if not reserved[8][i]:
                reserved[8][i] = True
            if not reserved[i][8]:
                reserved[i][8] = True
        for i in range(size - 8, size):
            if not reserved[8][i]:
                reserved[8][i] = True
            if not reserved[i][8]:
                reserved[i][8] = True

        # Place data bits
        data_bits = []
        for b in all_codewords:
            for bit_char in f"{b:08b}":
                data_bits.append(int(bit_char))

        bit_idx = 0
        bit_count = len(data_bits)
        row = size - 1
        col = size - 1
        dir_up = True

        while col > 0:
            if col == 6:
                col -= 1
            for _ in range(size):
                for c in (col, col - 1):
                    if not reserved[row][c]:
                        bit_val = data_bits[bit_idx] if bit_idx < bit_count else 0
                        # Mask 0: (row + col) % 2 == 0
                        masked = bit_val ^ (1 if (row + c) % 2 == 0 else 0)
                        matrix[row][c] = masked
                        bit_idx += 1
                row += -1 if dir_up else 1
            dir_up = not dir_up
            row += 1 if dir_up else -1
            col -= 2

        # Format info for Mask 0 and EC M: format bits = 101010000010010
        format_bits = [1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0]
        # Around top-left
        seq1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        for (r, c), bit in zip(seq1, format_bits):
            matrix[r][c] = bit

        # Around other corners
        seq2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
                (size - 5, 8), (size - 6, 8), (size - 7, 8)]
        for (r, c), bit in zip(seq2, format_bits[:7]):
            matrix[r][c] = bit
        seq3 = [(8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
                (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
        for (r, c), bit in zip(seq3, format_bits[7:]):
            matrix[r][c] = bit

        return matrix


def _matrix_to_ppm(matrix, scale=5, margin=2):
    """将矩阵转为 PPM 图片格式字节串（Tkinter PhotoImage 原生可读）。"""
    src_h = len(matrix)
    src_w = len(matrix[0])
    dst_w = (src_w + margin * 2) * scale
    dst_h = (src_h + margin * 2) * scale

    header = f"P6\n{dst_w} {dst_h}\n255\n".encode("ascii")
    black = b"\x00\x00\x00"
    white = b"\xff\xff\xff"

    rows = []
    white_row = white * dst_w

    # Top margin
    for _ in range(margin * scale):
        rows.append(white_row)

    for r in range(src_h):
        line = bytearray()
        line.extend(white * (margin * scale))
        for c in range(src_w):
            color = black if matrix[r][c] == 1 else white
            line.extend(color * scale)
        line.extend(white * (margin * scale))
        line_bytes = bytes(line)
        for _ in range(scale):
            rows.append(line_bytes)

    # Bottom margin
    for _ in range(margin * scale):
        rows.append(white_row)

    return header + b"".join(rows)


# ---------------------------------------------------------------------------
# 对外统一接口
# ---------------------------------------------------------------------------
def generate_qr_photo(master, text: str, target_size: int = 180) -> tk.PhotoImage:
    """生成并返回一个可在 Tkinter 中展示的 PhotoImage 对象。
    
    优先调用 macOS 原生 CoreImage 滤镜输出超清晰 PNG；
    失败时使用内置纯 Python 算法输出 PPM 数据。
    """
    tmp_path = None
    try:
        # 1. 尝试 macOS 原生 CoreImage
        if sys.platform == "darwin":
            # 基础模块约为 25-33 个，比例设为 6-7 即可得到 180-220 像素的高清图
            scale = max(4, target_size // 27)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp_path = tf.name

            if _generate_qr_macos(text, tmp_path, scale=scale):
                img = tk.PhotoImage(file=tmp_path, master=master)
                return img
    except Exception:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # 2. 纯 Python 降级生成 PPM
    try:
        matrix = _PurePythonQR.encode_text(text)
        src_size = len(matrix) + 4  # 加上左右 margin
        scale = max(2, target_size // src_size)
        ppm_data = _matrix_to_ppm(matrix, scale=scale, margin=2)
        img = tk.PhotoImage(data=ppm_data, master=master)
        return img
    except Exception as e:
        # 兜底：返回空图避免崩溃
        print(f"[QR] 生成二维码失败: {e}", file=sys.stderr)
        return None
