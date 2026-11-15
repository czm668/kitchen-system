# -*- coding: utf-8 -*-
"""ESP32 引脚能力常量 + GPIO 分配表的**可执行版本**。

`03_设计文档/02_硬件清单与GPIO分配.md` §2.2 那张表在这里落成数据 + 校验规则。
目的很具体：改线、换板、加外设时，人不会因为忘记下面这几条而白焊一周。

四条硬约束直接来自 ESP32 数据手册，与业务无关：

1. **ADC2 在 WiFi 工作时不可用**。ESP32 的 ADC2 与射频共用硬件，
   `WiFi.begin()` 之后对 ADC2 的读数是随机值，且**不报错**——
   这是本项目最容易踩、也最难排查的坑（后厨场景 WiFi 常开，等于 ADC2 永久不可用）。
2. **GPIO 34/35/36/39 仅输入**：没有输出驱动电路、没有内部上拉。
3. **GPIO 6–11 接 SPI flash**，任何情况下不可当普通 IO。
4. **strapping 脚（0/2/5/12/15）决定启动模式**，上电电平不对会进下载模式或反复重启。

`validate_plan()` 把这些变成可断言的检查。接进 CI 之后，分配表改错会当场失败，
而不是等焊完板子才发现 MQ-2 读数在跳。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# --------------------------------------------------------------------------
# ESP32 引脚能力（事实常量，勿改；换芯片时整份替换）
# --------------------------------------------------------------------------

#: ADC1 通道，WiFi 工作期间可正常采样
ADC1_PINS: frozenset[int] = frozenset({32, 33, 34, 35, 36, 37, 38, 39})

#: ADC2 通道，WiFi 工作期间**不可用**（射频占用），仅在不联网时可用于调试
ADC2_PINS: frozenset[int] = frozenset({0, 2, 4, 12, 13, 14, 15, 25, 26, 27})

#: 仅输入脚：无输出驱动、无内部上拉。做输出会静默失效（不报错，只是没反应）
INPUT_ONLY_PINS: frozenset[int] = frozenset({34, 35, 36, 39})

#: 连接 SPI flash，绝对不可用
FLASH_PINS: frozenset[int] = frozenset({6, 7, 8, 9, 10, 11})

#: strapping 脚 → 上电时的作用
STRAPPING_PINS: dict[int, str] = {
    0: "BOOT：低电平进下载模式，正常运行需在上电瞬间为高",
    2: "启动模式相关，上电需为低（部分模组要求）",
    5: "SDIO 从机时序，上电需为高",
    12: "MTDI：影响 flash 供电电压（3.3V/1.8V），上电需为低",
    15: "MTDO：影响启动日志输出，上电需为低",
}

#: UART0 调试串口，建议保留（烧写与日志都靠它）
UART0_PINS: frozenset[int] = frozenset({1, 3})

# --------------------------------------------------------------------------
# 角色
# --------------------------------------------------------------------------
ANALOG_IN = "analog_in"
DIGITAL_IN = "digital_in"
DIGITAL_OUT = "digital_out"
BUS = "bus"


@dataclass(frozen=True)
class PinUsage:
    """一条引脚分配记录，与文档 §2.2 的表格行一一对应。"""

    gpio: int
    peripheral: str
    signal: str
    role: str
    #: strapping 脚的上电期望电平："low" / "high" / "any"；非 strapping 脚为 None
    boot_level: str | None = None
    note: str = ""

    @property
    def is_strapping(self) -> bool:
        return self.gpio in STRAPPING_PINS


@dataclass(frozen=True)
class Issue:
    """校验问题。error 必改，warning 需在文档里写明缓解措施。"""

    severity: str  # "error" | "warning"
    gpio: int | None
    message: str

    def __str__(self) -> str:
        where = f"GPIO{self.gpio}" if self.gpio is not None else "整体"
        return f"[{self.severity.upper()}] {where}: {self.message}"


# --------------------------------------------------------------------------
# 当前分配表（与 03_设计文档/02_硬件清单与GPIO分配.md §2.2 保持一致）
# --------------------------------------------------------------------------
PIN_PLAN: tuple[PinUsage, ...] = (
    # --- SPI 总线：TFT / RC522 / MAX6675 共用，只登记一次 ---
    PinUsage(18, "SPI", "SCK", BUS, note="TFT / RC522 / MAX6675 共用"),
    PinUsage(23, "SPI", "MOSI", BUS, note="共用"),
    PinUsage(19, "SPI", "MISO", BUS, note="TFT 可只写不接；RC522 与 MAX6675 必须接"),
    # --- 2.8" TFT (LVGL) ---
    PinUsage(15, "TFT", "CS", DIGITAL_OUT, boot_level="low", note="strapping(MTDO)，上电须为低"),
    PinUsage(2, "TFT", "DC", DIGITAL_OUT, boot_level="low", note="strapping，上电须为低"),
    PinUsage(4, "TFT", "RST", DIGITAL_OUT, note="可腾出：改接 3.3V 常高后作 I2C"),
    # --- RC522 ---
    PinUsage(5, "RC522", "CS", DIGITAL_OUT, boot_level="high", note="strapping，上电高＝未选中"),
    PinUsage(27, "RC522", "RST", DIGITAL_OUT),
    # --- MAX6675 双路热电偶 ---
    PinUsage(16, "MAX6675_CH1", "CS", DIGITAL_OUT, note="排烟管壁温"),
    PinUsage(17, "MAX6675_CH2", "CS", DIGITAL_OUT, note="消毒柜温度"),
    # --- HX711 称重 ---
    PinUsage(34, "HX711", "DOUT", DIGITAL_IN, note="仅输入脚，无内部上拉；模块自带驱动"),
    PinUsage(33, "HX711", "SCK", DIGITAL_OUT),
    # --- DHT11 ---
    PinUsage(21, "DHT11", "DATA", DIGITAL_IN, note="4.7kΩ 上拉到 3.3V（单总线需双向，禁用仅输入脚）"),
    # --- DS18B20 x3 单总线 ---
    PinUsage(22, "DS18B20", "DQ", DIGITAL_IN, note="三条探头共用一条总线，4.7kΩ 上拉；需双向"),
    # --- 火焰传感器 A/B ---
    PinUsage(36, "FLAME_A", "AO", ANALOG_IN, note="仅输入脚 + ADC1，正合适"),
    PinUsage(39, "FLAME_B", "AO", ANALOG_IN, note="仅输入脚 + ADC1，正合适"),
    # --- MQ-2 ---
    PinUsage(35, "MQ2", "AO", ANALOG_IN, note="仅输入脚 + ADC1；模块 AO 需分压到 3.3V 以内"),
    # --- SCT-013 ---
    PinUsage(32, "SCT013", "ADC", ANALOG_IN, note="burden 电阻 + 偏置后进 ADC1"),
    # --- 三路继电器（低电平触发模块，IN 上电必须为高＝不吸合）---
    PinUsage(13, "RELAY_CH1", "IN", DIGITAL_OUT, boot_level="high",
             note="燃气阀：常开触点(NO)串在常闭电磁阀供电回路，不吸合/掉电＝阀关闭"),
    PinUsage(14, "RELAY_CH2", "IN", DIGITAL_OUT, boot_level="high",
             note="排风扇：常开触点，不吸合＝风机停"),
    PinUsage(26, "RELAY_CH3", "IN", DIGITAL_OUT, boot_level="high",
             note="工位插座：常开触点(NO)，吸合才供电；掉电即断电（拦截侧安全）"),
    # --- 语音 / 声光 ---
    PinUsage(25, "DFPLAYER", "TX", DIGITAL_OUT, note="只发不收，RX 不接"),
    PinUsage(12, "BUZZER", "IN", DIGITAL_OUT, boot_level="low", note="strapping(MTDI)，上电须为低；NPN 驱动"),
    PinUsage(0, "LED", "IN", DIGITAL_OUT, boot_level="high",
             note="BOOT 脚，上电为高＝正常运行；LED 高电平点亮，上电闪一下无害"),
)


# --------------------------------------------------------------------------
# PCF8575 扩展方案（GPIO 用满后的腾脚路径）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtensionStep:
    gpio: int
    current: str
    change: str
    becomes: str


#: GPIO 已全部分配。要加设备时按这两步腾脚，代价最小
EXTENSION_PLAN: tuple[ExtensionStep, ...] = (
    ExtensionStep(4, "TFT RST", "改接 3.3V 常高（模块自身有 RC 上电复位）", "I2C SDA"),
    ExtensionStep(0, "LED", "移除，或改挂 PCF8575 的 P0（反正是状态指示）", "I2C SCL"),
)


def i2c_pins_after_extension() -> tuple[int, int]:
    """返回按下 `EXTENSION_PLAN` 腾脚后的 I2C (SDA, SCL)。"""
    return (EXTENSION_PLAN[0].gpio, EXTENSION_PLAN[1].gpio)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def validate_plan(plan: tuple[PinUsage, ...] | list[PinUsage] = PIN_PLAN) -> list[Issue]:
    """检查分配表是否违反 ESP32 硬约束。返回空列表表示通过。

    检查项与「焊完才发现」的典型故障一一对应：
    - 模拟输入落在 ADC2  → WiFi 一开就读到随机值
    - 输出落在仅输入脚   → 代码不报错，外设就是不动
    - 用到 flash 脚       → 直接起不来
    - 同一脚被两个外设占用 → 功能互相打架
    """
    issues: list[Issue] = []
    seen: dict[int, PinUsage] = {}

    for usage in plan:
        gpio = usage.gpio

        if gpio in FLASH_PINS:
            issues.append(Issue("error", gpio,
                                f"{usage.peripheral} 用了 SPI flash 引脚，芯片无法正常启动"))

        if usage.role == ANALOG_IN:
            if gpio in ADC2_PINS:
                issues.append(Issue(
                    "error", gpio,
                    f"{usage.peripheral} 的模拟输入落在 ADC2。WiFi 工作时 ADC2 被射频占用，"
                    f"读数是随机值且不报错；只能用 ADC1（GPIO 32–39）"))
            elif gpio not in ADC1_PINS:
                issues.append(Issue("error", gpio,
                                    f"{usage.peripheral} 声明为模拟输入，但 GPIO{gpio} 不是 ADC 引脚"))

        if usage.role == DIGITAL_OUT and gpio in INPUT_ONLY_PINS:
            issues.append(Issue(
                "error", gpio,
                f"{usage.peripheral}.{usage.signal} 需要输出，但 GPIO{gpio} 是仅输入脚"
                f"（无驱动能力、无内部上拉）"))

        if usage.role == DIGITAL_IN and gpio in UART0_PINS:
            issues.append(Issue("warning", gpio,
                                f"{usage.peripheral} 占用了 UART0，烧写与日志会受影响"))

        if usage.is_strapping and usage.boot_level is None:
            issues.append(Issue(
                "warning", gpio,
                f"{usage.peripheral} 用了 strapping 脚（{STRAPPING_PINS[gpio]}），"
                f"但未声明上电期望电平"))

        if gpio in seen:
            prev = seen[gpio]
            issues.append(Issue(
                "error", gpio,
                f"重复分配：{prev.peripheral}.{prev.signal} 与 "
                f"{usage.peripheral}.{usage.signal} 都占了 GPIO{gpio}"))
        else:
            seen[gpio] = usage

    return issues


def validate_extension_plan(
    plan: tuple[PinUsage, ...] | list[PinUsage] = PIN_PLAN,
) -> list[Issue]:
    """检查 PCF8575 腾脚方案的副作用。

    关键风险：GPIO0 是 BOOT 脚，作 I2C SCL 后，**复位瞬间若从设备正在拉低总线**，
    会误进下载模式。缓解措施必须写进接线说明（见返回的 warning 文案）。
    """
    issues: list[Issue] = []
    sda, scl = i2c_pins_after_extension()

    if sda in STRAPPING_PINS or scl in STRAPPING_PINS:
        bad = sda if sda in STRAPPING_PINS else scl
        issues.append(Issue(
            "warning", bad,
            f"I2C 用了 strapping 脚 GPIO{bad}（{STRAPPING_PINS[bad]}）。缓解措施："
            f"总线加 4.7kΩ 上拉保证复位期为高；复位期间软件不发起 I2C 事务；"
            f"PCF8575 的 P 口默认输入态，不会主动拉低总线"))

    for step in EXTENSION_PLAN:
        usage = next((u for u in plan if u.gpio == step.gpio), None)
        if usage is None:
            issues.append(Issue("error", step.gpio,
                                f"腾脚方案假定 GPIO{step.gpio} 当前被 {step.current} 占用，"
                                f"但分配表里没有这条记录"))

    return issues


def extension_plan_summary() -> str:
    """生成可直接贴进文档的腾脚说明。"""
    lines = ["GPIO 已分配完毕。要接 PCF8575（I2C 扩展，约 +8 元）时按以下两步腾脚：", ""]
    for i, step in enumerate(EXTENSION_PLAN, 1):
        lines.append(f"{i}. GPIO{step.gpio}（当前：{step.current}）→ {step.change}"
                     f"，腾出后作 {step.becomes}")
    sda, scl = i2c_pins_after_extension()
    lines += [
        "",
        f"腾脚后 I2C：SDA=GPIO{sda}，SCL=GPIO{scl}，两条线各 4.7kΩ 上拉到 3.3V。",
        "⚠️ GPIO0 是 BOOT 脚：必须保证上电/复位瞬间为高（上拉即是保险），"
        "且复位期间软件不发起 I2C 事务，否则可能误进下载模式。",
    ]
    return "\n".join(lines)


def plan_table(plan: tuple[PinUsage, ...] | list[PinUsage] = PIN_PLAN) -> str:
    """把分配表渲染成 Markdown 表格，便于与文档比对。"""
    head = "| GPIO | 外设 | 信号 | 角色 | 约束 |\n|---:|---|---|---|---|"
    rows = [head]
    for u in sorted(plan, key=lambda x: x.gpio):
        caps = []
        if u.gpio in ADC1_PINS:
            caps.append("ADC1")
        if u.gpio in ADC2_PINS:
            caps.append("ADC2/WiFi下不可用")
        if u.gpio in INPUT_ONLY_PINS:
            caps.append("仅输入")
        if u.is_strapping:
            caps.append(f"strapping({u.boot_level or '未声明'})")
        note = u.note or "-"
        rows.append(f"| {u.gpio} | {u.peripheral} | {u.signal} | {u.role} | {note}；{'/'.join(caps) or '-'} |")
    return "\n".join(rows)


def clone_with_changes(**changes: object) -> tuple[PinUsage, ...]:
    """按 GPIO 号改分配表，供测试构造错配场景。

    用法：`clone_with_changes(**{"35": PinUsage(35, "MQ2", "AO", DIGITAL_OUT)})`
    """
    by_gpio = {str(u.gpio): u for u in PIN_PLAN}
    for key, value in changes.items():
        if key not in by_gpio:
            raise KeyError(f"分配表中没有 GPIO{key}")
        if not isinstance(value, PinUsage):
            raise TypeError("期望 PinUsage 实例")
        by_gpio[key] = value
    return tuple(by_gpio.values())


__all__ = [
    "ADC1_PINS", "ADC2_PINS", "INPUT_ONLY_PINS", "FLASH_PINS", "STRAPPING_PINS",
    "UART0_PINS", "ANALOG_IN", "DIGITAL_IN", "DIGITAL_OUT", "BUS",
    "PinUsage", "Issue", "PIN_PLAN", "EXTENSION_PLAN",
    "validate_plan", "validate_extension_plan", "i2c_pins_after_extension",
    "extension_plan_summary", "plan_table", "clone_with_changes", "replace",
]
