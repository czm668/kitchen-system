// 确认订单页：购物车明细 + 桌号 + 二次确认后下单。
//
// 口径说明：后端 POST /api/customer/orders 只接受 {items, table_id}，
// 没有「备注」字段。所以本页不提供备注输入框——做了也只会静默丢弃，
// 属于演示时一眼被拆穿的假功能。价格快照 / 配方版本锁定由后端完成。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    rows: [],
    summary: { qty: 0, money_text: '0.00', minutes: 0 },
    tableLabels: [],
    tableIndex: 0,
    table: config.DEFAULT_TABLE,
    submitting: false
  },

  onLoad: function () {
    var labels = config.TABLE_OPTIONS.map(function (n) { return n + ' 号桌'; });
    var table = store.getTable();
    this.setData({
      tableLabels: labels, table: table,
      tableIndex: Math.max(0, config.TABLE_OPTIONS.indexOf(table))
    });
    this.refresh();
  },

  onShow: function () { this.refresh(); },

  refresh: function () {
    var rows = store.getCart().map(function (row) {
      return {
        code: row.code, name: row.name, qty: row.qty,
        category: row.category, estimate_minutes: row.estimate_minutes,
        image: fmt.imageOf(row.code),        // 与菜单页同一套图，缺图时走占位块
        price_text: fmt.money(row.price),
        subtotal_text: fmt.money(row.qty * Number(row.price || 0))
      };
    });
    this.setData({ rows: rows, summary: store.cartSummary(store.getCart()) });
  },

  onTableChange: function (e) {
    var idx = Number(e.detail.value) || 0;
    var table = config.TABLE_OPTIONS[idx];
    store.setTable(table);
    var app = getApp();
    if (app && app.globalData) { app.globalData.tableId = table; }
    this.setData({ tableIndex: idx, table: table });
  },

  plus: function (e) {
    store.changeQty(e.currentTarget.dataset.code, 1);
    this.refresh();
  },

  minus: function (e) {
    store.changeQty(e.currentTarget.dataset.code, -1);
    this.refresh();
  },

  remove: function (e) {
    store.removeFromCart(e.currentTarget.dataset.code);
    this.refresh();
  },

  clear: function () {
    var that = this;
    wx.showModal({
      title: '清空购物车', content: '确定移除已选的全部菜品？',
      success: function (res) {
        if (res.confirm) { store.clearCart(); that.refresh(); }
      }
    });
  },

  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); },
  emptyAction: function () { this.goMenu(); },
  retry: function () { this.refresh(); },

  submit: function () {
    var that = this;
    if (this.data.submitting || this.data.rows.length === 0) { return; }
    var names = this.data.rows.map(function (r) { return r.name + ' ×' + r.qty; }).join('、');
    wx.showModal({
      title: '确认下单',
      content: this.data.table + ' 号桌 · ' + this.data.summary.qty + ' 份 · 合计 ￥' +
               this.data.summary.money_text + '\n' + names,
      confirmText: '确认下单',
      success: function (res) {
        if (!res.confirm) { return; }
        that.placeOrder();
      }
    });
  },

  placeOrder: function () {
    var that = this;
    var items = store.getCart().map(function (row) {
      return { code: row.code, qty: row.qty };
    });
    this.setData({ submitting: true });
    api.authed('POST', '/api/customer/orders', {
      items: items, table_id: this.data.table
    }).then(function (data) {
      store.clearCart();
      that.setData({ submitting: false, rows: [], summary: { qty: 0, money_text: '0.00', minutes: 0 } });
      wx.showToast({ title: '下单成功 ' + (data.order_no || ''), icon: 'none', duration: 1600 });
      setTimeout(function () { wx.switchTab({ url: '/pages/orders/orders' }); }, 800);
    }).catch(function (err) {
      that.setData({ submitting: false });
      // 409 = 沽清整单拒绝（后端 detail 里列出全部沽清项）
      if (err.status === 409) {
        wx.showModal({
          title: '有菜品沽清', content: err.detail || '请返回菜单调整',
          confirmText: '返回菜单', showCancel: false,
          success: function () { that.goMenu(); }
        });
      } else {
        wx.showModal({
          title: '下单未成功', content: err.message || '请稍后重试',
          showCancel: false
        });
      }
    });
  }
});
