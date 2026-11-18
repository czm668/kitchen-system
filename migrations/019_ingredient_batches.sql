-- 019 原料批次：溯源的对象是「这批肉/菜从哪来」，不是「这盘菜哪天做好」。
--
-- 与 dish_batches 的分工：
--   dish_batches（成品出餐批次）→ #14 留样、#15 出库门禁、售卖过期下架；
--   ingredient_batches（原料到货批次）→ 顾客扫码看到的原料来源与检疫。
-- 用 ingredient_name 与 dish_recipe_items.ingredient_name 同名关联，
-- 于是「配方清单」正好成了 菜品 → 原料批次 → 供应商 的骨架。
--
-- 不修改 014 的任何表（新库/存量库都靠本迁移补表），保持追加式升级。
CREATE TABLE IF NOT EXISTS ingredient_batches (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  batch_no VARCHAR(64) NOT NULL,
  ingredient_name VARCHAR(96) NOT NULL,
  supplier_id BIGINT NOT NULL,
  received_at DATETIME,
  expire_at DATETIME,
  inspection JSON,
  state VARCHAR(24) NOT NULL DEFAULT 'received',
  UNIQUE KEY uk_ing_batches_no (batch_no),
  KEY idx_ing_batches_name (ingredient_name, received_at),
  KEY idx_ing_batches_supplier (supplier_id),
  CONSTRAINT fk_ing_batches_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
