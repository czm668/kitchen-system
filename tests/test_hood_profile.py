# -*- coding: utf-8 -*-
"""#2 排烟积热的分档调度：档位表、配置缓存、端侧运行时。

三层各测各的，再在 `TestKitchenRuntime` 里做端到端。重点不是「正常路径能跑」，
而是这几类「跑起来看着正常、实际失效」的形态：

- 档位表重叠 → 排班不按预期执行；
- 切档重装规则 → 无关规则的计时归零，燃气泄漏要晚报警；
- 动作名写错 → 继电器永远不动，但告警照报；
- 配置缓存坏了 → 沉默不判定（本项目要求降级到最严档继续判）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kitchen.rules.config_store import (
    BLOB_SCHEMA,
    ConfigError,
    KitchenConfig,
    load_builtin,
    load_or_builtin,
)
from kitchen.rules.schedule import (
    MINUTES_PER_DAY,
    ProfileSchedule,
    ProfileSlot,
    ScheduleError,
    builtin_schedule,
    format_hhmm,
    parse_hhmm,
)
from kitchen.runtime import KitchenRuntime, RuntimeConfigError
from tests.conftest import _parse_insert_rows, _sql_value

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))


def ts(day: int, hour: int, minute: int, second: int = 0) -> float:
    """2026-11-12 是周四；day 指当月的日，方便写「周四 10:30」。"""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=CST).timestamp()


class FakeClock:
    """同时充当本地时钟（算档位）与单调时钟（算持续时长）。

    两者本来是两个域：档位要真实时间戳（2026-11-12 10:30 落在哪个时段），
    持续时长只要差值。测试里用同一个实例是为了省事——只要基准值取真实时间戳，
    两个语义同时成立。
    """

    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


def make_runtime(clock: FakeClock, config: KitchenConfig | None = None, **kwargs) -> KitchenRuntime:
    return KitchenRuntime(
        config if config is not None else load_builtin(),
        device_id="K-TEST",
        clock=clock,
        wall_clock=clock,
        **kwargs,
    )


def feed(rt: KitchenRuntime, clock: FakeClock, metrics: dict, seconds: float,
         step: float = 1.0) -> list:
    """按固定步长喂数据，返回每一步的 TickResult。"""
    results = []
    for _ in range(max(1, int(round(seconds / step)))):
        clock.advance(step)
        results.append(rt.tick(metrics))
    return results


def all_verdicts(results: list) -> list:
    return [verdict for result in results for verdict in result.verdicts]


def fired_rules(results: list) -> list[str]:
    return [v.rule_id for v in all_verdicts(results) if v.fired]


def all_actions(results: list) -> tuple[str, ...]:
    """所有帧下发的动作。

    不能只看最后一帧：冷却期（默认 60 秒）内引擎不会重复产生事件，
    所以「触发帧」之后那些帧的 actions 都是空的。
    """
    return tuple(action for result in results for action in result.actions)


def all_tickets(results: list) -> tuple[str, ...]:
    return tuple(ticket for result in results for ticket in result.tickets)


# ============================================================ 档位表
class TestProfileSchedule:
    def test_营业时段命中(self):
        assert builtin_schedule().profile_at(ts(12, 10, 30)) == "business"
        assert builtin_schedule().profile_at(ts(12, 20, 0)) == "business"

    def test_备餐时段命中(self):
        assert builtin_schedule().profile_at(ts(12, 15, 0)) == "prep"

    def test_收档时段跨零点前半段(self):
        assert builtin_schedule().profile_at(ts(12, 23, 30)) == "closed"

    def test_收档时段跨零点后半段(self):
        """凌晨 3 点属于「前一天 22:00 开始」的那个时段，不是新的一天。"""
        assert builtin_schedule().profile_at(ts(12, 3, 0)) == "closed"
        assert builtin_schedule().profile_at(ts(12, 8, 59)) == "closed"

    def test_跨零点按时段起始日归属(self):
        """只排周一的 22:00-02:00，周二凌晨 01:00 应命中（属于周一那个时段），
        而周一凌晨 01:00 不该命中（它属于周日，周日没排）。"""
        schedule = ProfileSchedule(
            slots=(ProfileSlot("closed", parse_hhmm("22:00"), parse_hhmm("02:00"), days=(0,)),),
            default="business",
        )
        # 2026-11-16 是周一，2026-11-17 是周二
        assert schedule.profile_at(ts(16, 23, 0)) == "closed"    # 周一 23:00
        assert schedule.profile_at(ts(17, 1, 0)) == "closed"     # 周二 01:00 ← 周一那段的尾巴
        assert schedule.profile_at(ts(16, 1, 0)) == "business"   # 周一 01:00 ← 属于周日，未排班
        assert schedule.profile_at(ts(17, 23, 0)) == "business"  # 周二没排

    def test_默认档是最严的收档(self):
        """时段表残缺时不能退回营业档只播语音——这是安全默认值。"""
        assert builtin_schedule().default == "closed"
        empty = ProfileSchedule(slots=())
        assert empty.profile_at(ts(12, 12, 0)) == "closed"

    def test_时段重叠报错(self):
        schedule = ProfileSchedule(slots=(
            ProfileSlot("business", parse_hhmm("10:00"), parse_hhmm("14:00")),
            ProfileSlot("prep", parse_hhmm("13:00"), parse_hhmm("16:00")),   # 与上一条重叠 1 小时
        ))
        with pytest.raises(ScheduleError, match="重叠"):
            schedule.validate({"business", "prep", "closed"})

    def test_跨零点时段也会被重叠检测抓到(self):
        """22:00-02:00 与次日 01:00-04:00 实际重叠，展开成绝对区间才查得出来。"""
        schedule = ProfileSchedule(slots=(
            ProfileSlot("closed", parse_hhmm("22:00"), parse_hhmm("02:00")),
            ProfileSlot("prep", parse_hhmm("01:00"), parse_hhmm("04:00")),
        ))
        with pytest.raises(ScheduleError, match="重叠"):
            schedule.validate({"closed", "prep"})

    def test_零长度时段报错(self):
        with pytest.raises(ScheduleError, match="长度为 0"):
            ProfileSlot("business", parse_hhmm("10:00"), parse_hhmm("10:00"))

    def test_全天时段写法(self):
        slot = ProfileSlot("business", parse_hhmm("00:00"), parse_hhmm("24:00"))
        schedule = ProfileSchedule(slots=(slot,))
        assert schedule.profile_at(ts(12, 0, 0)) == "business"
        assert schedule.profile_at(ts(12, 23, 59)) == "business"

    def test_时间格式错误直接报错(self):
        for bad in ("", "10", "10:70", "25:00", "abc"):
            with pytest.raises(ScheduleError):
                parse_hhmm(bad)
        assert format_hhmm(parse_hhmm("24:00")) == "00:00"

    def test_档位名必须在规则集里(self):
        """切到一个没有阈值的档位，引擎一条都不判——不报错、不告警、看着正常。"""
        schedule = ProfileSchedule(
            slots=(ProfileSlot("night", parse_hhmm("22:00"), parse_hhmm("06:00")),),
            default="closed",
        )
        with pytest.raises(ScheduleError, match="不在规则集里"):
            schedule.validate({"business", "prep"})

    def test_默认档位也要在规则集里(self):
        schedule = ProfileSchedule(slots=(), default="holiday")
        with pytest.raises(ScheduleError, match="默认档位"):
            schedule.validate({"business", "prep", "closed"})

    def test_next_change_给下一次切换时刻(self):
        schedule = builtin_schedule()
        nxt = schedule.next_change(ts(12, 10, 30))
        assert nxt == pytest.approx(ts(12, 14, 0))       # 营业 → 备餐

    def test_next_change_跨零点取次日边界(self):
        nxt = builtin_schedule().next_change(ts(12, 23, 0))
        # 22:00-09:00 这一段的下一个边界是次日 09:00，之后才是 10:00 营业
        assert nxt == pytest.approx(ts(13, 9, 0))

    def test_序列化往返(self):
        original = builtin_schedule()
        restored = ProfileSchedule.from_dict(json.loads(json.dumps(original.to_dict())))
        assert restored.slots == original.slots
        assert restored.default == original.default
        for moment in (ts(12, 3, 0), ts(12, 11, 0), ts(12, 15, 0), ts(12, 23, 0)):
            assert restored.profile_at(moment) == original.profile_at(moment)


# ============================================================ 配置缓存
class TestConfigStore:
    def test_内置默认可加载且不降级(self):
        config = load_builtin()
        assert config.ok
        assert config.source == "builtin"
        assert len(config.threshold_rows) == 25   # 24 条环境 + 1 条 #3 明火离人

    def test_内置默认与种子阈值行逐条一致(self):
        """`kitchen_config_default.json` 是从 seed SQL 生成的，**不能手改**。

        这条测试就是那道「生成物必须可复现」的护栏：谁直接改了 JSON 里的阈值，
        配置和数据库种子立刻对不上，现场就会出现「端侧按旧阈值判、平台按新阈值
        显示」的灵异现象。要改阈值请改 seed 再重新生成。
        """
        sql_text = (ROOT / "fixtures" / "seed_kitchen.sql").read_text(encoding="utf-8")
        point_metric = {}
        for cells in _parse_insert_rows(sql_text, "env_points"):
            point_metric[int(_sql_value(cells[0]))] = str(_sql_value(cells[3]))

        columns = ("id", "point_id", "kind", "profile", "lower_bound", "upper_bound",
                   "duration_sec", "window_sec", "aggregate", "level", "enabled")
        from_sql = []
        for cells in _parse_insert_rows(sql_text, "env_thresholds"):
            row = {name: _sql_value(value) for name, value in zip(columns, cells)}
            row["metric"] = point_metric[int(row["point_id"])]
            from_sql.append(row)

        from_json = [dict(row) for row in load_builtin().threshold_rows]
        assert len(from_json) == len(from_sql)
        for expected, actual in zip(from_sql, from_json):
            for key in columns:
                assert actual.get(key) == expected.get(key), (
                    f"阈值行 id={expected['id']} 的 {key} 与 seed 不一致"
                )
            assert actual.get("metric") == expected["metric"]

    def test_默认配置里三档温升窗口都是十分钟(self):
        """三档 `window_sec` 必须相同，否则切档时窗口 key 变化、温升从头计时。"""
        rows = load_builtin().rows_for()
        windows = {
            int(row["id"]): row["window_sec"]
            for row in rows
            if row["metric"] == "hood_temp" and row["kind"] == "window"
        }
        assert set(windows.values()) == {600.0}

    def test_demo口径筛掉real行(self):
        config = load_builtin()          # dataset=demo
        rows = config.rows_for()
        # profile=None 的行（#3 明火离人）不限档位也不限口径，两种配置里都在
        allowed = ("demo", "business", "prep", "closed", "")
        assert all(str(row.get("profile") or "") in allowed for row in rows)
        real_only = [row["id"] for row in rows if row["metric"] == "sterilize_temp"]
        # 消毒的三行全是 real 口径，demo 配置里不该出现
        assert real_only == []

    def test_blob往返(self):
        config = load_builtin()
        restored = KitchenConfig.from_blob(config.to_blob())
        assert restored.dataset == config.dataset
        assert len(restored.threshold_rows) == len(config.threshold_rows)
        assert restored.schedule.slots == config.schedule.slots

    def test_摘要不匹配判为损坏(self):
        blob = load_builtin().to_blob()
        tampered = blob.replace("280.0", "180.0")
        assert tampered != blob
        with pytest.raises(ConfigError, match="校验和"):
            KitchenConfig.from_blob(tampered)

    def test_版本不认识报错而不是凑合读(self):
        payload = json.loads(load_builtin().to_blob())
        payload["schema"] = BLOB_SCHEMA + 1
        with pytest.raises(ConfigError, match="版本"):
            KitchenConfig.from_blob(json.dumps(payload))

    def test_保存后读回不降级(self, tmp_path):
        path = tmp_path / "kitchen_config.json"
        config = load_builtin()
        config.version = 7
        config.save(path)
        restored = load_or_builtin(path)
        assert restored.ok
        assert restored.source == "nvs"
        assert restored.version == 7

    def test_缓存损坏时降级到内置默认而非罢工(self, tmp_path):
        """断网 + NVS 损坏 = 最危险的组合。要求：降级到最严档继续判定。"""
        path = tmp_path / "kitchen_config.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        config = load_or_builtin(path)
        assert not config.ok
        assert "降级" in config.degraded
        assert config.schedule.default == "closed"
        assert len(config.threshold_rows) == 25   # 24 条环境 + 1 条 #3 明火离人

    def test_首次上电没有缓存也降级(self, tmp_path):
        config = load_or_builtin(tmp_path / "nope.json")
        assert not config.ok
        assert "首次上电" in config.degraded

    def test_写入是原子替换(self, tmp_path):
        """写一半掉电不能把好配置毁掉，所以先落 .tmp 再 replace。"""
        path = tmp_path / "kitchen_config.json"
        config = load_builtin()
        config.save(path)
        config.save(path)                       # 覆盖写一次
        assert not (tmp_path / "kitchen_config.json.tmp").exists()
        assert load_or_builtin(path).ok


# ============================================================ 端侧运行时
class TestKitchenRuntime:
    def test_冷启动按本地时间定档(self):
        """凌晨 3 点冷启动不能假设自己在营业（营业档只播语音，等于不设防）。"""
        rt = make_runtime(FakeClock(ts(12, 3, 0)))
        assert rt.engine.profile == "closed"
        rt = make_runtime(FakeClock(ts(12, 11, 0)))
        assert rt.engine.profile == "business"

    def test_营业档超温只播报不动继电器(self):
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        assert rt.engine.profile == "business"
        results = feed(rt, clock, {"hood_temp": 300.0}, 5)
        fired = [v for v in all_verdicts(results) if v.fired]
        assert fired, "营业档 300℃ > 280℃ 应当触发"
        actions = all_actions(results)
        assert "voice_play" in actions
        assert "gas_valve_close" not in actions, "营业档不该动燃气阀——会打断出餐"
        assert "fan_on" not in actions
        assert all_tickets(results) == ()

    def test_备餐档超温联动关阀开排风(self):
        clock = FakeClock(ts(12, 15, 0))
        rt = make_runtime(clock)
        assert rt.engine.profile == "prep"
        results = feed(rt, clock, {"hood_temp": 300.0}, 5)
        actions = all_actions(results)
        assert "gas_valve_close" in actions
        assert "fan_on" in actions
        assert all_tickets(results) == ()      # 备餐有人在，不开清理单

    def test_收档档超温联动并带清理工单(self):
        clock = FakeClock(ts(12, 23, 0))
        rt = make_runtime(clock)
        assert rt.engine.profile == "closed"
        results = feed(rt, clock, {"hood_temp": 300.0}, 5)
        actions = all_actions(results)
        assert "gas_valve_close" in actions
        assert "alarm_light_on" in actions, "收档无人值守，要靠声光留痕"
        assert all_tickets(results) == ("cleanup",)

    def test_三档阈值随档位变化(self):
        """同一个 270℃：营业档不报（未到 280），备餐/收档要报（>250 / >200）。"""
        for profile, moment, should_fire in (
            ("business", ts(12, 11, 0), False),
            ("prep", ts(12, 15, 0), True),
            ("closed", ts(12, 23, 0), True),
        ):
            clock = FakeClock(moment)
            rt = make_runtime(clock)
            assert rt.engine.profile == profile
            results = feed(rt, clock, {"hood_temp": 270.0}, 5)
            has_fired = any(v.fired for v in all_verdicts(results))
            assert has_fired is should_fire, f"{profile} 档 270℃ 的判定不符合预期"

    def test_档位随本地时间自动切换(self):
        clock = FakeClock(ts(12, 13, 59, 50))
        rt = make_runtime(clock)
        assert rt.engine.profile == "business"
        results = feed(rt, clock, {"hood_temp": 60.0}, 20, step=5)
        changed = [r for r in results if r.profile_changed]
        assert len(changed) == 1
        assert changed[0].previous_profile == "business"
        assert changed[0].profile == "prep"
        assert rt.engine.profile == "prep"

    def test_切档不重置其他规则的计时(self):
        """切档只改 `set_profile`，不重装规则。

        这条最容易写错：如果切档时顺手 `set_rules()` 一次，`_timers` 全部归零，
        #1 燃气（持续 30 秒确认）在档位切换的那一帧重新计时——泄漏时的报警
        硬生生晚 30 秒。所以要用一条「与档位无关」的规则来验证计时连续。
        """
        clock = FakeClock(ts(12, 13, 59, 40))
        rt = make_runtime(clock)
        # 燃气 0.50：超预警门限 0.45，未到联锁门限 0.55，需持续 30 秒
        warmup = feed(rt, clock, {"gas_level": 0.50}, 20, step=5)
        assert fired_rules(warmup) == [], "才 20 秒，不该触发"
        assert any(r.profile_changed for r in warmup), "这 20 秒里应跨过 14:00 切档"
        rest = feed(rt, clock, {"gas_level": 0.50}, 15, step=5)
        assert fired_rules(rest), (
            "累计 35 秒 > 30 秒应当触发；没触发说明切档把计时重置了"
        )

    def test_只改时段不重装规则(self):
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        config = load_builtin()
        config.schedule = ProfileSchedule(
            slots=(ProfileSlot("business", parse_hhmm("09:00"), parse_hhmm("21:00")),),
            default="closed",
        )
        assert rt.apply_config(config) is False, "阈值没变，不该重装规则"
        assert rt.engine.profile == "business"

    def test_改阈值才重装规则(self):
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        config = load_builtin()
        rows = [dict(row) for row in config.threshold_rows]
        for row in rows:
            if row["id"] == 3:
                row["upper_bound"] = 260.0        # 营业档阈值 280 → 260
        config.threshold_rows = tuple(rows)
        assert rt.apply_config(config) is True

    def test_未知动作名装载期报错(self):
        """写错动作名 → 告警照报、继电器永不动。宁可在装载期炸。"""
        json.dumps({"a": 1})                     # 保证 json 已导入
        scenario = {
            "hood_temp": {"atype": "排烟积热", "actions": ["fanOn"], "latch": True},
        }
        with pytest.raises(RuntimeConfigError, match="未知动作"):
            KitchenRuntime(load_builtin(), scenario_rules=scenario)

    def test_档位表与规则集不一致报错(self):
        config = load_builtin()
        config.schedule = ProfileSchedule(
            slots=(ProfileSlot("holiday", parse_hhmm("10:00"), parse_hhmm("12:00")),),
            default="closed",
        )
        with pytest.raises(RuntimeConfigError, match="档位表"):
            KitchenRuntime(config)

    def test_十分钟温升超限触发(self):
        """#2 的另一半判据：温度没到绝对阈值，但 10 分钟涨太快同样要报。"""
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        for index in range(11):                  # 11:00 → 11:10，200 → 320
            clock.advance(60)
            rt.tick({"hood_temp": 200.0 + index * 12})
        # 同指标同等级的两条判据会被合并成一条上报（rule 3 在前，rule 4 被并入
        # 正文），所以不能靠 verdict.rule_id 判断，直接查规则自身的触发计数
        state = rt.engine.state_store.peek("4")
        assert state is not None and state.fire_count > 0, "温升窗口规则应当触发"

    def test_温升不足不触发(self):
        """缓慢升温（10 分钟只涨 40℃）不该报——这是「正常出餐」的样子。"""
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        for _ in range(11):
            clock.advance(60)
            rt.tick({"hood_temp": 240.0})
        state = rt.engine.state_store.peek("4")
        assert state is None or state.fire_count == 0, "240℃ 平稳 10 分钟不该报温升"

    def test_next_wakeup_给下一次切档时刻(self):
        rt = make_runtime(FakeClock(ts(12, 11, 0)))
        assert rt.next_wakeup() == pytest.approx(ts(12, 14, 0))

    def test_快照带档位与配置来源(self):
        rt = make_runtime(FakeClock(ts(12, 11, 0)))
        snap = rt.snapshot()
        assert snap["profile"] == "business"
        assert snap["profile_default"] == "closed"
        assert snap["config_source"] == "builtin"
        assert snap["config_degraded"] == ""

    def test_降级配置也要能跑起来(self, tmp_path):
        """NVS 坏了 → 内置默认 + 最严档，判定链路必须照常工作。"""
        path = tmp_path / "kitchen_config.json"
        path.write_text("broken", encoding="utf-8")
        config = load_or_builtin(path)
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock, config)
        assert rt.snapshot()["config_degraded"]
        results = feed(rt, clock, {"hood_temp": 300.0}, 5)
        assert any(v.fired for v in all_verdicts(results))

    def test_一次事故只开一张清理工单(self):
        """收档档 300 秒内 200→380：绝对温度（>200）先成立，温升（>50）随后成立。

        两条规则都带 `ticket=cleanup`，且闩锁规则每 60 秒重报一次。不做去重的话
        一次积热事故能开出几十张清理工单——值班表被刷满，真事故反被淹掉。
        """
        clock = FakeClock(ts(12, 23, 0))
        rt = make_runtime(clock)
        results = []
        for index in range(10):
            clock.advance(30)
            results.append(rt.tick({"hood_temp": 200.0 + index * 20}))
        tickets = all_tickets(results)
        assert tickets == ("cleanup",), f"一次事故只能开一张单，实际 {tickets}"
        assert any(v.latched for v in all_verdicts(results))

    def test_人工复位之后允许再次开单(self):
        """清掉去重标记的时机是「回落 + 人工复位」，不是「回落」——
        否则人还没到现场，第二张单又开出来了。"""
        clock = FakeClock(ts(12, 23, 0))
        rt = make_runtime(clock)
        first_fire = all_tickets(feed(rt, clock, {"hood_temp": 300.0}, 5))
        assert first_fire == ("cleanup",)
        assert all_tickets(feed(rt, clock, {"hood_temp": 60.0}, 5)) == (), \
            "回落阶段不该再开单"
        assert rt.ack_all("E009")[0].ok
        # 复位后还要过 60 秒冷却才会重新触发（引擎的 anti-flap 设计）
        second = all_tickets(feed(rt, clock, {"hood_temp": 300.0}, 70, step=5))
        assert second == ("cleanup",), "复位后再次积热应重新开单"

    def test_单帧毛刺不会变成告警(self):
        """窗口规则也走去抖：一个采样点的毛刺不该让用户收到一条告警。

        这条是 `effective_duration` 从 0 改成「交给引擎默认 3 秒」的直接理由——
        `rise` 在窗口内首点滑出时会突变，没有去抖就是告警自己闪三下。
        """
        clock = FakeClock(ts(12, 11, 0))
        rt = make_runtime(clock)
        results = feed(rt, clock, {"hood_temp": 60.0}, 20)      # 平稳
        clock.advance(1)
        results.append(rt.tick({"hood_temp": 400.0}))           # 单帧尖峰
        results.extend(feed(rt, clock, {"hood_temp": 60.0}, 5)) # 回落
        assert fired_rules(results) == []

    def test_工单只在触发时出现不在恢复时出现(self):
        """「恢复正常」不该再补一张清理单。"""
        clock = FakeClock(ts(12, 23, 0))
        rt = make_runtime(clock)
        feed(rt, clock, {"hood_temp": 300.0}, 5)
        assert rt.engine.snapshot()["latched"], "收档档应闩锁"
        recovered = feed(rt, clock, {"hood_temp": 60.0}, 5)
        released = [v for v in all_verdicts(recovered) if v.event == "released"]
        assert released, "应上报恢复"
        assert all(v.ticket == "" for v in released)
        assert all(r.tickets == () for r in recovered)
