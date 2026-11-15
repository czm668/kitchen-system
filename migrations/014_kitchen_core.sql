-- =============================================================================
-- 014_kitchen_core.sql —— 后厨调度与食安合规核心表（正式版）
-- =============================================================================
-- 题目：基于 ESP32 的后厨智能调度与食安合规管理系统的设计与实现
-- 权威文档：03_设计文档/03_数据库设计.md（29 张表清单 / 状态机 / 索引要求）
-- 日期：2026-11-12
--
-- 【与旧工程 014 草案的差异】旧草案只用于冻结表名，本文件是转正版，修正四处：
--   1. 全部表补 COLLATE=utf8mb4_unicode_ci。011 明确要求新表显式写排序规则，
--      只写 CHARSET 会引入 utf8mb4_0900_ai_ci，与旧表跨列比较报 1267
--      Illegal mix of collations。
--   2. offline_batches → offline_records。设计文档 §4 与功能 #17 要求按
--      (device_id, seq) 逐条去重，批次粒度做不到；改逐条表并把 batch_uid 作为分组列，
--      表数仍为 29。
--   3. disinfection_records 补 forbid_outbound（#15 不达标「该批餐具禁止出库」原缺落点）；
--      env_thresholds 补 kind（区分区间/时长/窗口/档位四类判据）。
--   4. 新表之间补物理外键（旧草案只有索引）。对 001-013 的旧表
--      （devices / users / alarm_events / detector_events）仍只建索引不加外键，
--      避免与已冻结的旧迁移耦合。
--
-- 【只新增，不改旧表结构】29 张 CREATE TABLE IF NOT EXISTS 全是新表；
-- 不修改 migrations/001-013 的任何字节（sha256 见 migrations/BASELINE.json）；
-- detector_events 只 ADD COLUMN / ADD INDEX，不改不删旧列。
--
-- 【幂等】MySQL 不支持 ADD COLUMN / CREATE INDEX IF NOT EXISTS，
-- 重复执行时 1050 表 / 1060 列 / 1061 索引 / 1826 外键名 由迁移器识别为幂等成功。
-- 需要撤销时追加 015+ 补偿迁移，不回头改本文件。
--
-- 【建表顺序】按外键依赖拓扑排序，不靠 SET FOREIGN_KEY_CHECKS=0。
-- =============================================================================

-- ---------------------------------------------------------------- ① 订单调度 7
-- 依赖顺序：dining_tables / dishes / work_stations 无新表依赖，先建

