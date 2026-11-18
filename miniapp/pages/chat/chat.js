// 对话式智能客服：能直接办的当场办（催菜/查单/菜单），真投诉才建工单。
var api = require('../../utils/api.js');
var config = require('../../config.js');

var QUICK_ASKS = [
  '我的菜还要多久', '帮我催一下', '查一下订单进度',
  '有什么招牌推荐', '这道菜多少钱', '谢谢'
];

var INTENT_KIND = {
  urge: '已催厨房', order_query: '订单进度', menu_query: '菜单',
  chitchat: '闲聊', complaint: '已生成工单'
};

Page({
  data: {
    messages: [], input: '', sending: false, thinking: false,
    quickAsks: QUICK_ASKS, scrollTo: '', orderIds: [], orderIndex: 0,
    orderLabels: []
  },

  onLoad: function () {
    var self = this;
    this.pushAi('你好，我是智能客服。直接说你的问题就行 —— '
      + '想催菜、查订单、问菜单，或描述遇到的问题，我来帮你处理。');
    // 拉一下订单列表，方便对话里带上关联订单
    api.authed('GET', '/api/customer/orders').then(function (data) {
      var orders = (data && data.orders) || [];
      self.setData({
        orderIds: orders.map(function (o) { return o.id; }),
        orderLabels: orders.map(function (o) { return o.order_no; })
      });
    }).catch(function () { /* 没单也能聊天 */ });
  },

  pushAi: function (text, extra) {
    var list = this.data.messages.slice();
    var id = 'm' + Date.now() + '-' + list.length;
    list.push(Object.assign({ id: id, role: 'ai', text: text }, extra || {}));
    this.setData({ messages: list, scrollTo: 'msg-' + id });
  },
  pushUser: function (text) {
    var list = this.data.messages.slice();
    var id = 'm' + Date.now() + '-' + list.length;
    list.push({ id: id, role: 'user', text: text });
    this.setData({ messages: list, scrollTo: 'msg-' + id });
  },

  retry: function () { /* 对话页无加载失败态，占位满足模板绑定 */ },
  onInput: function (e) { this.setData({ input: e.detail.value }); },
  onQuickAsk: function (e) {
    this.setData({ input: e.currentTarget.dataset.text || '' });
    this.send();
  },

  send: function () {
    var self = this;
    var text = (this.data.input || '').trim();
    if (!text || this.data.sending) { return; }
    this.pushUser(text);
    this.setData({ input: '', sending: true, thinking: true });

    var payload = {
      guest_key: api.getGuestKey(),
      text: text
    };
    if (this.data.orderIds.length) {
      payload.order_id = this.data.orderIds[this.data.orderIndex];
    }
    api.authed('POST', '/api/customer/guest/chat', payload).then(function (data) {
      self.setData({ sending: false, thinking: false });
      var extra = { kind: INTENT_KIND[data.intent] || '' };
      if (data.data && data.data.complaint_no) { extra.ticket = data.data.complaint_no; }
      self.pushAi(data.reply || '我记下了，稍后帮你看看。', extra);
      // 若刚生成了工单，提示可去客服页看进度
      if (data.action === 'complaint') {
        wx.showToast({ title: '已生成工单', icon: 'success' });
      }
    }).catch(function (err) {
      self.setData({ sending: false, thinking: false });
      self.pushAi('抱歉，我这边暂时没能连上服务：'
        + (err.detail || err.message || '请稍后再试')
        + '。你也可以到「智能客服」页正式提交。');
    });
  }
});
