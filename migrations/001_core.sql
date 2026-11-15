-- 001_core.sql —— S1 核心表（docs/interface.md §2 权威副本）
-- 迁移器保证同一版本只执行一次；IF NOT EXISTS 让存量库可安全重跑。

CREATE TABLE IF NOT EXISTS devices (
  id VARCHAR(48) PRIMARY KEY,
  scenario VARCHAR(32) NOT NULL,
  name VARCHAR(64),
  dtype VARCHAR(32),
  meta JSON,
  online TINYINT DEFAULT 0,
  last_seen DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sensor_readings (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id VARCHAR(48) NOT NULL,
  metric VARCHAR(32) NOT NULL,
  value DOUBLE NOT NULL,
  ts DATETIME NOT NULL,
  INDEX idx_dev_metric_ts (device_id, metric, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
