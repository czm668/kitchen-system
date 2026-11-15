# -*- coding: utf-8 -*-
"""检测器契约（A5-02 的模型侧）。

## 模型是可选依赖

真机用 YOLOv8（`ultralytics`），离线开发机没有这个包也不该让整个工程 import
失败。所以 `YoloDetector` **惰性导入**：只有真的实例化它时才 `import`，
`from kitchen.vision import ...` 在任何环境下都能过。

这不是洁癖：`tests/test_manifest.py` 会扫所有模块能不能导入，一个硬依赖
会让 652 条测试里的一半在没联网的机器上直接红。

## 输出原样不解释

检测器只负责「图 + 阈值 → 框列表」，**不做 NMS（模型已做）、不做 ROI 归属、
不做「这个人戴没戴帽子」**。这些都在 `postprocess`，因为它们能被单测覆盖，
而模型不能。答辩被问「模型漏检怎么办」时，答案必须是「漏检的后果在这里、
由这几条测试兜住」，而不是「我们调了调阈值」。

## 训练数据：先用公开集，再增量标注

`tools/vision_train.py --check` 会打印训练命令与数据集校验结果，但**不会真的
训练**（需要 GPU + 数据集，本机都没有）。数据集路径与类别顺序写在
`fixtures/vision_dataset.yaml`，与 `models.CLASSES` 必须一致——顺序错了模型
会把帽子认成手机，代码里完全看不出来。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import CLASSES, BBox, Detection, Frame, VisionError

log = logging.getLogger("kitchen.vision.detector")

__all__ = [
    "Detector",
    "ScriptedDetector",
    "YoloDetector",
    "from_yolo_rows",
    "load_detections",
]


@runtime_checkable
class Detector(Protocol):
    """检测器。`name` 与 `version` 会写进证据链——复核时要知道这张图是谁判的。"""

    name: str
    version: str

    def detect(self, frame: Frame, frame_bytes: bytes) -> list[Detection]: ...


def from_yolo_rows(rows: Iterable[Sequence[float]], *, width: int, height: int,
                   names: Sequence[str] = CLASSES) -> list[Detection]:
    """YOLO 的 `[x_center, y_center, w, h, conf, cls]` → `Detection`。

    **必须传像素宽高**：YOLO 输出是像素坐标，不转换就会拿 640 去跟 0.6 比，
    于是所有框都被判成「不在工位」。这个错误不会抛异常，只会让所有功能静默失效。
    """
    if width <= 0 or height <= 0:
        raise VisionError(f"帧尺寸非法：{width}x{height}")
    out: list[Detection] = []
    for row in rows:
        if len(row) < 6:
            raise VisionError(f"YOLO 行长度不足 6：{row}")
        cx, cy, w, h, conf, cls_id = (float(v) for v in row[:6])
        index = int(cls_id)
        if not 0 <= index < len(names):
            raise VisionError(f"类别 id 越界：{index}（共 {len(names)} 类）")
        x1 = max(0.0, min(1.0, (cx - w / 2) / width))
        y1 = max(0.0, min(1.0, (cy - h / 2) / height))
        x2 = max(0.0, min(1.0, (cx + w / 2) / width))
        y2 = max(0.0, min(1.0, (cy + h / 2) / height))
        if x2 <= x1 or y2 <= y1:
            continue        # 完全在画面外的框，丢弃
        out.append(Detection(cls=names[index], conf=conf, bbox=BBox(x1, y1, x2, y2)))
    return out


class ScriptedDetector:
    """按脚本吐检测框。离线开发、单测、答辩兜底都用它。

    两种用法：
    - `ScriptedDetector([[...], [...]])`：按帧序依次返回，用完重复最后一帧；
    - `ScriptedDetector.from_json(path)`：从夹具读（演示里用真实标注过的帧）。

    `strict=True` 时帧数用尽抛错——单测需要这个，否则「少喂了一帧」会被
    静默当成「画面没变」，用例永远绿。
    """

    name = "scripted"
    version = "1.0"

    def __init__(self, script: Sequence[Sequence[Detection]],
                 *, strict: bool = False) -> None:
        self.script = [list(item) for item in script]
        self.strict = strict
        self._cursor = 0
        self.calls: list[int] = []

    @classmethod
    def from_json(cls, path: Path | str, *, strict: bool = False) -> ScriptedDetector:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        script = [
            [
                Detection(cls=item["cls"], conf=float(item["conf"]),
                          bbox=BBox(*(float(v) for v in item["bbox"])))
                for item in frame
            ]
            for frame in raw
        ]
        return cls(script, strict=strict)

    def detect(self, frame: Frame, frame_bytes: bytes) -> list[Detection]:
        self.calls.append(frame.seq)
        if self._cursor >= len(self.script):
            if self.strict:
                raise VisionError(f"脚本已用尽（{len(self.script)} 帧），收到第 {frame.seq} 帧")
            return list(self.script[-1]) if self.script else []
        out = list(self.script[self._cursor])
        self._cursor += 1
        return out

    def reset(self) -> None:
        self._cursor = 0
        self.calls.clear()


class YoloDetector:
    """YOLOv8 检测器（A5-02 真机路径）。**惰性加载模型**。

    ```python
    det = YoloDetector("weights/kitchen.pt", conf=0.35, imgsz=416)
    ```

    `conf` 传模型侧的**低**门槛（0.25 左右），让后处理按类别分档过滤——
    在模型侧就砍到 0.5，「大部分人戴帽子」这种统计特征会被削掉。
    """

    name = "yolov8"

    def __init__(self, weights: Path | str, *, conf: float = 0.25,
                 imgsz: int = 416, device: str = "cpu",
                 names: Sequence[str] = CLASSES) -> None:
        self.weights = str(weights)
        self.conf = float(conf)
        self.imgsz = int(imgsz)
        self.device = device
        self.names = tuple(names)
        self.version = "unknown"
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO          # noqa: PLC0415  惰性导入
        except ImportError as exc:                # pragma: no cover - 环境相关
            raise VisionError(
                "未安装 ultralytics，真机检测不可用。离线开发请用 ScriptedDetector；"
                "判定层（postprocess）不依赖本类"
            ) from exc
        if not Path(self.weights).exists():
            raise VisionError(f"权重文件不存在：{self.weights}")
        self._model = YOLO(self.weights)
        self.version = Path(self.weights).stem
        return self._model

    def detect(self, frame: Frame, frame_bytes: bytes) -> list[Detection]:
        if not frame_bytes:
            raise VisionError(f"帧#{frame.seq} 没有像素数据")
        model = self._load()
        results = model.predict(
            self._decode(frame_bytes), conf=self.conf, imgsz=self.imgsz,
            device=self.device, verbose=False,
        )
        rows: list[Sequence[float]] = []
        for result in results:
            for box in getattr(result, "boxes", ()):
                coords = box.xyxy[0].tolist()
                x1, y1, x2, y2 = (float(v) for v in coords[:4])
                rows.append([
                    (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1,
                    float(box.conf[0]), float(box.cls[0]),
                ])
        return from_yolo_rows(rows, width=frame.width, height=frame.height,
                              names=self.names)

    @staticmethod
    def _decode(data: bytes) -> Any:                     # pragma: no cover
        """JPEG bytes → ndarray。依赖 cv2/numpy，仅真机路径用。"""
        import numpy as np                                 # noqa: PLC0415
        import cv2                                         # noqa: PLC0415
        buf = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if image is None:
            raise VisionError("JPEG 解码失败（帧数据损坏）")
        return image


def load_detections(source: Callable[[Frame], bytes],
                    detector: Detector) -> Callable[[Frame], list[Detection]]:
    """把「帧 → 检测框」包成一个函数，供 `VisionPipeline` 注入。"""

    def run(frame: Frame) -> list[Detection]:
        return detector.detect(frame, source(frame))

    return run
