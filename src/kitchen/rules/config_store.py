# -*- coding: utf-8 -*-
"""端侧配置缓存：档位表 + 阈值行，断网沿用最后一份，坏了降级到内置默认。

## 为什么需要这一层

04 文档对 #2 的要求是「服务器下发，端侧缓存 NVS，断网按最后配置执行」。
但「缓存」这两个字很容易被做成「内存里存一份」——那样重启就没了。真实约束是：

| 场景 | 期望行为 |
|---|---|
| 正常在线 | 用服务端最新配置 |
| 断网 | 用最后一次成功下发的配置，**继续判定**，不降级成「只采不判」 |
| 断网 + 重启 | 从 NVS 读回那份配置，行为和断网前一致 |
| NVS 内容损坏 / 版本不认识 | 用内置默认（最严档），并标记 `degraded`，不崩 |
| 首次上电（NVS 空） | 用内置默认，等首次下发覆盖 |

「断网就停止判定」是最常见的错误实现：气还在漏、温还在升，设备却因为拿不到
阈值而沉默。本项目的口径是 **阈值宁可旧，也不能没有**。

## 为什么阈值和数据包分开校验

`sha256` 只防传输截断和手工改坏，不防「配置本身合法但语义错」（比如阈值方向
写反）。所以本层只做格式与完整性校验，语义校验交给 `loader.validate_rule()`
——两层职责分开，出问题时能一眼看出是「文件坏了」还是「配置写错了」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schedule import ProfileSchedule, ScheduleError

log = logging.getLogger("kitchen.config")

__all__ = [
    "BLOB_SCHEMA",
    "ConfigError",
    "KitchenConfig",
    "default_config_path",
    "load_or_builtin",
]

BLOB_SCHEMA = 1

# fixtures/ 相对本文件：src/kitchen/rules/config_store.py → 上溯四级
_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
DEFAULT_CONFIG_FILE = "kitchen_config_default.json"


class ConfigError(ValueError):
    """配置内容不合法（格式 / 版本 / 校验和不匹配）。"""


def default_config_path() -> Path:
    return _FIXTURES / DEFAULT_CONFIG_FILE


def _digest(payload: Mapping[str, Any]) -> str:
    """对 payload 做规范化 JSON 后取 sha256。

    `sort_keys=True` + `separators` 固定：否则字典插入顺序一变，同一份内容算出
    不同摘要，重启后被判成「损坏」。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class KitchenConfig:
    """端侧运行时需要的全部配置：档位表 + 阈值行 + 动作语义 + 取值口径。

    ## 为什么动作语义（`scenario_rules`）也在这里

    一开始它被放在单独的 `kitchen_rules.json` 里，运行时再去读一遍。结果是
    `KitchenRuntime` 少传一个参数，所有规则的 `actions` 就都是空的——**告警照
    报、继电器永不动**，而且不报错。配置的「完整性」应该是结构上保证的，
    不能靠调用方记得多传一个参数。

    并进来还有两个好处：随配置一起下发（管理端改动作不用改固件）、随配置一起
    进 NVS（断网时动作语义和阈值同步沿用，不会出现「新阈值配旧动作」）。
    """

    schedule: ProfileSchedule
    threshold_rows: tuple[Mapping[str, Any], ...] = ()
    scenario_rules: dict[str, dict] = field(default_factory=dict)
    dataset: str = "demo"
    version: int = 1
    updated_at: float = 0.0
    source: str = "builtin"       # builtin | server | nvs
    degraded: str = ""            # 非空 = 当前用的是降级配置，原因写这里

    # ------------------------------------------------------------ 查询
    def rows_for(
        self,
        profile: str | None = None,
        *,
        dataset: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """按档位 / 口径筛阈值行，供 `loader.load_rows()` 直接用。

        ## 端侧装载要传 `profile=None`（一次装全量）

        引擎的档位切换走 `set_profile()`，不是重新 `set_rules()`：后者会重置
        去抖计时并裁剪窗口。而三档的 `window_sec` 相同（都是 600），所以「一次
        装全量 + 切档只改 profile」能让 10 分钟温升窗口全程连续，切档瞬间不会
        重新计时。传了 `profile` 就只能拿到那一档，切档必须重装——那是错的用法，
        这个参数只给管理端做「这台设备营业档会判哪些规则」的预览用。

        ## 口径行的识别

        种子里口径写在 `profile` 列（值域 real/demo），也有下发版本用独立的
        `dataset` 列。两种都认，避免一份数据要改两处。
        """
        wanted_dataset = self.dataset if dataset is None else dataset
        out: list[Mapping[str, Any]] = []
        for row in self.threshold_rows:
            row_profile = str(row.get("profile") or "")
            row_dataset = str(row.get("dataset") or "")
            if not row_dataset and row_profile in ("real", "demo"):
                row_dataset, row_profile = row_profile, ""
            if row_dataset and row_dataset != wanted_dataset:
                continue
            if profile is not None and row_profile and row_profile != profile:
                continue
            out.append(row)
        return out

    @property
    def ok(self) -> bool:
        return not self.degraded

    # ------------------------------------------------------------ 序列化
    def to_payload(self) -> dict:
        return {
            "schema": BLOB_SCHEMA,
            "dataset": self.dataset,
            "version": int(self.version),
            "updated_at": float(self.updated_at),
            "schedule": self.schedule.to_dict(),
            "scenario_rules": {name: dict(body) for name, body in self.scenario_rules.items()},
            "threshold_rows": [dict(row) for row in self.threshold_rows],
        }

    def to_blob(self) -> str:
        """写成字符串，ESP32 侧直接进 NVS（一个 key 一个 blob）。"""
        payload = self.to_payload()
        return json.dumps(
            {"schema": BLOB_SCHEMA, "payload": payload, "digest": _digest(payload)},
            ensure_ascii=False, separators=(",", ":"),
        )

    @classmethod
    def from_blob(cls, blob: str, *, source: str = "nvs") -> "KitchenConfig":
        try:
            outer = json.loads(blob)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"配置不是合法 JSON：{exc}") from exc
        if not isinstance(outer, Mapping):
            raise ConfigError("配置顶层必须是对象")
        schema = outer.get("schema")
        if schema != BLOB_SCHEMA:
            raise ConfigError(
                f"配置版本 {schema!r} 不认识（本机支持 {BLOB_SCHEMA}）——"
                "升级固件时要写迁移，不能靠「读一半凑合用」"
            )
        payload = outer.get("payload")
        if not isinstance(payload, Mapping):
            raise ConfigError("配置缺少 payload")
        expected = outer.get("digest")
        if expected != _digest(payload):
            raise ConfigError("配置校验和不匹配（内容被截断或改坏）")
        return cls.from_payload(payload, source=source)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, source: str = "server") -> "KitchenConfig":
        try:
            schedule = ProfileSchedule.from_dict(payload.get("schedule") or {})
        except ScheduleError as exc:
            raise ConfigError(f"档位表不合法：{exc}") from exc
        rows = payload.get("threshold_rows") or ()
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ConfigError("threshold_rows 必须是数组")
        scenario = payload.get("scenario_rules") or {}
        if not isinstance(scenario, Mapping):
            raise ConfigError("scenario_rules 必须是对象（metric → 语义配置）")
        return cls(
            schedule=schedule,
            threshold_rows=tuple(dict(row) for row in rows),
            scenario_rules={str(name): dict(body) for name, body in scenario.items()
                            if isinstance(body, Mapping)},
            dataset=str(payload.get("dataset") or "demo"),
            version=int(payload.get("version") or 1),
            updated_at=float(payload.get("updated_at") or 0.0),
            source=source,
        )

    # ------------------------------------------------------------ 落盘
    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：写一半掉电不能把好配置毁掉
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(self.to_blob(), encoding="utf-8")
        temp.replace(target)
        return target

    def describe(self) -> str:
        return (
            f"source={self.source} dataset={self.dataset} version={self.version} "
            f"阈值行={len(self.threshold_rows)} 档位表={self.schedule.describe()}"
        )


