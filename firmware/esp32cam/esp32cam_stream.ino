/*
 * ESP32-CAM MJPEG 推流（A5-01）
 *
 * ============================================================
 *  ⚠ 这份草图**没有在硬件上编译与烧录验证过**
 * ============================================================
 *
 * 板子还没到。代码按 Arduino-ESP32 core 2.x 的 `esp_camera` API 写，
 * 语法与调用对齐官方 CameraWebServer 示例，但 `board_config.h` 的引脚
 * 表要按实际买到的板子核对（AI-Thinker / WROVER / ESP32-S3-CAM 引脚不同，
 * 抄错会白屏或反复重启）。烧录后必须跑一遍 `tools/cam_check.py` 验收。
 *
 * ------------------------------------------------------------
 *  三条接线硬约束（写错就白干，不是建议）
 * ------------------------------------------------------------
 *
 * 1. **必须独立 5V/2A 供电，绝不接工位插座。**
 *    #16 的 power_cut 切的就是工位插座。摄像头跟着工位取电，一断电就瞎，
 *    于是「拍不到整改画面 → 复核只能凭人嘴说 → 双通道复核形同虚设」。
 *    更隐蔽的是断电瞬间的电涌会让 CAM 反复重启，串口日志刷屏，看起来
 *    像 WiFi 问题，实际是供电策略错了。
 *
 * 2. **USB-TTL 与 5V 不能同时接。**
 *    烧录时用 USB-TTL 的 5V；烧完拔掉，换独立电源。两路同时供会倒灌，
 *    轻则摄像头花屏，重则烧稳压。
 *
 * 3. **GPIO0 拉低才能进下载模式。**烧完必须断开 GPIO0-GND，否则重启后
 *    一直停在下载模式（表现为「摄像头连不上、串口无输出」）。
 *
 * ------------------------------------------------------------
 *  帧率与分辨率
 * ------------------------------------------------------------
 *
 * 640×480 @ 5fps。判定层的 `ViolationPolicy` 默认按 5fps 折算帧数
 * （3 帧 ≈ 1.5 秒）。把这里改成 15fps 会让同样的策略变成 0.2 秒就报警——
 * 单帧抖动全被当成真违规，现场会把摄像头遮住。改帧率必须同步改策略
 * （`ViolationPolicy.for_fps()`）。
 *
 * jpeg_quality 取 12（0 最好、63 最差）。再高会让单帧超过 60KB，
 * 2.4G 弱信号下推流卡顿、延迟堆到秒级，#3 的 12 秒判据就没意义了。
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <esp_task_wdt.h>

// ---------------------------------------------------------------- 板型
// 按实际板子改这块。AI-Thinker 是最常见的便宜板（约 20 元）
#define CAMERA_MODEL_AI_THINKER
#include "board_config.h"

// ---------------------------------------------------------------- 网络
// 放在 secrets.h 里，别把密码提到 git（本文件在版本库里）
#include "secrets.h"          // WIFI_SSID / WIFI_PASS / CAM_HOSTNAME

// ---------------------------------------------------------------- 参数
#define FRAME_INTERVAL_MS 200     // 5 fps，与判定层策略对齐
#define JPEG_QUALITY 12
#define WDT_TIMEOUT_SEC 20        // 任务看门狗：卡死 20 秒自动重启
#define STATUS_LED 33             // 有些板子没有，注释掉即可

WiFiServer server(80);
unsigned long lastFrameAt = 0;
unsigned long framesServed = 0;
unsigned long wifiDrops = 0;

void setupCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // 先小分辨率试一次：PSRAM 不足的板子直接给 UXGA 会 malloc 失败，
  // 表现为 `cam_hal: EV-EOF-OVF` 刷屏、帧率为 0
  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;   // 640x480
    config.jpeg_quality = JPEG_QUALITY;
    config.fb_count     = 2;               // 双缓冲，推流时不会因为取帧丢包
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;
  } else {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = JPEG_QUALITY;
    config.fb_count     = 1;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[cam] 初始化失败 0x%x，检查引脚表与 5V/2A 供电\n", err);
    delay(3000);
    ESP.restart();          // 不重启的话会一直返回空帧，上层看到的是「没人」
  }

  sensor_t *s = esp_camera_sensor_get();
  s->set_vflip(s, 1);       // 倒装的板子要开；装反了画面是天翻地覆的，
  s->set_hmirror(s, 1);     // 判定层不会报错，只会一直判错
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(CAM_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[wifi] 连接中");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - start > 20000) {
      // WiFi 连不上就不让 loop 空转：重启比「永远离线」好，
      // 至少上层能看到帧源在反复上下线
      wifiDrops++;
      Serial.println("\n[wifi] 20 秒未连上，重启");
      ESP.restart();
    }
  }
  Serial.printf("\n[wifi] %s → http://%s/stream\n",
                WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  pinMode(STATUS_LED, OUTPUT);

  // 任务看门狗：摄像头驱动偶发卡死（DMA 挂了），没有看门狗就整机僵住，
  // 上层只能看到「视觉离线」，而这个离线是永久性的
  esp_task_wdt_init(WDT_TIMEOUT_SEC, true);
  esp_task_wdt_add(NULL);

  setupCamera();
  connectWiFi();
  server.begin();
  Serial.println("[http] 服务已启动：/stream（MJPEG）、/snapshot（单帧）");
}

// ---------------------------------------------------------------- MJPEG 流
void handleStream(WiFiClient &client) {
  client.println("HTTP/1.1 200 OK");
  client.println("Content-Type: multipart/x-mixed-replace; boundary=frame");
  client.println("Cache-Control: no-cache");
  client.println("Connection: close");
  client.println();

  while (client.connected()) {
    esp_task_wdt_reset();                 // 服务客户端期间也要喂狗

    unsigned long now = millis();
    if (now - lastFrameAt < FRAME_INTERVAL_MS) {
      delay(5);
      continue;
    }
    lastFrameAt = now;

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[cam] 取帧失败");
      continue;                            // 不 break：偶发失败不该断流
    }
    client.printf("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                  fb->len);
    client.write(fb->buf, fb->len);
    client.print("\r\n");
    esp_camera_fb_return(fb);
    framesServed++;
  }
  client.stop();
}

// ---------------------------------------------------------------- 单帧
void handleSnapshot(WiFiClient &client) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    client.println("HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n");
    return;
  }
  client.println("HTTP/1.1 200 OK");
  client.println("Content-Type: image/jpeg");
  client.printf("Content-Length: %u\r\n", fb->len);
  client.println("Connection: close");
  client.println();
  client.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

void handleStatus(WiFiClient &client) {
  client.println("HTTP/1.1 200 OK");
  client.println("Content-Type: application/json");
  client.println("Connection: close");
  client.println();
  client.printf("{\"device_id\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,"
                "\"frames\":%lu,\"uptime_ms\":%lu,\"psram\":%s}\r\n",
                CAM_HOSTNAME,
                WiFi.localIP().toString().c_str(),
                WiFi.RSSI(),
                framesServed,
                millis(),
                psramFound() ? "true" : "false");
}

void loop() {
  esp_task_wdt_reset();

  // WiFi 掉线自己重连：不重连的话进程还在、端口还开着，但流是死的，
  // 上层看门狗要等 30 秒才判离线，中间这段时间的违规全漏
  if (WiFi.status() != WL_CONNECTED) {
    wifiDrops++;
    Serial.println("[wifi] 掉线，重连");
    WiFi.disconnect();
    connectWiFi();
    return;
  }

  WiFiClient client = server.available();
  if (!client) {
    delay(2);
    return;
  }

  // 读请求行（GET /stream HTTP/1.1）
  unsigned long deadline = millis() + 2000;
  String request = "";
  while (client.connected() && millis() < deadline && !request.endsWith("\r\n")) {
    if (client.available()) request += (char)client.read();
  }

  if (request.indexOf("GET /stream") >= 0) {
    handleStream(client);
  } else if (request.indexOf("GET /snapshot") >= 0) {
    handleSnapshot(client);
  } else if (request.indexOf("GET /status") >= 0) {
    handleStatus(client);
  } else {
    client.println("HTTP/1.1 404 Not Found\r\nConnection: close\r\n");
    client.println("可用端点：/stream /snapshot /status");
  }
  client.stop();
}
