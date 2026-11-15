# -*- coding: utf-8 -*-
"""A13 食安合规凭证域：每日检查表 / 批次追溯一页纸 / 合规日报 / CSV 导出。

只读聚合，不新增业务状态机；所有判定沿用 #8/#9/#14/#15 等主线域层结论。
"""
from .service import ComplianceService

__all__ = ["ComplianceService"]
