-- =============================================================================
-- fixtures/seed_kitchen.sql —— 演示与测试用的种子数据
-- =============================================================================
-- 用法：先跑完 migrations/001-014，再执行本文件。
-- 全部 INSERT IGNORE，可重复执行不报错、不产生重复行。
--
-- 覆盖范围（对应 017 条功能里需要主数据的部分）：
--   工位 A/B（#4 占用与派单）、菜品与配方 SOP（#12/#13）、
--   员工+工牌+健康证（#8/#9/#10）、监测点与阈值（#1/#2/#5/#6/#7）、
--   供应商+批次+溯源码（顾客端扫码）。
--
-- 演示阈值与真实值的换算口径见 04_功能规格与联动阈值.md §七；
-- 本文件同时写入真实值与演示值两套（profile='real' / 'demo'），
-- 管理端参数页据此展示「演示阈值 → 真实值」对应关系，避免答辩被质疑阈值造假。
-- =============================================================================

-- ---------------------------------------------------------------- 工位（#4）
INSERT IGNORE INTO work_stations (id, code, name, kind, device_id, state, capacity) VALUES
  (1, 'ST-A', 'A 工位（演示虚拟）', 'stove', 'kitchen-node-01', 'idle', 1),
  (2, 'ST-B', 'B 工位（演示虚拟）', 'stove', 'kitchen-node-01', 'idle', 1);

-- ---------------------------------------------------------------- 餐桌
INSERT IGNORE INTO dining_tables (id, code, seats, area, state) VALUES
  (1, 'T01', 4, '大厅', 'idle'),
  (2, 'T02', 4, '大厅', 'idle'),
  (3, 'T03', 2, '靠窗', 'idle');

-- ---------------------------------------------------------------- 菜品
-- 全品类菜单（24 道 / 7 类）：前端按 category 自动生成分类导航。
-- 前 4 道是原有固定 code，测试与扫码演示都依赖，保持不变；后续新增不带批次/配方，
-- 因此照常售卖（过期门禁只作用于有批次且全部过期的菜）。
-- 命名口径：品类用「热菜/凉菜/素菜/汤羹/主食/甜品/饮品」七值，大小写与前端一致。
-- prep_minutes 按烹饪难度标定（分钟），参考常见家常菜/食堂出餐时长：
--   现成/冲调（米饭、橙汁、酸梅汤）3~5；凉菜 5~8；快炒素菜/蛋汤 8~12；
--   快炒肉菜 13~15；现做主食与水煮/拔丝 15~25；红烧/炖汤/烤制 40~90。
--   它是菜单与交期的「参考值」，故意拉开档次，避免一刀切 15 分钟。
INSERT IGNORE INTO dishes (id, code, name, category, price, prep_minutes, sold_out, enabled, created_at) VALUES
  (1, 'D-QJRS', '青椒肉丝', '热菜', 28.0, 14, 0, 1, '2026-11-12 09:00:00'),
  (2, 'D-GBJD', '宫保鸡丁', '热菜', 32.0, 16, 0, 1, '2026-11-12 09:00:00'),
  (3, 'D-QGD',  '清炒时蔬', '素菜', 18.0, 8, 1, 1, '2026-11-12 09:00:00'),
  (4, 'D-SLTD', '酸辣土豆丝', '素菜', 16.0, 10, 0, 1, '2026-11-12 09:00:00'),
  -- 热菜
  (5, 'D-HSJR', '红烧肉', '热菜', 42.0, 60, 0, 1, '2026-11-12 09:00:00'),
  (6, 'D-SYR',  '水煮鱼', '热菜', 58.0, 25, 0, 1, '2026-11-12 09:00:00'),
  (7, 'D-KSJD', '烤鸭', '热菜', 68.0, 90, 0, 1, '2026-11-12 09:00:00'),
  (8, 'D-MJRS', '木须肉', '热菜', 30.0, 15, 0, 1, '2026-11-12 09:00:00'),
  -- 凉菜
  (9, 'D-LJHD', '凉拌海带丝', '凉菜', 12.0, 8, 0, 1, '2026-11-12 09:00:00'),
  (10,'D-LJHG', '凉拌黄瓜', '凉菜', 10.0, 5, 0, 1, '2026-11-12 09:00:00'),
  (11,'D-ZR',   '酱牛肉', '凉菜', 38.0, 20, 0, 1, '2026-11-12 09:00:00'),
  (12,'D-PD',   '皮蛋豆腐', '凉菜', 14.0, 5, 0, 1, '2026-11-12 09:00:00'),
  -- 素菜
  (13,'D-DSH',  '地三鲜', '素菜', 22.0, 15, 0, 1, '2026-11-12 09:00:00'),
  (14,'D-SC',   '手撕包菜', '素菜', 18.0, 10, 0, 1, '2026-11-12 09:00:00'),
  -- 汤羹
  (15,'D-XLDT', '西红柿蛋汤', '汤羹', 16.0, 10, 0, 1, '2026-11-12 09:00:00'),
  (16,'D-ZCST', '紫菜蛋花汤', '汤羹', 14.0, 8, 0, 1, '2026-11-12 09:00:00'),
  (17,'D-YMJG', '玉米排骨汤', '汤羹', 32.0, 45, 0, 1, '2026-11-12 09:00:00'),
  -- 主食
  (18,'D-MIF',  '米饭', '主食', 3.0, 3, 0, 1, '2026-11-12 09:00:00'),
  (19,'D-JIAOZ','饺子', '主食', 20.0, 15, 0, 1, '2026-11-12 09:00:00'),
  (20,'D-CHAOM','炒面', '主食', 18.0, 12, 0, 1, '2026-11-12 09:00:00'),
  -- 甜品
  (21,'D-HSJG', '红豆沙', '甜品', 12.0, 40, 0, 1, '2026-11-12 09:00:00'),
  (22,'D-BN',   '拔丝香蕉', '甜品', 20.0, 18, 0, 1, '2026-11-12 09:00:00'),
  -- 饮品
  (23,'D-NCZ',  '鲜榨橙汁', '饮品', 15.0, 3, 0, 1, '2026-11-12 09:00:00'),
  (24,'D-YCH',  '酸梅汤', '饮品', 10.0, 5, 0, 1, '2026-11-12 09:00:00');

