// 顾客端小程序配置。
//
// ## BASE_URL 怎么填（三个场景，只改这一行）
//
// ① 开发者工具模拟器：
//      http://127.0.0.1:8848
//    工具「详情 → 本地设置」勾选「不校验合法域名」，或保持
//    project.config.json 的 setting.urlCheck = false。
// ② 手机真机预览 / 真机调试（当前默认值）：
//      http://192.168.x.x:8848   ← 填电脑的局域网 IP
//    127.0.0.1 在手机上指的是手机自己，必然连不上。跑
//      python tools/miniapp_check.py --serve --lan
//    脚本会打印本机 LAN IP 并把服务绑到 0.0.0.0（默认只绑 127.0.0.1）。
//    另外三件事：手机与电脑同一 WiFi；手机端开发版小程序打开「调试模式」
//    才允许 http 非合法域名；Windows 防火墙放行 TCP 8848 入站。
// ③ 正式版 / 体验版发布（A9-07）：
//      https://<已备案域名>
//    只改这里没用，必须同时到小程序后台「开发设置 → 服务器域名 →
//    request 合法域名」登记。域名备案 / HTTPS / 主体类目见设计文档 09 §七。
//
// ⚠️ AppSecret 绝不写进本文件：小程序前端代码包是可反编译分发的。
//    后端要换 openid / 发订阅消息时放服务端环境变量，不进版本库。
// 当前电脑局域网 IP（2026-11-14 实测为 192.168.1.5）。换 WiFi / 换路由器后
// 这个地址会变，重新用 ipconfig 查一下改这一行；服务要同时用 --host 0.0.0.0 启动。
// 模拟器访问本机 IP 也可以，但前提是服务不能只绑 127.0.0.1。
const BASE_URL = 'http://192.168.1.5:8848';

const VERSION = '2.0.0';

module.exports = {
  BASE_URL: BASE_URL,
  VERSION: VERSION,
  // 订单页轮询间隔（毫秒）。2s：进度条足够跟手，又不会把演示机打爆。
  POLL_INTERVAL_MS: 2000,
  // 通知轮询间隔（毫秒）。
  NOTIFY_INTERVAL_MS: 4000,
  // 请求超时：正常 8s，探活 3s（连不上要快一点失败，别卡住「我的」页）
  REQUEST_TIMEOUT_MS: 8000,
  PROBE_TIMEOUT_MS: 3000,
  // 可选桌台。默认是 3 号桌（与演示脚本、旧版写死的 table_id 一致）。
  TABLE_OPTIONS: [1, 2, 3, 4, 5, 6, 7, 8],
  DEFAULT_TABLE: 3,
  // 催菜冷却秒数：与服务端 URGE_MIN_INTERVAL_SEC 一致，仅用于前端倒计时
  URGE_COOLDOWN_SEC: 30,
  // 本地缓存键集中管理，避免各页面拼字符串拼错
  // 与后端 guest/llm.py::TAGS 白名单一致，前后端都做白名单校验
  GUEST_TAG_OPTIONS: ['少辣', '忌辣', '免葱', '免蒜', '素食', '清淡', '重口'],
  KEYS: {
    TOKEN: 'customer_token',
    CUSTOMER_REF: 'miniapp_customer_ref',
    // 设备级匿名档案 ID：跨开桌不变，用于沉淀顾客习惯/画像；
    // 重新开桌、清缓存都【不清】它，否则画像永远攒不起来。
    GUEST_KEY: 'miniapp_guest_key',
    GUEST_TAGS: 'miniapp_guest_tags',
    CART: 'miniapp_cart',
    TABLE: 'miniapp_table',
    READ_SEQ: 'miniapp_notify_read_seq',
    TRACE_HISTORY: 'miniapp_trace_history',
    DETAIL_ORDER: 'miniapp_detail_order'
  }
};
