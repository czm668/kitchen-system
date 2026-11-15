# -*- coding: utf-8 -*-
"""#5 冷链两路 + #6 留样柜：分级超限 + 持续确认 + **开门波动过滤**。

## 为什么要单独一层，而不是再加几条阈值行

「超温就报警」用 `env_thresholds` 一条 `duration` 行就能做，本项目也确实是这么
做的（id=13/16/19 都是「超上限持续 120 秒」）。但冷链有一个传感器读不出来的
事实：**门开着的时候温度本来就会升**。

冷藏柜开门 30 秒，箱内温度从 4℃ 冲到 9~12℃ 是常态；门一关 1~2 分钟回落。
如果只看「超过 8℃ 持续 2 分钟」，一顿午饭高峰能报十几次，值班表被刷满，
真故障反而没人看。反过来把确认时长拉到 10 分钟，压缩机坏了也要 10 分钟才
报警——那已经不是「冷链」了。

所以这里做的是引擎做不到的事：**识别波形，而不是比大小**。

| 现象 | 波形特征 | 本层处置 |
|---|---|---|
| 开门 | 几十秒内陡升 3~8℃，门关上后回落 | 判为开门波动，样本**不喂引擎**，计时不推进 |
| 压缩机故障 / 冷媒泄漏 | 缓慢单调上升，几十分钟累积 5~15℃ | 升速率低，不被过滤，交给「持续 120 秒」报警 |
| 门长时间未关 | 陡升后**不回落**，一路往上 | 升幅超过 `escape_c` 立刻放行，按超温报警 |

## 过滤必须说出来，不能悄悄吞掉

`ColdChainGuard.observe()` 返回 `held`（哪些指标当前被过滤、为什么）和
`events`（进入/解除/失效放行）。静默过滤等于伪造正常——平台上看不到曲线，
事后也查不出「那天为什么没报警」。过滤期间照常上报原始温度，只是不参与判定。

## 分级靠两条阈值行叠加，不是靠本层判断

| 级别 | 判据行 | 确认时长 | 动作 |
|---|---|---|---|
| 1 预警 | `range` 越界 | 引擎默认 3 秒去抖 | 语音 / 推送，不开单 |
| 2 报警 | `duration` 越界 | 120 秒 | 声光 + 工单 `cold_chain` |

引擎的 `_suppress_lower_levels` 会把预警级压掉（避免同一次事故两条文案），
但会把预警级的动作合并上来，所以语音不会丢。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

log = logging.getLogger("kitchen.coldchain")

__all__ = [
    "ColdChainFrame",
    "ColdChainGuard",
    "DoorOpenFilter",
    "FilterEvent",
    "DEFAULT_COLD_METRICS",
]

#: #5 两路 + #6 留样柜。#7 温湿度不走本层（它不判定，只联动排风）。
DEFAULT_COLD_METRICS: tuple[str, ...] = ("fridge_temp", "freezer_temp", "sample_temp")

# 过滤事件类型
HOLD_ENTER = "hold_enter"      # 判为开门波动，开始过滤
HOLD_RELEASE = "hold_release"  # 温度回落，过滤解除（这是最常见的一种，属正常）
HOLD_ESCAPE = "hold_escape"    # 升幅过大 / 过滤超时 → 过滤失效放行（可能真坏了）
HOLD_TIMEOUT = "hold_timeout"  # 同 HOLD_ESCAPE，原因是时长用尽


@dataclass(frozen=True)
class FilterEvent:
    """一次过滤动作的记录。**必须上送**，不能只在日志里。"""

    metric: str
    kind: str
    moment: float
    value: float
    baseline: float
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.kind != HOLD_ENTER

    def to_payload(self, device_id: str = "") -> dict:
        body = {
            "kind": "cold_chain_filter",
            "metric": self.metric,
            "event": self.kind,
            "value": round(float(self.value), 2),
            "baseline": round(float(self.baseline), 2),
            "reason": self.reason,
            "ts": int(self.moment),
        }
        if device_id:
            body["device_id"] = device_id
        return body


@dataclass
class _Trace:
    """单指标的样本轨迹 + 过滤状态。"""

    points: deque = field(default_factory=lambda: deque(maxlen=256))
    hold_since: float | None = None
    baseline: float = 0.0
    peak: float = 0.0
    holds: int = 0
    #: 已经「认定真超温」并放行过了。为 True 时不再重新进入过滤，
    #: 否则一路升高的曲线会变成 enter→escape→enter→escape 每帧闪一次。
    hot: bool = False

    @property
    def holding(self) -> bool:
        return self.hold_since is not None


class DoorOpenFilter:
    """开门波动过滤器：识别「陡升 + 回落」的波形，把这类样本从判定流里摘掉。

    参数取值的依据（冷藏柜实测量级，不是拍脑袋）：

    - `rise_rate_c_per_min=6`：开门时箱温典型升速 8~15℃/min；压缩机故障通常
      不超过 1~2℃/min。6 是两者中间偏保守的一侧——宁可漏过滤（多报一次），
      也不要误过滤（漏报）。
    - `jump_c=2.5`：小于 2.5℃ 的波动不叫开门波动，那是除霜循环的正常起伏。
    - `rise_window_sec=20`：**升速只看最近 20 秒**。这一点必须说清楚——
      如果拿整个 30 秒回溯窗的最小值当基准，开门前那段平稳期会把均值稀释掉：
      真实 12℃/min 的跃升会被算成 5℃/min，于是过滤器根本不会启动。
    - `hold_sec=120`：开门最长也就 2 分钟（取货/补货）。超过 120 秒还没回落，
      说明门可能真的没关，必须放行让「持续 120 秒」那条规则接住。
    - `recover_margin_c=1.0`：回落到「基线 +1℃」以内即认定门关上了。要求完全
      回到基线太苛刻——放进去的货本身就是热的。
    - `escape_c=8`：升幅超过 8℃ 就不当开门波动了。开门再久也不至于把 4℃ 的
      冷藏柜抬到 12℃ 以上还回落，这个量级只可能是故障。

    全部可调，因为真实柜体的开机特性差异很大；调参不该改代码。
    """

    def __init__(
        self,
        *,
        lookback_sec: float = 30.0,
        rise_window_sec: float = 20.0,
        rise_rate_c_per_min: float = 6.0,
        jump_c: float = 2.5,
        hold_sec: float = 120.0,
        recover_margin_c: float = 1.0,
        escape_c: float = 8.0,
    ) -> None:
        if lookback_sec <= 0 or rise_window_sec <= 0:
            raise ValueError("lookback_sec / rise_window_sec 必须 > 0")
        if rise_window_sec > lookback_sec:
            raise ValueError("rise_window_sec 不能大于 lookback_sec（升速窗是回溯窗的子集）")
        if jump_c <= 0 or escape_c <= jump_c:
            raise ValueError("需要 0 < jump_c < escape_c")
        self.lookback_sec = float(lookback_sec)
        self.rise_window_sec = float(rise_window_sec)
        self.rise_rate_c_per_min = float(rise_rate_c_per_min)
        self.jump_c = float(jump_c)
        self.hold_sec = float(hold_sec)
        self.recover_margin_c = float(recover_margin_c)
        self.escape_c = float(escape_c)
        self._traces: dict[str, _Trace] = {}

    # ------------------------------------------------------------ 单帧
    def observe(self, metric: str, value: float, moment: float) -> tuple[bool, FilterEvent | None]:
        """返回 `(是否放行, 事件)`。

        放行 = 该样本应参与判定；`False` 表示被本次开门波动吃掉。
        """
        trace = self._traces.setdefault(metric, _Trace())
        trace.points.append((float(moment), float(value)))
        self._trim(trace, float(moment))

        if trace.holding:
            return self._while_holding(metric, trace, float(value), float(moment))
        return self._check_start(metric, trace, float(value), float(moment))

    def _trim(self, trace: _Trace, moment: float) -> None:
        edge = moment - self.lookback_sec
        while len(trace.points) > 2 and trace.points[0][0] < edge:
            trace.points.popleft()

    def _check_start(self, metric, trace, value, moment):
        """看最近 `rise_window_sec` 的升速够不够陡。"""
        lowest = float(min(val for _, val in trace.points))
        if trace.hot and value <= lowest + self.recover_margin_c:
            # 已经平稳（含「真的降下来了」）：解除「别再过过滤」的标记。
            # 平稳状态下本来也不会触发 hold（升幅不够），所以这里只看平稳性就够。
            trace.hot = False
        if trace.hot:
            return True, None            # 已认定真超温，不再二次过滤（避免每帧闪一次）

        edge = moment - self.rise_window_sec
        fast = [(ts, val) for ts, val in trace.points if ts >= edge]
        if len(fast) < 2:
            return True, None            # 样本不足以算速率，先放行
        base_ts, base_val = fast[0]
        span = moment - base_ts
        if span < 1.0:
            return True, None
        rise = value - base_val
        if rise < self.jump_c:
            return True, None            # 波动太小：正常起伏或缓慢故障，交给阈值判
        rate = rise / span * 60.0
        if rate < self.rise_rate_c_per_min:
            return True, None
        # 基线取**整个回溯窗的最小值**（= 开门前的箱温），而不是升速窗起点：
        # 升速窗起点已经是「升了一半」的温度，用它算 escape 会把阈值放宽一半。
        trace.baseline = float(min(val for _, val in trace.points))
        trace.hold_since = moment
        trace.peak = float(value)
        trace.holds += 1
        event = FilterEvent(
            metric, HOLD_ENTER, moment, value, trace.baseline,
            reason=(
                f"升幅 {rise:.1f}℃ / {span:.0f}s（{rate:.1f}℃/min）符合开门波动特征，"
                f"本帧不参与判定，{self.hold_sec:.0f}s 内回落则不报警"
            ),
        )
        log.info("冷链过滤：%s %s", metric, event.reason)
        return False, event

    def _while_holding(self, metric, trace, value, moment):
        trace.peak = max(trace.peak, value)
        rise = value - trace.baseline

        # ① 升幅过大：不能再当开门波动，否则真故障被吃掉
        if rise >= self.escape_c:
            return self._end(metric, trace, value, moment, HOLD_ESCAPE,
                             f"升幅 {rise:.1f}℃ 已超 {self.escape_c:.1f}℃ 上限，"
                             "判为真实超温，恢复判定")

        # ② 回落到位：门关上了
        if value <= trace.baseline + self.recover_margin_c:
            return self._end(metric, trace, value, moment, HOLD_RELEASE,
                             f"已回落到基线 {trace.baseline:.1f}℃ 附近，判为开门波动")

        # ③ 过滤超时：门可能真的没关
        if moment - trace.hold_since >= self.hold_sec:
            return self._end(metric, trace, value, moment, HOLD_TIMEOUT,
                             f"过滤已达 {self.hold_sec:.0f}s 未回落，恢复判定（门未关？）")

        return False, None

    def _end(self, metric, trace, value, moment, kind, reason):
        trace.hold_since = None
        if kind in (HOLD_ESCAPE, HOLD_TIMEOUT):
            # 认定「不是开门波动」：置 hot，直到温度重新平稳才允许再次过滤
            trace.hot = True
        event = FilterEvent(metric, kind, moment, value, trace.baseline, reason)
        log.info("冷链过滤解除：%s %s", metric, reason)
        return True, event

    # ------------------------------------------------------------ 观测
    def holding_metrics(self) -> dict[str, str]:
        return {
            metric: (
                f"开门波动过滤中（基线 {trace.baseline:.1f}℃，"
                f"已 {trace.holds} 次）"
            )
            for metric, trace in self._traces.items()
            if trace.holding
        }

    def snapshot(self) -> dict:
        return {
            "params": {
                "lookback_sec": self.lookback_sec,
                "rise_window_sec": self.rise_window_sec,
                "rise_rate_c_per_min": self.rise_rate_c_per_min,
                "jump_c": self.jump_c,
                "hold_sec": self.hold_sec,
                "recover_margin_c": self.recover_margin_c,
                "escape_c": self.escape_c,
            },
            "metrics": {
                metric: {
                    "holding": trace.holding,
                    "hot": trace.hot,
                    "baseline": round(trace.baseline, 2),
                    "peak": round(trace.peak, 2),
                    "holds": trace.holds,
                    "samples": len(trace.points),
                }
                for metric, trace in self._traces.items()
            },
        }

    def reset(self) -> None:
        self._traces.clear()


@dataclass(frozen=True)
class ColdChainFrame:
    """一帧的过滤结果：放行的样本 + 被扣的指标 + 事件。"""

    passed: Mapping[str, float]
    held: Mapping[str, str] = field(default_factory=dict)
    events: tuple[FilterEvent, ...] = ()

    @property
    def filtered(self) -> bool:
        return bool(self.held)

    def describe(self) -> str:
        if not self.held:
            return "全部放行"
        return "；".join(f"{metric}: {reason}" for metric, reason in self.held.items())


class ColdChainGuard:
    """冷链/留样柜的前置层：过滤开门波动，再把干净的样本交给运行时判定。

    用法::

        guard = ColdChainGuard()
        frame = guard.observe(telemetry)          # 过滤
        result = rt.tick(frame.passed)            # 判定（分级超限由阈值行负责）
        for event in frame.events:                # 过滤行为必须上送
            publish(event.to_payload())
    """

    def __init__(
        self,
        *,
        metrics: Sequence[str] = DEFAULT_COLD_METRICS,
        door_filter: DoorOpenFilter | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.metrics = tuple(metrics)
        self.filter = door_filter or DoorOpenFilter()
        self._clock = clock or time.monotonic

    def observe(
        self,
        metrics: Mapping[str, float],
        now: float | None = None,
    ) -> ColdChainFrame:
        """过滤所有登记的冷链指标；未登记的指标原样放行。"""
        moment = self._clock() if now is None else float(now)
        passed: dict[str, float] = {}
        held: dict[str, str] = {}
        events: list[FilterEvent] = []

        for metric, raw in metrics.items():
            if metric not in self.metrics:
                passed[metric] = raw
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                passed[metric] = raw       # 非数值原样透传，交给引擎按缺数据处理
                continue
            ok, event = self.filter.observe(metric, value, moment)
            if event is not None:
                events.append(event)
            if ok:
                passed[metric] = value
            else:
                # 刻意**不**把该指标放进 passed：引擎对缺数据是「宁可不判」，
                # 正好实现「过滤期间不推进持续时长」。
                held[metric] = event.reason if event is not None else "开门波动过滤中"
        return ColdChainFrame(passed, held, tuple(events))

    def snapshot(self) -> dict:
        body = self.filter.snapshot()
        body["metrics_covered"] = list(self.metrics)
        body["holding"] = self.filter.holding_metrics()
        return body


def describe_events(events: Iterable[FilterEvent]) -> list[str]:
    """给 demo / 日志用的一行行中文描述。"""
    return [f"{item.metric} {item.kind}：{item.reason}" for item in events]
