-- A13 扩展演示模块：食安合规凭证（每日检查表 / 批次追溯一页纸 / 合规日报）
--
-- 定位：17 条主线早已落地，但「合规」只有查询页、交不出凭证。本模块不新增
-- 业务判据，只把 #8 晨检 / #9 健康证 / #14 留样 / #15 消毒 / 工单 / 出库门禁
-- 的既有结论聚合成可存档、可导出、可举证的交付物。
--
-- 运行态实现是「只读聚合 + compliance_journal.jsonl 追加重放」（与
-- config_journal / guest_journal / finance_journal 同构）；本文件描述真接 MySQL
-- 后的目标表结构，供 tools/migrate.py 校验与部署使用。
--
-- 【日报不另造表】复用 004_llm_reports.sql 的 reports（scenario='compliance_daily'）
-- 与 llm_runs（AI 调用审计）。A12 经营日报用 scenario='finance_daily'，两者共用
-- 同一张表和同一套 model / prompt_version / degraded_reason / latency_ms 审计列。
--
-- 【证据边界】检查表按日快照保存，生成后不随源数据变动而回改；重新生成会覆盖
-- 当日快照并留痕。空数据项标 no_data，不进达标率分母，绝不视为通过。

CREATE TABLE IF NOT EXISTS compliance_check_snapshots (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  biz_date DATE NOT NULL,
  overall VARCHAR(16) NOT NULL,
  items_json JSON NOT NULL,
  failed_count INT NOT NULL DEFAULT 0,
  digest CHAR(64) NOT NULL,
  generated_at DATETIME NOT NULL,
  UNIQUE KEY uk_compliance_day (biz_date),
  KEY idx_compliance_overall (overall, biz_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
