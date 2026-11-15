-- 012: 命令来源（只增不改）
-- 自动处置与人工下发的命令需要在列表与审计里区分；commands 原表没有来源列。
-- 既有行默认 'manual'，不改变任何已有语义。
ALTER TABLE commands ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'manual';

-- 自动下发去重按 (device_id, action, created_at) 过滤，补覆盖索引避免随命令量增长退化为扫表
CREATE INDEX idx_command_dev_action_created ON commands (device_id, action, created_at);
