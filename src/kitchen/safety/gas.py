# -*- coding: utf-8 -*-
"""燃气泄漏判定（#1）：滑动中位数基线 + 突变率两级处置。

## 为什么不按绝对浓度报警

MQ-2 是个「对可燃气体都敏感」的宽谱传感器，酒精、油烟、杀虫剂都会让它涨；
器件之间的一致性又差（同一批货的 R0 能差一倍）。所以：

- **绝对浓度**只在「快漏了」时当联锁门限用（`absolute_threshold`），
  而且这个值必须现场标定；
- 真正的判据是**相对自己基线的突变率**——「比平时高 4 倍」比「到了 0.55」
  更能说明问题，也不受器件个体差异影响。

## 基线怎么来

滑动窗口中位数，不是均值：

| 统计量 | 一个 3 秒的油烟脉冲会怎样 |
|---|---|
| 均值 | 被拉高，基线跟着漂，真泄漏时反而「看起来正常」 |
| 中位数 | 纹丝不动，脉冲被无视 |

开机前 **5 分钟只建基线不判定**（`boot_grace_sec`），因为上电时灶台附近
本来就有残留气味，直接判定必误报。基线**每周固化一次**并写 NVS，
断电不丢——否则每次重启都要重新等 5 分钟，加上「冷启动漂移」，
累计误报率会高到没人愿意开这个功能。

## 明火期为什么要放宽

灶上有明火时 MQ-2 读数天然偏高（燃烧产物 + 热对流把灶台附近气体吹向探头），
此时按平时的突变率判会频繁误报。所以明火期间：

- 突变率阈值**上调**（2.0 → 3.5 倍）；
- 确认窗口**延长到 30 秒**（5 → 30 秒）。

这是「宁可慢 25 秒，也不要在厨师炒菜时把燃气阀关掉」的取舍，也与 04 文档
#1 的「误报抑制」一致。但注意：**绝对门限不因明火放宽**——真超过泄漏门限
说明浓度已经很高，跟是不是明火无关。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

from kitchen.rules.window import SlidingWindow

# 判定级别，与 env_thresholds.level 及 04 文档 §0.1 的两级动作对齐
LEVEL_NORMAL = 0
LEVEL_WARN = 1
LEVEL_ALARM = 2

# 基线未就绪时的原因串，方便日志里直接看出「为什么没报」
REASON_NO_BASELINE = "开机宽限中，基线未就绪"
REASON_NORMAL = "读数正常"
REASON_WARN = "浓度较基线突增，未达泄漏门限"
REASON_ALARM = "超过泄漏门限"
REASON_ALARM_RATIO = "突变率超联锁阈值"


@dataclass(frozen=True)
class GasBaseline:
    """一份可持久化的基线快照。`ready=False` 表示样本还不够，此时不该判定。"""

    value: float
    samples: int
    window_sec: float
    updated_at: float  # 墙钟；单调时钟不能跨重启，所以这里存墙钟
    ready: bool

    def to_nvs(self) -> str:
        """紧凑文本，模拟一个 NVS blob。`ready` 用 1/0 表示。"""
        return f"{self.value:.6f}|{self.samples}|{self.window_sec:g}|{self.updated_at:.0f}|{1 if self.ready else 0}"

    @classmethod
    def from_nvs(cls, text: str) -> "GasBaseline | None":
        """解析失败返回 None 而不是抛错：NVS 里半个字节坏掉不该让整个功能起不来。"""
        parts = (text or "").strip().split("|")
        if len(parts) < 5:
            return None
        try:
            return cls(
                value=float(parts[0]),
                samples=int(parts[1]),
                window_sec=float(parts[2]),
                updated_at=float(parts[3]),
                ready=parts[4].strip() == "1",
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class GasAssessment:
    """一次判定结论。`level` 决定上层是「只播报」还是「关阀 + 排风 + 工单」。"""

    level: int
    reason: str
    value: float
    baseline: float
    ratio: float          # 突变率 = (value - baseline) / baseline
    duration_required: float
    flame_active: bool

    @property
    def is_alarm(self) -> bool:
        return self.level == LEVEL_ALARM

    @property
    def should_actuate(self) -> bool:
        """只有联锁级才动继电器；预警级只播报（04 文档 §0.1）。"""
        return self.level == LEVEL_ALARM

    def to_payload(self) -> dict:
        return {
            "level": self.level,
            "reason": self.reason,
            "gas_value": round(self.value, 4),
            "gas_baseline": round(self.baseline, 4),
            "gas_ratio": None if self.ratio is None else round(self.ratio, 2),
            "duration_required": self.duration_required,
            "flame_active": self.flame_active,
        }


class GasBaselineTracker:
    """滑动窗口中位数基线。

    ## 调用方必须遵守的一条纪律：只在「判为正常」时喂数据

    `update()` 不区分状态，是**调用方**的责任：先拿上一帧基线算一次 `assess()`，
    `level == 0` 才 `update()`。否则会出这个必现事故：

    > 泄漏持续 3 分钟 → 窗口中位数跟着涨到 0.55 → 基线变成 0.55 →
    > 0.55 浓度「相对基线正常」→ 报警自行消失，而气还在漏。

    中位数只能抗住**短**脉冲（3 秒油烟），扛不住 3 分钟的持续高浓度。
    这是本项目最容易写错、也最容易被「看起来工作了」骗过去的一处。

    ## 第二道闸门：偏离基线太多的读数根本不进窗口（`guard_ratio`）

    只靠「调用方判为正常才喂」还不够。缓慢泄漏时浓度是**一点点**涨上去的，
    每一帧的「正常」判定都在用刚被抬高了一点点的基线做参照，形成正反馈：

    ```
    0.08 → 判正常 → 进窗口 → 基线 0.09
    0.10 → 判正常（相对 0.09 只高 11%）→ 进窗口 → 基线 0.11
    ...
    0.35 → 判正常（相对 0.30 只高 17%）→ 进窗口 → 基线 0.35
    ```

    等真到 0.55 时，基线已经 0.5 了，「相对正常」。中位数抗得住 3 秒的油烟
    脉冲，抗不住 5 分钟的缓慢泄漏。

    所以窗口自己再判一道：偏离当前中位数超过 `guard_ratio`（默认 25%）的读数
    一律不采，计入 `rejected`。宁可基线偏保守（锁在低位），也不能被污染源拖走。

    `min_samples` 是「样本够不够」的门槛：2Hz 采样下 60 个点约 30 秒。
    门槛太低会让一个瞬时脉冲成为基线（然后真读数全被判成正常）。
    """

    def __init__(
        self,
        window_sec: float = 300.0,
        min_samples: int = 60,
        *,
        guard_ratio: float = 0.25,
        persisted: GasBaseline | None = None,
    ) -> None:
        self.window = SlidingWindow(window_sec=float(window_sec), max_points=4096)
        self.min_samples = int(min_samples)
        self.guard_ratio = float(guard_ratio)
        self.rejected = 0            # 被抗污染闸门挡下的帧数，日志里要能看见
        self._persisted = persisted
        self._week_start: float | None = persisted.updated_at if persisted else None

    # ---------------------------------------------------------------- 采样
    def update(self, t: float, value: float) -> bool:
        """采样一帧，返回是否被接受。被闸门挡下的帧不进窗口（见类注释）。"""
        moment, val = float(t), float(value)
        base = self.median()
        if base is not None and self.is_ready() and base > 0:
            if abs(val - base) > self.guard_ratio * base:
                self.rejected += 1
                return False
        return self.window.push(moment, val)

    @property
    def samples(self) -> int:
        return len(self.window)

    def median(self) -> float | None:
        """当前窗口中位数；没有样本返回 None。"""
        return self.window.aggregate("median")

    def is_ready(self) -> bool:
        """样本够，或有周更固化下来的值（固化的值本身就是可信基线）。"""
        if self._persisted is not None and self._persisted.ready:
            return True
        return len(self.window) >= self.min_samples

    def baseline(self, wall_now: float) -> GasBaseline:
        """取当前基线：有实时中位数就用它，否则退回周更固化值。"""
        median = self.median()
        if median is not None and len(self.window) >= self.min_samples:
            return GasBaseline(
                value=median,
                samples=len(self.window),
                window_sec=self.window.window_sec,
                updated_at=wall_now,
                ready=True,
            )
        if self._persisted is not None and self._persisted.ready:
            return self._persisted
        return GasBaseline(
            value=median if median is not None else 0.0,
            samples=len(self.window),
            window_sec=self.window.window_sec,
            updated_at=wall_now,
            ready=False,
        )

    # ---------------------------------------------------------------- 周更
    WEEK_SEC = 7 * 24 * 3600.0

    def weekly_due(self, wall_now: float) -> bool:
        """是否到了该固化基线的时候（跨周）。

        用「距上次固化超过 7 天」判断，不用「星期几」——设备可能中途断电几天，
        按日历周算会漏掉更新；按时间差算，重启后一比对就知道该不该更新。
        """
        if self._week_start is None:
            return self.is_ready()
        return (wall_now - self._week_start) >= self.WEEK_SEC

    def freeze(self, wall_now: float) -> GasBaseline:
        """把当前中位数固化成持久基线（周更时调用，随后写 NVS）。"""
        current = self.baseline(wall_now)
        if not current.ready:
            return current
        self._persisted = current
        self._week_start = wall_now
        self.window.clear()  # 清空窗口，下一个周期的数据重新累积
        return current

    # ---------------------------------------------------------------- 持久化
    def to_nvs(self, wall_now: float) -> str:
        return self.baseline(wall_now).to_nvs()

    def save(self, path: str | Path, wall_now: float) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.baseline(wall_now).to_nvs(), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path, window_sec: float = 300.0,
             min_samples: int = 60) -> "GasBaselineTracker":
        target = Path(path)
        persisted = None
        if target.is_file():
            persisted = GasBaseline.from_nvs(target.read_text(encoding="utf-8"))
        return cls(window_sec=window_sec, min_samples=min_samples, persisted=persisted)


class GasLeakMonitor:
    """把「基线 + 当前读数 + 是否有明火」翻译成一个处置级别。

    纯函数式判定（除了 tracker 的采样），不碰时钟、不碰 IO，所有时间由调用方传。
    """

    def __init__(
        self,
        *,
        absolute_threshold: float = 0.55,
        warn_ratio: float = 2.0,
        alarm_ratio: float = 4.0,
        warn_ratio_flame: float = 3.5,
        alarm_ratio_flame: float = 5.0,
        duration_sec: float = 5.0,
        duration_sec_flame: float = 30.0,
        baseline_floor: float = 0.02,
    ) -> None:
        if absolute_threshold <= 0:
            raise ValueError("absolute_threshold 必须 > 0")
        if warn_ratio >= alarm_ratio:
            raise ValueError("预警突变率必须小于联锁突变率")
        if warn_ratio_flame >= alarm_ratio_flame:
            raise ValueError("明火期预警突变率必须小于联锁突变率")
        if duration_sec_flame < duration_sec:
            raise ValueError("明火期确认窗口不应短于平时，否则放宽就没有意义")
        # 明火期联锁点必须落在绝对门限以下，否则「靠突变率联锁」这条分支永远走不到：
        # 基线 0.08、比值 6 → 0.56 > 0.55，早被绝对门限先命中，放宽就成了假放宽。
        if baseline_floor * (1 + alarm_ratio_flame) >= absolute_threshold * 1.6:
            raise ValueError(
                "明火期联锁突变率过高：常见基线（≈0.08）下会越过绝对门限，"
                "该分支永远不生效，请下调 alarm_ratio_flame"
            )
        self.absolute_threshold = float(absolute_threshold)
        self.warn_ratio = float(warn_ratio)
        self.alarm_ratio = float(alarm_ratio)
        self.warn_ratio_flame = float(warn_ratio_flame)
        self.alarm_ratio_flame = float(alarm_ratio_flame)
        self.duration_sec = float(duration_sec)
        self.duration_sec_flame = float(duration_sec_flame)
        # 基线可能接近 0（干净空气），此时比值会爆炸，用一个地板值兜住
        self.baseline_floor = float(baseline_floor)

    def assess(
        self, value: float, baseline: GasBaseline, *, flame_active: bool = False,
    ) -> GasAssessment:
        flame = bool(flame_active)
        duration = self.duration_sec_flame if flame else self.duration_sec
        ratio = self._ratio(float(value), baseline.value)

        base_kwargs = dict(
            value=float(value), baseline=baseline.value, ratio=ratio,
            duration_required=duration, flame_active=flame,
        )

        if not baseline.ready:
            return GasAssessment(level=LEVEL_NORMAL, reason=REASON_NO_BASELINE, **base_kwargs)

        # ① 绝对门限优先，且不因明火放宽：到这个浓度说明不是「油烟有点大」
        if float(value) >= self.absolute_threshold:
            return GasAssessment(level=LEVEL_ALARM, reason=REASON_ALARM, **base_kwargs)

        alarm = self.alarm_ratio_flame if flame else self.alarm_ratio
        warn = self.warn_ratio_flame if flame else self.warn_ratio
        if ratio >= alarm:
            return GasAssessment(level=LEVEL_ALARM, reason=REASON_ALARM_RATIO, **base_kwargs)
        if ratio >= warn:
            return GasAssessment(level=LEVEL_WARN, reason=REASON_WARN, **base_kwargs)
        return GasAssessment(level=LEVEL_NORMAL, reason=REASON_NORMAL, **base_kwargs)

    def _ratio(self, value: float, baseline: float) -> float:
        # 取 6 位小数：0.48 与 0.08 算出来是 4.999999999999999，比阈值 5.0 小
        # 一点点就被判成「未达联锁」。传感器精度远低于 1e-6，这个舍入不损失信息，
        # 但能避免「阈值正好等于边界时结果随机」这种最难查的错误。
        return round((value - baseline) / max(abs(baseline), self.baseline_floor), 6)

    # ---------------------------------------------------------------- 阈值快照
    def thresholds(self, flame_active: bool = False) -> dict:
        """给管理端 / 演示页展示当前生效的阈值（明火期与平时两套）。"""
        flame = bool(flame_active)
        return {
            "absolute_threshold": self.absolute_threshold,
            "warn_ratio": self.warn_ratio_flame if flame else self.warn_ratio,
            "alarm_ratio": self.alarm_ratio_flame if flame else self.alarm_ratio,
            "duration_sec": self.duration_sec_flame if flame else self.duration_sec,
            "flame_active": flame,
        }


def median_of(values: list[float]) -> float | None:
    """小工具：给测试与离线脚本算中位数，避免各处重复写 statistics 导入。"""
    return statistics.median(values) if values else None
