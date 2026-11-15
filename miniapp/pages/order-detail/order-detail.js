// 订单详情页：整单进度 + 逐菜状态时间线 + 催菜。
// 数据来源：订单页把整单对象存进 store；本页再用列表接口刷新同一笔单，
// 不新增后端接口（顾客通道目前没有 /orders/{id} 这个路由）。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    order: null,
    loading: false,
    error: '',
    urgeCd: {}
  },

  onLoad: function () {
    var order = store.getDetailOrder();
    if (!order) {
      this.setData({ error: '请从订单列表进入详情页' });
      return;
    }
    this.setData({ order: fmt.decorateOrder(order) });
    var that = this;
    this._cd = setInterval(function () { that.tickCd(); }, 1000);
    this.refresh();
  },

  onUnload: function () { clearInterval(this._cd); },
  onPullDownRefresh: function () {
    var that = this;
    this.refresh().then(function () { wx.stopPullDownRefresh(); });
  },

  retry: function () { this.refresh(); },

  refresh: function () {
    var that = this;
    if (!this.data.order) { return Promise.resolve(); }
    this.setData({ loading: true });
    return api.authed('GET', '/api/customer/orders').then(function (data) {
      var match = null;
      (data.orders || []).forEach(function (row) {
        if (row.id === that.data.order.id) { match = fmt.decorateOrder(row); }
      });
      that.setData({
        loading: false, order: match ? that.applyUrge(match) : that.data.order,
        error: match ? '' : '这笔订单已不在当前会话中，可在「我的」里重新开桌'
      });
      if (match) { store.setDetailOrder(match); }
    }).catch(function (err) {
      that.setData({ loading: false, error: err.message || '刷新失败' });
    });
  },

  applyUrge: function (order) {
    var cd = this.data.urgeCd;
    var copy = {};
    Object.keys(order).forEach(function (k) { copy[k] = order[k]; });
    copy.items = order.items.map(function (item) {
      var row = {};
      Object.keys(item).forEach(function (k) { row[k] = item[k]; });
      var left = cd[row.id] || 0;
      row.urge_text = left > 0 ? (left + 's') : '催菜';
      row.urge_disabled = left > 0;
      return row;
    });
    return copy;
  },

  urge: function (e) {
    var that = this;
    var itemId = e.currentTarget.dataset.id;
    if (this.data.urgeCd[itemId]) { return; }
    api.authed('POST', '/api/customer/orders/items/' + itemId + '/urge')
      .then(function (data) {
        var cd = that.data.urgeCd;
        cd[itemId] = config.URGE_COOLDOWN_SEC;
        that.setData({ urgeCd: cd });
        wx.showToast({ title: '已催菜（第 ' + data.urge_count + ' 次）', icon: 'none' });
        that.refresh();
      })
      .catch(function (err) {
        wx.showToast({
          title: err.status === 429 ? (err.detail || '催菜太频繁') : (err.message || '催菜失败'),
          icon: 'none'
        });
      });
  },

  tickCd: function () {
    var cd = this.data.urgeCd;
    var keys = Object.keys(cd);
    if (!keys.length || !this.data.order) { return; }
    keys.forEach(function (key) {
      cd[key] -= 1;
      if (cd[key] <= 0) { delete cd[key]; }
    });
    this.setData({ urgeCd: cd, order: this.applyUrge(this.data.order) });
  },

  copyOrderNo: function () {
    if (!this.data.order) { return; }
    var that = this;
    wx.setClipboardData({
      data: this.data.order.order_no,
      success: function () { wx.showToast({ title: '订单号已复制', icon: 'none' }); }
    });
  },

  goTrace: function () { wx.switchTab({ url: '/pages/trace/trace' }); },
  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); },
  emptyAction: function () { this.goMenu(); }
});
