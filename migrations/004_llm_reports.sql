-- S5.1 大模型接入：日报表（在冻结 DDL 上增列）+ 调用审计表
-- reports 原列（id/scenario/rdate/content_md/summary/created_at）不改，只补充展示与溯源元数据
CREATE TABLE IF NOT EXISTS reports (
  id INT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32),
  rdate DATE,
  content_md MEDIUMTEXT,
  summary VARCHAR(512),
  created_at DATETIME,
  title VARCHAR(128),
  risk_level VARCHAR(16),
  source VARCHAR(8),
  model VARCHAR(64),
  prompt_version VARCHAR(64),
  latency_ms INT,
  degraded_reason VARCHAR(32),
  UNIQUE KEY uk_scen_date (scenario, rdate)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 每次模型调用（含失败与降级）的审计记录，用于复现提示词版本与排障
CREATE TABLE IF NOT EXISTS llm_runs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32),
  kind VARCHAR(16),
  report_id INT,
  mode VARCHAR(8),
  source VARCHAR(8),
  model VARCHAR(64),
  prompt_version VARCHAR(64),
  status VARCHAR(16),
  attempts INT,
  latency_ms INT,
  prompt_tokens INT,
  completion_tokens INT,
  error_code VARCHAR(48),
  error_msg VARCHAR(255),
  created_at DATETIME,
  INDEX idx_llm_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
