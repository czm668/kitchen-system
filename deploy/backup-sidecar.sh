#!/bin/sh
# 备份侧车：每天 02:00（容器默认 UTC，约合北京时间 10:00）打包 api_var 卷。
# 说明（答辩口径）：本侧车只做 api_var 卷的 tar 归档 + 14 天滚动清理；
# MySQL 的逻辑备份（mysqldump）尚未接入，需另装客户端与凭据，属未实施项。
# 用独立脚本而非 compose 内联 sh -c：compose 会对字符串做 $ 插值，
# 且 % 不是 compose 转义符（%% 会原样传入 sh），内联算术极易出错。
set -u

while true; do
    hhmm=$(date +%H%M)
    if [ "$hhmm" = "0200" ]; then
        day=$(date +%Y%m%d)
        if tar czf "/dest/api_var_${day}.tar.gz" -C /source var 2>/dev/null; then
            echo "$(date -Iseconds) backup ok: api_var_${day}.tar.gz"
        else
            echo "$(date -Iseconds) backup FAILED" >&2
        fi
        find /dest -name 'api_var_*.tar.gz' -mtime +14 -delete
    fi
    sleep 60
done
