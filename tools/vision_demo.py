# -*- coding: utf-8 -*-
"""视觉闭环演练（A5-01~06）：判定 → 抓拍 → 切电 → 复核 → 离岗转派 → 接 #3。

## 演示什么

九段，每段都走「真会出错的分支」：

1. **帧源与判定**：画面里两个人，看「谁在哪个工位、戴没戴」；
2. **去抖**：同一画面喂两次**不报警**，第三次才报——单帧永远不判违规；
3. **着装违规**：未戴口罩 → 只语音提醒，**不切电**（断插座不影响他戴口罩）；
4. **行为违规**：工位玩手机 → 切该工位插座 + 语音 + 开单；
5. **证据链**：多图抓拍 + sha256 → 校验通过；改一个字节 → 报哈希不符；
6. **双通道复核**：只有 TFT 时电送不上 → Web 二次确认 → 恢复供电、关单；
7. **离岗转派**：连续离岗 → 未开火的单转给在岗合格人员；**已开火的菜不转**；
8. **接 #3**：本工位无人 + 有明火 → `vision_no_person=1`（路过的人不算有人）；
9. **视觉被遮挡**：连续空帧而工位仍有占用 → 单独报「视觉被遮挡」。

## 用法

```
python tools/vision_demo.py
python tools/vision_demo.py --quiet                 # 只打印结论
python tools/vision_demo.py --evidence var/evidence
```
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kitchen.domain import CST                                        # noqa: E402
from kitchen.process import (                                        # noqa: E402
    DispatchBoard,
    MemoryProcessRepository,
    OrderService,
)
from kitchen.safety import FlameGuard, VISION_NO_PERSON               # noqa: E402
from kitchen.vision import (                                         # noqa: E402
    AnalysisPolicy,
    BBox,
    Detection,
    EvidenceStore,
    FlameVisionLink,
    Frame,
    ScriptedDetector,
    StationPowerBank,
    StationRoi,
    ViolationDisposal,
    ViolationPolicy,
    ViolationTracker,
    analyze,
    voice_text,
)

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)

ROIS: tuple[StationRoi, ...] = (
    StationRoi(station_id=1, bbox=BBox(0.0, 0.0, 0.42, 1.0), name="灶台A"),
    StationRoi(station_id=2, bbox=BBox(0.58, 0.0, 1.0, 1.0), name="灶台B"),
)

P1 = BBox(0.10, 0.20, 0.30, 0.90)
P2 = BBox(0.60, 0.20, 0.80, 0.90)
P_PASS = BBox(0.42, 0.25, 0.49, 0.90)

JPEG = b"\xff\xd8\xff\xe0" + b"demo-frame" * 40


# ------------------------------------------------------------------ 造检测框
def person(box: BBox, conf: float = 0.92) -> Detection:
    return Detection("person", conf, box)


def head_of(box: BBox) -> BBox:
    return BBox(box.x1 + 0.01, box.y1 + 0.01, box.x2 - 0.01, box.y1 + box.h * 0.30)


def mask_of(box: BBox) -> BBox:
    top = box.y1 + box.h * 0.22
    return BBox(box.x1 + 0.03, top, box.x2 - 0.03, top + box.h * 0.16)


def torso_of(box: BBox) -> BBox:
    top = box.y1 + box.h * 0.32
    return BBox(box.x1 + 0.005, top, box.x2 - 0.005, box.y1 + box.h * 0.92)


def hand_of(box: BBox) -> BBox:
    top = box.y1 + box.h * 0.50
    return BBox(box.x1 + 0.06, top, box.x2 - 0.06, top + box.h * 0.12)


def full_kit(box: BBox) -> list[Detection]:
    """穿戴齐全。帽子必须给足（正向证据优先只在多人时生效）。"""
    return [person(box),
            Detection("hat", 0.88, head_of(box)),
            Detection("mask", 0.86, mask_of(box)),
            Detection("uniform", 0.84, torso_of(box))]


def no_mask(box: BBox) -> list[Detection]:
    return [person(box),
            Detection("hat", 0.88, head_of(box)),
            Detection("uniform", 0.84, torso_of(box))]


def phone_at(box: BBox) -> Detection:
    return Detection("phone", 0.72, hand_of(box))


def frame(seq: int, *, at: float = 0.0) -> Frame:
    return Frame(seq=seq, ts=T0 + timedelta(seconds=at), width=640, height=480,
                 uri=f"demo:{seq}", device_id="esp32cam-01")


def view(dets, seq: int, *, at: float = 0.0):
    return analyze(dets, frame(seq, at=at), ROIS, AnalysisPolicy())


@dataclass
class FakeMatcher:
    """可控人员池：qualified 是资质在册，pool 是四查都过（候选集）。"""

    qualified: frozenset[int]
    pool: frozenset[int]

    def qualifies(self, staff_id: int, requirement: str) -> bool:
        return int(staff_id) in self.qualified

    def candidates(self, requirement: str) -> tuple[int, ...]:
        return tuple(sorted(self.pool))


def say(text: str, verbose: bool) -> None:
    if verbose:
        print(f"     {text}")


def head(title: str) -> None:
    print(f"\n【{title}】")


# ------------------------------------------------------------------ 各段
def step1_detect(verbose: bool) -> None:
    head("① 帧源与逐帧判定")
    dets = full_kit(P1) + no_mask(P2)
    result = view(dets, 1)
    say(f"画面：{result.describe()}", verbose)
    for state in result.persons:
        wear = "".join([
            "帽" if state.has_hat else "×",
            "罩" if state.has_mask else "×",
            "服" if state.has_uniform else "×",
        ])
        say(f"ST{state.station_id} 穿戴[{wear}] 置信度 {state.conf:.2f}", verbose)
    missing = [f"ST{s.station_id}:{s.missing()}" for s in result.persons if s.missing()]
    print(f"  结论：{result.headcount} 人，缺失 {missing}")


def step2_debounce(verbose: bool) -> None:
    head("② 去抖：单帧不判违规")
    tracker = ViolationTracker(ViolationPolicy(min_sec=1.0))
    dets = no_mask(P1)
    for index in range(4):
        out = tracker.observe(view(dets, index + 1, at=index * 0.8))
        tag = f"第 {index + 1} 帧 → " + (
            "、".join(v.describe() for v in out) if out else "不报警")
        say(tag, verbose)
        if out:
            print(f"  结论：累计 {out[0].frames} 帧、{out[0].elapsed:.1f}s 才出事件")


def step3_dress(verbose: bool, evidence_root: Path) -> None:
    head("③ 着装违规：只语音，不切电")
    bank = StationPowerBank(capacity=2)
    tracker = ViolationTracker(ViolationPolicy(min_sec=1.0))
    said: list[str] = []
    disposal = ViolationDisposal(bank, tracker=tracker, voice=said.append,
                                 clock=lambda: T0, min_cut_sec=0)
    fired = []
    for index in range(4):
        fired.extend(tracker.observe(view(no_mask(P1), index + 1, at=index * 0.8)))
    for violation in fired:
        result = disposal.dispose(violation, now=T0)
        says = voice_text(violation.kind, violation.station_id)
        dbg = f"{violation.describe()} → {result.reason}"
        say(dbg, verbose)
        print(f"  结论：{says}｜切电={bank.is_cut(1)}（着装类不断电）")
        break


def step4_behavior(verbose: bool) -> ViolationDisposal:
    head("④ 行为违规：切该工位插座 + 语音 + 开单")
    bank = StationPowerBank(capacity=2)
    tracker = ViolationTracker(ViolationPolicy(min_sec=1.0))
    said: list[str] = []
    disposal = ViolationDisposal(bank, tracker=tracker, voice=said.append,
                                 clock=lambda: T0, min_cut_sec=0)
    dets = full_kit(P1) + [phone_at(P1)]
    fired = []
    for index in range(4):
        fired.extend(tracker.observe(view(dets, index + 1, at=index * 0.8)))
    for violation in fired:
        result = disposal.dispose(violation, now=T0)
        say(result.describe(), verbose)
    print(f"  结论：{said[-1] if said else '—'}")
    print(f"        ST1 插座断电={bank.is_cut(1)}｜ST2 插座断电={bank.is_cut(2)}（只切违规工位）")
    return disposal


def step5_evidence(verbose: bool, root: Path) -> EvidenceStore:
    head("⑤ 证据链：多图 + 哈希，可验签")
    store = EvidenceStore(root, model_name="yolov8", model_version="kitchen-demo",
                          policy={"min_frames": 3, "min_sec": 1.5})
    tracker = ViolationTracker(ViolationPolicy(min_sec=1.0))
    fired = []
    for index in range(4):
        fired.extend(tracker.observe(
            view(full_kit(P1) + [phone_at(P1)], index + 1, at=index * 0.8)))
    violation = fired[0]
    frames = [(seq, T0 + timedelta(seconds=seq * 0.8)) for seq in violation.evidence_seq]
    package = store.capture(violation, frames, lambda seq: JPEG + bytes([seq & 0xFF]))
    say(package.describe(), verbose)
    for entry in package.files:
        say(f"{entry.name}  {entry.sha256[:16]}…  {entry.size}B", verbose)
    print(f"  结论：{len(package.files)} 张留证，校验={'通过' if not store.verify(package) else '失败'}")

    target = package.path / package.files[0].name
    original = target.read_bytes()
    target.write_bytes(original + b"tampered")
    problems = store.verify(package)
    print(f"        改一个字节后：{problems[0] if problems else '没发现（有问题）'}")
    target.write_bytes(original)
    return store


def step6_review(verbose: bool, disposal: ViolationDisposal) -> None:
    head("⑥ 双通道复核：一个人确认不够")
    bank = disposal.bank
    first = disposal.confirm(1, "tft", "王组长", now=T0 + timedelta(seconds=20))
    say(first.describe(), verbose)
    print(f"  结论：{first.describe()}")
    second = disposal.confirm(1, "web", "王组长", now=T0 + timedelta(seconds=22))
    say(second.describe(), verbose)
    ticket = second.ticket
    print(f"        两通道齐 → 恢复供电={not bank.is_cut(1)}，"
          f"工单#{ticket.id if ticket else '?'} 状态={ticket.state if ticket else '?'}")

    try:
        disposal.confirm(2, "tft", "张三", now=T0)
    except Exception as exc:                            # noqa: BLE001
        print(f"        非组长/本人复核被拒：{exc}")


def step7_absence(verbose: bool) -> None:
    head("⑦ 离岗转派：未开火能转，已开火不能转")
    from kitchen.vision.absence import AbsenceEvent, AbsenceReassigner

    def new_board(pool: frozenset[int]):
        repo = MemoryProcessRepository.from_seed()
        orders = OrderService(repo, clock=lambda: T0)
        board = DispatchBoard(repo, orders, clock=lambda: T0,
                              matcher=FakeMatcher(pool, pool))
        return orders, board

    # 7a：派了单、人还没走到灶前就跑 —— 这正是转派能救的场景
    orders, board = new_board(frozenset({1, 3, 5}))
    order = orders.place_order(table_id=1, items=[("D-QJRS", 1), ("D-GBJD", 1)])
    a, b = order.items
    board.dispatch_pending(staff_plan={a.id: 1, b.id: 3})
    say(f"派单：#{a.id}{a.dish_name}→ST1/1号，#{b.id}{b.dish_name}→ST2/3号", verbose)
    occupant = board.station_staff(1) or board.reserved_staff(1)
    say(f"ST1 当前责任人 = {occupant}（从预约里取，因为人还没上灶）", verbose)
    reassigner = AbsenceReassigner(board, notify=lambda t, p: say(f"推送 {t}", verbose))
    event = AbsenceEvent(station_id=1, staff_id=1, since=T0,
                         last=T0 + timedelta(seconds=13), seconds=13.0)
    result = reassigner.handle(event)
    say(result.describe(), verbose)
    print(f"  7a 结论：{result.describe()}")

    # 7b：人已经在灶上开火 —— 中途换人等于重做
    orders2, board2 = new_board(frozenset({1, 3}))
    order2 = orders2.place_order(table_id=2, items=[("D-QJRS", 1)])
    c = order2.items[0]
    board2.dispatch_pending(staff_plan={c.id: 1})
    board2.occupy(1, 1, item_id=c.id)
    say(f"#{c.id}{c.dish_name} 已上灶开火，1 号离岗", verbose)
    result2 = AbsenceReassigner(board2).handle(
        AbsenceEvent(station_id=1, staff_id=1, since=T0,
                     last=T0 + timedelta(seconds=13), seconds=13.0))
    for line in result2.blocked:
        say(f"· {line}", verbose)
    print(f"  7b 结论：{result2.blocked[0] if result2.blocked else '—'}")


def step8_flame(verbose: bool) -> None:
    head("⑧ 接 #3 明火离人：按本工位算人数")
    from kitchen.rules.config_store import load_builtin
    from kitchen.runtime import KitchenRuntime

    stamp = 1000.0
    runtime = KitchenRuntime(load_builtin(), device_id="K-DEMO",
                             clock=lambda: stamp, wall_clock=lambda: stamp)
    guard = FlameGuard(runtime.engine, card_lookup=lambda uid: None, clock=lambda: stamp)

    link_a = FlameVisionLink(guard, station_id=1, clock=lambda: stamp)
    link_b = FlameVisionLink(guard, station_id=2, clock=lambda: stamp)

    cases = [
        ("ST1 站着人", view(full_kit(P1), 1)),
        ("ST1 空、ST2 有人", view(full_kit(P2), 2)),
        ("只有过道有人", view(full_kit(P_PASS), 3)),
        ("全空", view([], 4)),
    ]
    for label, vf in cases:
        result = link_a.push(vf)
        say(f"{label:16} → {result.describe()}｜信号 "
            f"{result.signals.get(VISION_NO_PERSON)}", verbose)
        if label == "ST1 空、ST2 有人":
            print(f"  结论：ST1 判无人（{result.signals.get(VISION_NO_PERSON)}），"
                  f"ST2 的人不会被当成「有人看火」")
    offline = link_b.push(view([], 5), online=False)
    print(f"        离线不投递：{offline.describe()}")


def step9_blind(verbose: bool) -> None:
    head("⑨ 视觉被遮挡：连续空帧 + 工位仍有占用")
    from kitchen.vision.pipeline import VisionPipeline
    from kitchen.vision.stream import FrameReader, MemoryFrameSource

    frames = [frame(index + 1, at=index * 0.8) for index in range(6)]
    source = MemoryFrameSource(frames=frames,
                              pixels={f.uri: JPEG for f in frames})
    reader = FrameReader(source, clock=lambda: T0)
    reader.open()
    pipe = VisionPipeline(reader, ScriptedDetector([[] for _ in frames]),
                          rois=ROIS, occupant_of=lambda sid: 1 if sid == 1 else None,
                          blind_frames=5)
    results = pipe.run()
    for index, item in enumerate(results, start=1):
        if item.blind:
            print(f"  结论：第 {index} 帧起判定「视觉被遮挡」{item.blind}"
                  f"——不是离岗，是要人去看摄像头")
            break
        say(f"第 {index} 帧 {item.describe()}", verbose)


# ------------------------------------------------------------------ 入口
def make_root(raw: str) -> tuple[Path, bool]:
    """证据目录。默认落到 `var/evidence`（`var/` 已在 .gitignore）。

    **不用 `tempfile.mkdtemp()`**：本机 TEMP 是 8.3 短名、不可写，
    `gettempdir()` 会回退到当前工作目录，于是「临时目录」其实建在了工程根，
    跑完还常常删不掉（文件句柄占着）。固定到 `var/` 下可预测、可查看、
    也方便答辩后翻证据包。
    """
    if raw:
        root = Path(raw)
        root.mkdir(parents=True, exist_ok=True)
        return root, False
    root = Path("var") / "evidence"
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root, False


def run(*, verbose: bool = True, evidence_root: str = "") -> int:
    root, temporary = make_root(evidence_root)
    try:
        step1_detect(verbose)
        step2_debounce(verbose)
        step3_dress(verbose, root)
        disposal = step4_behavior(verbose)
        step5_evidence(verbose, root / "evidence")
        step6_review(verbose, disposal)
        step7_absence(verbose)
        step8_flame(verbose)
        step9_blind(verbose)
        print(f"\n证据目录：{root}")
        return 0
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="视觉闭环演练")
    parser.add_argument("--quiet", action="store_true", help="只打印关键结论")
    parser.add_argument("--evidence", default="", help="证据目录（默认临时目录）")
    args = parser.parse_args(argv)
    return run(verbose=not args.quiet, evidence_root=args.evidence)


if __name__ == "__main__":
    raise SystemExit(main())
