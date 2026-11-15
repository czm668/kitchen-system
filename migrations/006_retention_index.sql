-- 006: 数据保留清理所需的 ts/created_at 索引
-- 背景：后台保留任务按时间前缀分批删除；现有组合索引首列不是 ts，
--       直接 WHERE ts < cutoff 会退化为全表扫描。
CREATE INDEX idx_reading_ts ON sensor_readings (ts);
CREATE INDEX idx_detector_ts ON detector_events (ts);
CREATE INDEX idx_command_created ON commands (created_at);
