-- 菜品参考制作时长：按烹饪难度标定的分钟数（菜单/购物车/下单交期共用）。
-- 0 = 未标定，运行时回退到 SOP 步骤时长、再回退到保守默认值 15 分钟。
-- 存量库补列默认 0，不改动既有菜品行为；新菜品由种子写入具体值。
ALTER TABLE dishes ADD COLUMN prep_minutes INT NOT NULL DEFAULT 0;
