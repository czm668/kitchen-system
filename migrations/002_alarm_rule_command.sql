-- 002_alarm_rule_command.sql —— S2：报警 / 规则配置 / 命令状态机
-- 字段与 docs/interface.md §2 保持一致，只增不改。

CREATE TABLE IF NOT EXISTS alarm_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32) NOT NULL,
  device_id VARCHAR(48) NOT NULL,
  source ENUM('rule','detector','llm') DEFAULT 'rule',
  level TINYINT NOT NULL DEFAULT 1,
  atype VARCHAR(32) NOT NULL,
  message VARCHAR(255) NOT NULL,
  snapshot VARCHAR(255),
  handled TINYINT NOT NULL DEFAULT 0,
  ts DATETIME NOT NULL,
  INDEX idx_ts (ts),
  INDEX idx_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rule_configs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32) NOT NULL,
  metric VARCHAR(32) NOT NULL,
  op ENUM('>','<','>=','<=') DEFAULT '>',
  threshold DOUBLE NOT NULL,
  level TINYINT NOT NULL DEFAULT 1,
  enabled TINYINT NOT NULL DEFAULT 1,
  updated_by VARCHAR(32),
  updated_at DATETIME,
  UNIQUE KEY uk_scen_metric (scenario, metric)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS commands (
  cmd_id VARCHAR(36) PRIMARY KEY,
  device_id VARCHAR(48) NOT NULL,
  action VARCHAR(32) NOT NULL,
  payload JSON,
  status ENUM('pending','sent','acked','failed') DEFAULT 'pending',
  retries TINYINT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL,
  acked_at DATETIME,
  result JSON
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