def load_builtin(path: str | Path | None = None) -> KitchenConfig:
    """读内置默认配置（随固件打包，不依赖网络）。"""
    target = Path(path) if path is not None else default_config_path()
    if not target.is_file():
        raise ConfigError(f"内置默认配置缺失：{target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and "payload" in payload:
        # 允许直接吃 to_blob() 的输出，省一份格式
        payload = payload["payload"]
    config = KitchenConfig.from_payload(payload, source="builtin")
    config.updated_at = float(payload.get("updated_at") or 0.0)
    return config


def load_or_builtin(
    path: str | Path | None,
    *,
    builtin_path: str | Path | None = None,
) -> KitchenConfig:
    """读缓存；任何异常都降级到内置默认，并把原因写进 `degraded`。

    注意这里**不吞异常就当没事**：降级原因会随心跳上送，管理端能看见
    「这台设备的配置坏了」。静默降级等于把问题藏起来，等出事才发现。
    """
    builtin = load_builtin(builtin_path)
    if path is None:
        return builtin
    target = Path(path)
    if not target.is_file():
        builtin.degraded = f"缓存不存在（首次上电）：{target}"
        return builtin
    try:
        blob = target.read_text(encoding="utf-8")
        config = KitchenConfig.from_blob(blob, source="nvs")
    except (OSError, ConfigError) as exc:
        builtin.degraded = f"缓存不可用，已降级到内置默认：{exc}"
        log.warning("配置降级：%s", exc)
        return builtin
    if config.updated_at <= 0:
        config.updated_at = time.time()
    return config
