// ESP32-CAM 引脚表（A5-01）
//
// 默认 AI-Thinker 板（约 20 元，最常见）。换板子必须改这里。
//
// 常见板型的差异：
//   AI-Thinker   —— 下面这套
//   ESP32-S-CAM  —— 引脚不同，PWDN 有时是 -1
//   ESP32-S3-CAM —— 引脚大改，且 XIAO 版没有 PSRAM
//   WROVER-KIT   —— 有 PSRAM，但 CAM 接口要另外接线
//
// 引脚抄错的典型表现：
//   * 全黑 / 全绿      → D0~D7 有一根接错
//   * 初始化 0x105      → 检测不到 sensor，查 SIOD/SIOC/PWDN
//   * 连续重启          → 5V 供电不足，或 PWDN 接错
//
// 抄完别信注释，按固件 README 的验收清单跑一遍。

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#if defined(CAMERA_MODEL_AI_THINKER)

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1     // AI-Thinker 的 RESET 没引出
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26     // SCCB SDA
#define SIOC_GPIO_NUM     27     // SCCB SCL

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

#else
#error "未选择板型：在 esp32cam_stream.ino 里定义 CAMERA_MODEL_AI_THINKER，或在这里补一套引脚表"
#endif

#endif  // BOARD_CONFIG_H
