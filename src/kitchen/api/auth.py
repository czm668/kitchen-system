# -*- coding: utf-8 -*-
"""管理端鉴权：HMAC 签名令牌 + 角色权限 + 登录限速。

## 为什么不用 JWT

不是「JWT 不好」，而是**在这里没有一样好处，却多一批坑**：

1. 没有第三方要互操作，令牌只在本进程里签、本进程里验；
2. JWT 的 `alg` 字段是攻击面（`alg: none` / HS256 与 RS256 混用），
   自己写 `payload.sig` 就**没有 alg 这个字段**，也就没有这个洞；
3. 少一层 base64url(header) 解析，少一处「解析失败该不该放行」的判断。

令牌形态：`base64url(json_payload).base64url(hmac_sha256(secret, json_payload))`。
签名只覆盖 payload 本身（不含分隔符），验证时**先重算再 `compare_digest`**。

## 为什么不存会话表

教育/毕设场景是单进程、可能被重启、也可能开两个实例做 A/B 演示。服务端会话表
一重启就全掉线，现场表现是「答辩前夜全部人要重新登录」。无状态签名令牌重启不掉。

代价是**不能主动踢人**。所以令牌故意做得短（默认 8 小时），并且 `logout` 只清
浏览器侧。真要踢人，改 `secret` 重启即可——这条写进文档，别让人以为是 bug。

## 角色与「谁能做恢复动作」

| 角色 | 能看 | 能改阈值 | 能下单/派单 | 能复位联锁 |
|---|---|---|---|---|
| viewer | ✅ | ❌ | ❌ | ❌ |
| operator | ✅ | ❌ | ✅ | ❌ |
| lead（组长） | ✅ | ✅ | ✅ | ⚠️ 仅 CH3 的一票 |
| manager | ✅ | ✅ | ✅ | ✅ |

**注意**：角色**不是**联锁动作的最终裁决。`InterlockController.request()` 才是
唯一裁决点（人工确认、触发源全恢复、CH3 双通道复核、断网降级它都管）。这里只回
答「这个人有没有资格发起」。两处判断都留着是**故意**的：少了前者，viewer 能点
「恢复供电」；少了后者，组长一个人就能跳过双通道复核。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ROLES",
    "AuthError",
    "LoginLimiter",
    "Role",
    "Session",
    "TokenSigner",
    "can",
]

#: 角色集合（顺序即权限强弱，前端下拉框按这个顺序排）
ROLES: tuple[str, ...] = ("viewer", "operator", "lead", "manager")

#: 权限键 → 允许的角色。**加权限必须在这里登记**，否则默认拒绝。
_PERMISSIONS: dict[str, frozenset[str]] = {
    "state.read": frozenset(ROLES),
    "order.write": frozenset({"operator", "lead", "manager"}),
    "recipe.write": frozenset({"lead", "manager"}),
    "param.write": frozenset({"lead", "manager"}),
    "ticket.write": frozenset({"lead", "manager"}),
    "channel.trip": frozenset({"lead", "manager"}),
    # 复位（恢复类动作）只有 manager；组长的 CH3 一票走 `channel.review`
    "channel.restore": frozenset({"manager"}),
    "channel.review": frozenset({"lead", "manager"}),
    "staff.write": frozenset({"manager"}),
    # A11 客诉审批：核实证据=组长/经理；补偿终审=仅经理。
    # 单人直通（resolve）也仅经理：允许 manager 一人核实+终审，跳过双人复核；
    # 双人复核是否强制由 guest.service.DUAL_REVIEW 开关控制，默认关闭便于演示。
    "guest.review": frozenset({"lead", "manager"}),
    "guest.approve": frozenset({"manager"}),
    "guest.resolve": frozenset({"manager"}),
    # 低分单当日回访：由店长/经理执行（事件文案即"请店长当天回访"），
    # 组长不给——回访要能对顾客给结论，权限对齐终审档。
    "guest.followup": frozenset({"manager"}),
    # A12 经营账：毛利/净利/成本是店内经营数据，只给店长（manager）。
    # 组长、操作员、观察员一律 403——比阈值配置更敏感，宁可收窄。
    "finance.read": frozenset({"manager"}),
    "finance.write": frozenset({"manager"}),
    # A13 合规凭证：组长要拿检查表去现场核缺口，经理要看归档；
    # 观察员/操作员不给（缺口明细属管理信息）。导出同权限，不单独放宽。
    "compliance.read": frozenset({"lead", "manager"}),
    # A28 多融合 AI：面板/审计只读给全员（含观察员，无隐私数据），
    # 建议请求限组长/经理——防止顾客端或脚本刷在线模型调用产生费用。
    "ai.read": frozenset(ROLES),
    "ai.advise": frozenset({"lead", "manager"}),
}


class AuthError(ValueError):
    """鉴权失败基类。`status` 供 HTTP 层直接映射。"""

    status = 401


class TokenInvalid(AuthError):
    status = 401


class TokenExpired(AuthError):
    status = 401


class PermissionDenied(AuthError):
    status = 403


class LoginThrottled(AuthError):
    status = 429


class Role(str):
    """角色名字符串（用 str 子类而不是 Enum：JSON 输出无需转换）。"""

    __slots__ = ()


def can(role: str, permission: str) -> bool:
    """角色是否具备某项权限。**未登记的权限一律拒绝**（白名单语义）。"""
    allowed = _PERMISSIONS.get(str(permission))
    if allowed is None:
        return False
    return str(role) in allowed


def require(role: str, permission: str) -> None:
    """不具备权限就抛 `PermissionDenied`，消息里点明角色与权限。"""
    if not can(role, permission):
        raise PermissionDenied(f"角色 {role} 无 {permission} 权限")


@dataclass(frozen=True)
class Session:
    """一次通过验证的身份。`subject` 是人名/工号，会进审计。"""

    subject: str
    role: str
    issued_at: float
    expires_at: float
    nonce: str = ""

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()

    def has(self, permission: str) -> bool:
        return can(self.role, permission)

    def to_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "role": self.role,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "permissions": sorted(
                key for key in _PERMISSIONS if self.has(key)
            ),
        }


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class TokenSigner:
    """签发/校验无状态令牌。

    `secret` 为空时构造直接抛错——**不允许空密钥**。空密钥签出来的令牌
    谁都能伪造，而现场表现是「一切正常」，不会有人发现。
    """

    def __init__(self, secret: str | bytes, *, ttl_sec: float = 8 * 3600) -> None:
        if not secret:
            raise ValueError("令牌密钥不能为空（空密钥 = 任何人都能伪造）")
        if ttl_sec <= 0:
            raise ValueError("ttl_sec 必须为正")
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        self.ttl_sec = float(ttl_sec)

    # ------------------------------------------------------------ 签
    def _sign(self, body: bytes) -> str:
        return _b64(hmac.new(self._secret, body, hashlib.sha256).digest())

    def issue(
        self,
        subject: str,
        role: str,
        *,
        now: float | None = None,
        ttl_sec: float | None = None,
        nonce: str = "",
    ) -> str:
        subject = (subject or "").strip()
        if not subject:
            raise ValueError("subject 不能为空（审计要记是谁做的）")
        if role not in ROLES:
            raise ValueError(f"未知角色 {role!r}，允许：{list(ROLES)}")
        issued = float(time.time() if now is None else now)
        span = self.ttl_sec if ttl_sec is None else float(ttl_sec)
        claims = {
            "sub": subject,
            "role": str(role),
            "iat": issued,
            "exp": issued + span,
            "n": nonce or _b64(hashlib.sha256(
                f"{subject}:{issued}".encode("utf-8")
            ).digest()[:9]),
        }
        # `sort_keys` + `separators` 固定下来：同一个 claims 必须签出同一个串，
        # 否则「重发同一个请求两次」在服务端看起来是两个不同令牌。
        body = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{_b64(body)}.{self._sign(body)}"

    # ------------------------------------------------------------ 验
    def verify(self, token: str, *, now: float | None = None) -> Session:
        if not token or not isinstance(token, str):
            raise TokenInvalid("缺少令牌")
        parts = token.split(".")
        if len(parts) != 2:
            raise TokenInvalid("令牌格式错误（应为 payload.sig）")
        raw, sig = parts
        try:
            body = _unb64(raw)
        except Exception as exc:  # noqa: BLE001 - base64 解码异常族太散，统一成鉴权失败
            raise TokenInvalid("令牌 payload 无法解码") from exc
        # ① 先验签再解析：先 json.loads 再验签，等于让攻击者用畸形 JSON 探解析器
        if not hmac.compare_digest(self._sign(body), sig):
            raise TokenInvalid("令牌签名不匹配")
        try:
            claims = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise TokenInvalid("令牌 claims 不是合法 JSON") from exc
        if not isinstance(claims, dict):
            raise TokenInvalid("令牌 claims 必须是对象")
        try:
            session = Session(
                subject=str(claims["sub"]),
                role=str(claims["role"]),
                issued_at=float(claims["iat"]),
                expires_at=float(claims["exp"]),
                nonce=str(claims.get("n") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenInvalid(f"令牌 claims 缺字段：{exc}") from exc
        if session.role not in ROLES:
            raise TokenInvalid(f"令牌角色 {session.role!r} 不在允许集合内")
        moment = time.time() if now is None else float(now)
        if session.expires_at <= moment:
            raise TokenExpired("令牌已过期，请重新登录")
        return session

    # ------------------------------------------------------------ 口令
    def digest_password(self, password: str, salt: str) -> str:
        """口令摘要（PBKDF2-HMAC-SHA256，20 万轮）。

        不用裸 `sha256(password)`：彩虹表一挂就破。用户量小到不值得引 bcrypt，
        PBKDF2 是标准库里现成的、参数可控的折中。
        """
        if not salt:
            raise ValueError("salt 不能为空")
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000
        ).hex()

    def check_password(self, password: str, salt: str, expected: str) -> bool:
        return hmac.compare_digest(self.digest_password(password, salt), expected)


@dataclass
class LoginLimiter:
    """按「来源」累计失败次数，超限锁定。

    **锁定键是来源（IP）不是用户名**：只锁用户名的方案，攻击者换用户名继续撞库
    不受影响；而只锁 IP 会被误伤到同一间办公室的其他人，所以两者都记但只锁 IP。

    `failures` 只保留窗口内的记录：不清理会让长期运行的进程把次数一直攒着，
    三个月后正常用户第一次输错就被锁。
    """

    max_failures: int = 5
    window_sec: float = 300.0
    lock_sec: float = 60.0
    clock: Any = None
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _locked_until: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_failures < 1:
            raise ValueError("max_failures 至少为 1")
        if self.clock is None:
            self.clock = time.time

    def _now(self) -> float:
        return float(self.clock())

    def locked(self, source: str) -> float:
        """还需锁定多少秒（0 表示未锁定）。"""
        until = self._locked_until.get(source, 0.0)
        left = until - self._now()
        if left <= 0:
            self._locked_until.pop(source, None)
            return 0.0
        return left

    def check(self, source: str) -> None:
        left = self.locked(source)
        if left > 0:
            raise LoginThrottled(f"失败次数过多，请 {left:.0f} 秒后再试")

    def fail(self, source: str) -> None:
        moment = self._now()
        cutoff = moment - self.window_sec
        bucket = [ts for ts in self._failures.get(source, []) if ts >= cutoff]
        bucket.append(moment)
        self._failures[source] = bucket
        if len(bucket) >= self.max_failures:
            self._locked_until[source] = moment + self.lock_sec
            # 锁定后清空窗口：否则解锁后第一条失败立刻又被锁，等于无限锁
            self._failures[source] = []

    def succeed(self, source: str) -> None:
        self._failures.pop(source, None)
        self._locked_until.pop(source, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "locked": {key: round(left, 1)
                       for key in list(self._locked_until)
                       if (left := self.locked(key)) > 0},
            "tracked_sources": len(self._failures),
        }
