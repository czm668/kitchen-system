# -*- coding: utf-8 -*-
"""规则引擎：去抖 + 冷却（沿用旧工程）+ 本项目需要的六项扩展。

从旧工程 `vendor/proj/kernel/rules/engine.py` 改造。旧引擎只有「瞬时值比较 +
去抖 + 冷却」三件事，本项目要的东西多出来一截，逐项落点如下：

| 能力 | 落点 | 支撑的判定点 |
|---|---|---|
| `per_rule` 时长 | `Rule.duration_sec` 覆盖引擎默认去抖 | #5/#6 持续 2 分钟、#15 累计 10 分钟 |
| 窗口聚合 | `SlidingWindow` + `Rule.aggregate` | #2 十分钟温升 |
| 区间判定 | `compare()` 的 in/out 双边语义 | #15 100~120℃ 达标区 |
| 档位阈值 | `set_profile()` + `Rule.profile` | #2 营业/备餐/收档 |
| 多源交叉 | `Rule.requires`（AND） | #3 视觉 AND 火焰 |
| 闩锁 + 人工复位 | `Rule.latch` + `ack()` | #1/#2/#3/#16 |
| 开机宽限 | `boot()` + `boot_grace_sec` | #1 开机前 5 分钟只建基线 |
| NVS 持久化 | `RuleStateStore`（见 state.py） | #17 断网沿用 |

## 和旧引擎的三处刻意不同

1. **不依赖 SQLAlchemy**：规则由 `loader.py` 从普通字典（DB 行 / JSON）构造，
   引擎本身是纯标准库，PC 上直接跑单测，固件侧也能编译进同一个逻辑。
2. **时钟分离**：判定用 `clock`（默认 monotonic），闩锁展示用 `wall_clock`
   （默认 time.time）。混用会导致改系统时间就能解锁。
3. **`evaluate()` 只返回「状态变化」**：一直正常、持续越界但未到触发时长的规则
   都不产生事件，避免 2Hz 上报把下游刷爆。

## 开机宽限期间的语义

宽限期内**仍然采样、仍然算聚合**，只是不判定。这一点很关键：#1 要求
「开机前 5 分钟只建基线不判定」，等 5 分钟过去，中位数基线已经有 600 个点，
第一帧就能给出有意义的判定；如果宽限期内不采样，宽限结束那一刻基线是空的，
规则还要再等一个窗口才有效，实际宽限就变成了 15 分钟。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .model import Rule, RuleError, assert_rules_valid
from .state import LATCHED, NORMAL, RuleStateStore
from .window import SlidingWindow

log = logging.getLogger("kitchen.rules")

# 事件类型：只有这三种会出现在 evaluate() 的返回值里
EVENT_FIRED = "fired"       # 本次满足（含持续时长与冷却）→ 该报警/联锁了
EVENT_RELEASED = "released"  # 从越界回到正常；闩锁规则仍锁着，需人工确认
EVENT_LATCHED = "latched"    # 闩锁期间每过一个冷却周期重复上报一次


@dataclass(frozen=True)
class Verdict:
    """一次判定结论。字段自解释，不再用单字符串 state 表达多种含义。"""

    rule_id: str
    metric: str
    event: str
    value: float          # 参与比较的值（窗口规则为聚合值）
    raw: float            # 本次瞬时值，便于对照「聚合后」和「原始」
    level: int
    message: str
    actions: tuple[str, ...] = ()
    latched: bool = False
    elapsed: float = 0.0  # 已持续秒数；未越界为 0
    # 要开的工单类型（"" = 不开单）。#2 收档档触发时是 "cleanup"；只有 fired /
    # latched 事件带 ticket，released 不带——「恢复正常」不该再开一张清理单。
    ticket: str = ""

    @property
    def fired(self) -> bool:
        return self.event == EVENT_FIRED

    def to_payload(self, device_id: str = "", ts: int | None = None) -> dict:
        """转成能被 MQTT 发布 / 写库的扁平 payload（与契约的展平风格一致）。"""
        return {
            "rule_id": self.rule_id,
            "metric": self.metric,
            "event": self.event,
            "value": round(float(self.value), 4),
            "raw": round(float(self.raw), 4),
            "level": int(self.level),
            "latched": bool(self.latched),
            "elapsed": round(float(self.elapsed), 1),
            "actions": list(self.actions),
            "message": self.message,
            "ts": int(time.time() if ts is None else ts),
            **({"ticket": self.ticket} if self.ticket else {}),
            **({"device_id": device_id} if device_id else {}),
        }


@dataclass(frozen=True)
class AckResult:
    """人工复位结果。`ok=False` 时 `reason` 说明为什么不让解锁。"""

    ok: bool
    rule_id: str
    reason: str = ""
    reviewer: str = ""


@dataclass
class _Timers:
    """单条规则的去抖/冷却计时（内存态，不持久化，理由见 state.py）。

    `breached` 与 `alerted` 必须分开：
    - `breached`：这一帧是否越界（持续时长没到也算越界）；
    - `alerted`：是否**已经上报过**。

    合并成一个字段会出这个 bug：一次 2 秒的短暂越界（不足 30 秒持续时长）
    从未产生过报警，回落后却发出一条「已恢复正常」——用户看到一条他从未
    收到过的告警的「恢复通知」。
    """

    breach_since: float | None = None
    last_fired: float | None = None
    breached: bool = False
    alerted: bool = False


class RuleEngine:
    """有状态的判定器。线程安全（内部 RLock），可被 MQTT 线程与 HTTP 线程共用。"""

    def __init__(
        self,
        *,
        device_id: str = "",
        default_duration_sec: float = 3.0,
        cooldown_sec: float = 60.0,
        boot_grace_sec: float = 0.0,
        profile: str | None = None,
        dataset: str = "demo",
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        state_store: RuleStateStore | None = None,
        max_window_points: int = 8192,
    ) -> None:
        self.device_id = device_id
        self.default_duration_sec = float(default_duration_sec)
        self.cooldown_sec = float(cooldown_sec)
        self.boot_grace_sec = float(boot_grace_sec)
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self.profile = profile
        # 取值口径：real（真实阈值）/ demo（演示阈值）。答辩现场用 demo，
        # 生产切 real；两套阈值同时在库里，靠这个字段选，不靠改数据。
        self.dataset = dataset
        self.max_window_points = int(max_window_points)
        self._lock = threading.RLock()
        self._rules: list[Rule] = []
        self._timers: dict[str, _Timers] = {}
        self._windows: dict[tuple[str, float], SlidingWindow] = {}
        # 已开单的 (metric, ticket)：事故存续期间只开一张，见 _settle_tickets
        self._tickets_open: set[tuple[str, str]] = set()
        # 每个指标最近一次上报的 (等级, 时刻)：用于压制更低等级的重复上报。
        # 不能只看「是否闩锁」——#6 留样柜刻意不闩锁（超温要的是当场听见，
        # 不是要人跑去按确认键），只看闩锁的话，它的预警级会在报警级旁边
        # 每分钟冒一次，同一次事故在页面上显示成两条互相矛盾的告警。
        self._reported: dict[str, tuple[int, float]] = {}
        self._booted_at: float | None = None
        self.state_store = state_store or RuleStateStore(device_id=device_id)
        if not self.state_store.device_id:
            self.state_store.device_id = device_id

    # ---------------------------------------------------------------- 装载
    def set_rules(self, rules: Sequence[Rule]) -> list[Rule]:
        """装载规则集。会校验、去重，并按规则集重建/裁剪窗口。

        窗口**保留已有数据**（只增删组合，不清空）：档位从营业切到备餐时
        #2 的窗口参数完全一样，清空会让 10 分钟温升重新计时，切档瞬间丢判定。
        """
        materialized = list(rules)
        assert_rules_valid(materialized)
        with self._lock:
            self._rules = materialized
            alive_ids = {rule.id for rule in materialized}
            # 计时器只留活着的规则；配置变了就重新计去抖（同旧引擎的 clear 语义）
            self._timers = {rid: _Timers() for rid in alive_ids}
            # 配置变了就当「重新开始」：让进行中的事故可以重新开单（配置变更
            # 本身就是人工介入，重开一张单比沿用旧标记更可追溯）
            self._tickets_open.clear()
            self._reported.clear()
            wanted: dict[tuple[str, float], SlidingWindow] = {}
            for rule in materialized:
                key = rule.window_key
                if key is None:
                    continue
                wanted[key] = self._windows.get(
                    key, SlidingWindow(window_sec=key[1], max_points=self.max_window_points),
                )
            self._windows = wanted
            self.state_store.prune(alive_ids)
            log.info(
                "规则装载：%d 条（窗口 %d 个），device=%s profile=%s",
                len(materialized), len(wanted), self.device_id or "-", self.profile or "-",
            )
        return materialized

    def get_rules(self, *, include_disabled: bool = False) -> list[Rule]:
        with self._lock:
            return [rule for rule in self._rules if include_disabled or rule.enabled]

    def set_profile(self, profile: str | None) -> None:
        """切换档位（#2 营业/备餐/收档）。只影响判定，不动窗口数据。"""
        with self._lock:
            self.profile = profile
        log.info("档位切换：%s", profile or "-")

    def set_dataset(self, dataset: str) -> None:
        """切换取值口径（real/demo）。与档位切换互不影响，各管一个维度。"""
        with self._lock:
            self.dataset = dataset
        log.info("取值口径切换：%s", dataset)

    def set_boot_grace(self, seconds: float) -> None:
        if float(seconds) < 0:
            raise RuleError("boot_grace_sec 不能为负")
        with self._lock:
            self.boot_grace_sec = float(seconds)

    # ---------------------------------------------------------------- 开机宽限
    def boot(self, now: float | None = None) -> None:
        """记录开机时刻，之后 `boot_grace_sec` 内只建基线不判定。"""
        with self._lock:
            self._booted_at = self._clock() if now is None else float(now)

    def in_grace(self, now: float | None = None) -> bool:
        with self._lock:
            if self._booted_at is None or self.boot_grace_sec <= 0:
                return False
            moment = self._clock() if now is None else float(now)
            return (moment - self._booted_at) < self.boot_grace_sec

    def grace_remaining(self, now: float | None = None) -> float:
        with self._lock:
            if self._booted_at is None or self.boot_grace_sec <= 0:
                return 0.0
            moment = self._clock() if now is None else float(now)
            return max(0.0, self.boot_grace_sec - (moment - self._booted_at))

    # ---------------------------------------------------------------- 判定
    def evaluate(
        self,
        metrics: Mapping[str, float],
        now: float | None = None,
        *,
        raw_metrics: Mapping[str, float] | None = None,
    ) -> list[Verdict]:
        """喂一帧遥测，返回本次需要上报的事件（可能为空）。

        `raw_metrics` 供离线回放时给出「原始瞬时值」做对照；不传时等于 `metrics`。
        """
        moment = self._clock() if now is None else float(now)
        raw = dict(metrics if raw_metrics is None else raw_metrics)
        with self._lock:
            self._push_windows(moment, metrics)
            if self.in_grace(moment):
                return []  # 宽限期：采样已进窗口，但不判定
            verdicts: list[Verdict] = []
            for rule in self._rules:
                if not rule.enabled or not rule.accepts_profile(self.profile, self.dataset):
                    continue
                verdict = self._judge(rule, metrics, raw, moment)
                if verdict is not None:
                    verdicts.append(verdict)
            verdicts = self._merge_same_level(self._suppress_lower_levels(verdicts, moment))
            verdicts = self._settle_tickets(verdicts)
            self._close_settled_tickets()
            return verdicts

    def _suppress_lower_levels(
        self, verdicts: list[Verdict], moment: float | None = None,
    ) -> list[Verdict]:
        """同一指标上，高等级规则成立时不再上报低等级规则。

        没有这一步，「燃气 > 0.45 预警」和「燃气 > 0.55 联锁」会同时触发：
        既报「关阀」又报「提示一下」，小程序上看到两条互相矛盾的告警。
        真实场景里低等级也只是高等级的前兆，被压住是对的。

        三个「更高等级」的来源都要算，缺一个就会从缝里漏出低等级告警：

        1. 本帧成立的高等级判据；
        2. 已进入闩锁的高等级规则——冷却期内它不再产生事件，但事故还在；
        3. **最近 `cooldown_sec` 内上报过的高等级事件**——#6 留样柜刻意不闩锁，
           只靠前两条的话，它的预警级会在报警级旁边每分钟冒一次。
        """
        top: dict[str, int] = {}
        for rule in self._rules:
            if not rule.accepts_profile(self.profile, self.dataset):
                continue
            state = self.state_store.peek(rule.id)
            if state is not None and state.latched:
                top[rule.metric] = max(top.get(rule.metric, -1), rule.level)
        if moment is not None:
            for metric, (level, at) in self._reported.items():
                if moment - at < self.cooldown_sec:
                    top[metric] = max(top.get(metric, -1), level)
        for verdict in verdicts:
            if verdict.event in (EVENT_FIRED, EVENT_LATCHED):
                top[verdict.metric] = max(top.get(verdict.metric, -1), verdict.level)

        kept: list[Verdict] = []
        taken: dict[str, list[str]] = {}
        for verdict in verdicts:
            best = top.get(verdict.metric)
            if (best is not None and verdict.level < best
                    and verdict.event in (EVENT_FIRED, EVENT_LATCHED)):
                # 上报压制，**动作不能一起吞掉**。
                #
                # 被压制的通常是「同一指标的预警级」，而预警级往往也带无害动作。
                # #5 冷链就是实例：预警级（level=1）配 `voice_play`，报警级
                # （level=2）配声光 + 工单。真超温时两条同时成立，若不接管动作，
                # 语音就永远不播——告警在页面上有、现场没声音。
                if verdict.actions:
                    taken.setdefault(verdict.metric, []).extend(verdict.actions)
                continue
            kept.append(verdict)

        if moment is not None:
            for verdict in kept:
                if verdict.event in (EVENT_FIRED, EVENT_LATCHED):
                    old = self._reported.get(verdict.metric)
                    level = verdict.level if old is None else max(old[0], verdict.level)
                    self._reported[verdict.metric] = (level, moment)

        if not taken:
            return kept
        merged: list[Verdict] = []
        for verdict in kept:
            extra = taken.get(verdict.metric)
            if not extra or verdict.event not in (EVENT_FIRED, EVENT_LATCHED):
                merged.append(verdict)
                continue
            actions = list(verdict.actions)
            for action in extra:
                if action not in actions:
                    actions.append(action)
            merged.append(replace(verdict, actions=tuple(actions)) if actions
                          else verdict)
        return merged

    def _merge_same_level(self, verdicts: list[Verdict]) -> list[Verdict]:
        """同一指标、同一等级、同一事件类型只上报一条。

        #2 一个指标配了两条判据（罩温 > 250 与 10 分钟温升 > 80），一次积热冲击
        会让两条同时成立。不合并的后果不是「多一条日志」，而是：

        - 小程序上两条文案完全一样的告警，用户以为设备重复上报；
        - 两条规则都带 `ticket=cleanup` → **同一次事故开出两张清理工单**；
        - 两条都带 `gas_valve_close` → 互锁层收到两次重复请求。

        合并保留第一条，把其余判据的边界拼进正文（`（同时成立：> 80）`），
        既不丢信息，也不刷屏。已成立的判据在 `snapshot()` 里逐条可查。
        """
        if len(verdicts) < 2:
            return verdicts
        rules = {rule.id: rule for rule in self._rules}
        buckets: dict[tuple[str, int, str], list[Verdict]] = {}
        order: list[tuple[str, int, str]] = []
        for verdict in verdicts:
            key = (verdict.metric, verdict.level, verdict.event)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(verdict)

        merged: list[Verdict] = []
        for key in order:
            group = buckets[key]
            head = group[0]
            if len(group) > 1 and head.event in (EVENT_FIRED, EVENT_LATCHED):
                others = [rules[item.rule_id].describe_bound()
                          for item in group[1:] if item.rule_id in rules]
                if others:
                    head = replace(
                        head, message=f"{head.message}（同时成立：{'、'.join(others)}）",
                    )
            merged.append(head)
        return merged

    def _settle_tickets(self, verdicts: list[Verdict]) -> list[Verdict]:
        """工单去重：一次事故只开一张。

        收档档积热时，闩锁规则每 `cooldown_sec`（60 秒）重报一次 `latched`，
        每次都带 `ticket=cleanup`。不去重的话，一次积热事故能开出几十张清理
        工单，值班表直接被刷满，真事故反而被淹掉。

        口径：`(metric, ticket)` 为键，事故存续期间只开一张——存续指「同类规则
        仍在越界或仍在闩锁」，全部回落 + 人工复位之后才允许下一张开单。
        """
        kept: list[Verdict] = []
        for verdict in verdicts:
            if not verdict.ticket:
                kept.append(verdict)
                continue
            if verdict.event == EVENT_RELEASED:
                # 恢复正常不补单（同 _verdict 里的口径）
                kept.append(replace(verdict, ticket=""))
                continue
            key = (verdict.metric, verdict.ticket)
            if key in self._tickets_open:
                kept.append(replace(verdict, ticket=""))
                continue
            self._tickets_open.add(key)
            kept.append(verdict)
        return kept

    def _close_settled_tickets(self) -> None:
        """事故结束（全部回落 + 已人工复位）后清掉去重标记，允许下次再开单。"""
        for key in list(self._tickets_open):
            metric, ticket = key
            still_active = False
            for rule in self._rules:
                if rule.metric != metric or rule.ticket != ticket:
                    continue
                timers = self._timers.get(rule.id)
                state = self.state_store.peek(rule.id)
                if (timers is not None and timers.breached) or (
                    state is not None and state.latched
                ):
                    still_active = True
                    break
            if not still_active:
                self._tickets_open.discard(key)

    def _push_windows(self, moment: float, metrics: Mapping[str, float]) -> None:
        for (metric, _), window in self._windows.items():
            value = metrics.get(metric)
            if value is not None:
                window.push(moment, float(value))

    def _compare_value(self, rule: Rule, metrics: Mapping[str, float]) -> float | None:
        """取参与比较的值：窗口规则用聚合值，其余用瞬时值。"""
        if rule.kind != "window":
            value = metrics.get(rule.metric)
            return None if value is None else float(value)
        window = self._windows.get(rule.window_key)  # type: ignore[arg-type]
        if window is None:
            return None
        return window.aggregate(rule.aggregate)

    def _judge(
        self, rule: Rule, metrics: Mapping[str, float], raw: Mapping[str, float], moment: float,
    ) -> Verdict | None:
        timers = self._timers.setdefault(rule.id, _Timers())
        state = self.state_store.get(rule.id)
        value = self._compare_value(rule, metrics)
        if value is None:
            return None  # 没数据不猜值，宁可静默
        # 原始瞬时值只用于展示对照；回放没单独给时退化成参与比较的值
        raw_value = float(raw.get(rule.metric, value))

        # 多源交叉不成立 = 规则不成立：清计时，避免视觉恢复后火焰计时残留秒触发
        if not rule.requires_met(metrics):
            timers.breach_since = None
            return self._maybe_release(rule, timers, state, float(value), float(raw_value))

        breached = rule.matches(value)
        if not breached:
            timers.breach_since = None
            return self._maybe_release(rule, timers, state, float(value), float(raw_value))

        if timers.breach_since is None:
            timers.breach_since = moment
        elapsed = moment - timers.breach_since
        required = rule.effective_duration
        if required is None:
            required = self.default_duration_sec
        if elapsed < required:
            timers.breached = True
            return None  # 还在持续时长内，先不上报（注意不设 alerted）

        # 冷却：同一规则在冷却期内只报一次，避免 2Hz 刷屏
        if timers.last_fired is not None and moment - timers.last_fired < self.cooldown_sec:
            timers.breached = True
            return None

        timers.last_fired = moment
        timers.breached = True
        timers.alerted = True
        state.fire_count += 1
        if rule.latch and not state.latched:
            state.latch(self._wall_clock())
            return self._verdict(rule, EVENT_FIRED, float(value), float(raw_value), elapsed, True)
        if state.latched:
            return self._verdict(rule, EVENT_LATCHED, float(value), float(raw_value), elapsed, True)
        return self._verdict(rule, EVENT_FIRED, float(value), float(raw_value), elapsed, False)

    def _maybe_release(
        self, rule: Rule, timers: _Timers, state, value: float, raw_value: float,
    ) -> Verdict | None:
        """从越界回到正常时产一条 released；闩锁规则只报状态不发解锁。

        只有「之前真的报过」才发恢复通知——见 `_Timers` 的 `alerted` 说明。
        """
        timers.breached = False
        if not timers.alerted:
            return None
        timers.alerted = False
        return self._verdict(
            rule, EVENT_RELEASED, value, raw_value, 0.0, bool(state.latched),
        )

    def _verdict(
        self, rule: Rule, event: str, value: float, raw_value: float, elapsed: float, latched: bool,
    ) -> Verdict:
        if event == EVENT_RELEASED:
            # 恢复事件不能用报警文案：小程序上「已恢复正常」和「超阈值」是两句话
            if rule.requires and rule.matches(value):
                # AND 规则的特殊情形：主判据还成立却恢复了，说明是前置条件不再满足
                # （#3 火焰还在、但人回来了）。直接说「已恢复正常（flame_b=1）」会让
                # 用户以为是误报——必须把「哪个条件变了」讲清楚。
                message = (
                    f"{rule.atype} 判据已解除（{rule.metric}={value:g}，"
                    f"前置条件不再成立）"
                )
            else:
                message = f"{rule.atype} 已恢复正常（{rule.metric}={value:g}）"
            if latched:
                message += "，通道仍处闩锁，需人工确认"
        elif event == EVENT_LATCHED:
            message = f"{rule.atype} 仍在报警状态，待人工复位（{rule.metric}={value:g}）"
        else:
            message = rule.describe(value)
        return Verdict(
            rule_id=rule.id,
            metric=rule.metric,
            event=event,
            value=value,
            raw=raw_value,
            level=rule.level,
            message=message,
            # 恢复事件不带动作：联锁的「关闭」动作在这里重复下发只会多关一次阀，
            # 而真正的恢复动作（开阀、恢复供电）必须等人工确认之后才做——和闩锁
            # 的语义保持一致。谁负责恢复，由上层看 ack 结果决定，不在这里猜。
            actions=() if event == EVENT_RELEASED else rule.actions,
            latched=latched,
            elapsed=elapsed,
            # 恢复事件不带 ticket：报警解除后再补一张清理单，只会让工单列表变噪声
            ticket=rule.ticket if event != EVENT_RELEASED else "",
        )

    # ---------------------------------------------------------------- 人工复位
    def ack(self, rule_id: str, reviewer: str, *, by_card: bool = False) -> AckResult:
        """人工确认复位（#1「浓度回落且人工确认后复位」）。

        三条硬规则，缺一条都不放行：
        1. 规则必须真的处于闩锁；
        2. 复位人不能为空（留痕，进审计）；
        3. **当前必须已回落**——越界期间点确认无效，否则等于把「还在漏气」
           硬按成正常。

        唯一的例外是 `by_card=True` 且规则声明了 `card_reset`（#3 明火离人）：
        刷卡的语义就是「我人回来了」，而人回来正是这个判据失效的直接原因，
        等传感器反应过来再解锁只会让厨师以为卡刷坏了。燃气（#1）不声明这个
        属性，所以「气还在漏就按确认」依然被拒。
        """
        reviewer = str(reviewer or "").strip()
        with self._lock:
            rule = next((item for item in self._rules if item.id == rule_id), None)
            if rule is None:
                return AckResult(False, rule_id, "规则不在当前规则集中", reviewer)
            state = self.state_store.peek(rule_id)
            if state is None or not state.latched:
                return AckResult(False, rule_id, "该规则未处于闩锁，无需复位", reviewer)
            if not reviewer:
                return AckResult(False, rule_id, "复位人不能为空（需留痕）", reviewer)
            timers = self._timers.setdefault(rule_id, _Timers())
            if timers.breached and not (by_card and rule.card_reset):
                return AckResult(
                    False, rule_id,
                    "当前仍处于越界状态，必须先回落再人工确认（防「带故障复位」）", reviewer,
                )
            state.ack(reviewer, self._wall_clock())
            # 人工复位 = 本案结案：清掉工单去重标记，下次再积热要重新开单。
            # 只靠 `_close_settled_tickets` 的「回落 + 未闩锁」判断不够——复位
            # 后第一帧往往立刻又越界，那一帧即被误判成「事故仍在进行」。
            if rule.ticket:
                self._tickets_open.discard((rule.metric, rule.ticket))
            log.info("规则人工复位：rule=%s reviewer=%s", rule_id, reviewer)
            return AckResult(True, rule_id, "已复位", reviewer)

    def ack_all(self, reviewer: str) -> list[AckResult]:
        """批量复位（管理端「全部确认」按钮）；逐条返回结果，不因一条失败中断。"""
        return [self.ack(rid, reviewer) for rid in self.state_store.latched_rules()]

    def reset(self) -> None:
        """强制清空计时与闩锁（仅测试/现场检修用，不走审计）。"""
        with self._lock:
            self._timers = {rule.id: _Timers() for rule in self._rules}
            self._reported.clear()
            self.state_store.clear()

    # ---------------------------------------------------------------- 持久化
    def persist(self, path: str | None = None) -> dict | str:
        """落盘闩锁状态。给 path 写 JSON（服务端），不给就返回 NVS blob（端侧）。"""
        with self._lock:
            if path is None:
                return self.state_store.to_nvs_blob()
            self.state_store.save(path)
            return self.state_store.to_dict()

    def restore(self, *, path: str | None = None, nvs_blob: str | None = None) -> list[str]:
        """恢复闩锁。返回恢复后仍处于闩锁的规则 id（上电日志要打出来）。"""
        with self._lock:
            if nvs_blob is not None:
                self.state_store = RuleStateStore.from_nvs_blob(nvs_blob, self.device_id)
            elif path is not None:
                self.state_store = RuleStateStore.load(path, self.device_id)
            else:
                return []
            for broken in self.state_store.broken_lines:
                log.warning("NVS 状态 blob 第 %d 行损坏，已跳过", broken)
            latched = self.state_store.latched_rules()
            if latched:
                log.warning("上电恢复：%d 条规则仍处闩锁，需人工复位 %s", len(latched), latched)
            return latched

    # ---------------------------------------------------------------- 观测
    def snapshot(self, now: float | None = None) -> dict:
        """演示页/日志用只读快照：档位、宽限、每条规则的当前状态与窗口摘要。"""
        with self._lock:
            moment = self._clock() if now is None else float(now)
            rules: dict[str, dict] = {}
            for rule in self._rules:
                timers = self._timers.get(rule.id) or _Timers()
                state = self.state_store.peek(rule.id)
                rules[rule.id] = {
                    "metric": rule.metric,
                    "kind": rule.kind,
                    "profile": rule.profile,
                    "level": rule.level,
                    "bound": rule.describe_bound(),
                    "enabled": rule.enabled,
                    "profile_active": rule.accepts_profile(self.profile, self.dataset),
                    "profile_kind": rule.profile_kind,
                    "breached": timers.breached,
                    "elapsed_sec": (
                        0.0 if timers.breach_since is None
                        else round(moment - timers.breach_since, 1)
                    ),
                    "latched": bool(state and state.latched),
                    "latched_at": None if state is None else state.latched_at,
                    "acked_by": None if state is None else state.acked_by,
                    "fire_count": 0 if state is None else state.fire_count,
                }
            return {
                "device_id": self.device_id,
                "profile": self.profile,
                "dataset": self.dataset,
                "boot_grace_sec": self.boot_grace_sec,
                "in_grace": self.in_grace(moment),
                "grace_remaining_sec": round(self.grace_remaining(moment), 1),
                "rules": rules,
                "latched": self.state_store.latched_rules(),
                "windows": {
                    f"{metric}@{sec:g}s": window.snapshot()
                    for (metric, sec), window in self._windows.items()
                },
            }

    def window(self, metric: str, window_sec: float) -> SlidingWindow | None:
        with self._lock:
            return self._windows.get((metric, float(window_sec)))


__all__ = [
    "AckResult", "EVENT_FIRED", "EVENT_LATCHED", "EVENT_RELEASED",
    "LATCHED", "NORMAL", "RuleEngine", "Verdict",
]
