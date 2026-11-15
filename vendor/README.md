# vendor/ — 从素材库取用的文件落点

这里**只放通过 `python -m tools.legacy fetch <bundle>` 从旧工程取来的文件**，
按素材库原始相对路径存放（例如 `vendor/proj/kernel/bus/topics.py`），
这样旧代码的 `from kernel... import ...` 路径不会断。

## 纪律

1. **不要手改本目录里的文件**。它们是只读素材；要改造，先复制到 `src/kitchen/` 下再改。
2. 取用记录写在 `vendor/SOURCES.json`：每个文件的来源相对路径、sha256、取用时间、所属 bundle。
3. 换机器/旧目录更新后跑 `python -m tools.legacy verify` 核对哈希，不一致说明素材变了或本地被改过。
4. `.gitignore` 默认忽略取用来的文件本体，只保留本说明与台账，避免和素材库重复入库。
5. `FROZEN` 分区（旧场景 mingchu / lab_safety、厨师帽数据集、旧页面验收脚本）**禁止取用**，
   取用器会直接拒绝，理由见 `04_系统记录/20261112_代码复用性整理清单.md`。

## 命令

```bash
python -m tools.legacy list                     # 有哪些可复用素材包
python -m tools.legacy fetch platform-core      # 取用平台底座（CORE）
python -m tools.legacy fetch sensor-drivers --dry-run
python -m tools.legacy verify                  # 校验已取用文件
python -m tools.legacy check                   # CI 自检，退出码 0/1
```
