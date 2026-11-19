#!/usr/bin/env bash
# 安装仓库内 git 钩子（core.hooksPath 方式：钩子文件入库、可 review、可升级）。
# 不直接写 .git/hooks —— 那里的文件不入库，换机器就丢。
set -eu

cd "$(git rev-parse --show-toplevel)"

if ! git config core.hooksPath tools/git-hooks; then
    echo "安装失败：无法设置 core.hooksPath" >&2
    exit 1
fi
chmod +x tools/git-hooks/pre-push

echo "已安装 pre-push 钩子：tools/git-hooks/pre-push"
echo "  · 每次 push 前跑 python tools/ci.py（约 3 分钟）"
echo "  · 改动 deploy/ 时额外校验 docker compose config（R3）"
echo "  · 临时跳过：KITCHEN_SKIP_PREPUSH=1 git push"
echo "  · 卸载：git config --unset core.hooksPath"
