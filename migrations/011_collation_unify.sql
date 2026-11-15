-- 011_collation_unify.sql —— 把全库排序规则统一到 utf8mb4_unicode_ci
-- 状态：评估草案，尚未执行；等确认后再 `python -m kernel.db.migrate`。
--
-- 现状（2026-11-08 只读实测 mingchu）：
--   devices、sensor_readings 是库默认 utf8mb4_unicode_ci（早期 create_all 建的）；
--   其余 12 张表由 002+ 迁移以 `... DEFAULT CHARSET=utf8mb4`（未写 COLLATE）建成——
--   显式 CHARACTER SET 时 MySQL 取「字符集默认排序规则」utf8mb4_0900_ai_ci，
--   于是同库出现两组，跨组列间比较报 1267 Illegal mix of collations。
--   本库当前 0 条外键、0 视图/触发器/存储过程；唯一索引撞键预检已通过。
--
-- 为什么写成「条件 ALTER」而不是 14 条裸 ALTER：
--   现有库只需转 12 张小表（合计约 6.7 千行 / 2MB），
--   而全新库按 001 的 DDL 会把 devices/sensor_readings 也建成 0900_ai_ci，
--   011 必须把它们也收敛到 unicode_ci，否则新装库又漂移。
--   条件写法在两种库上都收敛：已是 unicode_ci 的表直接跳过（DO 0），
--   现有库不会白白重建 75 万行 / 119MB 的 sensor_readings。
--
-- 幂等：CONVERT 可重复执行；条件已带「同排序规则则跳过」，中途失败可整体重跑。
--
-- 前置：执行前先跑这条只读检查，必须返回 0 行：
--   SELECT CONSTRAINT_NAME, TABLE_NAME, REFERENCED_TABLE_NAME
--   FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE();
--   如将来出现外键，必须先 DROP 外键、两端都转换后再按同样 COLLATE 重建；
--   `SET FOREIGN_KEY_CHECKS=0` 不能替代删建（它只关校验，不会修正列定义）。
--
-- 新表 DDL 模板（务必显式 COLLATE，不要只写 CHARSET）：
--   CREATE TABLE IF NOT EXISTS xxx (
--     id VARCHAR(48) PRIMARY KEY,
--     scenario VARCHAR(32) NOT NULL,
--     ...
--   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
--
-- 不要回头修改 001~010：migrate.py 对已应用文件做 SHA-256 校验，改动即漂移。

-- alarm_events
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alarm_events');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `alarm_events` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- audit_logs
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_logs');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `audit_logs` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- commands
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'commands');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `commands` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- daily_briefs
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'daily_briefs');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `daily_briefs` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- detector_events
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'detector_events');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `detector_events` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- devices（现有库已是 unicode_ci，会跳过；新装库需要转换）
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'devices');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `devices` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- llm_runs
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_runs');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `llm_runs` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- reading_rollup_state（无字符列，只改表默认）
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'reading_rollup_state');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `reading_rollup_state` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- reading_rollups
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'reading_rollups');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `reading_rollups` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- reports
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'reports');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `reports` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- rule_configs
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rule_configs');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `rule_configs` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- schema_migrations
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'schema_migrations');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `schema_migrations` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- sensor_readings（现有库已是 unicode_ci，会跳过；新装库需要转换）
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sensor_readings');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `sensor_readings` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;

-- users
SET @c = (SELECT COALESCE(TABLE_COLLATION, '') FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users');
SET @s = IF(@c <> 'utf8mb4_unicode_ci',
            'ALTER TABLE `users` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'DO 0');
PREPARE m FROM @s; EXECUTE m; DEALLOCATE PREPARE m;
