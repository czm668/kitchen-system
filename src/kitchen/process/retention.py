# -*- coding: utf-8 -*-
"""#14 留样称重 + #6 的 48h 销毁任务（A4-05 / A4-07）。

## 留样为什么不能只做一个「>=125g 就过」的判断

留样是**证据**，不是操作。事后出事时监管要问三件事：留了没有、留了多少、
什么时候销毁的。所以这里的三条线缺一不可：

| 线 | 数据 | 用途 |
|---|---|---|
| 应留样清单 | `dish_batches`（当日批次） | 「该留的留了没有」——漏留是最常见的失分项 |
| 称重结论 | `retention_samples`（`=125g` 才落库） | 「留了多少」——不足 125g 等于没留 |
| 操作留痕 | `process_events(kind='retention')` | 「谁、什么时候、称了多少」 |

**达标才写 `retention_samples`**：把 118g 的记录也写进去，日报的「留样合格率」
就会变成 100%（因为不合格的也被算成「已留样」），而实际是没留够。不达标的
记录只进 `process_events`，这样「补足后重新称」不会留下两条互相矛盾的留样行。

## 48 小时的算法与演示加速

`destroy_due_at = sampled_at + 48h / time_scale`。真实运行 `time_scale=1`；
答辩演示 `time_scale=2880` 把 48 小时压成 1 分钟（48×60/2880 = 60s），
演示时不用真等两天。加速只作用在**到期时刻**上，不改判定逻辑——所以真实环境
与演示环境的代码路径完全一致，不会出现「演示能过、上线不灵」。

**重复称重会把 48 小时重新计时**（覆盖 `sampled_at`）：留样上架时间以最后一次
达标为准。若按第一次算，师傅补足后重新称，销毁时刻会比实际早了十几分钟，
留样柜里还多躺一盒没有记录的留样。

## 未到期不许销毁

`destroy()` 只接受已到期的样本。提前销毁等于撕掉证据链，必须报错而不是
「记一笔提前销毁」。真要回收，走「留样柜盘点」流程另开单据。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from kitchen.domain import CST, DomainError, NotFoundError

from .models import ProcessEvent
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "RetentionError", "NotDueError",
    "RETENTION_MIN_G", "RETENTION_KEEP_HOURS", "DESTROY_TYPE",
    "RetentionTarget", "RetentionCheck", "RetentionSample", "DestroyTask",
    "RetentionService",
]

#: 留样下限（g）。GB 31654《餐饮服务通用卫生规范》要求每品种留样不少于 125g
RETENTION_MIN_G = 125.0

#: 留样保存时长（小时）
RETENTION_KEEP_HOURS = 48

#: 销毁任务在 `work_orders.type` 里的取值
DESTROY_TYPE = "retention_destroy"

#: 演示加速：48 小时 → 1 分钟
DEMO_TIME_SCALE = RETENTION_KEEP_HOURS * 3600 / 60


class RetentionError(DomainError):
    """留样操作不合法（批次不存在、读数异常、重复销毁）。"""


class NotDueError(RetentionError):
    """销毁任务未到期。"""


@dataclass(frozen=True)
class RetentionTarget:
    """一条「应留样」项：一个批次就是一个品种。"""

    batch_id: int
    batch_no: str
    dish_id: int
    dish_name: str
    produced_at: datetime | None = None
    state: str = "produced"

    def describe(self) -> str:
        return f"{self.batch_no} {self.dish_name}（{self.state}）"


@dataclass(frozen=True)
class RetentionCheck:
    """一次留样称重结论。`ok=False` 时不落库，只留痕。"""

    batch_id: int
    batch_no: str
    dish_name: str
    weight_g: float
    min_g: float
    ok: bool
    detail: str = ""

    @property
    def short_g(self) -> float:
        return 0.0 if self.ok else round(max(0.0, self.min_g - self.weight_g), 1)

    @property
    def state_text(self) -> str:
        return "stored" if self.ok else "short"

    def describe(self) -> str:
        head = f"{self.batch_no} {self.dish_name} {self.weight_g:.0f}g"
        if self.ok:
            return f"{head} ≥ {self.min_g:.0f}g，留样达标"
        return f"{head} 不足 {self.min_g:.0f}g（差 {self.short_g:.0f}g）——{self.detail}"


@dataclass
class RetentionSample:
    """`retention_samples` 一行。只存**达标**的留样。"""

    id: int
    dish_batch_id: int
    weight_g: float
    sampled_at: datetime
    destroy_due_at: datetime
    trace_code_id: int | None = None
    state: str = "stored"
    destroyed_at: datetime | None = None

    @property
    def keep_until(self) -> datetime:
        return self.destroy_due_at

    def describe(self) -> str:
        tail = (f"已销毁 {self.destroyed_at:%m-%d %H:%M}" if self.destroyed_at
                else f"应销毁 {self.destroy_due_at:%m-%d %H:%M}")
        return f"留样#{self.id} 批次{self.dish_batch_id} {self.weight_g:.0f}g（{self.state}，{tail}）"


@dataclass
class DestroyTask:
    """留样到期销毁任务（`work_orders.type='retention_destroy'`）。"""

    id: int
    order_no: str
    sample_id: int
    batch_no: str
    dish_name: str
    due_at: datetime
    state: str = "open"
    notices: list[dict[str, object]] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.order_no} 销毁 {self.batch_no} {self.dish_name}（{self.state}）"


class RetentionService:
    """应留样清单 → 扫码 → 称重 → 到期销毁。

    与 `CookingSession` 分开：留样是**按批次**（一个品种留一盒），与订单/工位
    无关；硬要塞进做菜会话，就会出现「同一锅出了两个批次」时无处挂载的问题。
    """

    def __init__(
        self,
        repo: MemoryProcessRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        min_weight_g: float = RETENTION_MIN_G,
        keep_hours: float = RETENTION_KEEP_HOURS,
        time_scale: float = 1.0,
    ) -> None:
        self.repo = repo
        self._clock = clock or (lambda: datetime.now(CST))
        self.min_weight_g = float(min_weight_g)
        self.keep_hours = float(keep_hours)
        if time_scale <= 0:
            raise RetentionError(f"time_scale 必须 > 0，实际 {time_scale}")
        self.time_scale = float(time_scale)

        self._samples: dict[int, RetentionSample] = {}
        self._tasks: dict[int, DestroyTask] = {}
        self._events: list[ProcessEvent] = []
        self._next_sample = 1
        self._next_task = 1
        self._next_event = 1

    # ------------------------------------------------------------ 视图
    def now(self) -> datetime:
        return self._clock()

    @property
    def events(self) -> tuple[ProcessEvent, ...]:
        return tuple(self._events)

    @property
    def samples(self) -> tuple[RetentionSample, ...]:
        return tuple(self._samples[key] for key in sorted(self._samples))

    @property
    def tasks(self) -> tuple[DestroyTask, ...]:
        return tuple(self._tasks[key] for key in sorted(self._tasks))

    def sample_of(self, batch_id: int) -> RetentionSample | None:
        return self._samples.get(int(batch_id))

    @property
    def keep_delta(self) -> timedelta:
        """留样保存时长（已按加速因子折算）。"""
        return timedelta(hours=self.keep_hours) / self.time_scale

    # ------------------------------------------------------------ 应留样清单
    def plan(self, day: date | None = None) -> tuple[RetentionTarget, ...]:
        """按当日批次生成应留样清单。

        口径是「**当日生产**的批次」，不是「`dish_batches.state='produced'`」：
        昨天的批次今天仍是 `produced`，按状态取会把昨天的活儿天天重列一遍，
        现场就会以为「今天还有 8 个没留」，直接关掉这个页面。
        """
        target_day = day or self.now().date()
        out: list[RetentionTarget] = []
        for batch_id in self.repo.batch_ids():
            row = self.repo.batch(batch_id)
            produced = row.get("produced_at")
            stamp = produced if isinstance(produced, datetime) else None
            if stamp is not None and stamp.date() != target_day:
                continue
            dish_id = int(row.get("dish_id") or 0)
            try:
                dish_name = self.repo.dish(dish_id).name
            except NotFoundError:
                dish_name = f"菜品{dish_id}"
            out.append(RetentionTarget(
                batch_id=batch_id,
                batch_no=str(row.get("batch_no") or f"B{batch_id}"),
                dish_id=dish_id,
                dish_name=dish_name,
                produced_at=stamp,
                state=str(row.get("state") or "produced"),
            ))
        return tuple(out)

    def pending(self, day: date | None = None) -> tuple[RetentionTarget, ...]:
        """应留样但还没留（或留了不达标）的品种——TFT 上就该显示这几条。"""
        return tuple(target for target in self.plan(day)
                     if self.sample_of(target.batch_id) is None)

    def summary(self, day: date | None = None) -> dict[str, object]:
        targets = self.plan(day)
        pending = self.pending(day)
        return {
            "date": str(day or self.now().date()),
            "required": len(targets),
            "pending": [item.describe() for item in pending],
            "stored": len(targets) - len(pending),
            "due_destroy": [task.describe() for task in self.due_for_destroy()],
        }

    # ------------------------------------------------------------ 扫码
    def resolve(self, code: str) -> RetentionTarget:
        """扫码/输入批次号 → 应留样项。

        两种输入都支持：现场扫的是**溯源码**（贴在留样盒上），后台补录用的是
        **批次号**。只认溯源码时，漏贴标签的那一盒就没法留样——而漏贴标签
        恰恰是最容易发生的事。
        """
        text = str(code).strip()
        if not text:
            raise RetentionError("扫码内容为空")
        batch_id: int | None = None
        try:
            row = self.repo.trace_code(text)
            batch_id = int(row.get("batch_id") or 0) or None
        except NotFoundError:
            batch_id = None
        if batch_id is None:
            for candidate in self.repo.batch_ids():
                if str(self.repo.batch(candidate).get("batch_no") or "").upper() == text.upper():
                    batch_id = candidate
                    break
        if batch_id is None:
            raise NotFoundError(f"扫到的「{text}」既不是溯源码也不是批次号")

        for target in self.plan():
            if target.batch_id == batch_id:
                self._emit("scanned", {"code": text, "batch_no": target.batch_no},
                           target=target)
                return target
        # 不是当日的批次也要能查出来（补留/追溯），但状态照实说
        row = self.repo.batch(batch_id)
        dish_id = int(row.get("dish_id") or 0)
        target = RetentionTarget(
            batch_id=batch_id,
            batch_no=str(row.get("batch_no") or f"B{batch_id}"),
            dish_id=dish_id,
            dish_name=self.repo.dish(dish_id).name,
            produced_at=row.get("produced_at") if isinstance(row.get("produced_at"), datetime) else None,
            state=str(row.get("state") or "produced"),
        )
        self._emit("scanned", {"code": text, "batch_no": target.batch_no}, target=target)
        return target

    # ------------------------------------------------------------ 称重
    def weigh(
        self,
        code: str | RetentionTarget,
        weight_g: float,
        *,
        staff_id: int | None = None,
        trace_code_id: int | None = None,
        now: datetime | None = None,
    ) -> RetentionCheck:
        """称一盒留样。**不足 125g 不落库**，只留痕并提示补足多少。"""
        target = code if isinstance(code, RetentionTarget) else self.resolve(code)
        value = float(weight_g)
        if value < 0:
            raise RetentionError(
                f"读数为负（{value:g}g）——HX711 未去皮或称重盘未清空"
            )
        ok = value >= self.min_weight_g
        detail = "" if ok else f"请补足 {self.min_weight_g - value:.0f}g 后重新称"
        check = RetentionCheck(
            batch_id=target.batch_id,
            batch_no=target.batch_no,
            dish_name=target.dish_name,
            weight_g=round(value, 1),
            min_g=self.min_weight_g,
            ok=ok,
            detail=detail,
        )
        self._emit(check.state_text, {
            "batch_no": target.batch_no,
            "weight_g": check.weight_g,
            "min_g": self.min_weight_g,
            "ok": ok,
        }, target=target, staff_id=staff_id)
        if not ok:
            log.info("留样不足：%s", check.describe())
            return check

        moment = now or self.now()
        due = moment + self.keep_delta
        existing = self._samples.get(target.batch_id)
        if existing is not None:
            # 重复称重覆盖：以最后一次达标上架为准，48 小时重新计时
            existing.weight_g = check.weight_g
            existing.sampled_at = moment
            existing.destroy_due_at = due
            existing.state = "stored"
            existing.destroyed_at = None
            log.info("留样更新：%s", existing.describe())
            return check

        sample = RetentionSample(
            id=self._next_sample,
            dish_batch_id=target.batch_id,
            weight_g=check.weight_g,
            sampled_at=moment,
            destroy_due_at=due,
            trace_code_id=trace_code_id,
        )
        self._next_sample += 1
        self._samples[target.batch_id] = sample
        log.info("留样达标：%s", sample.describe())
        return check

    # ------------------------------------------------------------ 48h 销毁
    def due_for_destroy(self, now: datetime | None = None) -> tuple[DestroyTask, ...]:
        """生成/返回到期的销毁任务。

        幂等：同一份留样只开一张单。按秒扫描时若不幂等，一分钟就能开出上百张单，
        小程序的待办列表会直接刷屏。
        """
        moment = now or self.now()
        opened: list[DestroyTask] = []
        for sample in self.samples:
            if sample.state != "stored":
                continue
            if sample.destroy_due_at > moment:
                continue
            task = next((row for row in self._tasks.values()
                         if row.sample_id == sample.id and row.state == "open"), None)
            if task is None:
                task = self._open_task(sample, moment)
            opened.append(task)
        return tuple(opened)

    def _open_task(self, sample: RetentionSample, moment: datetime) -> DestroyTask:
        batch = self.repo.batch(sample.dish_batch_id)
        dish_id = int(batch.get("dish_id") or 0)
        try:
            dish_name = self.repo.dish(dish_id).name
        except NotFoundError:
            dish_name = f"菜品{dish_id}"
        task = DestroyTask(
            id=self._next_task,
            order_no=f"WO-RD-{self._next_task:04d}",
            sample_id=sample.id,
            batch_no=str(batch.get("batch_no") or f"B{sample.dish_batch_id}"),
            dish_name=dish_name,
            due_at=sample.destroy_due_at,
        )
        self._next_task += 1
        task.notices.append({
            "channel": "miniapp",
            "type": "retention_destroy",
            "title": "留样到期，请销毁",
            "body": f"{task.batch_no} {task.dish_name} 已满 "
                    f"{self.keep_hours:.0f} 小时，请登记销毁",
            "order_no": task.order_no,
        })
        self._tasks[task.id] = task
        self._emit("destroy_due", {
            "sample_id": sample.id,
            "order_no": task.order_no,
            "batch_no": task.batch_no,
        }, target=None, extra={"batch_id": sample.dish_batch_id})
        log.info("销毁任务：%s", task.describe())
        return task

    def destroy(
        self,
        task_id: int | None = None,
        *,
        batch_id: int | None = None,
        staff_id: int | None = None,
        now: datetime | None = None,
    ) -> DestroyTask:
        """登记销毁。未到期直接拒绝——提前销毁等于撕证据链。"""
        moment = now or self.now()
        sample = self._sample_for(task_id=task_id, batch_id=batch_id)
        if sample.state == "destroyed":
            raise RetentionError(
                f"留样#{sample.id} 已于 {sample.destroyed_at:%m-%d %H:%M} 销毁"
            )
        if sample.destroy_due_at > moment:
            raise NotDueError(
                f"留样#{sample.id} 未满保存期：应于 {sample.destroy_due_at:%m-%d %H:%M} 销毁，"
                f"当前 {moment:%m-%d %H:%M}"
            )
        task = self._task_for(sample, moment)
        sample.state = "destroyed"
        sample.destroyed_at = moment
        task.state = "closed"
        self._emit("destroyed", {
            "sample_id": sample.id,
            "order_no": task.order_no,
            "weight_g": sample.weight_g,
        }, target=None, staff_id=staff_id, extra={"batch_id": sample.dish_batch_id})
        log.info("已销毁：%s", task.describe())
        return task

    def _sample_for(self, *, task_id: int | None, batch_id: int | None) -> RetentionSample:
        if task_id is not None:
            task = self._tasks.get(int(task_id))
            if task is None:
                raise NotFoundError(f"销毁任务 {task_id} 不存在")
            sample = next((row for row in self._samples.values() if row.id == task.sample_id), None)
            if sample is None:
                raise NotFoundError(f"销毁任务 {task_id} 找不到对应留样")
            return sample
        if batch_id is None:
            raise RetentionError("销毁必须指定 task_id 或 batch_id")
        sample = self._samples.get(int(batch_id))
        if sample is None:
            raise NotFoundError(f"批次 {batch_id} 没有留样记录")
        return sample

    def _task_for(self, sample: RetentionSample, moment: datetime) -> DestroyTask:
        """取该留样的销毁任务；到期但还没开过单的，顺手补一张。

        补开是必要的：扫描是定时任务，演示/现场可能直接点「销毁」而扫描还没跑。
        此时报「任务不存在」会让操作员以为数据坏了。
        """
        task = next((row for row in self._tasks.values() if row.sample_id == sample.id), None)
        return task if task is not None else self._open_task(sample, moment)

    def sweep(self, now: datetime | None = None) -> tuple[DestroyTask, ...]:
        """定时任务入口（APScheduler 每 30 s / 演示每秒调一次）。"""
        return self.due_for_destroy(now)

    # ------------------------------------------------------------ 内部
    def _emit(self, state: str, payload: dict, *, target: RetentionTarget | None,
              staff_id: int | None = None,
              extra: dict[str, object] | None = None) -> ProcessEvent:
        body = dict(payload)
        if extra:
            body.update(extra)
        event = ProcessEvent(
            id=self._next_event,
            kind="retention",
            ts=self.now(),
            payload=body,
            order_item_id=None,
            station_id=None,
            staff_id=staff_id,
            session_uid=(f"RET-{target.batch_no}" if target is not None else None),
            state=state,
        )
        self._next_event += 1
        self._events.append(event)
        return event
