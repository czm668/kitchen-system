# -*- coding: utf-8 -*-
"""规则运行状态：闩锁、去抖计时、冷却时间，以及它们的持久化。

## 为什么只持久化闩锁

引擎里的计时用 `time.monotonic()`（不受系统改时间影响），**重启即失效**，
存下来毫无意义——重启后单调时钟从零开始，反序列化出一个 9000 秒的
`breach_since` 会让规则永远等不到「持续 30 秒」。

但闩锁必须跨重启保留：掉电重开如果自动解锁，燃气泄漏时「断电即恢复供气」
就是明确的安全事故（对应 04 文档 §0.5「恢复动作一律人工确认」）。所以：

| 状态 | 存不存 | 理由 |
|---|---|---|
| `latched` / `latched_at` / `acked_by` | **存** | 失效安全，重启不得自动解锁 |
| `breach_since`（去抖计时） | 不存 | 单调时钟重启归零，存了会算出负值 |
| `last_fired`（冷却计时） | 不存 | 同上；重启后重新计冷却更保守 |

`latched_at` 用**墙钟**（`time.time`）存，只为展示「锁了多久」，不参与判定。

## 两种落地形态同一份结构

- 端侧（ESP32）：`to_nvs_blob()` → 紧凑文本写 NVS；
- 服务端：`to_dict()` → JSON 落 `device_baselines` / 状态表。

同一份字段名，避免「端侧存一套、云端存一套，对比时对不上」。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import RuleError

# 闩锁状态（与 env_points.state 的取值保持同一套词表，见 03_数据库设计.md §3.1）
NORMAL = "normal"
BREACHED = "breached"
LATCHED = "latched"

LATCH_STATES: tuple[str, ...] = (NORMAL, BREACHED, LATCHED)


@dataclass
class RuleState:
    """单条规则在单个设备上的运行态。"""

    latched: bool = False
    latched_at: float | None = None  # 墙钟（time.time），仅展示
    acked_by: str | None = None
    acked_at: float | None = None
    fire_count: int = 0

    def latch(self, wall_now: float) -> None:
        self.latched = True
        self.latched_at = float(wall_now)
        self.acked_by = None
        self.acked_at = None

    def ack(self, reviewer: str, wall_now: float) -> None:
        self.latched = False
        self.acked_by = str(reviewer)
        self.acked_at = float(wall_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RuleState":
        """只认已知字段，多余键忽略——老版本 blob 反序列化不能炸。"""
        return cls(
            latched=bool(data.get("latched", False)),
            latched_at=_as_float(data.get("latched_at")),
            acked_by=(None if data.get("acked_by") in (None, "") else str(data["acked_by"])),
            acked_at=_as_float(data.get("acked_at")),
            fire_count=int(data.get("fire_count") or 0),
        )


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class RuleStateStore:
    """设备级状态集合；key = `f"{device_id}:{rule_id}"`。

    之所以把 device_id 编进 key 而不是做成两层字典：端侧 NVS 是扁平的
    key-value，扁平结构能一对一映射过去，不需要额外的编码约定。
    """

    device_id: str = ""
    states: dict[str, RuleState] = field(default_factory=dict)
    saved_at: float | None = None
    # 解析 NVS blob 时的坏行行号（宽容解析的副产物，供上电自检日志告警）
    broken_lines: list[int] = field(default_factory=list)

    # ------------------------------------------------------------ 读写
    @staticmethod
    def key(device_id: str, rule_id: str) -> str:
        return f"{device_id}:{rule_id}"

    def get(self, rule_id: str) -> RuleState:
        """取（或惰性建）规则状态。返回的是引用，调用方可直接改。"""
        return self.states.setdefault(self.key(self.device_id, rule_id), RuleState())

    def peek(self, rule_id: str) -> RuleState | None:
        return self.states.get(self.key(self.device_id, rule_id))

    def latched_rules(self) -> list[str]:
        """当前处于闩锁的规则 id 列表（去掉 device_id 前缀）。"""
        prefix = f"{self.device_id}:"
        return sorted(
            key[len(prefix):] if key.startswith(prefix) else key
            for key, state in self.states.items()
            if state.latched
        )

    def clear(self) -> None:
        self.states.clear()

    def prune(self, alive_rule_ids: set[str]) -> list[str]:
        """清掉已不在规则集里的状态，返回被清掉的 key。

        档位切换会带来规则集的增删，不清理的话 states 会随运行时间无限增长
        （端侧 NVS 有容量上限，这是必须做的）。
        """
        alive = {self.key(self.device_id, rid) for rid in alive_rule_ids}
        dead = [key for key in self.states if key not in alive]
        for key in dead:
            self.states.pop(key, None)
        return dead

    # ------------------------------------------------------------ 持久化：服务端 JSON
    def to_dict(self, wall_now: float | None = None) -> dict:
        return {
            "schema": 1,
            "device_id": self.device_id,
            "saved_at": float(time.time() if wall_now is None else wall_now),
            "states": {key: state.to_dict() for key, state in self.states.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleStateStore":
        if not isinstance(data, dict):
            raise RuleError("状态 blob 顶层必须是对象")
        store = cls(device_id=str(data.get("device_id") or ""))
        store.saved_at = _as_float(data.get("saved_at"))
        for key, raw in (data.get("states") or {}).items():
            if isinstance(raw, dict):
                store.states[str(key)] = RuleState.from_dict(raw)
        return store

    def save(self, path: str | Path) -> Path:
        """原子写：先写临时文件再替换，避免掉电写出半个 JSON（NVS 同理要靠双槽）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path, device_id: str = "") -> "RuleStateStore":
        """文件不存在返回空仓库（首次开机就是这种情形，不该报错）。"""
        target = Path(path)
        if not target.is_file():
            return cls(device_id=device_id)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuleError(f"状态文件损坏：{target}（{exc}）") from exc
        store = cls.from_dict(data)
        if device_id:
            store.device_id = device_id
        return store

    # ------------------------------------------------------------ 持久化：端侧 NVS blob
    def to_nvs_blob(self) -> str:
        """紧凑文本，模拟 ESP32 NVS 的一个 blob 值（行数 = 规则数）。

        格式：`rule_id|latched|latched_at|acked_by|fire_count`，只写有意义的行，
        空闩锁也写（0），因为「曾经清过锁」本身也是要覆盖的旧数据。
        字段内不允许出现 `|` 和换行 —— rule_id 来自数据库自增 id，天然安全。
        """
        prefix = f"{self.device_id}:"
        lines: list[str] = []
        for key in sorted(self.states):
            state = self.states[key]
            rule_id = key[len(prefix):] if key.startswith(prefix) else key
            lines.append("|".join([
                rule_id,
                "1" if state.latched else "0",
                "" if state.latched_at is None else f"{state.latched_at:.0f}",
                state.acked_by or "",
                str(state.fire_count),
            ]))
        return "\n".join(lines)

    @classmethod
    def from_nvs_blob(cls, blob: str, device_id: str = "") -> "RuleStateStore":
        """解析 NVS blob；单行损坏只跳过该行并记 warning 位置，不做全量失败。

        端侧上电时「一条坏行导致整个燃气锁失效」是不可接受的降级方式，
        所以这里选择宽容解析，但保留 `broken_lines` 供日志告警。
        """
        store = cls(device_id=device_id)
        for line_no, line in enumerate((blob or "").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 2:
                store.broken_lines.append(line_no)
                continue
            rule_id = parts[0].strip()
            if not rule_id:
                store.broken_lines.append(line_no)
                continue
            state = RuleState(
                latched=parts[1].strip() == "1",
                latched_at=_as_float(parts[2]) if len(parts) > 2 else None,
                acked_by=(parts[3] or None) if len(parts) > 3 else None,
                fire_count=int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
            )
            store.states[cls.key(device_id, rule_id)] = state
        return store
