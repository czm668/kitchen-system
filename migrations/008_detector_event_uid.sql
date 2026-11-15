-- 008_detector_event_uid.sql —— P0-2：检测事件业务幂等键
-- 相对冻结 DDL 只增不改：detector_events 增列 event_uid，并按 (scenario, event_uid) 建唯一索引。
-- event_uid 允许 NULL：旧版推理服务不传该字段时保持原行为（不去重）；新版本由检测侧生成，
-- 重试时保持不变，唯一索引兜底防并发重复入库。
-- 幂等：MySQL 不支持 ADD COLUMN / CREATE INDEX 的 IF NOT EXISTS（那是 MariaDB 语法），
-- 重复执行产生的「列已存在」(1060) 与「索引已存在」(1061) 由迁移器识别为幂等成功，
-- 整库重放不报错、结构不变（见 kernel/db/migrate.py）。

ALTER TABLE detector_events ADD COLUMN event_uid VARCHAR(96) NULL AFTER duration_ms;
CREATE UNIQUE INDEX uq_scen_event_uid ON detector_events (scenario, event_uid);
