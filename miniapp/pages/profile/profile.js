// 我的：桌台/会话状态、服务连通性探测、重新开桌、本机缓存管理、演示说明。
// 「重新开桌」会换掉顾客令牌：旧令牌下的订单归属簿在新的会话里看不到，
// 这是服务端的设计（顾客通道按令牌隔离），所以弹窗里必须说清楚。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    tokenShort: '未开桌',
    customerRef: '未开桌',
    tableLabels: [],
    tableIndex: 0,
    version: config.VERSION,
    baseUrl: config.BASE_URL,
    baseUrlOverridden: false,
    probeState: 'idle',       // idle | probing | ok | fail
    probeText: '尚未检测',
    unread: 0
  },

  onLoad: function () {
    var that = this;
    // 地址自愈 / 手动改地址后立刻同步这页的显示，不用等用户切走再切回
    if (this._offBaseUrl) { this._offBaseUrl(); }
    this._offBaseUrl = api.onBaseUrlChanged(function (url) {
      that.setData({ baseUrl: url, baseUrlOverridden: api.hasBaseUrlOverride() });
    });
    var labels = config.TABLE_OPTIONS.map(function (n) { return n + ' 号桌'; });
    this.setData({
      tableLabels: labels,
      tableIndex: Math.max(0, config.TABLE_OPTIONS.indexOf(store.getTable()))
    });
    this.refresh();
    this.probe();
  },

  onUnload: function () {
    if (this._offBaseUrl) { this._offBaseUrl(); this._offBaseUrl = null; }
  },

  onShow: function () { this.refresh(); },

  refresh: function () {
    var table = store.getTable();
    this.setData({
      tokenShort: fmt.tokenShort(api.getToken()),
      customerRef: api.getCustomerRef() || '未开桌',
      tableIndex: Math.max(0, config.TABLE_OPTIONS.indexOf(table)),
      baseUrl: api.getBaseUrl(),
      baseUrlOverridden: api.hasBaseUrlOverride()
    });
    var that = this;
    api.authed('GET', '/api/customer/notifications', { since: store.getReadSeq() })
      .then(function (data) {
        that.setData({ unread: (data.notifications || []).length });
      }).catch(function () { /* 探活/未读失败不阻塞页面 */ });
  },

  onTableChange: function (e) {
    var idx = Number(e.detail.value) || 0;
    var table = config.TABLE_OPTIONS[idx];
    store.setTable(table);
    var app = getApp();
    if (app && app.globalData) { app.globalData.tableId = table; }
    this.setData({ tableIndex: idx });
    wx.showToast({ title: '已切换到 ' + table + ' 号桌', icon: 'none' });
  },

  probe: function () {
    var that = this;
    this.setData({ probeState: 'probing', probeText: '正在连接…' });
    return api.probe().then(function (data) {
      that.setData({
        probeState: 'ok',
        probeText: '连接正常 · ' + (data.total || 0) + ' 道菜在售'
      });
    }).catch(function (err) {
      that.setData({
        probeState: 'fail',
        // status=0 是网络层失败：把实际请求的地址亮出来，一眼能看出
        // 手机是不是还在打向换 WiFi 前的旧 IP（旧代码包最常见的症状）。
        probeText: err.status === 0
          ? '连不上 ' + (err.baseUrl || api.getBaseUrl()) + ' · 可在下方改服务地址'
          : (err.message || '连接失败')
      });
    });
  },

  reopen: function () {
    var that = this;
    wx.showModal({
      title: '重新开桌',
      content: '会换一个顾客令牌：当前会话下的订单将不再显示（后厨数据不受影响）。确定继续？',
      confirmText: '重新开桌',
      success: function (res) {
        if (!res.confirm) { return; }
        store.resetSession();
        store.clearCart();
        api.reopenSession().then(function () {
          that.refresh();
          wx.showToast({ title: '已重新开桌', icon: 'success' });
        }).catch(function (err) {
          wx.showModal({ title: '开桌失败', content: err.message || '请检查服务', showCancel: false });
        });
      }
    });
  },

  clearCache: function () {
    var that = this;
    wx.showModal({
      title: '清理本机缓存',
      content: '将清空购物车、溯源查询记录与消息已读状态；桌号与会话保留。',
      success: function (res) {
        if (!res.confirm) { return; }
        store.clearCart();
        store.clearTraceHistory();
        store.setReadSeq(0);
        store.setDetailOrder(null);
        wx.showToast({ title: '已清理', icon: 'success' });
        that.refresh();
      }
    });
  },

  copyBaseUrl: function () {
    wx.setClipboardData({
      data: api.getBaseUrl(),
      success: function () { wx.showToast({ title: '服务地址已复制', icon: 'none' }); }
    });
  },

  // 换 WiFi / 电脑 IP 变了时，直接在手机上改地址。覆盖值存 storage，
  // 所有请求（含溯源二维码）都走 api.getBaseUrl()，立即生效、无需重编译。
  editBaseUrl: function () {
    var that = this;
    wx.showModal({
      title: '修改服务地址',
      content: api.getBaseUrl(),
      editable: true,
      placeholderText: 'http://192.168.x.x:8848',
      confirmText: '保存并测试',
      success: function (res) {
        if (!res.confirm) { return; }
        var url = api.normalizeBaseUrl(res.content);
        if (!url) {
          wx.showModal({
            title: '地址格式不对',
            content: '请填 http://电脑局域网IP:8848，不要带路径或结尾斜杠。',
            showCancel: false
          });
          return;
        }
        api.setBaseUrl(url);
        // 换了后端，旧令牌必然失效，清掉让 authed 自动重开桌
        api.clearToken();
        that.setData({ baseUrl: url, baseUrlOverridden: true });
        wx.showToast({ title: '已保存，正在测试', icon: 'none' });
        that.probe();
      }
    });
  },

  resetBaseUrl: function () {
    var that = this;
    wx.showModal({
      title: '恢复默认地址',
      content: '将改回代码包里编译的地址：' + config.BASE_URL,
      success: function (res) {
        if (!res.confirm) { return; }
        api.clearBaseUrl();
        api.clearToken();
        that.setData({ baseUrl: config.BASE_URL, baseUrlOverridden: false });
        that.probe();
        wx.showToast({ title: '已恢复默认', icon: 'none' });
      }
    });
  },

  retry: function () { this.probe(); },

  goOrders: function () { wx.switchTab({ url: '/pages/orders/orders' }); },
  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); },
  goTrace: function () { wx.switchTab({ url: '/pages/trace/trace' }); },
  goNotifications: function () { wx.navigateTo({ url: '/pages/notifications/notifications' }); },
  goService: function () { wx.navigateTo({ url: '/pages/service/service' }); }
});
