#!/bin/sh
# 备份侧车：每天 02:00 做两件事（时区看编排是否注入 TZ：
#   注入了 TZ=Asia/Shanghai → 北京时间 02:00；未注入 → 容器 UTC，约北京 10:00）：
#   1) mysqldump kitchen 库（--single-transaction 一致性快照，不锁表）→ gzip
#   2) tar 打包 api_var 卷（队列/审计等文件态）
# 产物落 /dest（api_backups 卷），14 天滚动清理。
# 镜像用 mysql:8.4（与 mysql 服务同镜像，自带 mysqldump 客户端，零新增下载）。
# 用独立脚本而非 compose 内联 sh -c：compose 会对字符串做 $ 插值，
# 且 % 不是 compose 转义符（%% 会原样传入 sh），内联算术极易出错。
# 2026-11-15 A19：接入 mysqldump（此前只归档 api_var，MySQL 卷不在备份范围）。
set -u

# —— 遥测表滚动清理（2026-11-18 A26）
# 1 帧/秒 × 12 指标 = 12 行/秒 ≈ 100 万行/天；实测 sensor_readings 约 300 B/行
# （含索引），即 **每天约 300 MB**。30 GB 系统盘一个月吃掉 9 GB，而且曲线查询
# 会越来越慢。演示看得上的是最近几天的曲线，保留 7 天足够（≈2 GB 封顶）。
# 分批 DELETE：1 GiB 机型上一次删 100 万行会长时间占住 undo 和 IO，
# 单批 2 万行 + 1 秒间隔，把压力摊到整晚。失败不致命，只记一行日志。
purge_table() {
    tbl=$1; keep=$2; batch=${3:-20000}; rounds=${4:-60}
    i=0; total=0
    while [ "$i" -lt "$rounds" ]; do
        n=$(mysql -h mysql -ukitchen -p"$MYSQL_PASSWORD" -N -B kitchen \
             -e "DELETE FROM ${tbl} WHERE ts < NOW() - INTERVAL ${keep} DAY LIMIT ${batch}; SELECT ROW_COUNT();" \
             2>/dev/null | tail -1)
        case "$n" in
            ''|*[!0-9]*) break ;;
        esac
        total=$((total + n))
        [ "$n" -lt "$batch" ] && break
        i=$((i + 1))
        sleep 1
    done
    echo "$(date -Iseconds) purge ${tbl}: keep ${keep}d, deleted ${total} rows"
}

while true; do
    # 02 点这一小时内只跑一次（用标记文件判定）。
    # 旧写法是 `date +%H%M` 精确等 "0200"：循环 sleep 60 加执行开销会缓慢
    # 漂移，一旦跳过 02:00 那一分钟当天就没有备份，且日志里看不出来。
    if [ "$(date +%H)" = "02" ]; then
        day=$(date +%Y%m%d)
        if [ -f /dest/.last_backup ] \
           && [ "$(cat /dest/.last_backup 2>/dev/null)" = "$day" ]; then
            sleep 60
            continue
        fi
        # —— MySQL 逻辑备份：先落 /tmp 再 gzip，mysqldump 失败不会被管道吞掉
        # --no-tablespaces：MySQL 8 默认会去 dump 表空间信息，而业务账号
        # 没有 PROCESS 权限，客户端会打印 "Access denied ... tablespaces"
        # 并以非零码退出——即使数据已全部导出，脚本也会判为 FAILED 而不落
        # 备份文件。加上这个参数即可彻底绕开（备份内容是表结构与数据，
        # 表空间属于实例级信息，与逻辑备份无关）。
        if mysqldump -h mysql -ukitchen -p"$MYSQL_PASSWORD" \
                --single-transaction --quick --routines --triggers \
                --no-tablespaces kitchen \
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
        # —— 遥测/告警表滚动清理：先备份再删，顺序不能反
        purge_table sensor_readings 7 20000 40
        purge_table alarm_events   30 20000 10
        # 标记今天已完成，02 点内后续轮次不再重复执行
        echo "$day" > /dest/.last_backup 2>/dev/null || true
    fi
    sleep 60
done
