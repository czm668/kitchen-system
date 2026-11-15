-- 007: 操作审计表
-- 背景：登录、报警处理、规则修改、命令下发需要可追溯（谁在什么时候做了什么）。
-- 只追加，不提供删除接口；按保留策略到期清理。
-- IF NOT EXISTS：存量库重复执行迁移时不报错、结构不变（幂等）。
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME NOT NULL,
  actor VARCHAR(64) NOT NULL,
  actor_role VARCHAR(16),
  action VARCHAR(48) NOT NULL,
  target VARCHAR(96),
  result VARCHAR(16) NOT NULL DEFAULT 'ok',
  ip VARCHAR(64),
  detail JSON,
  INDEX idx_audit_ts (ts),
  INDEX idx_audit_action (action),
  INDEX idx_audit_actor (actor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
