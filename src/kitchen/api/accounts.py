# -*- coding: utf-8 -*-
"""账号表：四个演示账号 + 环境变量覆盖。

## 默认口令这件事，说清楚比藏起来好

演示/答辩必须能直接登录，所以内置四个账号，口令统一 `kitchen2026`，**启动时
打一条 WARNING 明确告诉你这是默认口令**。把口令写死在代码里又不提示，才是真
隐患：接手的人不知道要改，评审的人也看不出来。

生产部署请用 `KITCHEN_ACCOUNTS` 传一份账号 JSON（口令只给 PBKDF2 摘要），
或者至少设 `KITCHEN_ADMIN_PASSWORD`。**账号表里永远不存明文口令**——
`Account.digest` 是摘要，`check()` 用 `compare_digest` 比对。

## 四个角色的分工（与 `auth.ROLES` 对应）

| 账号 | 角色 | 用来演示什么 |
|---|---|---|
| `admin` | manager | 全部权限；能复位 CH1/CH3 |
| `lead01` | lead | 改阈值、复核晨检、给 CH3 投一票（但**不能**单方面复位） |
| `chef01` | operator | 下单/派单/刷卡，改不了阈值 |
| `guest` | viewer | 只读，用来演示「权限不够时按钮应该置灰」 |
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .auth import ROLES, LoginLimiter, Session, TokenSigner

log = logging.getLogger("kitchen.api.accounts")

__all__ = ["DEFAULT_PASSWORD", "Account", "AccountStore", "LoginService"]

#: 演示默认口令。**生产必须覆盖**（启动时会 WARNING）
DEFAULT_PASSWORD = "kitchen2026"

_DEFAULT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("admin", "manager", "系统管理员"),
    ("lead01", "lead", "后厨组长"),
    ("chef01", "operator", "热菜厨师"),
    ("guest", "viewer", "只读观察员"),
)


@dataclass(frozen=True)
class Account:
    subject: str
    role: str
    display: str = ""
    salt: str = ""
    digest: str = ""
    enabled: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {"subject": self.subject, "role": self.role,
                "display": self.display or self.subject, "enabled": self.enabled}


@dataclass
class AccountStore:
    """账号表。默认口令的来源会被记下来，方便排查「为什么登不上」。"""

    signer: TokenSigner
    accounts: dict[str, Account] = field(default_factory=dict)
    source: str = "default"

    def __post_init__(self) -> None:
        if not self.accounts:
            self._load_defaults()

    # ------------------------------------------------------------ 装载
    def _load_defaults(self) -> None:
        env_table = os.environ.get("KITCHEN_ACCOUNTS", "").strip()
        if env_table:
            try:
                rows = json.loads(env_table)
                self._load_rows(rows)
                self.source = "env"
                return
            except Exception as exc:  # noqa: BLE001 - 配置坏掉就回落，但要说出来
                log.error("KITCHEN_ACCOUNTS 解析失败，回落默认账号：%s", exc)
        password = os.environ.get("KITCHEN_ADMIN_PASSWORD", "") or DEFAULT_PASSWORD
        salt = "kitchen-demo-salt"
        for subject, role, display in _DEFAULT_ROWS:
            secret = password if subject == "admin" else DEFAULT_PASSWORD
            self.accounts[subject] = Account(
                subject=subject, role=role, display=display, salt=salt,
                digest=self.signer.digest_password(secret, salt),
            )
        if password == DEFAULT_PASSWORD:
            log.warning(
                "管理端使用**演示默认口令**（%s 等 4 个账号，口令 %s）。"
                "生产部署请设 KITCHEN_ADMIN_PASSWORD 或 KITCHEN_ACCOUNTS。",
                "admin/lead01/chef01/guest", DEFAULT_PASSWORD,
            )

    def _load_rows(self, rows: Any) -> None:
        """**先全解析到临时表，全部合法才合并**。

        边解析边写会让「第 3 行角色写错」留下前两行已经在生效的半张表，
        而回落默认账号又只补默认的几个 key——结果是权限集合介于两套之间，
        谁也说不清谁能干什么。
        """
        if not isinstance(rows, list):
            raise ValueError("KITCHEN_ACCOUNTS 必须是数组")
        staged: dict[str, Account] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("KITCHEN_ACCOUNTS 每项必须是对象")
            subject = str(row.get("subject") or "").strip()
            role = str(row.get("role") or "").strip()
            if not subject or role not in ROLES:
                raise ValueError(f"账号 {subject!r} 的 subject/role 不合法")
            digest = str(row.get("digest") or "")
            salt = str(row.get("salt") or "")
            password = row.get("password")
            if not digest and password:
                # 允许传明文口令，但**只在这一刻**转成摘要，不保存明文
                salt = salt or "kitchen-env-salt"
                digest = self.signer.digest_password(str(password), salt)
            if not digest or not salt:
                raise ValueError(f"账号 {subject} 缺少 digest/salt 或 password")
            staged[subject] = Account(
                subject=subject, role=role,
                display=str(row.get("display") or subject),
                salt=salt, digest=digest,
                enabled=bool(row.get("enabled", True)),
            )
        if not staged:
            raise ValueError("KITCHEN_ACCOUNTS 里没有任何有效账号")
        self.accounts.update(staged)

    # ------------------------------------------------------------ 查
    def get(self, subject: str) -> Account | None:
        return self.accounts.get(str(subject or "").strip())

    def all(self) -> tuple[Account, ...]:
        return tuple(self.accounts[key] for key in sorted(self.accounts))

    def check(self, subject: str, password: str) -> Account | None:
        """口令正确且账号启用时返回账号，否则 None。**不区分「用户不存在」与
        「口令错」**，避免账号枚举。"""
        account = self.get(subject)
        if account is None or not account.enabled:
            # 即使账号不存在也算一次摘要，让响应时间不泄露账号是否存在
            self.signer.digest_password(password or "x", "kitchen-demo-salt")
            return None
        if self.signer.check_password(password or "", account.salt, account.digest):
            return account
        return None


@dataclass
class LoginService:
    """登录编排：限速 → 校验 → 签令牌 → 审计。"""

    signer: TokenSigner
    accounts: AccountStore
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    def login(
        self,
        subject: str,
        password: str,
        *,
        source: str = "",
        audit=None,
    ) -> tuple[str, Session]:
        self.limiter.check(source)
        account = self.accounts.check(subject, password)
        if account is None:
            self.limiter.fail(source)
            if audit is not None:
                audit.record(actor=str(subject or "?"), role="-", action="auth.login",
                             target=str(subject), result="denied",
                             detail="口令错误或账号不可用", source=source)
            raise PermissionError("账号或口令不正确")
        self.limiter.succeed(source)
        token = self.signer.issue(account.subject, account.role)
        session = self.signer.verify(token)
        if audit is not None:
            audit.record(actor=account.subject, role=account.role,
                         action="auth.login", target=account.subject,
                         result="ok", source=source)
        return token, session
