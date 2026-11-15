# ESP32-CAM 视觉节点（A5-01）

> **状态：草图，未编译验证。** 板子未到货，`board_config.h` 的引脚表要按实际
> 买到的板子核对。烧录后先跑验收清单，再谈「视觉上线」。

## 为什么需要这一层

#16（视觉违规处置）与 #3（明火离人）都建立在「服务端能持续拿到帧」上。
拿不到帧时，系统唯一正确的结论是「这条防线瞎了」——不是「工位没人」。
所以固件要保证三件事：**帧不断、断了自己能回来、回来之后能被上层发现**。

## 三条接线硬约束

| 约束 | 为什么 | 做错的后果 |
|---|---|---|
| **独立 5V/2A 供电，绝不接工位插座** | `power_cut` 切的就是工位插座 | 切电后摄像头黑掉 → 拍不到整改画面 → 双通道复核变成凭嘴说；断电电涌还会让 CAM 反复重启，看起来像 WiFi 问题 |
| **USB-TTL 与外部 5V 不同时接** | 两路电源倒灌 | 花屏（轻）／烧稳压（重） |
| **烧录后断开 GPIO0–GND** | GPIO0 拉低 = 下载模式 | 重启后一直停在下载模式，表现为「连不上、串口无输出」 |

路由器 / 中继同样不能接工位插座——切电后虽然摄像头活着，但推不出去。

## 帧率与分辨率

| 项 | 值 | 依据 |
|---|---|---|
| 分辨率 | 640×480（VGA） | 416 训练尺寸下再缩，人与帽子仍可分辨；再大则单帧 >100KB，2.4G 下延迟堆到秒级 |
| 帧率 | 5 fps | `ViolationPolicy` 默认按 5fps 折算（3 帧 ≈ 1.5 秒）。**改帧率必须同步改策略** |
| jpeg_quality | 12 | 单帧约 30~50KB。调到 5 会让单帧破 80KB |
| fb_count | 2（有 PSRAM 时） | 推流与抓拍同时取帧时不会互相顶掉 |

`ViolationPolicy.for_fps(fps)` 就是为这件事准备的：帧率变了，帧数阈值要跟着折算，
否则「连续 3 帧」的实际时长会悄悄变。

## 端点

| 端点 | 用途 |
|---|---|
| `GET /stream` | MJPEG（`multipart/x-mixed-replace`），服务端 `MjpegFrameSource` 消费 |
| `GET /snapshot` | 单帧 JPEG。#16 复核页面的「现在长什么样」用它 |
| `GET /status` | JSON：IP、RSSI、累计帧数、运行时长、是否有 PSRAM |

## 接入服务端

```python
from kitchen.vision import FrameReader, MjpegFrameSource

source = MjpegFrameSource("http://192.168.1.60/stream", device_id="esp32cam-01")
reader = FrameReader(source, stale_after_sec=30)
reader.open()

frame = reader.read()          # None = 没新帧（看门狗会判离线）
pixels = reader.read_bytes(frame)   # 抓拍时才取像素
```

切帧按 JPEG 的 `FFD8`/`FFD9` 标记做，**不解析 multipart boundary**：
boundary 各家固件写法不同，换块板子就会静默失效；JPEG 标记是规范强制的。
纯函数 `extract_jpegs()` 有 6 条单测，真机连不上时也能验证这段逻辑。

## 验收清单（板子到货后按顺序做）

1. [ ] 串口看到 `[wifi] <ip>`，浏览器打开 `http://<ip>/status` 返回 JSON
2. [ ] `http://<ip>/snapshot` 能存下一张清晰的 640×480 图
3. [ ] `http://<ip>/stream` 在 VLC 里能连续播放，**连续 10 分钟不断流**
4. [ ] 拔掉电源 5 秒再上电，30 秒内自动恢复推流（看门狗 + WiFi 重连生效）
5. [ ] 拔掉路由器电源再恢复，摄像头能自己重连（不是必须重启整机）
6. [ ] 服务端 `python tools/cam_check.py --url http://<ip>/stream` 连续收 100 帧，
       帧间隔稳定在 200±60ms，且 `FrameReader.health.clock_fixes == 0`
7. [ ] **把工位插座断电**，确认摄像头仍在推流（验证独立供电这条约束）
8. [ ] 用胶带挡住镜头 30 秒，确认服务端报「视觉被遮挡」而不是「无人」

第 7 条是这份固件存在的核心理由，答辩现场可以直接演示：切电之后画面还在。

## 已知限制

- **没有音频**：整改语音走的是厨房主控的 DFPlayer（A8-04），不在摄像头上。
  CAM 只有一个麦克风输入且音质很差，播报听不清等于没播。
- **没有本地缓存**：断网期间帧直接丢弃。视觉不是安全类数据（安全阈值在
  端侧主控执行，见 A6-01），断网时按「视觉离线」降级就够了，不需要补传。
- **单客户端**：`WiFiServer` 一次只服务一个连接。复核页面与判定主循环同时
  取流会互相打断——需要多客户端时改用 `esp_http_server` 的
  `httpd_resp_send_chunk`，或者在服务端做一路拉流、多路分发。