-- ---------------------------------------------------------------- 人员（#8/#9/#10）
-- qualifications 是 JSON 资质集合，#10 派单前与工序要求比对
INSERT IGNORE INTO staff (id, code, name, phone, qualifications, enabled, created_at) VALUES
  (1, 'E001', '张三', '13800000001', '["stove","cold_dish"]', 1, '2026-11-12 08:00:00'),
  (2, 'E002', '李四', '13800000002', '["stove"]',           1, '2026-11-12 08:00:00'),
  (3, 'E009', '王组长', '13800000009', '["stove","cold_dish","disinfection","lead"]', 1, '2026-11-12 08:00:00');

-- RC522 读到的 NTAG213 UID；换卡保留旧行只置 enabled=0
INSERT IGNORE INTO staff_cards (id, card_uid, staff_id, enabled, bound_at) VALUES
  (1, 'A1B2C3D4', 1, 1, '2026-11-12 08:10:00'),
  (2, 'E5F6A7B8', 2, 1, '2026-11-12 08:11:00'),
  (3, 'C9D0E1F2', 3, 1, '2026-11-12 08:12:00');

-- 健康证：#9 演示拦截用——李四的证已过期，刷卡应被拒
-- 【相对日期】写死 '2027-01-01' 会在 2027-05 答辩时让张三/王组长的证全过期，
-- 当天所有刷卡都被准入拒掉。CURDATE() 由 MySQL 执行时计算，端侧 sqlseed.py
-- 按同一语义求值；两边不会漂移。李四固定「半年前已过期」以保留拦截演示。
INSERT IGNORE INTO health_certificates (id, staff_id, cert_no, valid_from, valid_to, state) VALUES
  (1, 1, 'HC-2026-0001', CURDATE() - INTERVAL 2 YEAR, CURDATE() + INTERVAL 2 YEAR, 'valid'),
  (2, 2, 'HC-2025-0007', CURDATE() - INTERVAL 2 YEAR, CURDATE() - INTERVAL 180 DAY, 'expired'),
  (3, 3, 'HC-2026-0002', CURDATE() - INTERVAL 1 YEAR, CURDATE() + INTERVAL 1 YEAR, 'valid');

-- ---------------------------------------------------------------- 配方与 SOP（#12/#13）
INSERT IGNORE INTO dish_recipes (id, dish_id, version, name, enabled, created_at) VALUES
  (1, 1, 1, '青椒肉丝 v1', 1, '2026-11-12 09:00:00'),
  (2, 2, 1, '宫保鸡丁 v1', 1, '2026-11-12 09:00:00');

-- ±5% 判定基准（#12）：tolerance_pct 默认 5
INSERT IGNORE INTO dish_recipe_items (id, recipe_id, ingredient_name, weight_g, tolerance_pct, sort_no) VALUES
  (1, 1, '猪里脊',  150.0, 5, 1),
  (2, 1, '青椒',    100.0, 5, 2),
  (3, 1, '食用油',   20.0, 5, 3),
  (4, 1, '生抽',      8.0, 5, 4),
  (5, 2, '鸡胸肉',  150.0, 8, 1),
  (6, 2, '花生米',   40.0, 10, 2);

