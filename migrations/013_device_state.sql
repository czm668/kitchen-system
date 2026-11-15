-- 设备运行态：命令 acked 成功后回写，界面据此显示「阀是关的、风扇是开的」。
--
-- state     当前运行态（值），如 {"fan": "on", "valve": "closed"}
-- state_seq 每个状态键由哪条命令生效（命令创建时间的 epoch 微秒），
--           用于丢弃乱序到达的旧 ACK：旧命令的序号更小，不允许覆盖新命令造成的状态
ALTER TABLE devices ADD COLUMN state JSON NULL;
ALTER TABLE devices ADD COLUMN state_seq JSON NULL;
