#!/usr/bin/env bash
# 后厨管理系统 - 云主机 swap 初始化（A21）
#
# 为什么需要：阿里云轻量/ECS 的 Ubuntu 镜像默认**不开 swap**。内存一旦
# 触顶就是硬 OOM，内核直接 kill 进程（MySQL 首次初始化、19 份 DDL
# 执行时是最危险的一刻）。加一块 swap 不能变快，但能把"进程被杀"
# 变成"稍微卡一下"，对答辩演示是决定性的差别。
#
# 用法（root）：
#   sudo bash deploy/setup-swap.sh          # 默认 2G
#   sudo bash deploy/setup-swap.sh 1G       # 内存特别紧时可给 1G
#
# 幂等：已存在 /swapfile 时直接退出，不会重复挂载。

set -euo pipefail

SIZE="${1:-2G}"
SWAPFILE=/swapfile

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 执行：sudo bash deploy/setup-swap.sh ${SIZE}" >&2
  exit 1
fi

if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAPFILE"; then
  echo "==> ${SWAPFILE} 已在用，跳过"
  swapon --show
  exit 0
fi

if [ -e "$SWAPFILE" ]; then
  echo "==> ${SWAPFILE} 已存在但未启用，直接启用"
else
  echo "==> 创建 ${SIZE} swap 文件"
  if ! fallocate -l "$SIZE" "$SWAPFILE" 2>/dev/null; then
    # 某些文件系统（如部分 overlay/精简置备盘）不支持 fallocate
    echo "    fallocate 不可用，改用 dd（慢一些）"
    case "$SIZE" in
      *G) COUNT=$(( ${SIZE%G} * 1024 )) ;;
      *M) COUNT=${SIZE%M} ;;
      *)  echo "无法识别的容量：${SIZE}" >&2; exit 1 ;;
    esac
    dd if=/dev/zero of="$SWAPFILE" bs=1M count="$COUNT" status=progress
  fi
  chmod 600 "$SWAPFILE"
fi

if ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAPFILE"; then
  mkswap "$SWAPFILE" >/dev/null
  swapon "$SWAPFILE"
fi

# 开机自动挂载（重复执行不会写重复行）
if ! grep -qsE "^${SWAPFILE}[[:space:]]" /etc/fstab; then
  echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
  echo "==> 已写入 /etc/fstab"
fi

# 云上机器尽量少换出（swap 只作为 OOM 前的缓冲，不当作扩展内存用）
cat >/etc/sysctl.d/99-kitchen-swap.conf <<'EOF'
vm.swappiness=10
vm.vfs_cache_pressure=50
EOF
sysctl -q -p /etc/sysctl.d/99-kitchen-swap.conf 2>/dev/null || true

echo
echo "==================== 完成 ===================="
swapon --show
free -m
echo
echo "现在可以启动 MySQL 编排："
echo "  cd deploy && docker compose -f docker-compose.1g.yml up -d --build"
