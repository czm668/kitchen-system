# -*- coding: utf-8 -*-
"""滑动窗口聚合：一条规则要「10 分钟温升」「60 秒中位数」都从这里取数。

纯标准库、无第三方依赖，时间轴由调用方传入（测试可任意手推）。
之所以不用队列 + 定时清理，是因为测温是**事件驱动**的（2Hz 上报），
按「数据点时间戳」淘汰比按「墙钟定时器」更准，也不会漏清理。

三个实现细节，都是踩过的坑：

1. **淘汰边界用 `t - window_sec`，含等于**：2Hz 采样、600 秒窗口若排掉端点，
   实际只剩 599.5 秒，`rise` 会偏小；
2. **乱序数据不排序整表**：正常上报是单调的，遇到乱序点直接丢弃并在
   `dropped_out_of_order` 计数里体现，避免为了一个迟到点付出 O(n log n)；
3. **`rate` 用最小二乘斜率**：`(末值-首值)/时长` 对单点噪声极敏感，
   MQ-2 的脉冲干扰会把「无泄漏」算成正斜率，反而误报。
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

from .model import AGGREGATES, RuleError


@dataclass
class SlidingWindow:
    """定长时间窗；`push()` 时自动淘汰窗口外的点。

    `max_points` 是内存上限：2Hz × 600s = 1200 点，默认 8192 足够；
    超出时丢最旧的并计入 `dropped_overflow`，防止配置错成 1 小时窗口把内存吃光。
    """

    window_sec: float
    max_points: int = 8192
    _points: deque[tuple[float, float]] = field(default_factory=deque, repr=False)
    dropped_out_of_order: int = 0
    dropped_overflow: int = 0

    def __post_init__(self) -> None:
        if float(self.window_sec) <= 0:
            raise RuleError(f"window_sec 必须 > 0，收到 {self.window_sec}")
        if self.max_points < 2:
            raise RuleError("max_points 至少 2 个点（否则算不出 rise）")

    # ------------------------------------------------------------ 写入
    def push(self, t: float, value: float) -> bool:
        """写入一个采样点，返回是否被接受（乱序/溢出为 False）。"""
        moment, val = float(t), float(value)
        if self._points and moment < self._points[-1][0]:
            self.dropped_out_of_order += 1
            return False
        self._points.append((moment, val))
        if len(self._points) > self.max_points:
            self._points.popleft()
            self.dropped_overflow += 1
        self._evict(moment)
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - float(self.window_sec)
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    # ------------------------------------------------------------ 读取
    def __len__(self) -> int:
        return len(self._points)

    @property
    def empty(self) -> bool:
        return not self._points

    def values(self) -> list[float]:
        return [value for _, value in self._points]

    def times(self) -> list[float]:
        return [moment for moment, _ in self._points]

    def latest(self) -> tuple[float, float] | None:
        return self._points[-1] if self._points else None

    def span(self) -> float:
        """窗口内实际覆盖的时长；不足两点为 0。"""
        if len(self._points) < 2:
            return 0.0
        return self._points[-1][0] - self._points[0][0]

    def clear(self) -> None:
        self._points.clear()

    # ------------------------------------------------------------ 聚合
    def aggregate(self, kind: str) -> float | None:
        """按 `kind` 聚合；数据不足返回 None（调用方据此跳过判定，不猜 0）。"""
        if kind not in AGGREGATES:
            raise RuleError(f"aggregate={kind!r} 非法，允许 {list(AGGREGATES)}")
        if not self._points:
            return None
        values = self.values()
        if kind == "last":
            return values[-1]
        if len(values) < 2 and kind in ("rise", "rate", "std"):
            return None  # 单点算不出变化量，返回 None 比返回 0 安全
        if kind == "median":
            return statistics.median(values)
        if kind == "mean":
            return statistics.fmean(values)
        if kind == "max":
            return max(values)
        if kind == "min":
            return min(values)
        if kind == "rise":
            return values[-1] - values[0]
        if kind == "std":
            return statistics.pstdev(values)
        if kind == "rate":
            return self._slope()
        raise RuleError(f"未实现的聚合: {kind}")  # pragma: no cover

    def _slope(self) -> float | None:
        """最小二乘斜率（单位/秒）。时间戳先减去首点，避免大数相减丢精度。"""
        if len(self._points) < 2:
            return None
        t0 = self._points[0][0]
        xs = [moment - t0 for moment, _ in self._points]
        ys = self.values()
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return None  # 所有点同一时刻，斜率无定义
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        return sxy / sxx

    def snapshot(self) -> dict:
        """给日志/演示页用的只读摘要。"""
        latest = self.latest()
        return {
            "window_sec": self.window_sec,
            "points": len(self._points),
            "span_sec": round(self.span(), 3),
            "latest": None if latest is None else {"t": latest[0], "value": latest[1]},
            "dropped_out_of_order": self.dropped_out_of_order,
            "dropped_overflow": self.dropped_overflow,
        }
