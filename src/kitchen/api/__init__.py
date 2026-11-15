# -*- coding: utf-8 -*-
"""管理端后端（A7）：REST + SSE，零三方依赖。

这个包把六个业务域装成一个**能直接跑的 HTTP 服务**：

```
浏览器（Vue3 SPA）
   │  GET/POST /api/*  +  GET /api/stream（SSE）
   ▼
server.py      标准库 ThreadingHTTPServer：解析请求、认令牌、发 SSE、发静态
   ▼
routes.py      路由表 + 权限校验 + 异常→状态码
   ├── views.py     读接口（16 个 view，纯函数）
   └── actions.py   写接口（20 个动作，全部落审计）
   ▼
state.py       AppState：六域装配 + 后台采样线程 + 聚合快照
   ├── 业务域  process / staff / disinfection（datetime 轴）
   ├── 安全域  rules / guards / interlock（单调秒轴）
   └── 兜底域  queue / upload / health / degrade
```

## 三个不该在这一层做的事

1. **不自己判权限以外的业务规则**。谁能复位联锁、通道能不能恢复，一律问
   `InterlockController`；资质够不够，一律问 `DispatchBoard.matcher`。API 只回答
   「这个请求是谁发的、他有没有资格发起」。
2. **不在 handler 里拼对象**。六个域共享实例的顺序写死在 `AppState.__init__`，
   handler 直接取。让 handler 自己 new 一个 `DispatchBoard`，第三个 handler 就会
   拿到另一份工位状态。
3. **不在读接口里改状态**。`views.py` 里出现任何 `set_` / `close` / `apply`，
   就意味着「刷一次页面可能改变设备状态」，这种 bug 极难复现。

## 快速启动

    # 只起 API（前端用 vite dev）
    python -m kitchen.api.server --port 8848 --var var/api

    # 带上构建好的前端
    python -m kitchen.api.server --port 8848 --static web/dist

默认账号：`admin` / `lead01` / `chef01` / `guest`，口令 `kitchen2026`
（演示用，启动会打 WARNING；生产设 `KITCHEN_ADMIN_PASSWORD` 或 `KITCHEN_ACCOUNTS`）。
"""
from .accounts import DEFAULT_PASSWORD, Account, AccountStore, LoginService
from .actions import ACTIONS
from .audit import AuditEntry, AuditLog
from .auth import (
    ROLES,
    LoginLimiter,
    PermissionDenied,
    Session,
    TokenExpired,
    TokenInvalid,
    TokenSigner,
    can,
)
from .customer import CustomerService
from .routes import NotFound, Route, Router, http_status
from .server import ApiServer, create_server
from .sse import format_event, hello_frame, ping_frame
from .state import METRIC_META, METRIC_ORDER, AppState, AppStateConfig, TelemetryRing

__all__ = [
    "ACTIONS",
    "ApiServer",
    "AppState",
    "AppStateConfig",
    "Account",
    "AccountStore",
    "AuditEntry",
    "AuditLog",
    "CustomerService",
    "DEFAULT_PASSWORD",
    "LoginLimiter",
    "LoginService",
    "METRIC_META",
    "METRIC_ORDER",
    "NotFound",
    "PermissionDenied",
    "ROLES",
    "Route",
    "Router",
    "Session",
    "TelemetryRing",
    "TokenExpired",
    "TokenInvalid",
    "TokenSigner",
    "can",
    "create_server",
    "format_event",
    "hello_frame",
    "http_status",
    "ping_frame",
]
