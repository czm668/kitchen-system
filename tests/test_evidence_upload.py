# -*- coding: utf-8 -*-
"""客诉图片证据上传测试：类型/大小/鉴权/读取穿越防护。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from api_support import data_of, make_server
from kitchen.guest.uploads import UploadError, detect_ext

KEY = "g-upload-001"
# 1x1 透明 PNG
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=3, guest_key=KEY, tags=[])


def _post_png(server, token, guest_key, data=PNG, *, headers=None):
    url = f"{server.url}/api/customer/guest/evidence-upload"
    hdr = {"X-Customer-Token": token, "X-Guest-Key": guest_key,
           "Content-Type": "application/octet-stream"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestDetectExt:
    @pytest.mark.parametrize("head,ext", [
        (b"\x89PNG\r\n\x1a\n...", "png"),
        (b"\xff\xd8\xff\xe0...", "jpg"),
        (b"RIFF....WEBP...", "webp"),
        (b"GIF89a...", "gif"),
    ])
    def test_魔数识别(self, head, ext):
        assert detect_ext(head) == ext

    def test_非图片被拒(self):
        with pytest.raises(UploadError):
            detect_ext(b"this is not an image")


class TestUploadHttp:
    def test_合法图片上传并读回(self, server):
        s = _open(server)
        server.start()
        try:
            status, payload = _post_png(server, s["customer_token"], KEY)
            assert status == 200
            assert payload["url"].startswith("/api/customer/guest/evidence-image")
            name = payload["url"].split("name=")[1]
            with urllib.request.urlopen(f"{server.url}{payload['url']}", timeout=5) as r:
                assert r.read() == PNG
                assert r.headers.get_content_type() == "image/png"
        finally:
            server.stop()

    def test_错误令牌被拒(self, server):
        _open(server)
        server.start()
        try:
            status, payload = _post_png(server, "wrong-token", KEY)
            assert status == 400
            assert "url" not in payload
        finally:
            server.stop()

    def test_非图片内容被拒(self, server):
        s = _open(server)
        server.start()
        try:
            status, payload = _post_png(server, s["customer_token"], KEY,
                                        data=b"not an image at all")
            assert status == 400
        finally:
            server.stop()

    def test_multipart上传并读回(self, server):
        """小程序 wx.uploadFile 发的是 multipart/form-data，必须能真正落盘。"""
        s = _open(server)
        server.start()
        try:
            boundary = "----KitchenBoundary12345"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="image"; filename="a.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
            url = f"{server.url}/api/customer/guest/evidence-upload"
            hdr = {"X-Customer-Token": s["customer_token"], "X-Guest-Key": KEY,
                   "Content-Type": f"multipart/form-data; boundary={boundary}"}
            req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read())
            assert payload["url"].startswith("/api/customer/guest/evidence-image")
            with urllib.request.urlopen(f"{server.url}{payload['url']}", timeout=5) as r:
                assert r.read() == PNG
        finally:
            server.stop()

    def test_multipart缺图片字段被拒(self, server):
        s = _open(server)
        server.start()
        try:
            boundary = "----KitchenBoundary12345"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="note"\r\n\r\n'
                f"只是一段文字\r\n--{boundary}--\r\n"
            ).encode()
            url = f"{server.url}/api/customer/guest/evidence-upload"
            hdr = {"X-Customer-Token": s["customer_token"], "X-Guest-Key": KEY,
                   "Content-Type": f"multipart/form-data; boundary={boundary}"}
            req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 400
        finally:
            server.stop()

    def test_取图拒绝穿越文件名(self, server):
        _open(server)
        server.start()
        try:
            url = f"{server.url}/api/customer/guest/evidence-image?name=../../etc/passwd"
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(url, timeout=5)
            assert exc.value.code == 404
        finally:
            server.stop()

    def test_图片作为证据进工单(self, server):
        s = _open(server)
        server.start()
        try:
            status, payload = _post_png(server, s["customer_token"], KEY)
            url = payload["url"]
            # 用上传得到的 url 提交一张带图的客诉
            row = data_of(server, "POST", "/api/customer/guest/complaints",
                          customer_token=s["customer_token"], guest_key=KEY,
                          text="菜里有异物，附现场照片",
                          evidences=[{"kind": "photo", "content": "", "url": url}])
            assert row["state"] in ("submitted", "auto_closed")
            complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                                 customer_token=s["customer_token"], guest_key=KEY)
            ev = complaints["complaints"][0]["evidences"][0]
            assert ev["kind"] == "photo"
            assert ev["url"] == url
        finally:
            server.stop()
