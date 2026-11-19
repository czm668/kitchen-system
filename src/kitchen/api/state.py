# -*- coding: utf-8 -*-
"""应用状态：把六个域装成一份可查询的运行时，供管理端读写。

## 为什么要有这一层（而不是让 HTTP handler 直接 new 各种 Service）

六个域各自都能跑，但**装配顺序和共享实例**是有讲究的：

- `DispatchBoard` 必须拿到**同一个** `OrderService`，否则派单看到的是另一份订单；
- `DispatchBoard.matcher` 必须是 `AdmissionController` 的适配器，不能自己判资质
  （否则会出现「证过期但资质在册」的人被派上灶，见 A4 硬口径④）；
- 三条保守保护的输出要进**同一个** `InterlockSupervisor`，否则通道状态机分叉；
- 业务域用「后厨时区的 datetime」，保护层用「单调秒」，两者不能互相喂。

把这些写死在 `AppState.__init__` 里，handler 只负责转发。如果让每个 handler
自己拼，第 3 个 handler 就会漏掉一处，而症状是「某个页面看到的灶是空的」。

## 时间轴：一根加速轴 + 一根真实轴

演示要「60 分钟长烧压成 60 秒」，所以：

| 轴 | 来源 | 用途 |
|---|---|---|
| `now()` | `time.monotonic() - t0` | 保护层去抖、视觉去抖、SSE 节流 |
| `wall()` | `wall_base + 真实流逝 × time_scale` | 所有业务域 datetime、审计时间、展示 |

**两根轴不能混**：把加速后的秒喂给保护层，`#1` 的 30 秒确认窗口会变成 0.5 秒，
一次传感器抖动就能关阀；反过来把单调秒喂给业务域，订单 ETA 会显示成 1970 年。

## 为什么 tick 在后台线程、且不在 tick 里做网络

沿用 A6 硬口径：`tick()` 内不碰网络。后台线程只管「采一帧 → 判定 → 入队」，
补传由 `flush()` 由调用方（`/api/admin/flush` 或定时任务）显式触发。这样
「网络慢」最坏只影响上报延迟，不会把采样间隔拉长。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..channels import Channel, InterlockController
from ..domain import CST
from ..finance.service import FinanceService
from ..compliance.service import ComplianceService
from ..guest.llm import make_provider
from ..guest.service import GuestService
from ..process.cooking import CookingBoard
from ..process.dispatch import DispatchBoard
from ..process.disinfection import DisinfectionPolicy, DisinfectionService
from ..process.journal import append_journal, replay_journal
from ..process.orders import OrderService
from ..process.repository import MemoryProcessRepository
from ..process.retention import RetentionService
from ..process.trace import TraceService
from ..resilience.guards import ConservativeGuards, GuardPolicy
from ..resilience.health import SensorHealthMonitor
from ..scheduler import JobSpec, TaskScheduler
from ..resilience.offline import OfflineFirstRuntime, StepResult
from ..resilience.queue import LocalQueue
from ..resilience.upload import MemoryOfflineStore, ReplayUploader
from ..rules.config_store import load_builtin
from ..safety.interlock import InterlockSupervisor
from ..runtime import KitchenRuntime
from ..staff.admission import AdmissionController, CertificateSweeper, StaffMatcherAdapter
from ..staff.duty import DutyGuard
from ..staff.repository import MemoryStaffRepository
from ..storage.recorders import RuntimeRecorder
from .audit import AuditLog
from .customer import CustomerService

log = logging.getLogger("kitchen.api.state")

__all__ = ["AppState", "AppStateConfig", "TelemetryRing", "METRIC_ORDER", "METRIC_META"]

#: 12 遥测 + 单位（顺序即前端曲线图例顺序）。第 13 类信号是 RC522 刷卡事件，
#: 不是遥测，所以不在这里——`kitchen.json` 的 12 条 metrics 才是这条口径的依据。
METRIC_META: dict[str, dict[str, str]] = {
    "t": {"unit": "°C", "label": "环境温度", "group": "env"},
    "h": {"unit": "%RH", "label": "环境湿度", "group": "env"},
    "gas_level": {"unit": "0~1", "label": "燃气浓度（MQ-2 归一化）", "group": "safety"},
    "hood_temp": {"unit": "°C", "label": "排烟罩温度", "group": "safety"},
    "sterilize_temp": {"unit": "°C", "label": "消毒柜温度", "group": "equipment"},
    "fridge_temp": {"unit": "°C", "label": "冷藏温度", "group": "cold"},
    "freezer_temp": {"unit": "°C", "label": "冷冻温度", "group": "cold"},
    "sample_temp": {"unit": "°C", "label": "留样柜温度", "group": "cold"},
    "flame_a": {"unit": "-", "label": "A 灶火焰", "group": "safety"},
    "flame_b": {"unit": "-", "label": "B 灶火焰", "group": "safety"},
    "sct_current": {"unit": "A", "label": "排烟风机电流（SCT-013）", "group": "equipment"},
    "weight_g": {"unit": "g", "label": "称重台读数", "group": "process"},
}
METRIC_ORDER: tuple[str, ...] = tuple(METRIC_META)


class TelemetryRing:
    """按指标保存最近 N 个点。**定长**，满了顶掉最旧的。

    不定长会随运行时间线性增长：跑一整晚（8 小时 × 1Hz × 12 指标）就是 34 万
    个点，前端一次性拉全量直接卡死。定长 600 点 = 1Hz 下 10 分钟，正好是
    「排烟积热 10 分钟温升窗口」的长度。
    """

    def __init__(self, points: int = 600) -> None:
        if points < 2:
            raise ValueError("points 至少为 2")
        self.points = int(points)
        #: 采样线程 append、请求线程遍历，**必须加锁**。`deque` 在迭代时被
        #: append 会抛 `RuntimeError: deque mutated during iteration`——症状是
        #: 曲线页偶发 500，而且只在「正好在采样」的那一刻复现。
        self._lock = threading.Lock()
        self._series: dict[str, deque[tuple[float, float]]] = {
            metric: deque(maxlen=self.points) for metric in METRIC_ORDER
        }

    def push(self, ts: float, metrics: Mapping[str, float]) -> None:
        moment = float(ts)
        rows: list[tuple[str, float]] = []
        for metric in METRIC_ORDER:
            if metric not in metrics:
                continue
            try:
                value = float(metrics[metric])
            except (TypeError, ValueError):
                continue  # 坏值不进曲线：NaN 会让整条 ECharts 折线断掉
            if value != value:  # NaN
                continue
            rows.append((metric, value))
        with self._lock:
            for metric, value in rows:
                self._series[metric].append((moment, value))

    def series(
        self,
        metrics: Iterable[str] | None = None,
        *,
        points: int | None = None,
    ) -> dict[str, list[dict[str, float]]]:
        wanted = [m for m in (metrics or METRIC_ORDER) if m in self._series]
        limit = self.points if points is None else max(1, min(int(points), self.points))
        out: dict[str, list[dict[str, float]]] = {}
        with self._lock:
            snapshot = {metric: list(self._series[metric])[-limit:]
                        for metric in wanted}
        for metric, rows in snapshot.items():
            out[metric] = [{"ts": round(ts, 3), "v": round(value, 4)}
                           for ts, value in rows]
        return out

    def latest(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            snapshot = {metric: (rows[-1] if rows else None)
                        for metric, rows in self._series.items()}
        for metric, last in snapshot.items():
            meta = METRIC_META[metric]
            rows = () if last is None else (last,)
            if not rows:
                out[metric] = {**meta, "ts": None, "v": None, "stale": True}
                continue
            ts, value = rows[-1]
            out[metric] = {**meta, "ts": round(ts, 3), "v": round(value, 4),
                           "stale": False}
        return out

    def clear(self) -> None:
        with self._lock:
            for rows in self._series.values():
                rows.clear()

    def __len__(self) -> int:
        return max((len(rows) for rows in self._series.values()), default=0)


@dataclass
class AppStateConfig:
    """运行参数。**全部有默认值**，单测只覆盖要改的那一项。"""

    device_id: str = "kitchen-node-01"
    tick_interval: float = 1.0
    #: 演示加速：1 真实秒 = time_scale 虚拟秒。1.0 = 真实时间
    time_scale: float = 1.0
    history_points: int = 600
    #: 长烧保护门限（秒）。3600 是设计值，演示期靠 time_scale 压缩
    flame_hold_sec: float = 3600.0
    #: 落盘目录。None = 纯内存（单测默认）
    var_dir: Path | None = None
    #: 运行时库（A1）：遥测帧/告警事件落库，重启后历史可查。
    #: None = 纯内存（既有默认）；指向 SQLite 文件即启用（dev 演示口径，
    #: DDL 与 migrations/ 一致，prod 换 MySQL 连接同 schema）
    db_path: Path | None = None
    #: 运行时库 URL（A18）：`mysql://user:pass@host:port/db`。
    #: 设置后优先于 db_path，走 MySQL 后端；连不上明确抛错，不静默降级。
    #: None = 沿用 db_path 的 SQLite 行为（既有 1363 例单测零影响）。
    db_url: str | None = None
    #: 定时任务托管（A3）：False = 不起调度线程（既有默认，单测零影响）。
    #: True 时托管两个真缺的任务：留样到期扫描（#14）+ 证件每日扫描（#9）。
    #: 切档/工单升级在帧内已自动化，不经调度器（见 src/kitchen/scheduler.py）。
    scheduler_enabled: bool = False
    #: 证件扫描的每日时刻（真实时刻语义，prod 06:00）
    cert_sweep_at: str = "06:00"
    #: 留样到期扫描间隔秒（prod 30；演示用 time_scale 折算，这里可再缩短）
    retention_sweep_sec: float = 30.0
    vision_online: bool = True
    queue_capacity: int = 2000
    event_capacity: int = 500


class AppState:
    """全局单实例。**所有**读接口从这里的快照取数，所有写接口改这里的状态。"""

    def __init__(
        self,
        *,
        config: AppStateConfig | None = None,
        rules=None,
        process_seed: str | Path | None = None,
        staff_seed: str | Path | None = None,
        online: bool = True,
        time_scale: float | None = None,
        sock_sink: bool = True,
        auto_start: bool = False,
    ) -> None:
        self.config = config or AppStateConfig()
        if time_scale is not None:
            self.config.time_scale = float(time_scale)
        if self.config.time_scale <= 0:
            raise ValueError("time_scale 必须为正")
        self._lock = threading.RLock()
        self._t0 = time.monotonic()
        self._wall_base = time.time()
        self._ticks = 0
        self._last_tick_at: float | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=self.config.event_capacity)
        self._event_seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._online = bool(online)

        var = self.config.var_dir
        if var is not None:
            var.mkdir(parents=True, exist_ok=True)

        # ---------------------------------------------------------- 运行时库（A1/A18）
        # db_path 指向 SQLite 文件即启用遥测/告警落库；db_url（mysql://）优先，
        # 走 MySQL 后端。写失败只计数不抛——落库是锦上添花，
        # 不能把采样线程打挂（口径同 AuditLog.write_failures）。
        self.recorder: RuntimeRecorder | None = None
        if self.config.db_url:
            # MySQL：URL 里带口令，绝不落日志；连接失败由 backend 直接抛出
            self.recorder = RuntimeRecorder(
                self.config.db_url, device_id=self.config.device_id
            )
        elif self.config.db_path is not None:
            self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.recorder = RuntimeRecorder(
                self.config.db_path, device_id=self.config.device_id
            )

        # ---------------------------------------------------------- 业务域
        self.repo = MemoryProcessRepository.from_seed(process_seed)
        # 配置日志重放：界面保存的菜品/配方跨重启不丢（seed 是出厂基线，
        # journal 只承载出厂之后的改动）。纯内存（var_dir=None）时自动跳过。
        self._journal_path = (
            self.config.var_dir / "config_journal.jsonl"
            if self.config.var_dir is not None else None
        )
        self._journal_applied, self._journal_skipped = replay_journal(
            self._journal_path, self.repo
        )
        self.staff_repo = MemoryStaffRepository.from_seed(staff_seed)
        # ------------------------------------------------------ 业务落库接线（A23）
        # 写器存在时：仓储/订单/看板共用同一个连接与锁；不存在时全部保持
        # None = 纯内存（既有行为零变化）。写失败只在写器内计数不抛。
        if self.recorder is not None:
            self.repo.recorder = self.recorder
            self.staff_repo.recorder = self.recorder
            self.orders = OrderService(
                self.repo, clock=self.to_datetime, sink=self._persist_order,
            )
        else:
            self.orders = OrderService(self.repo, clock=self.to_datetime)
        #: 基线物化结果（表 → 行数），写器未启用时为空字典
        self.baseline_written: dict[str, int] = {}
        self.admission = AdmissionController(self.staff_repo)
        self.board = DispatchBoard(
            self.repo, self.orders, clock=self.to_datetime,
            matcher=StaffMatcherAdapter(self.admission),
            kitchen_device_id=self.config.device_id,
        )
        self.disinfection = DisinfectionService(self.repo, clock=self.to_datetime)
        # #14 留样（A3 装配）：域层早已实现，此前只有 demo 脚本手动调。
        # 48h 保存期按 time_scale 折算（演示口径），判定逻辑不变。
        self.retention = RetentionService(
            self.repo, clock=self.to_datetime, time_scale=self.config.time_scale
        )
        # #12/#13：配菜称重 + SOP 分步 + 出餐门禁。TFT 工序屏的唯一入口。
        # 挂在运行时上（而不是每请求 new）是因为工序进度要跨请求保留。
        self.cooking = CookingBoard(
            self.repo, self.orders, clock=self.to_datetime
        )
        # A23：过程执行域的看板接同一个写器（占用/工序/留样/消毒）
        if self.recorder is not None:
            self.board.recorder = self.recorder
            self.disinfection.recorder = self.recorder
            self.retention.recorder = self.recorder
            self.cooking.recorder = self.recorder
            # 基线物化：seed + 配置日志重放后的内存快照 → 配置表（INSERT IGNORE 幂等）
            self.baseline_written = self.recorder.sync_baseline(self.repo, self.staff_repo)
        # 顾客端通道（A9）：令牌/归属/限频/通知。不复制业务逻辑，
        # 下单与催菜仍走上面的 orders——这条通道只做顾客视角的过滤。
        # 令牌摘要 + 订单归属落 customer_journal，重启后顾客凭原令牌继续查单。
        # 装配仍在 guest 之前：回放只恢复会话数据，不触发 guest.bind()。
        self.customer = CustomerService(
            self, journal_path=None if var is None else var / "customer_journal.jsonl",
        )
        # A11 扩展模块：顾客档案/画像/AI 客服/客诉与补偿审批。
        # provider 由环境变量决定（无 key 时为离线规则层，CI 不依赖网络）；
        # guest_journal 与 config_journal 同构，凭证类数据重启不丢。
        # 装配放在 customer 之后：register() 运行时会调用 guest.bind()。
        self.llm_provider = make_provider()
        self.guest = GuestService(
            self, provider=self.llm_provider,
            journal_path=None if var is None else var / "guest_journal.jsonl",
        )
        # A12 扩展模块：经营流水 / 成本 / 毛利净利 / 日报。
        # 装配必须在 guest 之后——sync() 要读已批准的补偿凭证来冲账；
        # provider 复用同一个实例，DeepSeek 配 key 后客诉与日报一起切 live，
        # 不另起一套模型配置与审计口径。
        self.finance = FinanceService(
            self, provider=self.llm_provider,
            journal_path=None if var is None else var / "finance_journal.jsonl",
        )
        # 溯源查询（A9-05）：域层早已有 TraceService，此前只有批次页在用；
        # 顾客扫码要直接查 view/label，这里装配成正式接口。
        self.trace = TraceService(self.repo)
        # A13 扩展模块：食安合规凭证（每日检查表 / 批次一页纸 / 合规日报）。
        # 必须装配在 trace 之后——dossier() 运行时要用 codes_of/view/outbound_blockers。
        # 与 A11/A12 共用同一个 provider 实例，配 key 后三处一起切 live。
        self.compliance = ComplianceService(
            self, provider=self.llm_provider,
            journal_path=None if var is None else var / "compliance_journal.jsonl",
        )
        # A28 多融合 AI 中枢（只读建议层）：与上面三域共用同一个 provider 实例，
        # 配 key 后 anomaly/dispatch/compliance_qa 三场景一起切 live；无 key 走
        # 各自的确定性离线规则。它不持有业务状态、不写业务表，摘掉不影响主链路。
        from ..ai import AiService
        self.ai = AiService(self, provider=self.llm_provider)
        self.duty = DutyGuard(
            self.admission, clock=self.monotonic, wall_clock=self.to_datetime
        )

        # ---------------------------------------------------------- 安全链路
        self.runtime = KitchenRuntime(
            load_builtin() if rules is None else rules,
            device_id=self.config.device_id,
            wall_clock=self.wall_epoch,
        )
        self.supervisor = InterlockSupervisor()
        self.controller: InterlockController = self.supervisor.controller
        self.health = SensorHealthMonitor(clock=self.wall_epoch)

        # 队列与补传：var_dir 没给就落系统临时目录下的固定子目录，
        # **不用 tempfile.mkdtemp()**（本机 TEMP 是 8.3 短名，不可写）
        queue_dir = var if var is not None else Path(".local/tmp/api-api-store")
        queue_dir.mkdir(parents=True, exist_ok=True)
        self.store = MemoryOfflineStore()
        self.queue = LocalQueue(
            queue_dir / "queue.jsonl", device_id=self.config.device_id,
            capacity=self.config.queue_capacity, wall_clock=self.wall_epoch,
        )
        self.uploader = ReplayUploader(
            self.queue, self.store, device_id=self.config.device_id
        ) if sock_sink else None
        self.offline = OfflineFirstRuntime(
            self.runtime, self.supervisor,
            device_id=self.config.device_id,
            queue=self.queue,
            uploader=self.uploader,
            guards=ConservativeGuards(GuardPolicy(
                time_scale=self.config.time_scale,
                flame_hold_sec=self.config.flame_hold_sec,
            )),
            health=self.health,
            online=self._online,
            clock=self.monotonic,
            wall_clock=self.wall_epoch,
        )
        self.telemetry = TelemetryRing(self.config.history_points)
        self.audit = AuditLog(
            None if var is None else var / "audit.jsonl", clock=self.wall_epoch,
            sql_sink=None if self.recorder is None else self.recorder.append_audit,
        )
        # 演示数据：订单、消毒会话、工单。单独一步，方便测试只装空壳
        self.demo = _DemoSeed(self)

        # ---------------------------------------------------------- 定时任务托管（A3）
        # 托管两个真缺的任务（切档/工单升级已在帧内自动化，不经调度器）；
        # 任务结果经 emit 进业务事件流（recorder 存在时同步落库）。
        self.scheduler: TaskScheduler | None = None
        if self.config.scheduler_enabled:
            def _cert_sweep() -> dict[str, Any]:
                sweep = CertificateSweeper(self.staff_repo).run(
                    self.to_datetime().date()
                )
                return {
                    "expired": len(sweep.expired),
                    "expiring_soon": len(sweep.expiring_soon),
                    "blocked_shifts": len(sweep.blocked_shifts),
                    "describe": sweep.describe(),
                }

            def _retention_sweep() -> dict[str, Any]:
                tasks = self.retention.sweep()
                return {
                    "opened": len(tasks),
                    "sample_ids": [task.sample_id for task in tasks],
                }

            self.scheduler = TaskScheduler(
                [
                    JobSpec("certificate-sweep", _cert_sweep,
                            at=self.config.cert_sweep_at),
                    JobSpec("retention-sweep", _retention_sweep,
                            every_sec=self.config.retention_sweep_sec),
                ],
                poll_sec=0.5,
                on_result=lambda name, result: self.emit(f"sched-{name}", result),
            )

        if auto_start:
            self.start()

    # ---------------------------------------------------------------- 时间
    # R5（审查报告 §3/§5.3）：两个时间轴必须用名字区分，不再叫 now/wall。
    # 11-18 的告警刷屏正是把单调秒当 epoch 传进 due_escalations 造成的。
    def monotonic(self) -> float:
        """单调秒（从启动起算，**不受 time_scale 影响**）。只用于计时/超时。"""
        return time.monotonic() - self._t0

    def wall_epoch(self) -> float:
        """加速后的 epoch 秒。展示、落库与业务时间戳用这个。"""
        return self._wall_base + self.monotonic() * self.config.time_scale

    def to_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.wall_epoch(), CST)

    def set_time_scale(self, scale: float) -> dict[str, Any]:
        """改加速因子。**要把已经流逝的时间按旧因子结清**，否则历史会被重算。"""
        scale = float(scale)
        if scale <= 0:
            raise ValueError("time_scale 必须为正")
        with self._lock:
            elapsed = self.monotonic()
            self._wall_base = self.wall_epoch() - elapsed * scale
            self.config.time_scale = scale
            # `GuardPolicy` 是 frozen dataclass：**整体替换**而不是改字段。
            # 只换 policy 对象、保留 guards 实例，触发状态（燃气基线、长烧计时）
            # 才不会因为「演示时拖了一下滑杆」被清零。
            import dataclasses

            guards = self.offline.guards
            guards.policy = dataclasses.replace(guards.policy, time_scale=scale)
            return self.demo_map()

    def demo_map(self) -> dict[str, Any]:
        """A7-09 要求：**演示阈值与真实值的对应关系可查**（答辩防质疑）。

        静态时长来自 fixtures/kitchen_constants.json（审查报告 §3 P2）；
        长烧时长是运行期配置 `flame_hold_sec`，不写死在常量表里。
        """
        from ..constants import constants as _constants

        scale = self.config.time_scale
        policy = self.offline.guards.policy
        conf = _constants()
        specs = [(row["label"], float(row["real_sec"]), row["note"])
                 for row in conf["demo_durations"]]
        # 长烧项由运行期配置决定，按 JSON 里的 insert_at 插回原顺序
        insert_at = int(conf.get("demo_durations_insert_at", len(specs)))
        specs.insert(insert_at, (
            "#3 长烧无人看管", self.config.flame_hold_sec,
            "设计值：60 分钟无刷卡/按键 → 关气源"))
        rows = []
        for label, real_sec, note in specs:
            effective = real_sec / scale if scale > 1 else real_sec
            rows.append({
                "item": label,
                "real_sec": real_sec,
                "real_text": _duration_text(real_sec),
                "effective_sec": round(effective, 2),
                "effective_text": _duration_text(effective),
                "note": note,
            })
        return {
            "time_scale": scale,
            "scale_note": (
                "1 真实秒 = %.1f 演示秒；仅压缩时间轴，判定逻辑与阈值不变" % scale
                if scale != 1 else "真实时间（未加速）"
            ),
            "hold_effective_sec": round(policy.hold_effective_sec, 2),
            "rows": rows,
            "hard_rule": (
                "所有阈值都是配置值，没有任何一条因为演示被改小；"
                "加速只作用于时间轴（time_scale），因此「1 分钟」在演示里等于 "
                f"{_duration_text(60.0 / scale if scale > 1 else 60.0)}。"
            ),
        }

    # ---------------------------------------------------------------- 循环
    def start(self) -> None:
        """起后台采样线程。**幂等**：重复调用不会起第二个线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="kitchen-tick", daemon=True
        )
        self._thread.start()
        if self.scheduler is not None:
            self.scheduler.start()

    def stop(self, *, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler = None
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None

    def _loop(self) -> None:
        interval = max(0.05, float(self.config.tick_interval))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 线程里抛异常会静默死掉，必须兜住
                log.exception("采样线程异常（继续下一帧）")
            spent = time.monotonic() - started
            self._stop.wait(max(0.0, interval - spent))

    def tick(self, metrics: Mapping[str, float] | None = None) -> StepResult:
        """推进一帧。`metrics` 为空时用仿真档位采样（演示/联调用的就是这条路）。"""
        with self._lock:
            moment = self.monotonic()
            frame = dict(metrics) if metrics is not None else self.demo.frame(moment)
            step = self.offline.tick(frame, now=moment)
            self.telemetry.push(self.wall_epoch(), frame)
            if self.recorder is not None:
                self.recorder.record_frame(frame, self.to_datetime())
            self._ticks += 1
            self._last_tick_at = moment
            self._collect(step, frame)
            return step

    def _collect(self, step: StepResult, frame: Mapping[str, float]) -> None:
        """把一帧里的「值得推送的东西」转成事件，供 SSE 增量拉取。"""
        for verdict in step.verdicts:
            self._emit("rule", {
                "rule_id": verdict.rule_id, "metric": verdict.metric,
                "event": verdict.event, "level": verdict.level,
                "message": verdict.message, "actions": list(verdict.actions),
            })
        for verdict in step.guard_verdicts:
            self._emit("guard", {
                "rule_id": verdict.rule_id, "event": verdict.event,
                "message": verdict.message, "actions": list(verdict.actions),
            })
        # 工单时间戳是 `self.monotonic()`（单调秒，见 offline.tick → health.observe），
        # 升级判定必须用同一个时钟域。两个坑都踩过（2026-11-18 云端实测）：
        # 1. 传 `self.wall_epoch()`（epoch）→ `now >= due_at + 900` 永远成立，工单一开
        #    就立刻「超时升级」，超期时长还会算出几千万分钟的荒谬值；
        # 2. 只发事件不调 `escalate()` → `escalated_at` 一直是 None，
        #    `is_due_for_escalation` 每帧都成立，同一张单每秒发 3 行进
        #    `alarm_events`，跑一天 26 万行，30 GB 盘迟早撑爆。
        # 正确语义：开单 30 分钟没人接单 → 升级一次（L1→L2），之后不再重复。
        moment = self.monotonic()
        for ticket in self.health.due_escalations(now=moment):
            self.health.escalate(ticket.id, now=moment)
            self._emit("fault-escalate", ticket.to_payload())

    def _emit(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        row = {
            "seq": self._event_seq,
            "kind": kind,
            "ts": round(self.wall_epoch(), 3),
            "time": self.to_datetime().strftime("%H:%M:%S"),
            **body,
        }
        self._events.append(row)
        if self.recorder is not None:
            self.recorder.record_event(kind, row, self.to_datetime())
        return row

    def emit(self, kind: str, body: Mapping[str, Any] | None = None,
             **extra: Any) -> dict[str, Any]:
        """给写接口用：业务事件也走同一条流，前端只订阅一个端点。

        同时接受「一个字典」和「关键字」两种写法：调用方有的地方手里已经有
        现成的 payload，非要它再拆成 kwargs 只是多一次手抄。
        """
        merged = dict(body or {})
        merged.update(extra)
        return self._emit(kind, merged)

    def _persist_order(self, order: Any, event: Any) -> None:
        """订单写路径的落库旁路（A23）：每条 order_events 落地时，
        订单/子项快照 UPSERT + 事件追加。失败在写器内计数，不抛。"""
        if self.recorder is None:
            return
        self.recorder.persist_order(order)
        self.recorder.append_order_event(event)

    def journal_config(self, record: dict[str, Any]) -> bool:
        """配置写操作落盘（跨重启重放）。写失败返回 False，由调用方提示。"""
        return append_journal(self._journal_path, record)

    def events(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = [row for row in self._events if row["seq"] > int(since)]
        return rows[-limit:] if limit > 0 else rows

    @property
    def event_seq(self) -> int:
        return self._event_seq

    @property
    def ticks(self) -> int:
        return self._ticks

    # ---------------------------------------------------------------- 网络
    def set_online(self, online: bool) -> dict[str, Any]:
        report = self.offline.set_online(online, wall_epoch=self.wall_epoch())
        self._online = bool(online)
        self._emit("network", {"online": self._online,
                               "queued": len(self.queue)})
        return {
            "online": self._online,
            "replay": None if report is None else report.to_payload(),
            "queued": len(self.queue),
        }

    def flush(self) -> dict[str, Any]:
        report = self.offline.flush()
        return {
            "replay": None if report is None else report.to_payload(),
            "queued": len(self.queue),
            "store_rows": len(self.store),
        }

    # ---------------------------------------------------------------- 聚合快照
    def overview(self) -> dict[str, Any]:
        """一次拿全：九个页面共用，避免现场 wifi 下打十几个请求。"""
        with self._lock:
            return self._overview()

    def _overview(self) -> dict[str, Any]:
        channels = self.controller.snapshot()
        degrade = self.degrade()
        return {
            "device_id": self.config.device_id,
            "server_time": self.to_datetime().isoformat(timespec="seconds"),
            "epoch": round(self.wall_epoch(), 3),
            "time_scale": self.config.time_scale,
            "online": self._online,
            "ticks": self._ticks,
            "profile": self.runtime.engine.profile,
            "channels": channels,
            "degrade": degrade,
            "telemetry": self.telemetry.latest(),
            "counts": {
                "orders_open": len(self.orders.open_orders()),
                "queue": len(self.queue),
                "stations": len(self.repo.stations()),
                "busy_stations": len(self.board.occupancies()),
                "tickets_open": len([t for t in self.health.tickets(open_only=True)]),
                "guards": len(self.offline.guards.tripped()),
            },
            "guards": {
                "tripped": list(self.offline.guards.tripped()),
                "snapshot": self.offline.guards.snapshot(),
            },
            "event_seq": self._event_seq,
        }

    def degrade(self) -> dict[str, Any]:
        with self._lock:
            return self._degrade()

    def _degrade(self) -> dict[str, Any]:
        """三端降级报告（A6-05）。视觉在线与否由 `config.vision_online` 决定。"""
        from ..resilience.degrade import build_degrade

        offline_sec = self.offline.offline_sec
        return build_degrade(
            network_online=self._online,
            vision={
                "online": bool(self.config.vision_online),
                "age_sec": round(offline_sec, 1) if not self.config.vision_online else None,
            },
            sensor_faults=list(self.health.faulty()),
            config_degraded=bool(self.runtime.config.degraded),
            queue_pending=len(self.queue),
            wall_epoch=self.wall_epoch(),
        ).to_payload()

    def channel_action(
        self,
        action: str,
        *,
        reviewer: str,
        manual_confirmed: bool,
        source: str = "",
    ) -> dict[str, Any]:
        """三通道手动动作。**唯一裁决点是 `InterlockController.request()`**。"""
        binding = self.controller.request(
            action,
            manual_confirmed=bool(manual_confirmed),
            source=source or f"web:{reviewer}",
            scenario="kitchen",
        )
        self._emit("channel", {
            "action": action, "channel": binding.channel.value,
            "label": binding.label, "by": reviewer,
        })
        # 声光/语音走独立 GPIO，**不在三通道状态机里**：直接查 `channel_state`
        # 会 KeyError（`_channels` 只有 CH1~CH3），而现场表现是「报警按钮点了报错」。
        state_text = ("signal" if binding.is_signal
                      else self.controller.channel_state(binding.channel))
        return {
            "action": action,
            "channel": binding.channel.value,
            "label": binding.label,
            "state": state_text,
        }

    def review_channel(self, channel: str, *, reviewer: str) -> dict[str, Any]:
        """双通道复核：只登记「有人复核过」，不产生动作。

        `confirm_manual_reset()` 的参数要跟 `{"web","tft"}` 比对，
        所以这里传的是**通道名**，人名记审计。
        """
        reviewers = self.controller.confirm_manual_reset(channel, reviewer="web")
        return {
            "channel": Channel(channel).value,
            "reviewers": sorted(reviewers),
            "state": self.controller.channel_state(channel),
        }

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "config": {
                "device_id": self.config.device_id,
                "tick_interval": self.config.tick_interval,
                "time_scale": self.config.time_scale,
                "history_points": self.config.history_points,
                "vision_online": self.config.vision_online,
            },
            "runtime": self.runtime.snapshot(),
            "offline": self.offline.snapshot(now=self.monotonic()),
            "queue": self.queue.snapshot() if hasattr(self.queue, "snapshot") else {},
            "health": self.health.snapshot(now=self.monotonic()),
            "audit": self.audit.snapshot(),
            "ticks": self._ticks,
            "online": self._online,
            "demo": self.demo.snapshot(),
        }


def _duration_text(seconds: float) -> str:
    seconds = float(seconds)
    if seconds >= 3600:
        hours = seconds / 3600.0
        return f"{hours:.0f} 小时" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} 小时"
    if seconds >= 60:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds:.1f} 秒"


