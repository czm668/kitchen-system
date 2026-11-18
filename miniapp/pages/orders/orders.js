// 订单页：进行中 / 已完成分组 + 整单进度 + 逐菜状态 + 催菜 + 未读消息角标。
// 后端只有「订单列表」接口，没有单笔详情；本页把整单数据存进 store 供详情页读，
// 详情页再用同一个列表接口刷新，不额外编接口。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    activeOrders: [],
    doneOrders: [],
    loading: true,
    error: '',
    urgeCd: {},       // item_id → 剩余冷却秒
    unread: 0,
    empty: false
  },

  onLoad: function () {
    var that = this;
    // 服务不通时一次请求要等满 8s 超时。若还按 2s/4s 无脑轮询，会同时堆起
    // 四五个请求，控制台刷一屏 ERR_CONNECTION_TIMED_OUT。所以：
    //   _busy：上一个请求没回来就跳过本轮，不并发；
    //   _netFail / _skip：连续网络失败后指数退避（最多跳过 16 轮 = 32s），
    //   成功一次立刻恢复原间隔。api.js 的地址自愈成功后请求会直接成功，
    //   因此换 WiFi 的场景根本不会进入退避。
    this._busy = { orders: false, notify: false };
    this._netFail = { orders: 0, notify: 0 };
    this._skip = { orders: 0, notify: 0 };
    this._poll = setInterval(function () {
      if (that._busy.orders) { return; }
      if (that._skip.orders > 0) { that._skip.orders -= 1; return; }
      that.reload();
    }, config.POLL_INTERVAL_MS);
    this._notify = setInterval(function () {
      if (that._busy.notify) { return; }
      if (that._skip.notify > 0) { that._skip.notify -= 1; return; }
      that.loadNotifications();
    }, config.NOTIFY_INTERVAL_MS);
    this._cd = setInterval(function () { that.tickCd(); }, 1000);
    this.reload();
    this.loadNotifications();
  },

  onUnload: function () {
    // 三个定时器必须全清：切 tab 回来会重新 onLoad，不清会叠加把演示机打爆
    clearInterval(this._poll);
    clearInterval(this._notify);
    clearInterval(this._cd);
  },

  onShow: function () { this.reload(); this.loadNotifications(); },

  onPullDownRefresh: function () {
    var that = this;
    this.reload().then(function () { wx.stopPullDownRefresh(); });
  },

  retry: function () { this.reload(); },

  reload: function () {
    var that = this;
    if (this._busy.orders) { return Promise.resolve(false); }
    this._busy.orders = true;
    return api.authed('GET', '/api/customer/orders').then(function (data) {
      that._busy.orders = false;
      that._netFail.orders = 0; that._skip.orders = 0;
      var all = (data.orders || []).map(fmt.decorateOrder);
      var active = [], done = [];
      all.forEach(function (order) {
        if (order.finished) { done.push(order); } else { active.push(order); }
      });
      // 最新的排前面
      active.reverse(); done.reverse();
      that.setData({
        activeOrders: that.applyUrge(active),
        doneOrders: that.applyUrge(done),
        empty: all.length === 0, loading: false, error: ''
      });
      return true;
    }).catch(function (err) {
      that._busy.orders = false;
      if (err.status === 0) {
        that._netFail.orders = Math.min(that._netFail.orders + 1, 4);
        that._skip.orders = Math.min(1 << that._netFail.orders, 16);
      }
      that.setData({
        loading: false,
        error: err.status === 0
          ? '连不上 ' + (err.baseUrl || api.getBaseUrl()) + '，稍后自动重试'
          : (err.message || '订单加载失败')
      });
      return false;
    });
  },

  // 把倒计时写进 item 字段，WXML 不用动态下标（urgeCd[dish.id] 在低版本会失效）
  applyUrge: function (orders) {
    var cd = this.data.urgeCd;
    return orders.map(function (order) {
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
    });
  },

  refreshUrgeText: function () {
    this.setData({
      activeOrders: this.applyUrge(this.data.activeOrders),
      doneOrders: this.applyUrge(this.data.doneOrders)
    });
  },

  loadNotifications: function () {
    var that = this;
    if (this._busy.notify) { return; }
    this._busy.notify = true;
    api.authed('GET', '/api/customer/notifications', { since: store.getReadSeq() })
      .then(function (data) {
        that._busy.notify = false;
        that._netFail.notify = 0; that._skip.notify = 0;
        that.setData({ unread: (data.notifications || []).length });
      }).catch(function (err) {
        that._busy.notify = false;
        if (err.status === 0) {
          that._netFail.notify = Math.min(that._netFail.notify + 1, 4);
          that._skip.notify = Math.min(1 << that._netFail.notify, 16);
        }
        /* 通知失败不打扰订单主流程，只做退避 */
      });
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
        that.reload();
      })
      .catch(function (err) {
        // 429 才是真闸门（前端倒计时只是体验优化）
        wx.showToast({
          title: err.status === 429 ? (err.detail || '催菜太频繁') : (err.message || '催菜失败'),
          icon: 'none'
        });
      });
  },

  tickCd: function () {
    var cd = this.data.urgeCd;
    var keys = Object.keys(cd);
    if (!keys.length) { return; }
    keys.forEach(function (key) {
      cd[key] -= 1;
      if (cd[key] <= 0) { delete cd[key]; }
    });
    this.setData({ urgeCd: cd });
    this.refreshUrgeText();
  },

  openDetail: function (e) {
    var id = Number(e.currentTarget.dataset.id);
    var all = this.data.activeOrders.concat(this.data.doneOrders);
    for (var i = 0; i < all.length; i++) {
      if (all[i].id === id) {
        store.setDetailOrder(all[i]);
        wx.navigateTo({ url: '/pages/order-detail/order-detail' });
        return;
      }
    }
  },

  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); },
  goNotifications: function () { wx.navigateTo({ url: '/pages/notifications/notifications' }); },
  goTrace: function () { wx.switchTab({ url: '/pages/trace/trace' }); },
  emptyAction: function () { this.goMenu(); }
});
