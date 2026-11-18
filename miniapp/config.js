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
// 这一行由服务启动时自动写回，不用手改：
//   python tools/miniapp_check.py --serve --lan（或双击 start_miniapp_server.bat）
// 会探测本机 WLAN 的局域网 IP，并同步 BASE_URL。换 WiFi / DHCP 重分配后
// 重新起一次服务即可对齐，避免写死的旧 IP 让请求悄悄超时。
// 服务必须同时绑 0.0.0.0（--lan）；模拟器访问本机 IP 也可以，但前提是
// 服务不能只绑 127.0.0.1。自动同步改过这一行后，记得在开发者工具点「编译」。
// 手机上若还留着旧的手动覆盖值（KEYS.BASE_URL_OVERRIDE），utils/api.js 会在
// 网络层失败时自动探测本行地址并清掉旧覆盖值；但代码包本身仍是旧的时，
// 本行也还是旧 IP —— 这一步只能靠点「编译」/重新扫码，代码绕不过去。
// 当前默认值指向已部署的云端（A24，2026-11-18 上线）：
//   http://101.37.82.186
// 选它做默认是因为它**不依赖现场网络**：校园 WiFi、4G 热点、答辩教室都能连，
// 而局域网 IP 换网就失效。本地开发时把这一行改回 http://<电脑局域网IP>:8848
// （`python tools/miniapp_check.py --serve --lan` 会自动写回），或者在「我的 →
// 服务地址」里当场覆盖（存 storage，不必重新编译）。
//
// ⚠️ 高校/企业网络有时会封非常规端口，云端用的是 80，一般不受影响。
const BASE_URL = 'http://101.37.82.186';

// —— 正式版 / 体验版发布地址（A20）——
// 发布前做三件事，缺一不可：
//   1) 把下面改成 https://你的已备案域名（不要带端口、不要带结尾斜杠）
//   2) 把上面 BASE_URL 也改成同一个值
//   3) 到小程序后台「开发设置 → 服务器域名 → request 合法域名」登记同一域名
// 注意：微信只认 ICP 备案域名 + HTTPS，用 IP 或 http 一律被拦。
//
// 当前这台机器是**1 个月试用套餐，开不了备案服务号**（要求包年包月且剩余
// ≥3 个月），所以没有域名、没有 HTTPS。可行的演示方式只有开发模式：
//   · 开发者工具：详情 → 本地设置 → 勾「不校验合法域名、web-view（业务域名）、
//     TLS 版本以及 HTTPS 证书」
//   · 真机预览 / 真机调试：手机端开发版小程序里打开「调试模式」，才允许
//     http 的非合法域名；关闭调试模式会立刻请求失败
//   · 体验版 / 正式版：走不通，必须等备案域名 + HTTPS
// 这段限制答辩时照实说明即可，属于试用机的客观约束，不是实现问题。
//
// 云端这套服务对顾客端通不通，不用靠"打开小程序看看"：账号本机跑
//   python tools/miniapp_cloud_check.py          # 只读 16 项，不动数据
//   python tools/miniapp_cloud_check.py --write  # 再加下单/催菜/客服 8 项
// 从外网按本文件的口径打真接口（GET 令牌放 query、POST 放 body），地址取的
// 就是下面这行 BASE_URL。2026-11-18 实测 24/24 通过，订单轮询 p100≈216ms。
const PROD_BASE_URL = 'http://101.37.82.186';

const VERSION = '2.0.0';

module.exports = {
  BASE_URL: BASE_URL,
  PROD_BASE_URL: PROD_BASE_URL,
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
  // 客诉处理方案：与后端 guest/llm.py::COMPENSATIONS 白名单一致。
  // 演示环境无支付链路，全部生成补偿凭证，不发生实际退款。
  COMPENSATION_OPTIONS: [
    { key: 'redo', label: '重做一份' },
    { key: 'waive', label: '本单免单' },
    { key: 'coupon', label: '补偿优惠券' },
    { key: 'dish_gift', label: '赠送菜品' },
    { key: 'voucher', label: '代金券' },
    { key: 'priority', label: '加急重做' },
    { key: 'apology', label: '致歉说明' },
    { key: 'points', label: '积分补偿' }
  ],
  // 客诉输入框的常用语句：点一下填入 textarea，可再改后提交。
  // 每条都带具体信息（菜名/时间/诉求），后端据此判断阶段与卡点更准。
  COMPLAINT_QUICK_PHRASES: [
    '等了很久还没上菜，请帮我看一下还要多久',
    '菜品上来是凉的，麻烦核实一下出餐时间',
    '菜里有异物，希望核实留样记录',
    '味道不对，怀疑没有按配方制作',
    '服务员态度不好，叫了几次没人回应',
    '想申请重做，请按订单快照核对金额'
  ],
  KEYS: {
    TOKEN: 'customer_token',
    // 手机上手动指定的服务地址（「我的 → 服务地址」）。优先级高于本文件的
    // BASE_URL：电脑换 WiFi / DHCP 重分配后，在手机上改一下就能继续演示，
    // 不用重新编译代码包、不用重新扫码。留空 = 用上面编译进包里的默认值。
    BASE_URL_OVERRIDE: 'miniapp_base_url_override',
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