class _DemoSeed:
    """演示数据装配。

    真实部署里这些来自 MySQL 种子 + 小程序下单；演示环境需要「一打开就有东西看」。
    **刻意不随机**：每次启动看到同一批订单，答辩演练时不会「昨天还有的那个单呢」。
    """

    def __init__(self, state: AppState) -> None:
        self.state = state
        self._profile = None
        self._orders: list[int] = []
        self._vision_frames = 0
        try:
            from ..sim import KitchenSimProfile
            from pathlib import Path as _Path

            path = _Path(__file__).resolve().parents[3] / "fixtures" / "kitchen_sim_profiles.json"
            if path.exists():
                self._profile = KitchenSimProfile.from_file(path)
        except Exception as exc:  # noqa: BLE001 - 仿真档位缺失不该让 API 起不来
            log.warning("仿真档位装载失败（将用固定基线）：%s", exc)

    def frame(self, moment: float) -> dict[str, float]:
        if self._profile is not None:
            try:
                return self._profile.sample_all(moment)
            except Exception as exc:  # noqa: BLE001
                log.warning("仿真采样失败，回落到基线：%s", exc)
        return {
            "t": 24.0, "h": 55.0, "gas_level": 0.06, "hood_temp": 60.0,
            "sterilize_temp": 40.0, "fridge_temp": 4.0, "freezer_temp": -18.0,
            "sample_temp": 3.0, "sct_current": 0.6, "weight_g": 100.0,
            "flame_a": 0.0, "flame_b": 0.0,
        }

    def seed_orders(self, dish_codes: Sequence[str] = ()) -> dict[str, Any]:
        """下一批演示订单。`dish_codes` 为空时按菜谱里前三个菜各下一份。"""
        state = self.state
        dishes = [d for d in state.repo.dishes() if d.enabled and not d.sold_out]
        if not dishes:
            return {"created": 0, "note": "没有可售菜品"}
        wanted = list(dish_codes) or [dish.code for dish in dishes[:3]]
        created: list[int] = []
        skipped: list[str] = []
        for code in wanted:
            try:
                dish = state.repo.dish_by_code(code)
                order = state.orders.place_order(
                    items=[(dish.id, 1)],
                    source="miniapp",
                    channel="miniapp",
                )
            except Exception as exc:  # noqa: BLE001
                # **沽清/停售不该让整批失败**：种子里本来就有一道沽清菜，
                # 前端点「生成演示订单」得到 400 是纯坑。
                skipped.append(f"{code}: {exc}")
                continue
            created.append(order.id)
            self._orders.append(order.id)
        state.emit("order", {"created": created, "source": "demo-seed"})
        return {"created": len(created), "order_ids": created, "skipped": skipped}

    def snapshot(self) -> dict[str, Any]:
        return {
            "orders": list(self._orders),
            "sim_profile": self._profile is not None,
            "vision_frames": self._vision_frames,
        }
