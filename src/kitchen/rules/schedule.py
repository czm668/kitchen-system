# -*- coding: utf-8 -*-
"""#2 排烟的档位调度：营业 / 备餐 / 收档。

## 为什么端侧自己算，不等服务端下发「当前档位」

04 文档 #2 写的是「服务器 APScheduler 按时段下发，端侧缓存 NVS，断网按最后
配置执行」。这里有个容易做错的实现口径：

- 下发**当前档位**（`business` / `prep` / `closed` 这个字符串）→ 断网那一刻
  档位就冻结了。19:00 断网，22:00 收档时间到了，端侧还停在营业档（阈值
  280℃、只播语音不动作），收档后无人时段的积热根本没人管——**这是安全漏洞**；
- 下发**时段表**（哪个时段是什么档）→ 端侧按本地时间自己算。断网只影响
  「表能不能更新」，不影响「现在该是什么档」。

所以本模块算的是「按表 + 当前本地时间 → 档位」，服务端 APScheduler 的职责
退化为「更新这张表」（`config_store.py`），不再参与实时判定。

## 三道装载期校验

1. **档位名必须在规则集里存在**。切到一个没有阈值的档位，`accepts_profile()`
   会把所有规则都过滤掉，引擎一条都不判——不报错、不告警、看起来一切正常。
   这类「静默全静默」比报错难查得多，所以装载时就炸。
2. **时段不许重叠**。静默覆盖（后写的赢）会让人以为排班按预期生效了，实际
   深夜那段被白天的配置吃掉了。
3. **兜底档必须是已知档位，且默认取最严的那个**（`closed`）。配置丢失、时段
   表残缺时，宁可多关一次排风阀、多开一张清理工单，也不能退回营业档只播个
   语音了事。

## 跨零点时段

`22:00 → 02:00` 这种必须支持（收档后到次日备餐前本来就跨零点）。语义是
**左闭右开、按起始日归属**：周一 22:00 到周二 02:00 属于「周一」的时段，
所以周日不排班的店，周一凌晨 0 点那段不该命中。实现上要回退一天查 `days`，
这一点写反了会让周一的配置在周日凌晨生效。
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "MINUTES_PER_DAY",
    "ProfileSchedule",
    "ProfileSlot",
    "ScheduleError",
    "builtin_schedule",
    "parse_hhmm",
]


MINUTES_PER_DAY = 24 * 60

# 最严档位：配置不明时用它兜底（见模块 docstring 第 3 条）
SAFEST_PROFILE = "closed"

# 按时段调度的已知档位（#2 的三档）。规则集里出现别的档位名会被校验拦下。
SCHEDULE_PROFILES = ("business", "prep", "closed")


class ScheduleError(ValueError):
    """档位表不合法。装载期抛出，不要留到运行期。"""


def parse_hhmm(text: str) -> int:
    """`"08:30"` → 510。`"24:00"` 允许，表示一天的末尾。"""
    raw = str(text).strip()
    if not raw:
        raise ScheduleError("时间不能为空")
    parts = raw.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"时间格式应为 HH:MM，收到 {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError(f"时间格式应为 HH:MM，收到 {text!r}") from exc
    if hour == 24 and minute == 0:
        return MINUTES_PER_DAY
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"时间越界：{text!r}")
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    total = int(minutes) % MINUTES_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def _local_parts(wall_ts: float, tz_offset_min: int) -> tuple[int, int, int]:
    """UTC 时间戳 → (本地日序号, 当日分钟, 星期几 0=周一)。

    不用 `time.localtime()`：那会让单测结果随开发机的时区漂移。偏移显式传入，
    ESP32 上由 NTP/DS3231 给出固定偏移（中国无夏令时），服务端同样固定 +8。
    """
    shifted = float(wall_ts) + tz_offset_min * 60.0
    day = int(shifted // 86400)
    minute_of_day = int((shifted - day * 86400) // 60)
    # 1970-01-01 是周四，Python 的 weekday() 里周四 = 3（0=周一）
    weekday = (day + 3) % 7
    return day, minute_of_day, weekday


@dataclass(frozen=True)
class ProfileSlot:
    """一个时段：`[start, end)` 内属于 `profile` 档。

    `days` 为空表示每天；跨零点时段按**起始日**归属（见模块 docstring）。
    """

    profile: str
    start: int                       # 当日分钟，0~1440
    end: int                         # 当日分钟，0~1440；end <= start 表示跨零点
    days: tuple[int, ...] = ()       # 0=周一 … 6=周日；空 = 每天

    def __post_init__(self) -> None:
        if not self.profile:
            raise ScheduleError("时段缺少档位名")
        if not (0 <= self.start <= MINUTES_PER_DAY):
            raise ScheduleError(f"start 越界：{self.start}")
        if not (0 <= self.end <= MINUTES_PER_DAY):
            raise ScheduleError(f"end 越界：{self.end}")
        if self.start == self.end:
            raise ScheduleError(
                f"时段 {format_hhmm(self.start)}~{format_hhmm(self.end)} 长度为 0，"
                "要表达「全天」请写 00:00~24:00"
            )
        for day in self.days:
            if not (0 <= int(day) <= 6):
                raise ScheduleError(f"days 里的 {day} 非法，只支持 0~6（0=周一）")

    @property
    def cross_midnight(self) -> bool:
        return self.end < self.start

    @property
    def every_day(self) -> bool:
        return not self.days

    def covers(self, weekday: int, minute_of_day: int) -> bool:
        if not self.cross_midnight:
            if not (self.start <= minute_of_day < self.end):
                return False
            return self.every_day or weekday in self.days
        # 跨零点：前半段在起始日 [start, 24:00)，后半段在次日 [0, end)
        if minute_of_day >= self.start:
            return self.every_day or weekday in self.days
        if minute_of_day < self.end:
            # 这一分钟属于「昨天」那个时段的尾巴，按昨天的星期几判断
            previous = (weekday - 1) % 7
            return self.every_day or previous in self.days
        return False

    def boundaries(self) -> tuple[int, int]:
        return self.start, self.end

    def to_dict(self) -> dict:
        body: dict = {
            "profile": self.profile,
            "start": format_hhmm(self.start),
            "end": format_hhmm(self.end),
        }
        if self.days:
            body["days"] = list(self.days)
        return body

    @classmethod
    def from_dict(cls, data: Mapping) -> "ProfileSlot":
        if not isinstance(data, Mapping):
            raise ScheduleError(f"时段必须是对象，收到 {type(data).__name__}")
        days_raw = data.get("days") or ()
        if isinstance(days_raw, str):
            raise ScheduleError("days 必须是数组，不能是字符串")
        return cls(
            profile=str(data.get("profile") or "").strip(),
            start=parse_hhmm(data.get("start", "")),
            end=parse_hhmm(data.get("end", "")),
            days=tuple(int(day) for day in days_raw),
        )

    def describe(self) -> str:
        span = f"{format_hhmm(self.start)}-{format_hhmm(self.end)}"
        if self.every_day:
            return f"{span} {self.profile}"
        names = "一二三四五六日"
        return f"{span} 周{''.join(names[day] for day in self.days)} {self.profile}"


@dataclass(frozen=True)
class ProfileSchedule:
    """档位时段表。冻结对象，可安全跨线程共享。"""

    slots: tuple[ProfileSlot, ...] = ()
    default: str = SAFEST_PROFILE
    tz_offset_min: int = 480          # 北京时间 +8
    source: str = "builtin"           # builtin | server | nvs

    def __post_init__(self) -> None:
        if not self.default:
            raise ScheduleError("默认档位不能为空")
        if not (-720 <= int(self.tz_offset_min) <= 840):
            raise ScheduleError(f"时区偏移异常：{self.tz_offset_min} 分钟")

    # ------------------------------------------------------------ 校验
    def validate(self, known_profiles: Iterable[str] | None = None) -> None:
        """装载期校验。`known_profiles` 传规则集里实际存在的档位名。"""
        problems: list[str] = []
        if known_profiles is not None:
            known = {str(item) for item in known_profiles if item}
            if self.default not in known:
                problems.append(
                    f"默认档位 {self.default!r} 不在规则集里（已有：{sorted(known)}）——"
                    "兜底时会一条规则都匹配不上"
                )
            for slot in self.slots:
                if slot.profile not in known:
                    problems.append(
                        f"时段 {slot.describe()} 的档位 {slot.profile!r} 不在规则集里"
                    )
        problems.extend(self._overlaps())
        if problems:
            raise ScheduleError("档位表校验失败：\n- " + "\n- ".join(problems))

    def _overlaps(self) -> list[str]:
        """重叠检测：把每个时段展开成「周内分钟区间」，两两求交。"""
        spans: list[tuple[int, int, ProfileSlot]] = []
        for slot in self.slots:
            days = slot.days or tuple(range(7))
            for day in days:
                base = day * MINUTES_PER_DAY
                start = base + slot.start
                end = base + slot.end
                if slot.cross_midnight:
                    end += MINUTES_PER_DAY      # 归到起始日，语义才对齐
                spans.append((start, end, slot))
        spans.sort(key=lambda item: item[0])
        problems: list[str] = []
        for index in range(1, len(spans)):
            prev_start, prev_end, prev_slot = spans[index - 1]
            cur_start, cur_end, cur_slot = spans[index]
            if cur_start < prev_end:
                problems.append(
                    f"时段重叠：{prev_slot.describe()} 与 {cur_slot.describe()}"
                    "（重叠会让「谁生效」取决于书写顺序，排班实际不按预期执行）"
                )
        return problems

    # ------------------------------------------------------------ 计算
    def profile_at(self, wall_ts: float | None = None) -> str:
        """给定本地时间戳返回档位；不在任何时段 → `default`（最严档）。"""
        moment = time.time() if wall_ts is None else float(wall_ts)
        _, minute_of_day, weekday = _local_parts(moment, self.tz_offset_min)
        for slot in self.slots:
            if slot.covers(weekday, minute_of_day):
                return slot.profile
        return self.default

    def next_change(self, wall_ts: float | None = None) -> float | None:
        """下一次档位切换的时间戳（找不到返回 None）。

        端侧用它在「切换时刻」唤醒一次，而不是每秒轮询——ESP32 常态休眠时
        轮询是纯浪费。
        """
        moment = time.time() if wall_ts is None else float(wall_ts)
        day, _, _ = _local_parts(moment, self.tz_offset_min)
        candidates: list[float] = []
        for offset in range(0, 9):          # 跨 8 天足够覆盖「每周只排一天」的极端表
            base = (day + offset) * 86400 - self.tz_offset_min * 60
            for slot in self.slots:
                for minute in slot.boundaries():
                    moment_ts = base + minute * 60
                    if moment_ts > moment + 1e-6:
                        candidates.append(moment_ts)
        return min(candidates) if candidates else None

    def slots_at(self, wall_ts: float | None = None) -> tuple[ProfileSlot, ...]:
        """当前命中的时段列表（调试 / 管理端展示用，正常只有 0~1 条）。"""
        moment = time.time() if wall_ts is None else float(wall_ts)
        _, minute_of_day, weekday = _local_parts(moment, self.tz_offset_min)
        return tuple(slot for slot in self.slots if slot.covers(weekday, minute_of_day))

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {
            "default": self.default,
            "tz_offset_min": int(self.tz_offset_min),
            "source": self.source,
            "slots": [slot.to_dict() for slot in self.slots],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ProfileSchedule":
        if not isinstance(data, Mapping):
            raise ScheduleError(f"档位表必须是对象，收到 {type(data).__name__}")
        raw = data.get("slots")
        if raw is None:
            raw = []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ScheduleError("slots 必须是数组")
        return cls(
            slots=tuple(ProfileSlot.from_dict(item) for item in raw),
            default=str(data.get("default") or SAFEST_PROFILE),
            tz_offset_min=int(data.get("tz_offset_min") or 480),
            source=str(data.get("source") or "server"),
        )

    def describe(self) -> str:
        body = "；".join(slot.describe() for slot in self.slots) or "（未配置时段）"
        return f"{body}；其余时间 → {self.default}"


def builtin_schedule() -> ProfileSchedule:
    """内置默认档位表：小店作息（10:00 开门、14:00-16:30 备餐、22:00 收档）。

    演示和单测都用它。真实门店由管理端改表后下发，覆盖到 NVS。
    """
    return ProfileSchedule(
        slots=(
            ProfileSlot("business", parse_hhmm("10:00"), parse_hhmm("14:00")),
            ProfileSlot("prep", parse_hhmm("14:00"), parse_hhmm("16:30")),
            ProfileSlot("business", parse_hhmm("16:30"), parse_hhmm("22:00")),
            # 22:00 到次日 09:00 是收档后无人时段；用跨零点时段显式表达，
            # 而不是「靠 default 兜底」——写出来才能被 validate() 查重叠。
            ProfileSlot("closed", parse_hhmm("22:00"), parse_hhmm("09:00")),
        ),
        default=SAFEST_PROFILE,
    )