-- SOP（#13）：ccp=1 是关键控制点，未确认不允许出餐；requires_card=1 必须刷卡确认
INSERT IGNORE INTO sop_steps (id, recipe_id, step_no, title, instruction, heat_level, duration_sec, ccp, requires_card) VALUES
  (1, 1, 1, '切配',     '里脊切丝 3mm，青椒去籽切丝',           'none',  180, 1, 1),
  (2, 1, 2, '腌制',     '肉丝加生抽抓匀，静置 5 分钟',           'none',  300, 0, 0),
  (3, 1, 3, '热锅下油', '锅温 180℃，下油 20g',                  'high',   30, 0, 0),
  (4, 1, 4, '滑炒肉丝', '肉丝下锅快速滑散至变色',                'high',   45, 1, 1),
  (5, 1, 5, '下青椒',   '下青椒丝翻炒 60 秒，断生即可',          'high',   60, 0, 0),
  (6, 1, 6, '出锅装盘', '确认中心温度 ≥75℃ 后出锅',             'medium', 20, 1, 1),
  (7, 2, 1, '腌制',     '料酒盐淀粉抓匀，静置 2 分钟',           'none',  120, 0, 0),
  (8, 2, 2, '爆炒',     '大火快炒，中心熟透后出锅',              'high',   90, 1, 1);

