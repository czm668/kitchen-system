-- A12 扩展演示模块：经营流水 / 食材成本 / 毛利 / 净利润 / 经营日报
--
-- 定位：不计入申报方案的 17 条主线功能，独立成域。运行态实现是
-- 「内存 + finance_journal.jsonl 追加重放」（与 config_journal / guest_journal 同构）；
-- 本文件描述真接 MySQL 后的目标表结构，供 migrate.py 校验与部署使用。
--
-- 【资金边界】项目无支付链路（设计文档 09 §一）。ledger_entries 记的是
-- 「已上桌子项 × 下单快照价」确认的经营口径营业额，不是实收、不是银行流水；
-- 补偿凭证只冲减账面，不发生资金流。不得把本表当支付流水接网关。
--
-- 【日报不另造表】复用 004_llm_reports.sql 的 reports（scenario='finance_daily'）
-- 与 llm_runs（AI 调用审计），列名已含 model / prompt_version / degraded_reason /
-- latency_ms，无需新增。这是「审查报告 §7 DeepSeek 日报缺口」的落地载体。
--
-- 【账面冻结】成本率或标准成本变更后只影响新确认的账目，已落账行不重算；
-- 重放 journal 同样不重算，符合会计的历史不可变口径。

CREATE TABLE IF NOT EXISTS ledger_entries (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  entry_no VARCHAR(32) NOT NULL,
  biz_date DATE NOT NULL,
  occurred_at DATETIME NOT NULL,
  kind VARCHAR(24) NOT NULL,
  category VARCHAR(24) NOT NULL,
  amount_cents BIGINT NOT NULL,
  order_no VARCHAR(32) NOT NULL DEFAULT '',
  dish_id BIGINT NULL,
  dish_name VARCHAR(128) NOT NULL DEFAULT '',
  qty INT NOT NULL DEFAULT 0,
  cost_source VARCHAR(16) NOT NULL DEFAULT 'n/a',
  source_type VARCHAR(16) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  note VARCHAR(255) NOT NULL DEFAULT '',
  created_at DATETIME,
  UNIQUE KEY uk_ledger_dedup (source_type, source_id, kind, category),
  KEY idx_ledger_date (biz_date, kind),
  KEY idx_ledger_dish (dish_id, biz_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dish_costs (
  dish_id BIGINT NOT NULL PRIMARY KEY,
  cost DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  source VARCHAR(16) NOT NULL DEFAULT 'manual',
  updated_by VARCHAR(64) NOT NULL DEFAULT '',
  updated_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS daily_overheads (
  rdate DATE NOT NULL PRIMARY KEY,
  labor DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  rent DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  utilities DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  other DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  updated_by VARCHAR(64) NOT NULL DEFAULT '',
  updated_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
