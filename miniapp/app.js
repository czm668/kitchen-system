// app 入口：初始化桌号 + 保证有顾客令牌。
//
// 服务没起来时**不阻塞页面打开**：各页面自己显示错误卡与「重试」按钮，
// 错误信息里带 BASE_URL 和修复动作（见 utils/api.js）。
var api = require('./utils/api.js');
var store = require('./utils/store.js');
var config = require('./config.js');

App({
  globalData: {
    version: config.VERSION,
    baseUrl: config.BASE_URL,   // onLaunch 会覆盖成实际生效地址（含手机上的自定义值）
    tableId: config.DEFAULT_TABLE,
    sessionReady: null          // 开桌中的 Promise，authed() 会等它落盘再发首请求
  },

  onLaunch: function () {
    this.globalData.baseUrl = api.getBaseUrl();
    // 桌号首次进来落一次盘，之后「我的」页可改，菜单/确认页共用
    this.globalData.tableId = store.getTable();
    if (!api.getToken()) {
      // 暴露给 authed()：页面 onLoad 若早于开桌完成，先等这个 Promise，
      // 避免每次冷启动都在 Console 留两条 401（orders + notifications）。
      this.globalData.sessionReady = api.openSession(store.getTable()).catch(function () {
        // 页面各自处理网络错误，这里静默即可
      });
    }
  },

  onShow: function () {
    this.globalData.tableId = store.getTable();
  }
});
