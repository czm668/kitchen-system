# -*- coding: utf-8 -*-
"""二维码编码器（A4-08 溯源标签）测试。

三层校验，缺一层都不够：

1. **外部真值**：`tests/fixtures/qr_golden.json` 里满容量用例的矩阵哈希来自
   `segno`（逐版本 × 逐掩码比对过），锁住走位/纠错/掩码/格式信息。
2. **自解码往返**：把矩阵按标准走位重新读出来，反掩码、拆块、做 Reed-Solomon
   余式校验（必须全 0），再解析出负载与原文比对。这一层证明「读得回来」，
   第 1 层只证明「和某个实现一样」。
3. **结构断言**：定位图形、定时图形、暗模块、静区、PNG 头。

非满容量内容**不**与 segno 比矩阵：`segno.encoder.write_padding_bits` 在
`length % 8 == 0` 时会多补 8 个 0 位（规范要求此时不补），于是填充码字整体
后移一位。两者都能正常扫，但矩阵必然不同——拿它当基准会锁死一个偏差。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from kitchen.process import qr

FIXTURE = Path(__file__).with_name("fixtures") / "qr_golden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 自解码器
def _extract_bits(code: qr.QrCode) -> list[int]:
    """按 ISO 7.7.3 的走位把数据读回来（含反掩码）。"""
    builder = qr._Builder(code.version)
    builder.draw_functions()
    func = builder.func
    size = code.size
    bits: list[int] = []
    right = size - 1
    while right >= 1:
        # 只在正好是 6 时让一步（列宽恒为奇数，右列只在偶数上）：写成 `<= 6`
        # 会在 5/3/1 上继续减，列号变负后取到的是上一行末尾，静默读错。
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if func[row][col]:
                    continue
                bit = 1 if code.modules[row][col] else 0
                if qr._Builder.mask_hit(code.mask, row, col):
                    bit ^= 1
                bits.append(bit)
        right -= 2
    return bits


def _deinterleave(bits: list[int], version: int) -> list[list[int]]:
    """交织的逆过程：还原每个块的完整码字（数据 + 纠错）。"""
    ec_per_block, groups = qr._ECC_M[version]
    lengths: list[int] = []
    for blocks, data_len in groups:
        lengths.extend([data_len] * blocks)
    ec_len = ec_per_block

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits) - 7, 8)]
    data_blocks: list[list[int]] = [[] for _ in lengths]
    ec_blocks: list[list[int]] = [[] for _ in lengths]
    cursor = 0
    for index in range(max(lengths)):
        for block, length in enumerate(lengths):
            if index < length:
                data_blocks[block].append(codewords[cursor])
                cursor += 1
    for _ in range(ec_len):
        for block in range(len(lengths)):
            ec_blocks[block].append(codewords[cursor])
            cursor += 1
    return [data + ec for data, ec in zip(data_blocks, ec_blocks)]


def _rs_syndrome(block: list[int], ec_len: int) -> list[int]:
    """把整块码字当多项式，在 α^0..α^(ec-1) 处求值；合法码字必须全 0。"""
    out = []
    for i in range(ec_len):
        alpha = 1
        for _ in range(i):
            alpha = qr._gf_mul(alpha, 0x02)
        value = 0
        for coef in block:
            value = qr._gf_mul(value, alpha) ^ coef
        out.append(value)
    return out


def decode(code: qr.QrCode) -> str:
    """把矩阵解回文本；任何一致性检查失败都直接抛断言。"""
    blocks = _deinterleave(_extract_bits(code), code.version)
    ec_len = qr._ECC_M[code.version][0]
    for block in blocks:
        # 短块与长块相差 1 个数据码字是正常的（v8~v10 分两组），纠错码字数必须一致
        assert len(block) - ec_len in [data for _blocks, data in qr._ECC_M[code.version][1]]
        assert _rs_syndrome(block, ec_len) == [0] * ec_len, "纠错码字不合法（RS 余式非 0）"

    payload_bytes: list[int] = []
    for block in blocks:
        payload_bytes.extend(block[: len(block) - ec_len])

    bits = [int(ch) for byte in payload_bytes for ch in f"{byte:08b}"]
    assert bits[0:4] == [0, 1, 0, 0], "模式指示符不是 byte 模式"
    count_bits = 8 if code.version <= 9 else 16
    count = int("".join(str(b) for b in bits[4:4 + count_bits]), 2)
    body = bits[4 + count_bits:4 + count_bits + count * 8]
    return bytes(int("".join(str(b) for b in body[i:i + 8]), 2)
                 for i in range(0, len(body), 8)).decode("utf-8")


# ---------------------------------------------------------------- 1. 外部真值
def test_golden_exact_capacity_all_versions(golden: dict) -> None:
    """满容量用例：v1~v10 × mask 0~7 全部与 segno 一致。"""
    assert len(golden["exact_capacity"]) == 10
    for case in golden["exact_capacity"]:
        version = case["version"]
        payload = case["payload"]
        assert len(payload.encode()) == qr.BYTE_CAPACITY_M[version]
        for mask, expected in enumerate(case["masks"]):
            code = qr.encode(payload, version=version, mask=mask)
            assert code.hex_digest() == expected, f"v{version} mask={mask} 与基准不符"


def test_golden_regression(golden: dict) -> None:
    """真实标签内容的行为锁（防止改动悄悄改变输出）。"""
    for case in golden["regression"]:
        code = qr.encode(case["text"])
        assert code.version == case["version"]
        assert code.mask == case["mask"]
        assert code.size == case["size"]
        assert code.hex_digest() == case["sha256"]


# ---------------------------------------------------------------- 2. 自解码往返
@pytest.mark.parametrize("text", [
    "https://k.example.com/t/TC-DEMO-0001",
    "TC-DEMO-0001",
    "http://192.168.1.20:8000/trace?code=TC-DEMO-0001",
    "青椒肉丝 B-DEMO-001 鲜丰肉联厂",
    "A",
    "x" * 60,
    "y" * 130,
    "z" * 213,
])
def test_roundtrip_decode(text: str) -> None:
    """编码 → 走位读出 → 纠错校验 → 解回原文。"""
    code = qr.encode(text)
    assert decode(code) == text


@pytest.mark.parametrize("version", sorted(qr.BYTE_CAPACITY_M))
def test_roundtrip_every_version(version: int) -> None:
    """每个版本都要能解回来（含 v7+ 的版本信息区与多块交织）。"""
    cap = qr.BYTE_CAPACITY_M[version]
    text = ("TRACE-" + str(version) + "-" + "A1B2C3D4" * 40)[:cap]
    code = qr.encode(text, version=version)
    assert decode(code) == text


def test_roundtrip_all_masks() -> None:
    """八个掩码都要能解回来——掩码公式写错时，只有反掩码一致才解得开。"""
    text = "https://k.example.com/t/TC-DEMO-0001"
    for mask in range(8):
        assert decode(qr.encode(text, mask=mask)) == text


# ---------------------------------------------------------------- 3. 结构
def test_finder_patterns_and_dark_module() -> None:
    code = qr.encode("TC-DEMO-0001")
    size = code.size
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dy in range(7):
            for dx in range(7):
                dist = max(abs(dy - 3), abs(dx - 3))
                assert code.modules[top + dy][left + dx] is (dist != 2)
    # 暗模块固定深色
    assert code.modules[size - 8][8] is True


def test_timing_pattern_alternates() -> None:
    code = qr.encode("TC-DEMO-0001")
    for i in range(8, code.size - 8):
        assert code.modules[6][i] is (i % 2 == 0)
        assert code.modules[i][6] is (i % 2 == 0)


def test_version_info_present_from_v7() -> None:
    """v7+ 才有版本信息；v6 及以下那块区域是数据。"""
    code = qr.encode("A" * 200)
    assert code.version >= 7
    bits = qr._version_bits(code.version)
    for i in range(18):
        expected = ((bits >> i) & 1) == 1
        a = code.size - 11 + i % 3
        b = i // 3
        assert code.modules[b][a] is expected
        assert code.modules[a][b] is expected


def test_ascii_preview_and_png() -> None:
    code = qr.encode("TC-DEMO-0001")
    art = qr.to_ascii(code, quiet=1)
    assert len(art.splitlines()) == code.size + 2

    png = qr.to_png(code, scale=3, quiet=4)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    side = (code.size + 8) * 3
    assert (width, height) == (side, side)
    assert png.rstrip().endswith(b"IEND\xaeB`\x82")


# ---------------------------------------------------------------- 4. 边界
def test_empty_payload_rejected() -> None:
    with pytest.raises(qr.QrError):
        qr.encode("")


def test_oversized_payload_rejected() -> None:
    with pytest.raises(qr.QrError) as exc:
        qr.encode("x" * 214)
    assert "v10" in str(exc.value)


def test_invalid_mask_rejected() -> None:
    with pytest.raises(qr.QrError):
        qr.encode("A", mask=8)


def test_version_capacity_overflow_rejected() -> None:
    with pytest.raises(qr.QrError) as exc:
        qr.encode("x" * 15, version=1)
    assert "容量" in str(exc.value)


def test_version_selection_picks_smallest_fit() -> None:
    for version, cap in sorted(qr.BYTE_CAPACITY_M.items()):
        assert qr.encode("x" * cap).version == version
        assert qr.encode("x" * (cap - 1)).version == version


def test_table_self_check_matches_known_totals() -> None:
    """码字总数表自查：写错一位，纠错就整体错，扫出来是乱码但不会报错。"""
    for version, (ec, groups) in qr._ECC_M.items():
        total = sum(blocks * (data + ec) for blocks, data in groups)
        assert total == qr._TOTAL_CODEWORDS[version]
        # byte 模式容量 = 数据码字 - 2（v10+ 因计数位宽翻倍再减 1）
        data_total = sum(blocks * data for blocks, data in groups)
        overhead = 2 if version <= 9 else 3
        assert qr.BYTE_CAPACITY_M[version] in (data_total - overhead, data_total - overhead + 1)
