-- 003_detector_events.sql —— S3：视觉检测事件（推理服务上报的元数据，不含视频帧）
-- 相对 docs/interface.md §2 的冻结 DDL 只增不改：新增 scenario 列用于多场景隔离。

CREATE TABLE IF NOT EXISTS detector_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32) NOT NULL,
  camera_id VARCHAR(48) NOT NULL,
  classes JSON,
  boxes JSON,
  confidence DOUBLE,
  snapshot VARCHAR(255),
  duration_ms INT NOT NULL DEFAULT 0,
  ts DATETIME NOT NULL,
  INDEX idx_cam_ts (camera_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
