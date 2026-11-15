# -*- coding: utf-8 -*-
"""抓拍与证据链（A5-03）。

## 为什么「存了图」不等于「有证据」

复核争议时要说清三件事：**这张图是哪一刻的、是谁判的、有没有被改过**。
只把 jpg 丢进目录，三件都说不出：

- 没有时间戳 → 「这张是上次违规的图」；
- 没有模型版本 → 换了模型之后，同一张图判出相反结论，无法解释；
- 没有哈希 → 图可以被替换，「证据」就成了装饰。

所以每个证据包是一个目录：

```
evidence/20261112/ST1/20261112T113245-ST1-smoking/
├── 0003-113245-120.jpg     帧序号-时刻-毫秒
├── 0004-113246-340.jpg     （违规前若干帧一起存）
├── 0005-113246-540.jpg     （违规成立那一帧）
└── meta.json               哈希清单 + 判决参数 + 模型版本
```

## 三条硬口径

1. **meta.json 最后写，且原子落盘**。先写清单再写图，中途磁盘满就会留下
   「有清单、图不全」的包，校验工具报「证据损坏」，人会以为系统坏了。
   反过来（先图后清单）最坏情况是「有图无清单」——一眼看出是未完成的包。
2. **只存文件名不带路径的哈希，验签时按相对路径重算**。存绝对路径的清单
   换台机器就全红，答辩换电脑跑一遍就「证据全部被篡改」。
3. **帧窗口 ≥ 3 帧**。只存报警那一帧，无法回答「是不是只有这一帧」，而这
   恰恰是现场申诉的第一句话。

## 存储位置

默认 `var/evidence/`（`--evidence-root` 可改）。**不放临时目录**：临时目录
在重启后会被清，而留证的意义是「一个月后还能查」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..domain import CST
from .models import VisionError
from .violations import Violation

log = logging.getLogger("kitchen.vision.evidence")

__all__ = [
    "EvidenceFile",
    "EvidencePackage",
    "EvidenceStore",
    "EvidenceError",
    "DEFAULT_EVIDENCE_ROOT",
    "MAX_EVIDENCE_FILES",
]

#: 默认根目录。相对路径按工程根解析（`02_项目代码/kitchen/var/evidence`）
DEFAULT_EVIDENCE_ROOT = "var/evidence"

#: 单个证据包最多存几张图。抓拍窗口配大了会把磁盘吃满——
#: 一次违规 5 帧 × 60KB ≈ 300KB，一天 30 次违规还不到 10MB，可接受；
#: 但配成 50 帧就是 3MB/次，SD 卡一周就满。
MAX_EVIDENCE_FILES = 12


class EvidenceError(VisionError):
    """证据链相关错误（写盘失败、哈希不匹配、包不完整）。"""


@dataclass(frozen=True)
class EvidenceFile:
    """证据包里的一张图。"""

    name: str
    sha256: str
    size: int
    frame_seq: int
    ts: datetime

    def to_payload(self) -> dict:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "frame_seq": self.frame_seq,
            "ts": int(self.ts.timestamp() * 1000),
        }

    @classmethod
    def from_payload(cls, raw: dict) -> EvidenceFile:
        return cls(
            name=str(raw["name"]),
            sha256=str(raw["sha256"]),
            size=int(raw["size"]),
            frame_seq=int(raw["frame_seq"]),
            ts=datetime.fromtimestamp(int(raw["ts"]) / 1000, CST),
        )


@dataclass
class EvidencePackage:
    """一个已落盘的证据包。"""

    event_id: str
    path: Path
    kind: str
    station_id: int
    files: tuple[EvidenceFile, ...] = ()
    meta: dict = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def describe(self) -> str:
        return (f"证据包 {self.event_id}：{len(self.files)} 张，"
                f"{self.total_bytes / 1024:.0f}KB → {self.path}")

    def primary(self) -> EvidenceFile | None:
        """最后一张（违规成立那一刻）——工单缩略图用它。"""
        return self.files[-1] if self.files else None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvidenceStore:
    """证据包读写。

    `reader` 是「给一个帧序号/帧对象，返回像素」的函数——证据层不自己开帧源，
    否则它就跟 stream 层耦合，单测必须造图。默认从 `frames` 字典取，没有就
    退回帧源。
    """

    def __init__(self, root: Path | str = DEFAULT_EVIDENCE_ROOT,
                 *,
                 model_name: str = "scripted",
                 model_version: str = "1.0",
                 policy: dict | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.root = Path(root)
        self.model_name = model_name
        self.model_version = model_version
        self.policy = dict(policy or {})
        self._clock = clock or (lambda: datetime.now(CST))

    # ---------------------------------------------------------------- 抓拍
    def capture(
        self,
        violation: Violation,
        frames: Sequence[tuple[int, datetime]],
        read_bytes: Callable[[int], bytes],
        *,
        extra: dict | None = None,
    ) -> EvidencePackage:
        """按帧序号列表抓拍并存证。

        `frames` 是 `(帧序号, 时间戳)` 列表，`read_bytes(seq)` 返回像素。
        取不到像素的帧**跳过而不是整包失败**：三帧里坏一帧，剩下两帧仍
        能证明「持续违规」，为此丢掉整个证据包是过度反应。但一帧都取不到
        就必须报错——空证据包比没有证据包更糟（复核页面上是一片空白）。
        """
        if len(frames) > MAX_EVIDENCE_FILES:
            # 保留最后 MAX 帧（越靠近成立时刻越重要）
            frames = frames[-MAX_EVIDENCE_FILES:]
        if not frames:
            raise EvidenceError("抓拍帧列表为空")

        event_id = self._event_id(violation)
        folder = self.root / violation.since.strftime("%Y%m%d") \
            / f"ST{violation.station_id}" / event_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EvidenceError(f"证据目录创建失败：{folder}（{exc}）") from exc

        saved: list[EvidenceFile] = []
        skipped: list[int] = []
        for seq, ts in frames:
            try:
                data = read_bytes(int(seq))
            except Exception as exc:                    # noqa: BLE001
                log.warning("帧#%s 取像素失败，跳过：%s", seq, exc)
                skipped.append(int(seq))
                continue
            if not data:
                skipped.append(int(seq))
                continue
            name = f"{int(seq):04d}-{ts.strftime('%H%M%S')}-{ts.microsecond // 1000:03d}.jpg"
            target = folder / name
            try:
                self._atomic_write(target, data)
            except OSError as exc:
                raise EvidenceError(f"证据写入失败：{target}（{exc}）") from exc
            saved.append(EvidenceFile(name=name, sha256=sha256_of(data),
                                      size=len(data), frame_seq=int(seq), ts=ts))

        if not saved:
            raise EvidenceError(
                f"{event_id}：{len(frames)} 帧全部无法读取，拒绝生成空证据包"
            )

        meta = {
            "event_id": event_id,
            "kind": violation.kind,
            "label": violation.label,
            "station_id": violation.station_id,
            "since": int(violation.since.timestamp() * 1000),
            "last": int(violation.last.timestamp() * 1000),
            "frames": violation.frames,
            "elapsed_sec": round(violation.elapsed, 2),
            "power_cut": violation.power,
            "repeat": violation.repeat,
            "model": {"name": self.model_name, "version": self.model_version},
            "policy": self.policy,
            "files": [f.to_payload() for f in saved],
            "skipped_frames": skipped,
            "created_at": int(self._clock().timestamp() * 1000),
            "extra": dict(extra or {}),
        }
        # meta 最后写：中途失败最多留下「有图无清单」，一眼能看出是未完成的包
        try:
            self._atomic_write(
                folder / "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except OSError as exc:
            raise EvidenceError(f"证据清单写入失败：{folder}（{exc}）") from exc

        package = EvidencePackage(event_id=event_id, path=folder,
                                  kind=violation.kind,
                                  station_id=violation.station_id,
                                  files=tuple(saved), meta=meta)
        log.info("%s", package.describe())
        return package

    # ---------------------------------------------------------------- 校验
    def verify(self, package: EvidencePackage | Path | str) -> list[str]:
        """重算哈希。返回**问题列表**（空 = 完好）。

        返回列表而不是 bool：复核人要看到「第 3 张图不匹配」，而不是一个
        「校验失败」然后自己一张张比对。
        """
        folder = package.path if isinstance(package, EvidencePackage) else Path(package)
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            raise EvidenceError(f"证据包缺少 meta.json：{folder}")
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvidenceError(f"meta.json 无法解析：{meta_path}（{exc}）") from exc

        problems: list[str] = []
        for item in raw.get("files", []):
            entry = EvidenceFile.from_payload(item)
            target = folder / entry.name
            if not target.is_file():
                problems.append(f"缺文件：{entry.name}")
                continue
            data = target.read_bytes()
            if len(data) != entry.size:
                problems.append(f"大小不符：{entry.name}（{len(data)}≠{entry.size}）")
            digest = sha256_of(data)
            if digest != entry.sha256:
                problems.append(f"哈希不符：{entry.name}")
        if not raw.get("files"):
            problems.append("清单里没有任何文件")
        return problems

    def load(self, folder: Path | str) -> EvidencePackage:
        """读回一个证据包（Web 复核页面用）。"""
        root = Path(folder)
        meta_path = root / "meta.json"
        if not meta_path.is_file():
            raise EvidenceError(f"证据包不存在：{root}")
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return EvidencePackage(
            event_id=str(raw.get("event_id", root.name)),
            path=root,
            kind=str(raw.get("kind", "")),
            station_id=int(raw.get("station_id", 0)),
            files=tuple(EvidenceFile.from_payload(f) for f in raw.get("files", [])),
            meta=raw,
        )

    def list_day(self, day: datetime | str | None = None) -> list[EvidencePackage]:
        """列某天的全部证据包（按时间倒序）。`day` 支持 `date`/`datetime`/`YYYYMMDD`。"""
        if day is None:
            stamp = self._clock().strftime("%Y%m%d")
        elif isinstance(day, str):
            stamp = day.replace("-", "")
        else:
            stamp = day.strftime("%Y%m%d")
        root = self.root / stamp
        if not root.is_dir():
            return []
        out: list[EvidencePackage] = []
        for meta_path in sorted(root.glob("ST*/**/meta.json"), reverse=True):
            try:
                out.append(self.load(meta_path.parent))
            except (EvidenceError, ValueError, KeyError) as exc:
                log.warning("跳过损坏的证据包 %s：%s", meta_path.parent, exc)
        return out

    # ---------------------------------------------------------------- 内部
    def _event_id(self, violation: Violation) -> str:
        stamp = violation.since.strftime("%Y%m%dT%H%M%S")
        return f"{stamp}-ST{violation.station_id}-{violation.kind}"

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """先写 `.part` 再原子改名。断电只会留下 `.part`，不会留下半张图。"""
        tmp = target.with_name(target.name + ".part")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
