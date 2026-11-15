-- 010: 聚合读接口预聚合（只增不改）
-- 背景：7 天窗口 + 1 小时桶的聚合在 775 万行时退化为全表扫描 + 临时表，单次约 2.1s。
-- 方案：按 device+metric 的分钟粒度存 avg/min/max/count，聚合接口优先读本表，
--       未汇总区间自动回退原始表；原始表补 (device_id, metric, ts, value) 覆盖索引，
--       让回退路径也能走索引区间扫描而不是回表。
CREATE TABLE IF NOT EXISTS reading_rollups (
  device_id VARCHAR(48) NOT NULL,
  metric VARCHAR(32) NOT NULL,
  bucket_start DATETIME NOT NULL,
  sample_count INT NOT NULL,
  value_avg DOUBLE NOT NULL,
  value_min DOUBLE NOT NULL,
  value_max DOUBLE NOT NULL,
  PRIMARY KEY (device_id, metric, bucket_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 汇总水位：只记录「从最早数据到 covered_until 已完整汇总」，聚合接口据此决定
-- 哪些区间可走 rollup、哪些必须回退原始表。单行表（id 固定为 1）。
CREATE TABLE IF NOT EXISTS reading_rollup_state (
  id TINYINT PRIMARY KEY,
  covered_until DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 覆盖索引：回退路径按 device_id + metric + ts 区间取值，value 进索引后无需回表
CREATE INDEX idx_dev_metric_ts_value ON sensor_readings (device_id, metric, ts, value);
