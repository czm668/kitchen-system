# -*- coding: utf-8 -*-
"""检测层契约（A5-02 模型侧）的专门单测。

以前检测层零单测——"不依赖 ultralytics 也能验检测契约"只是句承诺，没被钉死。
本文件用 monkeypatch 注入假的 ultralytics 模块，全程不 import 真包、不碰权重，
覆盖 from_yolo_rows 的坐标换算/越界/丢弃、ScriptedDetector 的帧序语义、
YoloDetector 的惰性加载与 detect 输出契约。
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from vision_support import frame

from kitchen.vision.detector import (
    ScriptedDetector,
    YoloDetector,
    from_yolo_rows,
    load_detections,
)
from kitchen.vision.models import BBox, Detection, VisionError


class TestFromYoloRows:
    """YOLO [cx,cy,w,h,conf,cls] → 归一化 Detection。"""

    def test_像素坐标换算成归一化(self):
        # 640x480 画面中央一个 128x96 的框：x1=(320-64)/640=0.4, x2=(320+64)/640=0.6
        got = from_yolo_rows([[320, 240, 128, 96, 0.9, 0]], width=640, height=480)
        assert len(got) == 1
        d = got[0]
        assert d.cls == "person"
        assert d.conf == pytest.approx(0.9)
        assert d.bbox == BBox(0.4, 0.4, 0.6, 0.6)

    def test_自定义类别名(self):
        got = from_yolo_rows([[100, 100, 20, 20, 0.7, 5]], width=200, height=200,
                             names=("person", "hat", "mask", "uniform", "cig", "phone"))
        assert got[0].cls == "phone"

    @pytest.mark.parametrize("wh", [(0, 480), (640, 0), (-1, 480)])
    def test_帧尺寸非法(self, wh):
        with pytest.raises(VisionError, match="帧尺寸非法"):
            from_yolo_rows([], width=wh[0], height=wh[1])

    def test_行长度不足报错(self):
        with pytest.raises(VisionError, match="行长度不足"):
            from_yolo_rows([[1, 2, 3]], width=640, height=480)

    @pytest.mark.parametrize("cls_id", [-1, 6, 99])
    def test_类别越界报错(self, cls_id):
        with pytest.raises(VisionError, match="类别 id 越界"):
            from_yolo_rows([[10, 10, 5, 5, 0.9, cls_id]], width=640, height=480)

    def test_完全在画面外的框丢弃(self):
        # 框心在 (-100,-100)，半宽 10 → x2=max(0,(-90)/640)=0, x1=0 → 退化被丢
        rows = [[-100, -100, 20, 20, 0.9, 0], [320, 240, 100, 100, 0.8, 1]]
        got = from_yolo_rows(rows, width=640, height=480)
        assert len(got) == 1
        assert got[0].cls == "hat"

    def test_超出画面的框被clamp到边界(self):
        got = from_yolo_rows([[640, 480, 2000, 2000, 0.9, 0]], width=640, height=480)
        assert got[0].bbox == BBox(0.0, 0.0, 1.0, 1.0)

    def test_空行列表返回空(self):
        assert from_yolo_rows([], width=640, height=480) == []


class TestScriptedDetector:
    def test_按帧序返回并用完重复最后一帧(self):
        det = ScriptedDetector([[], [Detection("person", 0.9, BBox(0.1, 0.1, 0.3, 0.9))]])
        assert det.detect(frame(1), b"x") == []
        assert det.detect(frame(2), b"x")[0].cls == "person"
        # 帧用尽后重复最后一帧（非 strict）
        assert det.detect(frame(3), b"x")[0].cls == "person"
        assert det.calls == [1, 2, 3]

    def test_strict帧用尽报错(self):
        det = ScriptedDetector([[Detection("person", 0.9, BBox(0.1, 0.1, 0.3, 0.9))]], strict=True)
        det.detect(frame(1), b"x")
        with pytest.raises(VisionError, match="脚本已用尽"):
            det.detect(frame(2), b"x")

    def test_reset回到开头(self):
        det = ScriptedDetector([[Detection("person", 0.9, BBox(0.1, 0.1, 0.3, 0.9))]])
        det.detect(frame(1), b"x")
        det.reset()
        assert det.calls == []
        assert det.detect(frame(1), b"x")[0].cls == "person"

    def test_from_json读夹具(self, tmp_path):
        data = [
            [{"cls": "person", "conf": 0.92, "bbox": [0.1, 0.2, 0.3, 0.9]}],
            [],
        ]
        p = tmp_path / "script.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        det = ScriptedDetector.from_json(p)
        got = det.detect(frame(1), b"x")
        assert got[0].cls == "person"
        assert got[0].bbox == BBox(0.1, 0.2, 0.3, 0.9)
        assert det.detect(frame(2), b"x") == []

    def test_from_json_strict透传(self, tmp_path):
        p = tmp_path / "script.json"
        p.write_text("[]", encoding="utf-8")
        assert ScriptedDetector.from_json(p, strict=True).strict is True


class TestYoloDetector:
    def test_构造不触发import(self):
        # 真机环境没装 ultralytics，构造也必须成功（惰性）
        det = YoloDetector("weights/kitchen.pt")
        assert det.version == "unknown"
        assert det._model is None

    def test_无像素数据报错(self):
        det = YoloDetector("weights/kitchen.pt")
        with pytest.raises(VisionError, match="没有像素数据"):
            det.detect(frame(1), b"")

    def test_未装ultralytics降级为VisionError(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ultralytics", None)  # import 会抛 ImportError
        det = YoloDetector("weights/kitchen.pt")
        with pytest.raises(VisionError, match="未安装 ultralytics"):
            det._load()

    def test_权重不存在报错(self, monkeypatch, tmp_path):
        fake = types.ModuleType("ultralytics")
        fake.YOLO = object
        monkeypatch.setitem(sys.modules, "ultralytics", fake)
        det = YoloDetector(str(tmp_path / "missing.pt"))
        with pytest.raises(VisionError, match="权重文件不存在"):
            det._load()

    def test_detect输出契约(self, monkeypatch, tmp_path):
        """注入假 YOLO：验证 predict 调用参数、xyxy→Detection、version 更新。"""
        weight = tmp_path / "kitchen.pt"
        weight.write_bytes(b"fake")

        class FakeTensor:
            """模拟 torch tensor：只有 detect 用到的 .tolist() / float() 接口。"""
            def __init__(self, value):
                self._value = value

            def tolist(self):
                return self._value

            def __float__(self):
                return float(self._value)

        class FakeBox:
            def __init__(self, xyxy, conf, cls):
                self.xyxy = [FakeTensor(xyxy)]
                self.conf = [FakeTensor(conf)]
                self.cls = [FakeTensor(cls)]

        class FakeResult:
            boxes = [FakeBox([100, 100, 200, 300], 0.91, 0)]  # 640x480 中央

        captured = {}

        class FakeYOLO:
            def __init__(self, path):
                captured["path"] = path

            def predict(self, image, **kwargs):
                captured["kwargs"] = kwargs
                return [FakeResult()]

        fake = types.ModuleType("ultralytics")
        fake.YOLO = FakeYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", fake)
        # _decode 依赖 cv2/numpy，用假图像对象替掉，只验 detect 的数据流
        monkeypatch.setattr(YoloDetector, "_decode", staticmethod(lambda data: "FAKE_IMAGE"))

        det = YoloDetector(str(weight), conf=0.3, imgsz=320, device="cpu")
        got = det.detect(frame(1, width=640, height=480), b"jpeg")

        assert captured["path"] == str(weight)
        assert captured["kwargs"]["conf"] == 0.3
        assert captured["kwargs"]["imgsz"] == 320
        assert det.version == "kitchen"   # 权重文件名（去扩展名）
        assert len(got) == 1
        assert got[0].cls == "person"
        assert got[0].conf == pytest.approx(0.91)
        assert got[0].bbox == BBox(100 / 640, 100 / 480, 200 / 640, 300 / 480)

    def test_load只加载一次(self, monkeypatch, tmp_path):
        weight = tmp_path / "kitchen.pt"
        weight.write_bytes(b"fake")
        calls = []

        class FakeYOLO:
            def __init__(self, path):
                calls.append(path)

        fake = types.ModuleType("ultralytics")
        fake.YOLO = FakeYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", fake)
        det = YoloDetector(str(weight))
        det._load()
        det._load()
        assert len(calls) == 1


class TestLoadDetections:
    def test_把检测器和帧源包成函数(self):
        det = ScriptedDetector([[Detection("person", 0.9, BBox(0.1, 0.1, 0.3, 0.9))]])
        fn = load_detections(lambda f: b"jpeg", det)
        got = fn(frame(1))
        assert got[0].cls == "person"
