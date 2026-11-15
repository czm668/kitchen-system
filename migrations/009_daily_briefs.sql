-- S5 今日简报：结构化存储，供总览页读取与答辩复现。
-- 与 reports 分开：日报是长文，简报是「结论 + 要点 + 昨日对比」的结构化对象。
CREATE TABLE IF NOT EXISTS daily_briefs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  scenario VARCHAR(32) NOT NULL,
  rdate DATE NOT NULL,
  payload JSON NOT NULL,
  headline VARCHAR(128),
  action_required TINYINT NOT NULL DEFAULT 0,
  source VARCHAR(8) NOT NULL,
  model VARCHAR(64),
  prompt_version VARCHAR(64),
  latency_ms INT,
  degraded_reason VARCHAR(32),
  created_at DATETIME NOT NULL,
  UNIQUE KEY uk_brief_scen_date (scenario, rdate)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
