// 消息中心：订阅消息的轮询模拟（A9-06）。since=0 拉本会话全部通知，
// 进入页面后把最新 seq 记为已读；订单页的未读角标用同一个 readSeq。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    rows: [],
    loading: true,
    error: '',
    unread: 0
  },

  onLoad: function () { this.load(); },
  onShow: function () { this.load(); },
  onPullDownRefresh: function () {
    var that = this;
    this.load().then(function () { wx.stopPullDownRefresh(); });
  },

  retry: function () { this.load(); },
  emptyAction: function () { this.goOrders(); },

  load: function () {
    var that = this;
    this.setData({ loading: this.data.rows.length === 0 });
    var readSeq = store.getReadSeq();
    return api.authed('GET', '/api/customer/notifications', { since: 0 })
      .then(function (data) {
        var maxSeq = 0;
        var rows = (data.notifications || []).map(function (row) {
          if (Number(row.seq) > maxSeq) { maxSeq = Number(row.seq); }
          return {
            seq: row.seq, message: row.message,
            time_text: fmt.timeText(row.time),
            kind_label: fmt.kindLabel(row.kind),
            order_no: row.order_no || '',
            unread: Number(row.seq) > readSeq
          };
        }).reverse();   // 最新的在最上面
        var unread = rows.filter(function (row) { return row.unread; }).length;
        that.setData({ rows: rows, loading: false, error: '', unread: unread });
        // 只在确实拿到更新时写，避免无谓的 storage IO
        if (maxSeq > readSeq) { store.setReadSeq(maxSeq); }
      }).catch(function (err) {
        that.setData({
          loading: false,
          error: err.status === 0 ? '后厨服务暂不可达，请检查连接' : (err.message || '消息加载失败')
        });
      });
  },

  markAllRead: function () {
    var rows = this.data.rows.map(function (row) {
      var copy = {};
      Object.keys(row).forEach(function (k) { copy[k] = row[k]; });
      copy.unread = false;
      return copy;
    });
    this.setData({ rows: rows, unread: 0 });
    wx.showToast({ title: '已全部标为已读', icon: 'none' });
  },

  goOrders: function () { wx.switchTab({ url: '/pages/orders/orders' }); }
});
