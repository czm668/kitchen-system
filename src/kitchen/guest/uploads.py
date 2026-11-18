# -*- coding: utf-8 -*-
"""客诉图片证据的落盘与读取（标准库实现，无第三方依赖）。

## 安全边界
* 文件类型白名单：JPEG/PNG/WebP/GIF，按**文件头魔数**判定，不信扩展名。
* 大小上限 2MB；一次性读完再校验，防止超大 body 吃内存。
* 文件名由 `secrets.token_hex` 生成，杜绝路径穿越与覆盖。
* 读取端点只接受 `^[a-f0-9]{16}\\.(jpg|png|webp|gif)$` 的文件名，
  不接受任何子目录，从根上堵死 `../` 穿越。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

__all__ = ["UploadError", "ImageStore", "detect_ext", "MAX_UPLOAD_BYTES",
           "extract_multipart_file"]

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
_ALLOWED = {"jpg", "png", "webp", "gif"}


class UploadError(ValueError):
    status = 400


def detect_ext(data: bytes) -> str:
    """按魔数判定图片类型，返回扩展名（不含点）；非白名单抛错。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    raise UploadError("只支持 JPG / PNG / WebP / GIF 图片")


def extract_multipart_file(raw: bytes, content_type: str,
                           *, field: str = "image") -> bytes:
    """从 `multipart/form-data` 请求体里取出名为 `field` 的文件原始字节。

    小程序的 `wx.uploadFile` 只能发 multipart（body 以 `--boundary` 开头），
    不能像脚本那样发裸二进制。旧实现把整段 multipart 当图片做魔数校验，
    必然失败——这是「选了图却传不上去」的根因。这里用标准库按 boundary
    切分，取出真正的图片字节，再交给 `detect_ext` 校验。

    只取第一个同名 file part；非 multipart 或找不到字段时抛 `UploadError`。
    boundary 里的引号/空白按 RFC 2046 处理，文件名不做信任依据（仍靠魔数）。
    """
    ctype = str(content_type or "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise UploadError("不是 multipart/form-data 请求")
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype, re.IGNORECASE)
    if not match:
        raise UploadError("multipart 缺少 boundary 参数")
    boundary = (match.group(1) or match.group(2) or "").strip()
    if not boundary or len(boundary) > 200:
        raise UploadError("multipart boundary 非法")
    delim = b"--" + boundary.encode("ascii", "ignore")
    # 以 --boundary 分段；每段形如：
    #   Content-Disposition: form-data; name="image"; filename="a.png"\r\n
    #   Content-Type: image/png\r\n\r\n<bytes>\r\n
    for segment in raw.split(delim):
        segment = segment.lstrip(b"\r\n")
        if segment in (b"--", b"--\r\n", b"") or not segment:
            continue
        head, sep, body = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_text = head.decode("latin-1", "ignore")
        # 字段名匹配且确实是文件 part（带 filename=），避免把普通文本字段当图
        if not re.search(r'\bname\s*=\s*"?%s' % re.escape(field),
                         head_text, re.IGNORECASE):
            continue
        if "filename" not in head_text.lower():
            continue
        # body 末尾的 \r\n 属于 multipart 帧，不属于图片本身
        if body.endswith(b"\r\n"):
            body = body[:-2]
        return body
    raise UploadError("multipart 中没有找到图片字段")


class ImageStore:
    """把图片写到 `root`，返回可访问的相对 URL。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes) -> str:
        if not data:
            raise UploadError("图片内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError("图片超过 2MB 上限")
        ext = detect_ext(data)
        name = f"{secrets.token_hex(8)}.{ext}"
        (self.root / name).write_bytes(data)
        return f"/api/customer/guest/evidence-image?name={name}"

    def read(self, name: str) -> tuple[bytes, str] | None:
        """安全读取：`name` 必须是白名单文件名，不接受任何路径分隔符。"""
        import re
        if not re.fullmatch(r"[a-f0-9]{16}\.(jpg|png|webp|gif)", str(name or "")):
            return None
        path = self.root / name
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root)
        except (ValueError, OSError):
            return None
        if not resolved.is_file():
            return None
        return resolved.read_bytes(), resolved.suffix.lstrip(".")
