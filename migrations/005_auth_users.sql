-- 005_auth_users.sql —— S7 鉴权用户表（docs/interface.md §2 冻结 DDL + 只增列）
-- 在原 DDL（id/username/pwd_hash/role）基础上只增：enabled / token_version / created_at / updated_at
CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(32) UNIQUE NOT NULL,
  pwd_hash VARCHAR(128) NOT NULL,
  role ENUM('admin','viewer') DEFAULT 'admin',
  enabled TINYINT DEFAULT 1,
  token_version INT DEFAULT 0,
  created_at DATETIME NOT NULL,
  updated_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
