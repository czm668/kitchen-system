# -*- coding: utf-8 -*-
"""二维码编码（A4-08 溯源标签）：byte 模式 / 纠错等级 M / 版本 1~10，纯标准库。

## 为什么自己写，而不是装个库

`qrcode` / `segno` 都要联网 pip 安装，而本工程运行在**离线便携 Python** 上
（`D:/liulan1/毕设/.local/python311`，pip 装包还得先绕过 `TEMP` 是 8.3 短名
不可写的坑）。溯源标签是现场打印功能：打印失败等于整批菜查不到来源，
不能依赖「先联网装个包」这个前提。所以这里只实现**用得到的那一小块**：

| 项 | 取值 | 理由 |
|---|---|---|
| 模式 | byte（UTF-8） | 溯源码/URL 是 ASCII+中文混排，byte 模式通吃 |
| 纠错 | M（约 15%） | L 太脆（标签沾油就废），Q/H 让码变密（标签只有 3cm） |
| 版本 | 1~10（最大 213 字节） | 一条溯源 URL 约 40~60 字节，10 版有 3 倍余量 |

**正确性靠基准比对**：实现完成后用 `segno` 逐 mask（0~7）比对矩阵，把结果
固化成 `tests/fixtures/qr_golden.json`（`segno` 只在生成基准时用过，不进依赖）。
单测跑的是基准哈希，不跑 `segno`。

## 一张图看懂流水线

```
文本 → 选版本 → 拼比特流(mode+count+data+pad) → 分块 RS 纠错 → 交织
     → 铺功能图形(定位/定时/校正/格式/版本) → 填数据(zigzag) → 选 mask → 输出
```

## 三处容易写错的地方

1. **数据填充的走位**：从右下角起、两列一组、上下往返，跳过第 6 列（定时图形）。
2. **纠错块交织**：组 1（少 1 个数据码字）必须先填完再轮到组 2 的额外那个，
   否则读码器解出来的是一堆乱码——而且**扫描器不报错，只是解不出来**。
3. **格式信息要写两份**（左上 + 右上/左下），且固定异或 `0x5412`；
   漏一份时大部分手机仍能扫，只有廉价扫码枪会失败，属于最难查的一类 bug。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

__all__ = [
    "QrError", "QrCode", "encode", "to_png", "to_ascii",
    "BYTE_CAPACITY_M", "ECC_LEVEL_M_BITS",
]

#: 纠错等级 M 的格式信息位（ISO/IEC 18004 表 12：L=01 M=00 Q=11 H=10）
ECC_LEVEL_M_BITS = 0b00

#: 纠错等级 M 下 byte 模式的容量（字节），下标=版本号
BYTE_CAPACITY_M: dict[int, int] = {
    1: 14, 2: 26, 3: 42, 4: 62, 5: 84,
    6: 106, 7: 122, 8: 152, 9: 180, 10: 213,
}

#: 版本 → (每块纠错码字数, [(块数, 每块数据码字数), ...])
#: 数据来自 ISO/IEC 18004 表 13-22（等级 M）。校验方式：块数×(数据+纠错) 必须
#: 等于该版本的总码字数（v1=26 / v2=44 / v3=70 / v4=100 / v5=134 / v6=172 /
#: v7=196 / v8=242 / v9=292 / v10=346），`_self_check_tables()` 会跑一遍。
_ECC_M: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {
    1: (10, ((1, 16),)),
    2: (16, ((1, 28),)),
    3: (26, ((1, 44),)),
    4: (18, ((2, 32),)),
    5: (24, ((2, 43),)),
    6: (16, ((4, 27),)),
    7: (18, ((4, 31),)),
    8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}

#: 版本 → 总码字数（用于自检）
_TOTAL_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196,
                    8: 242, 9: 292, 10: 346}

#: 版本 → 校正图形中心坐标（v1 无校正图形）
_ALIGN_POS: dict[int, tuple[int, ...]] = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30),
    6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}


class QrError(ValueError):
    """内容超出可编码范围，或参数非法。"""


def _self_check_tables() -> None:
    for version, (ec_per_block, groups) in _ECC_M.items():
        total = sum(blocks * (data + ec_per_block) for blocks, data in groups)
        if total != _TOTAL_CODEWORDS[version]:
            raise AssertionError(
                f"v{version} 码字数不符：表内 {total}，应为 {_TOTAL_CODEWORDS[version]}"
            )


_self_check_tables()


# ---------------------------------------------------------------- GF(256)
def _gf_mul(x: int, y: int) -> int:
    """GF(2^8) 乘法，本原多项式 0x11D（QR 规定）。"""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    """生成多项式 (x-α^0)(x-α^1)...(x-α^(degree-1)) 的系数。"""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    """多项式除法取余——就是纠错码字。"""
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# ---------------------------------------------------------------- 编码
@dataclass(frozen=True)
class QrCode:
    """编码结果：`modules[row][col] is True` 表示黑块（**不含静区**）。"""

    version: int
    mask: int
    size: int
    modules: tuple[tuple[bool, ...], ...]

    def rows_text(self) -> list[str]:
        return ["".join("1" if cell else "0" for cell in row) for row in self.modules]

    def hex_digest(self) -> str:
        """矩阵指纹（单测基准用）：把矩阵按行拼串后取 sha256。"""
        import hashlib

        return hashlib.sha256("".join(self.rows_text()).encode("ascii")).hexdigest()


def _pick_version(length: int) -> int:
    for version in sorted(BYTE_CAPACITY_M):
        if length <= BYTE_CAPACITY_M[version]:
            return version
    raise QrError(
        f"内容 {length} 字节，超出等级 M 的 v10 上限（{BYTE_CAPACITY_M[10]} 字节）；"
        "溯源标签请只放短链或溯源码本体"
    )


def _bit_stream(payload: bytes, version: int) -> list[int]:
    """mode(4) + 字符计数(8/16) + 数据 + 终止符 + 补齐到字节 + 填充码字。"""
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for i in reversed(range(width)):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                                  # byte 模式
    push(len(payload), 8 if version <= 9 else 16)     # v10+ 计数位宽翻倍
    for byte in payload:
        push(byte, 8)

    data_capacity = sum(blocks * data for blocks, data in _ECC_M[version][1]) * 8
    if len(bits) > data_capacity:
        raise QrError(f"v{version} 放不下 {len(bits)} 比特（上限 {data_capacity}）")
    bits.extend([0] * min(4, data_capacity - len(bits)))   # 终止符最多 4 位
    bits.extend([0] * (-len(bits) % 8))                    # 补齐到整字节
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # 交替填充 0xEC / 0x11：ISO 规定的两个填充码字，用别的值也能扫，
    # 但某些解码器会拿它做「版本自检」，用错会偶发失败
    pad = (0xEC, 0x11)
    index = 0
    while len(codewords) * 8 < data_capacity:
        codewords.append(pad[index % 2])
        index += 1
    return codewords


def _interleave(codewords: list[int], version: int) -> list[int]:
    """分块算纠错码，再按 ISO 规则交织。"""
    ec_per_block, groups = _ECC_M[version]
    divisor = _rs_divisor(ec_per_block)
    blocks: list[tuple[list[int], list[int]]] = []
    cursor = 0
    for block_count, data_len in groups:
        for _ in range(block_count):
            chunk = codewords[cursor:cursor + data_len]
            cursor += data_len
            if len(chunk) != data_len:
                raise QrError(f"v{version} 数据码字不足：需要 {data_len}，实际 {len(chunk)}")
            blocks.append((chunk, _rs_remainder(chunk, divisor)))

    out: list[int] = []
    max_data = max(len(data) for data, _ec in blocks)
    for index in range(max_data):
        for data, _ec in blocks:
            if index < len(data):
                out.append(data[index])
    for index in range(ec_per_block):
        for _data, ec in blocks:
            out.append(ec[index])
    return out


def _format_bits(mask: int) -> int:
    """15 位格式信息：5 位数据 + BCH(15,5) 纠错，最后异或 0x5412。"""
    data = (ECC_LEVEL_M_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    """18 位版本信息（v7+ 才写）：6 位数据 + BCH(18,6)。"""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


class _Builder:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    # --- 功能图形 ---
    def set_func(self, row: int, col: int, dark: bool) -> None:
        self.modules[row][col] = dark
        self.func[row][col] = True

    def _finder(self, row: int, col: int) -> None:
        """7×7 定位图形 + 1 格白色分隔带（越界部分自动裁掉）。"""
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                r, c = row + dy, col + dx
                if not (0 <= r < self.size and 0 <= c < self.size):
                    continue
                dist = max(abs(dy), abs(dx))
                self.set_func(r, c, dist != 2 and dist != 4)

    def _alignment(self, row: int, col: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set_func(row + dy, col + dx, max(abs(dy), abs(dx)) != 1)

    def draw_functions(self) -> None:
        size = self.size
        # 定时图形（第 6 行 / 第 6 列，黑白交替），放在定位图形之前画也不冲突
        for i in range(size):
            if not self.func[6][i]:
                self.set_func(6, i, i % 2 == 0)
            if not self.func[i][6]:
                self.set_func(i, 6, i % 2 == 0)

        self._finder(3, 3)
        self._finder(3, size - 4)
        self._finder(size - 4, 3)

        positions = _ALIGN_POS[self.version]
        last = len(positions) - 1
        for i, row in enumerate(positions):
            for j, col in enumerate(positions):
                # 三个角上的校正图形与定位图形重叠，跳过
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                self._alignment(row, col)

        # 预留格式信息区（先画占位，避免数据填进去）
        for i in range(9):
            if not self.func[8][i]:
                self.set_func(8, i, False)
            if not self.func[i][8]:
                self.set_func(i, 8, False)
        for i in range(8):
            self.set_func(8, size - 1 - i, False)
        for i in range(7):
            self.set_func(size - 1 - i, 8, False)
        # 固定黑块（第 8 列倒数第 8 行）**必须在这里预留**：它虽然恒为深色，
        # 但也在数据走位路径上。放到 draw_format 里才置位，会让它先被写入 1 个
        # 数据比特、随后被覆盖——此后全部数据比特整体错位 1 位。矩阵看着「很正常」，
        # 扫出来却是乱码，属于最难查的一类。
        self.set_func(size - 8, 8, True)

        if self.version >= 7:
            bits = _version_bits(self.version)
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a = size - 11 + i % 3
                b = i // 3
                self.set_func(b, a, bit)
                self.set_func(a, b, bit)

    # --- 数据 ---
    def draw_codewords(self, codewords: list[int]) -> None:
        index = 0
        total_bits = len(codewords) * 8
        right = self.size - 1
        while right >= 1:
            if right == 6:          # 第 6 列是定时图形，整列跳过
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    col = right - j
                    upward = ((right + 1) & 2) == 0
                    row = (self.size - 1 - vert) if upward else vert
                    if self.func[row][col] or index >= total_bits:
                        continue
                    self.modules[row][col] = (
                        (codewords[index >> 3] >> (7 - (index & 7))) & 1
                    ) == 1
                    index += 1
            right -= 2
        if index != total_bits:
            raise QrError(f"数据未填完：{index}/{total_bits} 比特（版本表可能算错）")

    # --- 掩码 ---
    @staticmethod
    def mask_hit(mask: int, row: int, col: int) -> bool:
        if mask == 0:
            return (row + col) % 2 == 0
        if mask == 1:
            return row % 2 == 0
        if mask == 2:
            return col % 3 == 0
        if mask == 3:
            return (row + col) % 3 == 0
        if mask == 4:
            return (row // 2 + col // 3) % 2 == 0
        if mask == 5:
            return (row * col) % 2 + (row * col) % 3 == 0
        if mask == 6:
            return ((row * col) % 2 + (row * col) % 3) % 2 == 0
        if mask == 7:
            return ((row + col) % 2 + (row * col) % 3) % 2 == 0
        raise QrError(f"mask 只能是 0~7，实际 {mask}")

    def apply_mask(self, mask: int) -> None:
        for row in range(self.size):
            for col in range(self.size):
                if not self.func[row][col] and self.mask_hit(mask, row, col):
                    self.modules[row][col] = not self.modules[row][col]

    def draw_format(self, mask: int) -> None:
        bits = _format_bits(mask)
        size = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        # 第一份：左上角，沿第 8 列向上、再沿第 8 行向左
        for i in range(6):
            self.set_func(i, 8, bit(i))
        self.set_func(7, 8, bit(6))
        self.set_func(8, 8, bit(7))
        self.set_func(8, 7, bit(8))
        for i in range(9, 15):
            self.set_func(8, 14 - i, bit(i))
        # 第二份：右上 / 左下
        for i in range(8):
            self.set_func(8, size - 1 - i, bit(i))
        for i in range(8, 15):
            self.set_func(size - 15 + i, 8, bit(i))
        # 固定黑块（尺寸 21 起都固定深色）
        self.set_func(size - 8, 8, True)

    # --- 惩罚分 ---
    def penalty(self) -> int:
        size = self.size
        grid = [[1 if cell else 0 for cell in row] for row in self.modules]
        score = 0

        def line_score(seq: list[int]) -> int:
            total = 0
            run_color = seq[0]
            run_len = 1
            for value in seq[1:]:
                if value == run_color:
                    run_len += 1
                else:
                    if run_len >= 5:
                        total += 3 + (run_len - 5)
                    run_color, run_len = value, 1
            if run_len >= 5:
                total += 3 + (run_len - 5)
            return total

        for i in range(size):
            score += line_score(grid[i])
            score += line_score([grid[r][i] for r in range(size)])

        for row in range(size - 1):
            for col in range(size - 1):
                block = grid[row][col] + grid[row][col + 1] + \
                    grid[row + 1][col] + grid[row + 1][col + 1]
                if block in (0, 4):
                    score += 3

        # 类定位图形（1:1:3:1:1 且一侧有 4 格浅色）
        pattern = (1, 0, 1, 1, 1, 0, 1)
        for i in range(size):
            row_seq = [grid[i][c] for c in range(size)]
            col_seq = [grid[r][i] for r in range(size)]
            for seq in (row_seq, col_seq):
                for start in range(size - 6):
                    if tuple(seq[start:start + 7]) != pattern:
                        continue
                    before = seq[max(0, start - 4):start]
                    after = seq[start + 7:start + 11]
                    if (len(before) == 4 and not any(before)) or \
                            (len(after) == 4 and not any(after)):
                        score += 40

        dark = sum(sum(row) for row in grid)
        total = size * size
        k = abs(dark * 20 - total * 10) // total      # 偏离 50% 的 5% 档数
        score += k * 10
        return score


def encode(text: str, *, mask: int | None = None, version: int | None = None) -> QrCode:
    """把文本编成二维码矩阵。

    `mask`/`version` 只在单测与基准比对时显式指定；业务侧一律自动选。
    """
    payload = text.encode("utf-8")
    if not payload:
        raise QrError("内容为空，生成不了二维码")
    chosen = int(version) if version is not None else _pick_version(len(payload))
    if chosen not in _ECC_M:
        raise QrError(f"只支持版本 1~10，实际 {chosen}")
    if len(payload) > BYTE_CAPACITY_M[chosen]:
        raise QrError(
            f"内容 {len(payload)} 字节超出 v{chosen} 的容量 {BYTE_CAPACITY_M[chosen]} 字节"
        )

    codewords = _interleave(_bit_stream(payload, chosen), chosen)

    def build(mask_id: int) -> _Builder:
        builder = _Builder(chosen)
        builder.draw_functions()
        builder.draw_codewords(codewords)
        builder.apply_mask(mask_id)
        builder.draw_format(mask_id)
        return builder

    if mask is not None:
        if not 0 <= int(mask) <= 7:
            raise QrError(f"mask 只能是 0~7，实际 {mask}")
        builder = build(int(mask))
        final_mask = int(mask)
    else:
        scored = [(build(candidate).penalty(), candidate) for candidate in range(8)]
        scored.sort()
        final_mask = scored[0][1]
        builder = build(final_mask)

    return QrCode(
        version=chosen,
        mask=final_mask,
        size=builder.size,
        modules=tuple(tuple(row) for row in builder.modules),
    )


# ---------------------------------------------------------------- 输出
def to_ascii(code: QrCode, *, quiet: int = 2, on: str = "██", off: str = "  ") -> str:
    """终端预览（演示脚本用）。"""
    blank = off * (code.size + quiet * 2)
    lines = [blank] * quiet
    for row in code.modules:
        body = "".join(on if cell else off for cell in row)
        lines.append(off * quiet + body + off * quiet)
    lines.extend([blank] * quiet)
    return "\n".join(lines)


def to_png(code: QrCode, *, scale: int = 6, quiet: int = 4) -> bytes:
    """输出 PNG 字节（灰度 8 位，纯 zlib 手写，不依赖 Pillow）。

    为什么不直接用 Pillow：这个模块要在离线便携 Python 上跑，Pillow 是又一个
    需要预装的重依赖；而 PNG 的最小实现只有一个 chunk 结构，30 行就够。
    """
    if scale < 1:
        raise QrError("scale 必须 ≥ 1")
    side = (code.size + quiet * 2) * scale
    dark = bytes((0,))
    light = bytes((255,))
    raw = bytearray()
    blank_row = b"\x00" + light * side          # 每行首字节是 filter type 0
    for _ in range(quiet * scale):
        raw += blank_row
    for row in code.modules:
        line = bytearray(b"\x00")
        line += light * (quiet * scale)
        for cell in row:
            line += (dark if cell else light) * scale
        line += light * (quiet * scale)
        for _ in range(scale):
            raw += line
    for _ in range(quiet * scale):
        raw += blank_row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)   # 灰度、无隔行
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