CREATE TABLE IF NOT EXISTS dining_tables (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(48) NOT NULL,
  seats INT DEFAULT 4,
  area VARCHAR(48),
  state VARCHAR(24) NOT NULL DEFAULT 'idle',
  UNIQUE KEY uk_tables_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dishes (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(48) NOT NULL,
  name VARCHAR(128) NOT NULL,
  category VARCHAR(48),
  price DOUBLE DEFAULT 0,
  sold_out TINYINT NOT NULL DEFAULT 0,
  enabled TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME,
  UNIQUE KEY uk_dishes_code (code),
  KEY idx_dishes_category (category, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 工位：A/B 两个虚拟工位共用一个采集节点，device_id 指向 devices（001，旧表只建索引）
CREATE TABLE IF NOT EXISTS work_stations (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(48) NOT NULL,
  name VARCHAR(96),
  kind VARCHAR(24) DEFAULT 'stove',
  device_id VARCHAR(48),
  state VARCHAR(24) NOT NULL DEFAULT 'idle',
  capacity INT NOT NULL DEFAULT 1,
  UNIQUE KEY uk_stations_code (code),
  KEY idx_stations_device (device_id),
  KEY idx_stations_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS orders (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_no VARCHAR(48) NOT NULL,
  table_id BIGINT NULL,
  source VARCHAR(16) DEFAULT 'web',
  state VARCHAR(24) NOT NULL DEFAULT 'created',
  version INT NOT NULL DEFAULT 0,
  promised_at DATETIME NULL,
  created_at DATETIME,
  updated_at DATETIME,
  UNIQUE KEY uk_orders_no (order_no),
  -- 派单扫描：按状态过滤 + 交期最近优先（设计文档 §4）
  KEY idx_orders_state_promised (state, promised_at),
  KEY idx_orders_table (table_id),
  CONSTRAINT fk_orders_table FOREIGN KEY (table_id) REFERENCES dining_tables (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS order_items (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_id BIGINT NOT NULL,
  dish_id BIGINT NULL,
  qty INT NOT NULL DEFAULT 1,
  state VARCHAR(24) NOT NULL DEFAULT 'pending',
  urge_count INT NOT NULL DEFAULT 0,
  station_id BIGINT NULL,
  note VARCHAR(255),
  created_at DATETIME,
  KEY idx_items_order (order_id),
  KEY idx_items_dish (dish_id),
  KEY idx_items_station_state (station_id, state),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id),
  CONSTRAINT fk_items_dish FOREIGN KEY (dish_id) REFERENCES dishes (id),
  CONSTRAINT fk_items_station FOREIGN KEY (station_id) REFERENCES work_stations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 订单状态机流水：唯一事实源，orders.state 是它的物化结果（设计文档 §1.5）
CREATE TABLE IF NOT EXISTS order_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_id BIGINT NOT NULL,
  from_state VARCHAR(24) NULL,
  to_state VARCHAR(24) NOT NULL,
  actor VARCHAR(64),
  channel VARCHAR(16),
  client_event_id VARCHAR(96),
  detail JSON,
  ts DATETIME NOT NULL,
  KEY idx_oe_order_ts (order_id, ts),
  KEY idx_oe_channel_ts (channel, ts),
  -- 三端并发改同一单的幂等键：同一渠道同一客户端事件只落一次
  UNIQUE KEY uk_oe_idem (channel, client_event_id),
  CONSTRAINT fk_oe_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ② 人员准入 5
-- staff 无新表依赖；station_occupancy 依赖它，所以本域先于 station_occupancy 建

CREATE TABLE IF NOT EXISTS staff (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(48) NOT NULL,
  name VARCHAR(64) NOT NULL,
  phone VARCHAR(32),
  -- 资质集合，如 ["stove","cold_dish","disinfection"]；#10 资质匹配派单的依据
  qualifications JSON,
  enabled TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME,
  UNIQUE KEY uk_staff_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- RC522 读到的卡号（NTAG213 UID）；换卡保留旧行只置 enabled=0，留痕可查
CREATE TABLE IF NOT EXISTS staff_cards (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  card_uid VARCHAR(96) NOT NULL,
  staff_id BIGINT NOT NULL,
  enabled TINYINT NOT NULL DEFAULT 1,
  bound_at DATETIME,
  UNIQUE KEY uk_cards_uid (card_uid),
  KEY idx_cards_staff (staff_id),
  CONSTRAINT fk_cards_staff FOREIGN KEY (staff_id) REFERENCES staff (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 班次 / 人员-工位绑定；健康证到期时本表对应排班被置 blocked
CREATE TABLE IF NOT EXISTS shifts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  staff_id BIGINT NOT NULL,
  station_id BIGINT NULL,
  shift_date DATE NOT NULL,
  shift_no INT NOT NULL DEFAULT 1,
  state VARCHAR(24) NOT NULL DEFAULT 'scheduled',
  assignment JSON,
  KEY idx_shifts_staff_date (staff_id, shift_date),
  KEY idx_shifts_station_date (station_id, shift_date),
  UNIQUE KEY uk_shifts_slot (staff_id, shift_date, shift_no),
  CONSTRAINT fk_shifts_staff FOREIGN KEY (staff_id) REFERENCES staff (id),
  CONSTRAINT fk_shifts_station FOREIGN KEY (station_id) REFERENCES work_stations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 健康证台账：#9 到期自动拦截排班并拒绝刷卡，故在 valid_to 上建索引供每日扫描
CREATE TABLE IF NOT EXISTS health_certificates (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  staff_id BIGINT NOT NULL,
  cert_no VARCHAR(64),
  valid_from DATE,
  valid_to DATE NOT NULL,
  state VARCHAR(24) NOT NULL DEFAULT 'valid',
  KEY idx_cert_staff_expiry (staff_id, valid_to),
  KEY idx_cert_expiry_state (valid_to, state),
  CONSTRAINT fk_cert_staff FOREIGN KEY (staff_id) REFERENCES staff (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 晨检问卷：#8 当人不合格则当日权限锁定，confirmed_by 指 users（005，旧表只索引）
CREATE TABLE IF NOT EXISTS morning_checks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  staff_id BIGINT NOT NULL,
  check_date DATE NOT NULL,
  questionnaire JSON,
  result VARCHAR(24) NOT NULL DEFAULT 'pending',
  confirmed_by INT NULL,
  channel VARCHAR(16),
  ts DATETIME,
  UNIQUE KEY uk_mc_staff_date (staff_id, check_date),
  KEY idx_mc_date_result (check_date, result),
  CONSTRAINT fk_mc_staff FOREIGN KEY (staff_id) REFERENCES staff (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ③ 过程执行 6
-- 配方 / 批次先建，留样与消毒依赖它们

CREATE TABLE IF NOT EXISTS dish_recipes (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  dish_id BIGINT NOT NULL,
  version INT NOT NULL DEFAULT 1,
  name VARCHAR(96),
  enabled TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME,
  UNIQUE KEY uk_recipes_dish_version (dish_id, version),
  KEY idx_recipes_dish (dish_id),
  CONSTRAINT fk_recipes_dish FOREIGN KEY (dish_id) REFERENCES dishes (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ±5%（#12）判定基准：weight_g 是目标克重，tolerance_pct 默认 5
CREATE TABLE IF NOT EXISTS dish_recipe_items (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  recipe_id BIGINT NOT NULL,
  ingredient_name VARCHAR(96) NOT NULL,
  weight_g DOUBLE NOT NULL,
  tolerance_pct DOUBLE NOT NULL DEFAULT 5,
  sort_no INT NOT NULL DEFAULT 0,
  KEY idx_recipe_items_recipe (recipe_id, sort_no),
  CONSTRAINT fk_recipe_items_recipe FOREIGN KEY (recipe_id) REFERENCES dish_recipes (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- SOP 步骤（#13）：ccp=关键控制点，未确认不允许出餐；
-- requires_card=该步必须刷卡确认（身份进 process_events）
CREATE TABLE IF NOT EXISTS sop_steps (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  recipe_id BIGINT NOT NULL,
  step_no INT NOT NULL,
  title VARCHAR(128) NOT NULL,
  instruction VARCHAR(255),
  heat_level VARCHAR(24),
  duration_sec INT DEFAULT 0,
  ccp TINYINT NOT NULL DEFAULT 0,
  requires_card TINYINT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_sop_recipe_step (recipe_id, step_no),
  KEY idx_sop_recipe_no (recipe_id, step_no),
  CONSTRAINT fk_sop_recipe FOREIGN KEY (recipe_id) REFERENCES dish_recipes (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dish_batches (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  batch_no VARCHAR(64) NOT NULL,
  dish_id BIGINT NOT NULL,
  produced_at DATETIME,
  expire_at DATETIME,
  state VARCHAR(24) NOT NULL DEFAULT 'produced',
  UNIQUE KEY uk_batches_no (batch_no),
  KEY idx_batches_dish (dish_id),
  KEY idx_batches_state (state, produced_at),
  CONSTRAINT fk_batches_dish FOREIGN KEY (dish_id) REFERENCES dishes (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 过程事件流水（#12 称重 / #13 SOP / #14 留样 / #15 消毒共用）
CREATE TABLE IF NOT EXISTS process_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_item_id BIGINT NULL,
  station_id BIGINT NULL,
  staff_id BIGINT NULL,
  kind VARCHAR(32) NOT NULL,
  state VARCHAR(24),
  session_uid VARCHAR(96),
  payload JSON,
  ts DATETIME NOT NULL,
  KEY idx_pe_item_kind_ts (order_item_id, kind, ts),
  KEY idx_pe_session (session_uid),
  KEY idx_pe_station_ts (station_id, ts),
  CONSTRAINT fk_pe_item FOREIGN KEY (order_item_id) REFERENCES order_items (id),
  CONSTRAINT fk_pe_station FOREIGN KEY (station_id) REFERENCES work_stations (id),
  CONSTRAINT fk_pe_staff FOREIGN KEY (staff_id) REFERENCES staff (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 工位占用会话（#4）：上灶刷卡开、出餐确认关；ended_at 为 NULL 即「未释放」。
-- MySQL 没有部分索引，未释放查询走 (station_id, state='open') 复合索引等价实现，
-- 不按设计文档字面写 released_at IS NULL（那是 PG 语法）。
CREATE TABLE IF NOT EXISTS station_occupancy (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  station_id BIGINT NOT NULL,
  order_item_id BIGINT NULL,
  staff_id BIGINT NOT NULL,
  device_id VARCHAR(48),
  state VARCHAR(24) NOT NULL DEFAULT 'open',
  flame_verified TINYINT NOT NULL DEFAULT 0,
  started_at DATETIME NOT NULL,
  ended_at DATETIME NULL,
  KEY idx_occ_station_state (station_id, state, ended_at),
  KEY idx_occ_item (order_item_id),
  KEY idx_occ_staff (staff_id),
  CONSTRAINT fk_occ_station FOREIGN KEY (station_id) REFERENCES work_stations (id),
  CONSTRAINT fk_occ_item FOREIGN KEY (order_item_id) REFERENCES order_items (id),
  CONSTRAINT fk_occ_staff FOREIGN KEY (staff_id) REFERENCES staff (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ④ 批次/扫码 2

CREATE TABLE IF NOT EXISTS suppliers (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(128) NOT NULL,
  license_no VARCHAR(64),
  contact VARCHAR(64),
  qualifications JSON,
  state VARCHAR(24) NOT NULL DEFAULT 'active',
  created_at DATETIME,
  UNIQUE KEY uk_suppliers_license (license_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 溯源码（顾客端小程序扫码用，设计文档标注「必须保留」）
CREATE TABLE IF NOT EXISTS trace_codes (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(96) NOT NULL,
  batch_id BIGINT NULL,
  supplier_id BIGINT NULL,
  payload JSON,
  created_at DATETIME,
  UNIQUE KEY uk_trace_code (code),
  KEY idx_trace_batch (batch_id),
  KEY idx_trace_supplier (supplier_id),
  CONSTRAINT fk_trace_batch FOREIGN KEY (batch_id) REFERENCES dish_batches (id),
  CONSTRAINT fk_trace_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 留样（#14）：≥125g，48 小时销毁（演示加速 1 分钟）；
-- (state, destroy_due_at) 供销毁任务扫描到期项（设计文档 §4）
CREATE TABLE IF NOT EXISTS retention_samples (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  dish_batch_id BIGINT NOT NULL,
  trace_code_id BIGINT NULL,
  weight_g DOUBLE NOT NULL,
  state VARCHAR(24) NOT NULL DEFAULT 'pending',
  sampled_at DATETIME,
  destroy_due_at DATETIME,
  destroyed_at DATETIME,
  KEY idx_rs_batch (dish_batch_id),
  KEY idx_rs_due (state, destroy_due_at),
  CONSTRAINT fk_rs_batch FOREIGN KEY (dish_batch_id) REFERENCES dish_batches (id),
  CONSTRAINT fk_rs_trace FOREIGN KEY (trace_code_id) REFERENCES trace_codes (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ⑤ 环境与冷链 4

-- 监测点 + 互锁状态机（设计文档 §3.1）：
-- state ∈ normal / latched / releasing / manual_reset_required / degraded
CREATE TABLE IF NOT EXISTS env_points (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(64) NOT NULL,
  device_id VARCHAR(48),
  metric VARCHAR(32) NOT NULL,
  zone VARCHAR(64),
  state VARCHAR(24) NOT NULL DEFAULT 'normal',
  latch_key VARCHAR(64),
  state_changed_at DATETIME,
  updated_at DATETIME,
  UNIQUE KEY uk_env_points_code (code),
  KEY idx_env_device (device_id),
  KEY idx_env_latch (latch_key, state),
  KEY idx_env_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 阈值配置（17 条判据的落点）。kind 区分四种判据形态：
--   range    区间判定（#15 100~120℃ 达标区）
--   duration 持续时长（#5/#6 超温 2 分钟）
--   window   窗口聚合（#2 10 分钟温升）
--   tier     档位阈值（#2 营业/备餐/收档，profile 存档位名）
CREATE TABLE IF NOT EXISTS env_thresholds (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  point_id BIGINT NOT NULL,
  kind VARCHAR(24) NOT NULL DEFAULT 'range',
  profile VARCHAR(24),
  lower_bound DOUBLE,
  upper_bound DOUBLE,
  duration_sec INT,
  window_sec INT,
  aggregate VARCHAR(16),
  level TINYINT NOT NULL DEFAULT 1,
  enabled TINYINT NOT NULL DEFAULT 1,
  updated_by INT NULL,
  updated_at DATETIME,
  KEY idx_th_point_kind_profile (point_id, kind, profile),
  CONSTRAINT fk_th_point FOREIGN KEY (point_id) REFERENCES env_points (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 燃气滑动中位数基线（#1）：NVS 周更，突变率判定以它为基准
CREATE TABLE IF NOT EXISTS device_baselines (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id VARCHAR(48) NOT NULL,
  metric VARCHAR(32) NOT NULL,
  baseline DOUBLE NOT NULL,
  window_sec INT,
  source VARCHAR(16) DEFAULT 'median',
  updated_at DATETIME,
  UNIQUE KEY uk_baseline_dev_metric (device_id, metric)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 断网补传（#17）：逐条去重，(device_id, seq) 唯一；batch_uid 只是一次补传会话的分组
CREATE TABLE IF NOT EXISTS offline_records (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id VARCHAR(48) NOT NULL,
  seq BIGINT NOT NULL,
  batch_uid VARCHAR(96),
  metric VARCHAR(32),
  value_num DOUBLE,
  payload JSON,
  ts DATETIME NOT NULL,
  received_at DATETIME,
  UNIQUE KEY uk_offline_dev_seq (device_id, seq),
  KEY idx_offline_batch (batch_uid),
  KEY idx_offline_device_ts (device_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ⑥ AI 视觉 1
-- work_orders 先建（vision_disposals 引用它）

CREATE TABLE IF NOT EXISTS work_orders (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_no VARCHAR(48) NOT NULL,
  type VARCHAR(32) NOT NULL,
  state VARCHAR(24) NOT NULL DEFAULT 'open',
  ref_type VARCHAR(32),
  ref_id BIGINT,
  alarm_id BIGINT NULL,
  assigned_to INT NULL,
  due_at DATETIME NULL,
  rectification JSON,
  created_at DATETIME,
  closed_at DATETIME,
  UNIQUE KEY uk_wo_no (order_no),
  KEY idx_wo_state_due (state, due_at),
  KEY idx_wo_type_state (type, state),
  KEY idx_wo_alarm (alarm_id),
  KEY idx_wo_assigned (assigned_to, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 违规复核流水（#16）：TFT 刷组长牌 + Web 二次确认两条通道都要留痕
CREATE TABLE IF NOT EXISTS vision_disposals (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id BIGINT NOT NULL,
  work_order_id BIGINT NULL,
  channel VARCHAR(16) NOT NULL,
  reviewer VARCHAR(64),
  result VARCHAR(24) NOT NULL DEFAULT 'pending',
  note VARCHAR(255),
  ts DATETIME NOT NULL,
  KEY idx_vd_event (event_id),
  KEY idx_vd_order (work_order_id),
  CONSTRAINT fk_vd_order FOREIGN KEY (work_order_id) REFERENCES work_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- ⑦ 工单与合规 4

-- 消毒曲线会话（#15）：100℃ 以上累计 ≥600s 达标；
-- forbid_outbound=1 表示该批餐具禁止出库，直到复消达标关单
CREATE TABLE IF NOT EXISTS disinfection_records (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  batch_id BIGINT NULL,
  session_uid VARCHAR(96),
  start_ts DATETIME,
  end_ts DATETIME,
  cumulative_sec INT NOT NULL DEFAULT 0,
  peak_temp DOUBLE,
  state VARCHAR(24) NOT NULL DEFAULT 'running',
  forbid_outbound TINYINT NOT NULL DEFAULT 0,
  rework_order_id BIGINT NULL,
  KEY idx_dis_batch (batch_id),
  UNIQUE KEY uk_dis_session (session_uid),
  KEY idx_dis_state (state),
  CONSTRAINT fk_dis_batch FOREIGN KEY (batch_id) REFERENCES dish_batches (id),
  CONSTRAINT fk_dis_rework FOREIGN KEY (rework_order_id) REFERENCES work_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 日管控台账（总局令第 60 号 / 餐饮服务操作规范）
CREATE TABLE IF NOT EXISTS daily_ledgers (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ledger_date DATE NOT NULL,
  type VARCHAR(32) NOT NULL,
  state VARCHAR(24) NOT NULL DEFAULT 'draft',
  generated_at DATETIME,
  exported_at DATETIME,
  file_path VARCHAR(255),
  UNIQUE KEY uk_ledger_date_type (ledger_date, type),
  KEY idx_ledger_date_state (ledger_date, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ledger_items (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ledger_id BIGINT NOT NULL,
  ref_type VARCHAR(32),
  ref_id BIGINT,
  content JSON,
  ts DATETIME,
  KEY idx_li_ledger (ledger_id),
  CONSTRAINT fk_li_ledger FOREIGN KEY (ledger_id) REFERENCES daily_ledgers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- detector_events 扩字段：证据链 + 处置状态（只增列，不改不删旧列）
-- -----------------------------------------------------------------------------
-- 旧客户端不传新字段时 disposal_state 落 'pending'，行为完全不变。
-- 幂等：重复执行时 1060（列已存在）/ 1061（索引已存在）由迁移器吞掉。
-- =============================================================================
ALTER TABLE detector_events ADD COLUMN evidence JSON NULL;
ALTER TABLE detector_events ADD COLUMN zone_id VARCHAR(48) NULL;
ALTER TABLE detector_events ADD COLUMN trace_id VARCHAR(64) NULL;
ALTER TABLE detector_events ADD COLUMN source_device_id VARCHAR(48) NULL;
ALTER TABLE detector_events ADD COLUMN disposal_state VARCHAR(24) NOT NULL DEFAULT 'pending';
ALTER TABLE detector_events ADD COLUMN review_channel VARCHAR(16) NULL;
ALTER TABLE detector_events ADD COLUMN reviewed_by VARCHAR(64) NULL;
ALTER TABLE detector_events ADD COLUMN reviewed_at DATETIME NULL;
ALTER TABLE detector_events ADD COLUMN disposal_by VARCHAR(64) NULL;
ALTER TABLE detector_events ADD COLUMN disposal_at DATETIME NULL;
ALTER TABLE detector_events ADD COLUMN work_order_id BIGINT NULL;
ALTER TABLE detector_events ADD COLUMN disposal_note VARCHAR(255) NULL;
CREATE INDEX idx_de_disposal ON detector_events (scenario, disposal_state, ts);
CREATE INDEX idx_de_camera_ts_disposal ON detector_events (camera_id, ts, disposal_state);
