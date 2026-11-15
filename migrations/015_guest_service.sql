-- A11 扩展演示模块：顾客画像 / AI 客服 / 客诉与补偿审批
--
-- 定位：不计入申报方案的 17 条主线功能，独立成域，不改动 014 任何既有表。
-- 运行态实现是「内存 + guest_journal.jsonl 追加重放」（与 config_journal 同构）；
-- 本文件描述的是真接 MySQL 后的目标表结构，供 migrate.py 校验与部署使用。
--
-- 资金边界：项目无支付链路（设计文档 09 §一），审批通过只生成补偿凭证，
-- guest_compensation_vouchers.settled 恒为 0，表示「未发生实际资金流」。
-- 不要把这张表当成退款流水表，也不要给它接支付网关。
--
-- AI 调用审计复用 004_llm_reports.sql 的 llm_runs 表（scenario / mode /
-- model / prompt_version / degraded_reason / latency_ms 已经够用），不另造一套。

CREATE TABLE IF NOT EXISTS guest_profiles (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  guest_key VARCHAR(64) NOT NULL,
  tags VARCHAR(255) NOT NULL DEFAULT '',
  openid VARCHAR(64) NULL,
  portrait TEXT,
  portrait_mode VARCHAR(8) NOT NULL DEFAULT 'rule',
  portrait_updated_at DATETIME NULL,
  created_at DATETIME,
  UNIQUE KEY uk_guest_profiles_key (guest_key),
  KEY idx_guest_profiles_openid (openid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guest_complaints (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  complaint_no VARCHAR(32) NOT NULL,
  guest_key VARCHAR(64) NOT NULL,
  customer_ref VARCHAR(32) NULL,
  table_id BIGINT NULL,
  order_id BIGINT NULL,
  item_id BIGINT NULL,
  category VARCHAR(16) NOT NULL DEFAULT '其他',
  text VARCHAR(1000) NOT NULL,
  wants_refund TINYINT NOT NULL DEFAULT 0,
  amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  method VARCHAR(16) NOT NULL DEFAULT 'none',
  state VARCHAR(24) NOT NULL DEFAULT 'submitted',
  version INT NOT NULL DEFAULT 1,
  ai_category VARCHAR(16) NULL,
  ai_mode VARCHAR(8) NULL,
  ai_summary VARCHAR(512) NULL,
  reviewer VARCHAR(64) NOT NULL DEFAULT '',
  approver VARCHAR(64) NOT NULL DEFAULT '',
  reviewed_at DATETIME NULL,
  decided_at DATETIME NULL,
  reject_reason VARCHAR(255) NOT NULL DEFAULT '',
  voucher_no VARCHAR(32) NULL,
  created_at DATETIME,
  updated_at DATETIME,
  UNIQUE KEY uk_guest_complaints_no (complaint_no),
  KEY idx_guest_complaints_guest (guest_key),
  KEY idx_guest_complaints_state (state, updated_at),
  CONSTRAINT fk_guest_complaints_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guest_evidence (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  complaint_id BIGINT NOT NULL,
  kind VARCHAR(16) NOT NULL DEFAULT 'text',
  content VARCHAR(600) NOT NULL,
  url VARCHAR(300) NOT NULL DEFAULT '',
  verified TINYINT NOT NULL DEFAULT 0,
  verified_by VARCHAR(64) NOT NULL DEFAULT '',
  verified_at DATETIME NULL,
  note VARCHAR(255) NOT NULL DEFAULT '',
  KEY idx_guest_evidence_complaint (complaint_id, verified),
  CONSTRAINT fk_guest_evidence_complaint FOREIGN KEY (complaint_id) REFERENCES guest_complaints (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guest_compensation_vouchers (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  voucher_no VARCHAR(32) NOT NULL,
  complaint_id BIGINT NOT NULL,
  guest_key VARCHAR(64) NOT NULL,
  method VARCHAR(16) NOT NULL,
  amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  reviewer VARCHAR(64) NOT NULL DEFAULT '',
  approver VARCHAR(64) NOT NULL,
  decided_at DATETIME,
  settled TINYINT NOT NULL DEFAULT 0,
  note VARCHAR(255) NOT NULL DEFAULT '',
  UNIQUE KEY uk_guest_vouchers_no (voucher_no),
  KEY idx_guest_vouchers_guest (guest_key),
  CONSTRAINT fk_guest_vouchers_complaint FOREIGN KEY (complaint_id) REFERENCES guest_complaints (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
