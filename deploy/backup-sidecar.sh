#!/bin/sh
# 备份侧车：每天 02:00（容器默认 UTC，约合北京时间 10:00）做两件事：
#   1) mysqldump kitchen 库（--single-transaction 一致性快照，不锁表）→ gzip
#   2) tar 打包 api_var 卷（队列/审计等文件态）
# 产物落 /dest（api_backups 卷），14 天滚动清理。
# 镜像用 mysql:8.4（与 mysql 服务同镜像，自带 mysqldump 客户端，零新增下载）。
# 用独立脚本而非 compose 内联 sh -c：compose 会对字符串做 $ 插值，
# 且 % 不是 compose 转义符（%% 会原样传入 sh），内联算术极易出错。
# 2026-11-15 A19：接入 mysqldump（此前只归档 api_var，MySQL 卷不在备份范围）。
set -u

while true; do
    hhmm=$(date +%H%M)
    if [ "$hhmm" = "0200" ]; then
        day=$(date +%Y%m%d)
        # —— MySQL 逻辑备份：先落 /tmp 再 gzip，mysqldump 失败不会被管道吞掉
        if mysqldump -h mysql -ukitchen -p"$MYSQL_PASSWORD" \
                --single-transaction --quick --routines --triggers kitchen \
                > "/tmp/mysql_${day}.sql" 2>/dev/null \
           && gzip -c "/tmp/mysql_${day}.sql" > "/dest/mysql_${day}.sql.gz" \
           && rm -f "/tmp/mysql_${day}.sql"; then
            echo "$(date -Iseconds) backup ok: mysql_${day}.sql.gz"
        else
            echo "$(date -Iseconds) mysql dump FAILED" >&2
            rm -f "/tmp/mysql_${day}.sql"
        fi
        # —— api_var 卷归档（A17 起既有逻辑，保持不变）
        if tar czf "/dest/api_var_${day}.tar.gz" -C /source var 2>/dev/null; then
            echo "$(date -Iseconds) backup ok: api_var_${day}.tar.gz"
        else
            echo "$(date -Iseconds) api_var tar FAILED" >&2
        fi
        find /dest -name 'mysql_*.sql.gz' -mtime +14 -delete
        find /dest -name 'api_var_*.tar.gz' -mtime +14 -delete
    fi
    sleep 60
done