-- ---------------------------------------------------------------- 监测点（#1/#2/#5/#6/#7）
-- metric 与 scenarios/kitchen.json 的 12 个遥测 id 一一对应
-- state 初值 normal，互锁状态机见 03_数据库设计.md §3.1
INSERT IGNORE INTO env_points (id, code, device_id, metric, zone, state, latch_key, state_changed_at, updated_at) VALUES
  (1, 'GAS-LEAK',     'kitchen-node-01', 'gas_level',      '灶台区', 'normal', 'ch1_gas_valve', '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (2, 'HOOD-TEMP',    'kitchen-node-01', 'hood_temp',      '排烟管', 'normal', 'ch2_fan',       '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (3, 'STERILIZE',    'kitchen-node-01', 'sterilize_temp', '消毒柜', 'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (4, 'FRIDGE',       'kitchen-node-01', 'fridge_temp',    '冷藏柜', 'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (5, 'FREEZER',      'kitchen-node-01', 'freezer_temp',   '冷冻柜', 'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (6, 'SAMPLE-CAB',   'kitchen-node-01', 'sample_temp',    '留样柜', 'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  (7, 'AMBIENT',      'kitchen-node-01', 't',              '后厨',   'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00'),
  -- #3 明火离人：火焰是主判据，「无人」由视觉侧信号经 Rule.requires 做 AND 叠加。
  -- 单独建点而不复用 flame_a：A 探头朝灶眼（炒菜时常驻高温），B 探头朝工位；
  -- 离人判定要的是「灶上有火 + 工位没人」，两者装的不是同一个观察面。
  (8, 'FLAME-B',      'kitchen-node-01', 'flame_b',        '灶台区', 'normal', NULL,            '2026-11-12 09:00:00', '2026-11-12 09:00:00');

-- ---------------------------------------------------------------- 阈值（对 04 文档逐条落）
-- kind 的语义与 src/kitchen/rules/model.py 严格一一对应，写错在装载期直接报错：
--   range    瞬时值越界；lower_bound / upper_bound 至少给一个
--   tier     档位阈值；必须给 profile，仅当前档位匹配时参与判定
--   duration 同一判定要持续 duration_sec 才算成立（「持续 2 分钟」靠它）
--   window   window_sec 内按 aggregate 聚合后再判（「10 分钟温升」靠它）
--
-- 【边界方向，最容易写错的一处】只给 upper_bound → 高于即违规；
-- 只给 lower_bound → 低于即违规；两个都给 → 落在区间外即违规。
-- 所以所有「超温 / 超浓度」阈值都必须填 upper_bound。填成 lower_bound
-- 会变成「低温告警」，规则照样装得进去但永远不触发——已在 rules 单测里锁死。
--
-- 【level】0 仅展示不报警（#7 温湿度）；1 预警（语音+小程序）；2 联锁（动继电器）；3 最高级
-- 【profile】real 真实值 / demo 演示值 / business 营业档 / prep 备餐档 / closed 收档档

-- #1 燃气：按基线算突变率，不按绝对浓度；泄漏阈值仅作联锁门限
--    真实阈值需现场标定（MQ-2 个体差异大），此处给演示起点
--    ① 超泄漏门限 0.55 → 立即联锁（04 文档 §0.2：安全类联锁不设人工等待延迟）
--    ② 超 0.45 持续 30 秒 → 预警级，只播报不动作
INSERT IGNORE INTO env_thresholds (id, point_id, kind, profile, lower_bound, upper_bound, duration_sec, window_sec, aggregate, level, enabled, updated_at) VALUES
  (1, 1, 'range',    'demo', NULL,  0.55, NULL, NULL, NULL,     2, 1, '2026-11-12 09:00:00'),
  (2, 1, 'duration', 'demo', NULL,  0.45,   30, NULL, NULL,     1, 1, '2026-11-12 09:00:00'),

-- #2 排烟三档（档位阈值 + 10 分钟温升窗口聚合）
  (3, 2, 'tier',   'business', NULL, 280.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
  (4, 2, 'window', 'business', NULL, 100.0, NULL,  600, 'rise',1, 1, '2026-11-12 09:00:00'),
  (5, 2, 'tier',   'prep',     NULL, 250.0, NULL, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
  (6, 2, 'window', 'prep',     NULL,  80.0, NULL,  600, 'rise',2, 1, '2026-11-12 09:00:00'),
  (7, 2, 'tier',   'closed',   NULL, 200.0, NULL, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
  (8, 2, 'window', 'closed',   NULL,  50.0, NULL,  600, 'rise',2, 1, '2026-11-12 09:00:00'),

-- #15 消毒：100~120℃ 达标区；高于 120 持续 600 秒 = 控温失效；高于 150 立即判异常超温
  (9, 3, 'range',    'real', 100.0, 120.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (10, 3, 'duration', 'real',  NULL, 120.0,  600, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (11, 3, 'tier',     'real',  NULL, 150.0, NULL, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),

-- #5 冷链：**分级超限**（A2-05）
--   level 1 = 预警：瞬时越界 + 引擎默认 3 秒去抖 → 只播语音、不开单
--   level 2 = 报警：超上限**持续 120 秒**（kind=duration）→ 声光 + 工单 cold_chain
-- 冷藏 0~8℃、冷冻 -30~-18℃；演示口径 8℃ / 5℃（用温水/常温物就能演）
-- 两级的动作与闩锁差异在 kitchen_rules.json 的 by_level 里配置，不在表里重复。
 (12, 4, 'range',    'real',   0.0,   8.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (13, 4, 'duration', 'real',  NULL,   8.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
 (14, 4, 'range',    'demo',   0.0,   8.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
-- demo 口径的报警级必须单独写：不写，演示时只有预警级生效，
-- 「持续 2 分钟才确认」这条最关键的语义在答辩现场根本演不出来。
 (23, 4, 'duration', 'demo',  NULL,   8.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
 (15, 5, 'range',    'real', -30.0, -18.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (16, 5, 'duration', 'real',  NULL, -18.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
 (17, 5, 'range',    'demo', -30.0,   5.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (24, 5, 'duration', 'demo',  NULL,   5.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),

-- #6 留样柜：真实 0~8℃，演示 25℃；48h 销毁（演示加速 1 分钟）
-- 同样分两级：这句「超温报警 + 语音」里的「报警」指的是 120 秒确认后的那一声，
-- 不是温度一过线就响——留样柜开门取放比冷藏柜还频繁。
 (18, 6, 'range',    'real',   0.0,   8.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (19, 6, 'duration', 'real',  NULL,   8.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),
 (20, 6, 'range',    'demo',   0.0,  25.0, NULL, NULL, NULL, 1, 1, '2026-11-12 09:00:00'),
 (25, 6, 'duration', 'demo',  NULL,  25.0,  120, NULL, NULL, 2, 1, '2026-11-12 09:00:00'),

-- #7 温湿度：辅助展示，不参与报警判定（level=0 表示仅展示）
 (21, 7, 'range',    'demo',  10.0,  35.0, NULL, NULL, NULL, 0, 1, '2026-11-12 09:00:00'),

-- #3 明火离人：火焰有效（>0.5）**且**视觉判定无人，持续 12 秒
--     「无人」不在本表里（不属 12 类遥测契约，由 ai_service 注入），
--     靠 kitchen_rules.json 的 requires 做 AND —— 两个条件缺一不可：
--     只有火焰 = 正常炒菜（每天都这样）；只有视觉无人 = 厨师背对摄像头。
--     duration 取 12 秒（文档 10~15 s）：太短会被「转身取料」触发，
--     太长则油锅起火的黄金处置期（约 30 s）已经过了一半。
 (22, 8, 'duration', NULL, NULL, 0.5, 12, NULL, NULL, 2, 1, '2026-11-12 09:00:00');

-- ---------------------------------------------------------------- 燃气基线（#1）
-- 滑动窗口中位数，NVS 周更；这里给演示起点，真机首次开机前 5 分钟自动重建
INSERT IGNORE INTO device_baselines (id, device_id, metric, baseline, window_sec, source, updated_at) VALUES
  (1, 'kitchen-node-01', 'gas_level', 0.08, 300, 'median', '2026-11-12 09:00:00');

-- ---------------------------------------------------------------- 供应商与溯源
INSERT IGNORE INTO suppliers (id, name, license_no, contact, qualifications, state, created_at) VALUES
  (1, '鲜丰肉联厂',      'SC11110010001', '刘经理 13900000001', '["营业执照","食品生产许可证"]', 'active', '2026-11-12 07:00:00'),
  (2, '绿源蔬菜基地',    'SC11110010002', '陈经理 13900000002', '["营业执照","农产品合格证"]',   'active', '2026-11-12 07:00:00'),
  -- 全品类溯源：水产/粮油饮品分别由专营供应商供货，24 道菜均可扫码
  (3, '滨海水产有限公司','SC11110010003', '周经理 13900000003', '["营业执照","食品生产许可证","水产检疫证"]', 'active', '2026-11-12 07:00:00'),
  (4, '金穗粮油贸易',    'SC11110010004', '孙经理 13900000004', '["营业执照","食品生产许可证","粮油质量证"]', 'active', '2026-11-12 07:00:00'),
  (5, '清源饮品厂',      'SC11110010005', '吴经理 13900000005', '["营业执照","饮料生产许可证"]', 'active', '2026-11-12 07:00:00');

INSERT IGNORE INTO dish_batches (id, batch_no, dish_id, produced_at, expire_at, state) VALUES
-- 【相对时刻】produced_at 锚定「当天 11:30」：写死 '2026-11-12 11:30' 会让
-- #14 应留样清单从 11/13 起恒为空（plan() 只取当日生产的批次）。
-- expire_at = 生产 + 48h。链式 INTERVAL 必须日期单位在前（见 sqlseed.py 模块注释）。
-- 【编号】批次号/溯源码不含日期（B-DEMO-001），否则「编号 20261112、
-- 生产日期今天」的截图会在答辩时被追问。编号只要唯一即可。
  (1, 'B-DEMO-001', 1, CURDATE() + INTERVAL 11 HOUR + INTERVAL 30 MINUTE,
     CURDATE() + INTERVAL 2 DAY + INTERVAL 11 HOUR + INTERVAL 30 MINUTE, 'produced'),
-- 下面三批刻意放在「非当日」：#14 应留样清单只认当日生产批次，
-- 若也锚今天，清单会从 1 条变 4 条，破坏既有验收口径。
-- 三批保质期各不相同，演示溯源页进度条的绿/橙/红三态：
-- B-002 还剩约 7 天（新鲜·绿）、B-003 还剩约 1 天（临界·橙）、B-004 已过期（红）。
  (2, 'B-DEMO-002', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR,
     CURDATE() + INTERVAL 6 DAY + INTERVAL 10 HOUR, 'produced'),
  (3, 'B-DEMO-003', 3, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR,
     CURDATE() + INTERVAL 1 DAY + INTERVAL 9 HOUR, 'produced'),
  (4, 'B-DEMO-004', 4, CURDATE() - INTERVAL 10 DAY + INTERVAL 8 HOUR,
     CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR, 'produced'),
-- 其余 20 道菜的批次：同样锚「昨天生产」以免挤进 #14 当日应留样清单（清单仍只认 B-DEMO-001），
-- expire_at 统一在生产后 6 天（新鲜·绿），保证全品类照常售卖、不触发过期下架门禁。
  (5, 'B-DEMO-005', 5,  CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, 'produced'),
  (6, 'B-DEMO-006', 6,  CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 9 HOUR, 'produced'),
  (7, 'B-DEMO-007', 7,  CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, 'produced'),
  (8, 'B-DEMO-008', 8,  CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 11 HOUR, 'produced'),
  (9, 'B-DEMO-009', 9,  CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 12 HOUR, 'produced'),
  (10,'B-DEMO-010', 10, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, 'produced'),
  (11,'B-DEMO-011', 11, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, 'produced'),
  (12,'B-DEMO-012', 12, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 9 HOUR, 'produced'),
  (13,'B-DEMO-013', 13, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, 'produced'),
  (14,'B-DEMO-014', 14, CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 11 HOUR, 'produced'),
  (15,'B-DEMO-015', 15, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 12 HOUR, 'produced'),
  (16,'B-DEMO-016', 16, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, 'produced'),
  (17,'B-DEMO-017', 17, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, 'produced'),
  (18,'B-DEMO-018', 18, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 9 HOUR, 'produced'),
  (19,'B-DEMO-019', 19, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, 'produced'),
  (20,'B-DEMO-020', 20, CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 11 HOUR, 'produced'),
  (21,'B-DEMO-021', 21, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 12 HOUR, 'produced'),
  (22,'B-DEMO-022', 22, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, 'produced'),
  (23,'B-DEMO-023', 23, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, 'produced'),
  (24,'B-DEMO-024', 24, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR,
     CURDATE() + INTERVAL 5 DAY + INTERVAL 9 HOUR, 'produced');

-- ---------------------------------------------------------------- 原料批次（溯源主体）
-- 溯源的是原料：49 种原料各有到货批次、供应商与检疫报告。
-- ingredient_name 与 dish_recipe_items.ingredient_name 同名，配方即骨架；
-- 未在配方标准化的菜，靠 trace_codes.payload.ingredients 声明同样能追到原料。
-- 日期相对化，生鲜 3-5 天、粮油干货 30 天，避免过期门禁误伤。
INSERT IGNORE INTO ingredient_batches (id, batch_no, ingredient_name, supplier_id, received_at, expire_at, inspection, state) VALUES
  (1, 'IB-001', '乌梅', 2, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 9 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-5337"}', 'received'),
  (2, 'IB-002', '五花肉', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-3339"}', 'received'),
  (3, 'IB-003', '八角', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 11 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-3122"}', 'received'),
  (4, 'IB-004', '冰糖', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 12 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-4864"}', 'received'),
  (5, 'IB-005', '包菜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 13 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-6861"}', 'received'),
  (6, 'IB-006', '土豆', 2, CURDATE() - INTERVAL 2 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 8 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-1903"}', 'received'),
  (7, 'IB-007', '大米', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 9 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-7874"}', 'received'),
  (8, 'IB-008', '嫩豆腐', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 10 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-8227"}', 'received'),
  (9, 'IB-009', '山楂', 2, CURDATE() - INTERVAL 2 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 11 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-8639"}', 'received'),
  (10, 'IB-010', '干辣椒', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 12 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-9689"}', 'received'),
  (11, 'IB-011', '排骨', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-8833"}', 'received'),
  (12, 'IB-012', '时令青菜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 8 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-9086"}', 'received'),
  (13, 'IB-013', '木耳', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 9 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-3103"}', 'received'),
  (14, 'IB-014', '橙子', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 10 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-8417"}', 'received'),
  (15, 'IB-015', '海带', 3, CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 11 HOUR, '{"项目":"孔雀石绿","结果":"合格","检验员":"周质检","报告":"JY-5905"}', 'received'),
  (16, 'IB-016', '牛腱子', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 12 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-4542"}', 'received'),
  (17, 'IB-017', '猪肉馅', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-1614"}', 'received'),
  (18, 'IB-018', '猪里脊', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-3343"}', 'received'),
  (19, 'IB-019', '玉米', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 9 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-5069"}', 'received'),
  (20, 'IB-020', '甜面酱', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 10 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-8265"}', 'received'),
  (21, 'IB-021', '生抽', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 11 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-2092"}', 'received'),
  (22, 'IB-022', '白糖', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 12 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-6644"}', 'received'),
  (23, 'IB-023', '白菜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 13 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-9274"}', 'received'),
  (24, 'IB-024', '白醋', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 8 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-5812"}', 'received'),
  (25, 'IB-025', '皮蛋', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 9 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-3331"}', 'received'),
  (26, 'IB-026', '紫菜', 3, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, '{"项目":"孔雀石绿","结果":"合格","检验员":"周质检","报告":"JY-5630"}', 'received'),
  (27, 'IB-027', '红豆', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 11 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-6756"}', 'received'),
  (28, 'IB-028', '胡萝卜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 12 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-7820"}', 'received'),
  (29, 'IB-029', '花椒', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 13 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-2966"}', 'received'),
  (30, 'IB-030', '花生米', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 8 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-6205"}', 'received'),
  (31, 'IB-031', '茄子', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 9 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-2328"}', 'received'),
  (32, 'IB-032', '草鱼', 3, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 10 HOUR, '{"项目":"孔雀石绿","结果":"合格","检验员":"周质检","报告":"JY-2727"}', 'received'),
  (33, 'IB-033', '葱', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 11 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-3933"}', 'received'),
  (34, 'IB-034', '蒜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 12 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-8205"}', 'received'),
  (35, 'IB-035', '虾皮', 3, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, '{"项目":"孔雀石绿","结果":"合格","检验员":"周质检","报告":"JY-3979"}', 'received'),
  (36, 'IB-036', '西红柿', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 8 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-6912"}', 'received'),
  (37, 'IB-037', '豆瓣酱', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 9 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-8085"}', 'received'),
  (38, 'IB-038', '青椒', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 10 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-2465"}', 'received'),
  (39, 'IB-039', '青菜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 11 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-6416"}', 'received'),
  (40, 'IB-040', '面条', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 12 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-5931"}', 'received'),
  (41, 'IB-041', '面粉', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 13 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-8564"}', 'received'),
  (42, 'IB-042', '食用油', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 8 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-6410"}', 'received'),
  (43, 'IB-043', '香油', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 9 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-1960"}', 'received'),
  (44, 'IB-044', '香蕉', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 10 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-4611"}', 'received'),
  (45, 'IB-045', '香醋', 4, CURDATE() - INTERVAL 2 DAY + INTERVAL 11 HOUR, CURDATE() + INTERVAL 30 DAY + INTERVAL 11 HOUR, '{"项目":"微生物","结果":"合格","检验员":"孙质检","报告":"JY-7769"}', 'received'),
  (46, 'IB-046', '鸡胸肉', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 12 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-1210"}', 'received'),
  (47, 'IB-047', '鸡蛋', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 13 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-6226"}', 'received'),
  (48, 'IB-048', '鸭坯', 1, CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR, CURDATE() + INTERVAL 5 DAY + INTERVAL 8 HOUR, '{"项目":"兽药残留","结果":"合格","检验员":"王组长","报告":"JY-6446"}', 'received'),
  (49, 'IB-049', '黄瓜', 2, CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR, CURDATE() + INTERVAL 3 DAY + INTERVAL 9 HOUR, '{"项目":"农残","结果":"合格","检验员":"陈质检","报告":"JY-3422"}', 'received');

-- 溯源码：顾客端小程序扫码查批次/供应商/检测记录
-- created_at 与批次同日（生产 5 分钟后贴码），同样相对化。
-- 前四码覆盖三种检测/保质状态（新鲜/临期/已过期），是既有验收口径，保持不变；
-- 后二十码让全部 24 道菜都能扫码，检测项目按品类（兽残/农残/水产/粮油/饮品）区分。
INSERT IGNORE INTO trace_codes (id, code, batch_id, supplier_id, payload, created_at) VALUES
  (1, 'TC-DEMO-0001', 1, 1, '{"dish":"青椒肉丝","检测":"合格","检验员":"王组长","报告":"JC-2026-1120","ingredients":[{"name":"猪里脊","weight_g":150},{"name":"青椒","weight_g":100},{"name":"食用油","weight_g":20},{"name":"生抽","weight_g":8}]}',
     CURDATE() + INTERVAL 11 HOUR + INTERVAL 35 MINUTE),
  (2, 'TC-DEMO-0002', 2, 1, '{"dish":"宫保鸡丁","检测":"合格","检验员":"王组长","项目":"瘦肉精","ingredients":[{"name":"鸡胸肉","weight_g":150},{"name":"花生米","weight_g":40},{"name":"食用油","weight_g":20},{"name":"干辣椒","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR + INTERVAL 5 MINUTE),
  (3, 'TC-DEMO-0003', 3, 2, '{"dish":"清炒时蔬","检测":"合格","检验员":"陈质检","项目":"农残","ingredients":[{"name":"时令青菜","weight_g":200},{"name":"蒜","weight_g":10},{"name":"食用油","weight_g":15}]}',
     CURDATE() - INTERVAL 2 DAY + INTERVAL 9 HOUR + INTERVAL 5 MINUTE),
  (4, 'TC-DEMO-0004', 4, 2, '{"dish":"酸辣土豆丝","检测":"合格","检验员":"陈质检","备注":"临期下架","ingredients":[{"name":"土豆","weight_g":200},{"name":"干辣椒","weight_g":8},{"name":"白醋","weight_g":15}]}',
     CURDATE() - INTERVAL 10 DAY + INTERVAL 8 HOUR + INTERVAL 5 MINUTE),
  (5, 'TC-DEMO-0005', 5,  1, '{"检测":"合格","ingredients":[{"name":"五花肉","weight_g":250},{"name":"冰糖","weight_g":30},{"name":"生抽","weight_g":20},{"name":"八角","weight_g":5}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR + INTERVAL 5 MINUTE),
  (6, 'TC-DEMO-0006', 6,  3, '{"检测":"合格","ingredients":[{"name":"草鱼","weight_g":300},{"name":"豆瓣酱","weight_g":40},{"name":"干辣椒","weight_g":20},{"name":"花椒","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR + INTERVAL 5 MINUTE),
  (7, 'TC-DEMO-0007', 7,  1, '{"检测":"合格","ingredients":[{"name":"鸭坯","weight_g":400},{"name":"甜面酱","weight_g":30},{"name":"葱","weight_g":20}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR + INTERVAL 5 MINUTE),
  (8, 'TC-DEMO-0008', 8,  1, '{"检测":"合格","ingredients":[{"name":"猪里脊","weight_g":120},{"name":"鸡蛋","weight_g":100},{"name":"木耳","weight_g":30},{"name":"黄瓜","weight_g":80}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR + INTERVAL 5 MINUTE),
  (9, 'TC-DEMO-0009', 9,  3, '{"检测":"合格","ingredients":[{"name":"海带","weight_g":150},{"name":"蒜","weight_g":15},{"name":"香醋","weight_g":20}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR + INTERVAL 5 MINUTE),
  (10,'TC-DEMO-0010', 10, 2, '{"检测":"合格","ingredients":[{"name":"黄瓜","weight_g":200},{"name":"蒜","weight_g":10},{"name":"香油","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR + INTERVAL 5 MINUTE),
  (11,'TC-DEMO-0011', 11, 1, '{"检测":"合格","ingredients":[{"name":"牛腱子","weight_g":300},{"name":"生抽","weight_g":30},{"name":"八角","weight_g":5}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR + INTERVAL 5 MINUTE),
  (12,'TC-DEMO-0012', 12, 4, '{"检测":"合格","ingredients":[{"name":"皮蛋","weight_g":100},{"name":"嫩豆腐","weight_g":200},{"name":"香油","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR + INTERVAL 5 MINUTE),
  (13,'TC-DEMO-0013', 13, 2, '{"检测":"合格","ingredients":[{"name":"土豆","weight_g":120},{"name":"茄子","weight_g":120},{"name":"青椒","weight_g":80}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR + INTERVAL 5 MINUTE),
  (14,'TC-DEMO-0014', 14, 2, '{"检测":"合格","ingredients":[{"name":"包菜","weight_g":250},{"name":"干辣椒","weight_g":10},{"name":"蒜","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR + INTERVAL 5 MINUTE),
  (15,'TC-DEMO-0015', 15, 2, '{"检测":"合格","ingredients":[{"name":"西红柿","weight_g":150},{"name":"鸡蛋","weight_g":100},{"name":"葱","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR + INTERVAL 5 MINUTE),
  (16,'TC-DEMO-0016', 16, 3, '{"检测":"合格","ingredients":[{"name":"紫菜","weight_g":20},{"name":"鸡蛋","weight_g":100},{"name":"虾皮","weight_g":10}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR + INTERVAL 5 MINUTE),
  (17,'TC-DEMO-0017', 17, 1, '{"检测":"合格","ingredients":[{"name":"排骨","weight_g":250},{"name":"玉米","weight_g":200},{"name":"胡萝卜","weight_g":100}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR + INTERVAL 5 MINUTE),
  (18,'TC-DEMO-0018', 18, 4, '{"检测":"合格","ingredients":[{"name":"大米","weight_g":150}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR + INTERVAL 5 MINUTE),
  (19,'TC-DEMO-0019', 19, 4, '{"检测":"合格","ingredients":[{"name":"面粉","weight_g":200},{"name":"猪肉馅","weight_g":150},{"name":"白菜","weight_g":100}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 10 HOUR + INTERVAL 5 MINUTE),
  (20,'TC-DEMO-0020', 20, 4, '{"检测":"合格","ingredients":[{"name":"面条","weight_g":200},{"name":"鸡蛋","weight_g":80},{"name":"青菜","weight_g":100}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 11 HOUR + INTERVAL 5 MINUTE),
  (21,'TC-DEMO-0021', 21, 4, '{"检测":"合格","ingredients":[{"name":"红豆","weight_g":150},{"name":"冰糖","weight_g":40}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 12 HOUR + INTERVAL 5 MINUTE),
  (22,'TC-DEMO-0022', 22, 2, '{"检测":"合格","ingredients":[{"name":"香蕉","weight_g":200},{"name":"白糖","weight_g":60},{"name":"食用油","weight_g":30}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 13 HOUR + INTERVAL 5 MINUTE),
  (23,'TC-DEMO-0023', 23, 5, '{"检测":"合格","ingredients":[{"name":"橙子","weight_g":250}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 8 HOUR + INTERVAL 5 MINUTE),
  (24,'TC-DEMO-0024', 24, 5, '{"检测":"合格","ingredients":[{"name":"乌梅","weight_g":30},{"name":"山楂","weight_g":20},{"name":"冰糖","weight_g":20}]}',
     CURDATE() - INTERVAL 1 DAY + INTERVAL 9 HOUR + INTERVAL 5 MINUTE);

-- ---------------------------------------------------------------- 演示用人员当日排班
-- 健康证过期的李四（staff_id=2）不排班，#9 拦截效果在排班层也可见
-- 【相对日期】必须按运行当天排班：写死日期第二天就查不到当日排班，
-- `AdmissionController.scan()` 会在「排班」这道门直接拒绝（不弹晨检问卷）。
INSERT IGNORE INTO shifts (id, staff_id, station_id, shift_date, shift_no, state, assignment) VALUES
  (1, 1, 1, CURDATE(), 1, 'scheduled', '{"role":"chef"}'),
  (2, 3, 2, CURDATE(), 1, 'scheduled', '{"role":"lead"}');
