# -*- coding: utf-8 -*-
"""管理端测试夹具。

`AppState` 会真的落盘（队列 + 审计），所以每个用例必须给它一个 `tmp_path`：
用默认路径会让用例之间互相污染，而且跑完在仓库里留下一堆 `var/`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from kitchen.api import (
    AppState,
    AppStateConfig,
    ApiServer,
    Session,
    TokenSigner,
    create_server,
)

#: 演示默认口令（与 `accounts.DEFAULT_PASSWORD` 保持一致）
PASSWORD = "kitchen2026"


def make_state(tmp_path: Path, **overrides: Any) -> AppState:
    """纯内存量级的 AppState：落盘到 tmp_path，不起后台线程。"""
    config = AppStateConfig(
        var_dir=Path(tmp_path) / "var",
        tick_interval=0.05,
        **overrides,
    )
    return AppState(config=config, online=True)


def make_server(tmp_path: Path, state: AppState | None = None, **kwargs: Any) -> ApiServer:
    """不绑定真实端口的服务（`Router` 可直接 call）。"""
    state = state or make_state(tmp_path)
    kwargs.setdefault("secret", "test-secret")
    return create_server(state, port=0, **kwargs)


def login(server: ApiServer, subject: str = "admin", password: str = PASSWORD) -> Session:
    _token, session = server.login.login(subject, password, source="test")
    return session


def token_of(server: ApiServer, subject: str = "admin", password: str = PASSWORD) -> str:
    token, _session = server.login.login(subject, password, source="test")
    return token


def force_session(subject: str = "admin", role: str = "manager") -> Session:
    """绕过口令造一个会话：只测路由/权限时用它，省掉登录流程。"""
    signer = TokenSigner("unit-secret")
    return signer.verify(signer.issue(subject, role))


def call(server: ApiServer, method: str, path: str, *, session=None,
         **data: Any) -> tuple[int, dict]:
    """直调路由（不起 HTTP）。返回 `(status, payload)`。

    路径里带 `?a=1` 会按 HTTP 的规则拆成 data：`Router.call()` 只认纯路径，
    让测试写 `"/api/audit?action=x"` 更接近真实请求的样子。
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(path)
    if parsed.query:
        merged = {key: values[0] if len(values) == 1 else values
                  for key, values in parse_qs(parsed.query).items()}
        merged.update(data)
        data = merged
    return server.router.call(method, parsed.path, data=data, session=session)


def data_of(server: ApiServer, method: str, path: str, *, session=None,
            **data: Any) -> Any:
    status, payload = call(server, method, path, session=session, **data)
    assert status == 200, f"{method} {path} → {status}: {payload}"
    assert payload.get("ok") is True, payload
    return payload["data"]
