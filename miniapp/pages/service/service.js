// 智能客服与客诉：AI 即时受理 + 进度查询 + 忌口标签 + 补证重提。
//
// 口径（和后端 guest/service.py 一致，别在前端偷偷放宽）：
// - AI 只给分类与安抚话术，绝不显示「已批准/已退款」这类字样；
// - 涉及补偿必须等「组长核证据 → 经理终审」，界面如实显示当前环节；
// - 演示环境无支付，凭证不是退款流水。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');

var STATE = {
  submitted: { label: '待组长核实证据', tone: 'warn' },
  evidence_ok: { label: '待管理人员终审', tone: 'info' },
  approved: { label: '已批准 · 凭证已生成', tone: 'ok' },
  rejected: { label: '已驳回 · 可补证', tone: 'danger' },
  auto_closed: { label: '智能客服已答复', tone: 'ok' }
};

var COMPENSATIONS = [
  { key: 'redo', label: '重做一份' },
  { key: 'waive', label: '本单免单' },
  { key: 'coupon', label: '补偿优惠券' }
];

Page({
  data: {
    guestKey: '',
    tagOptions: [],
    tags: [],
    portrait: '',
    portraitMode: '',
    facts: {},
    topDishText: '',
    text: '',
    orderIndex: 0,
    orderLabels: [],
    orderIds: [],
    wantsRefund: false,
    compensationIndex: 0,
    compensationLabels: [],
    evidenceText: '',
    submitting: false,
    reply: null,
    complaints: [],
    loading: true,
    error: ''
  },

  onLoad: function () {
    var tags = store.getGuestTags();
    var options = config.GUEST_TAG_OPTIONS.map(function (t) {
      return { name: t, selected: tags.indexOf(t) >= 0 };
    });
    var comp = COMPENSATIONS.map(function (c) { return c.label; });
    this.setData({
      guestKey: api.getGuestKey(), tagOptions: options, tags: tags,
      compensationLabels: comp
    });
    this.refresh();
  },

  onPullDownRefresh: function () {
    var that = this;
    this.refresh().then(function () { wx.stopPullDownRefresh(); });
  },

  refresh: function () {
    var that = this;
    var key = api.getGuestKey();
    this.setData({ loading: true, error: '' });
    return Promise.all([
      api.authed('GET', '/api/customer/guest/profile', { guest_key: key }),
      api.authed('GET', '/api/customer/guest/complaints', { guest_key: key }),
      api.authed('GET', '/api/customer/orders')
    ]).then(function (res) {
      var profile = res[0] || {};
      var complaints = (res[1] && res[1].complaints) || [];
      var orders = (res[2] && res[2].orders) || [];
      var facts = (profile.portrait && profile.portrait.facts) || {};
      that.setData({
        portrait: (profile.portrait && profile.portrait.portrait) || '',
        portraitMode: (profile.portrait && profile.portrait.mode) || '',
        facts: facts,
        topDishText: (facts.top_dishes || []).join('、'),
        complaints: complaints.map(that.decorate),
        orderLabels: orders.map(function (o) {
          return o.order_no + ' · ' + (o.table_id || '-') + ' 号桌';
        }),
        orderIds: orders.map(function (o) { return o.id; }),
        loading: false
      });
    }).catch(function (err) {
      that.setData({
        loading: false,
        error: err.status === 0 ? '后厨服务暂不可达，稍后重试' : (err.message || '加载失败')
      });
    });
  },

  // WXML 不能调函数，状态/文案全部在这里预处理成字段
  decorate: function (row) {
    var meta = STATE[row.state] || { label: row.state, tone: 'plain' };
    var ai = row.ai_suggestion || {};
    return {
      id: row.id, complaint_no: row.complaint_no, text: row.text,
      category: row.category, state: row.state,
      state_label: meta.label, state_tone: meta.tone,
      amount: row.amount, wants_refund: row.wants_refund,
      voucher_no: row.voucher_no, reject_reason: row.reject_reason,
      reviewer: row.reviewer, approver: row.approver,
      version: row.version,
      ai_summary: ai.summary || '',
      ai_mode_label: ai.mode === 'live' ? '在线模型' : '离线规则引擎',
      degraded: ai.degraded_reason || '',
      can_supplement: !!row.can_supplement,
      compensation_note: row.compensation_note || '',
      history_count: (row.history || []).length
    };
  },

  onTextInput: function (e) { this.setData({ text: e.detail.value }); },
  onEvidenceInput: function (e) { this.setData({ evidenceText: e.detail.value }); },
  onOrderChange: function (e) { this.setData({ orderIndex: Number(e.detail.value) || 0 }); },
  onCompensationChange: function (e) {
    this.setData({ compensationIndex: Number(e.detail.value) || 0 });
  },
  onRefundChange: function (e) { this.setData({ wantsRefund: !!e.detail.value }); },

  toggleTag: function (e) {
    var name = e.currentTarget.dataset.name;
    var options = this.data.tagOptions.map(function (t) {
      if (t.name === name) { t.selected = !t.selected; }
      return t;
    });
    var tags = options.filter(function (t) { return t.selected; })
      .map(function (t) { return t.name; });
    store.setGuestTags(tags);
    this.setData({ tagOptions: options, tags: tags });
    wx.showToast({ title: '标签已保存，下次开桌生效', icon: 'none' });
  },

  submit: function () {
    var that = this;
    var text = (this.data.text || '').trim();
    if (text.length < 5) {
      wx.showToast({ title: '请至少填写 5 个字', icon: 'none' });
      return;
    }
    var payload = {
      guest_key: api.getGuestKey(),
      text: text,
      wants_refund: this.data.wantsRefund,
      compensation: COMPENSATIONS[this.data.compensationIndex].key,
      evidences: this.data.evidenceText.trim()
        ? [{ kind: 'text', content: this.data.evidenceText.trim() }] : []
    };
    if (this.data.orderIds.length) {
      payload.order_id = this.data.orderIds[this.data.orderIndex];
    }
    if (payload.wants_refund && !payload.order_id) {
      wx.showModal({
        title: '需要关联订单',
        content: '补偿申请要核对下单时的金额快照，请先在本机下过一单再提交。',
        showCancel: false
      });
      return;
    }
    this.setData({ submitting: true, reply: null });
    api.authed('POST', '/api/customer/guest/complaints', payload)
      .then(function (data) {
        that.setData({
          submitting: false, reply: data, text: '', evidenceText: '',
          wantsRefund: false
        });
        wx.showToast({ title: data.state === 'auto_closed' ? 'AI 已答复' : '已受理', icon: 'success' });
        that.refresh();
      }).catch(function (err) {
        that.setData({ submitting: false });
        wx.showModal({
          title: '提交失败', content: err.detail || err.message || '请稍后重试',
          showCancel: false
        });
      });
  },

  supplement: function (e) {
    var that = this;
    var id = Number(e.currentTarget.dataset.id);
    var version = Number(e.currentTarget.dataset.version);
    wx.showModal({
      title: '补充证据', content: '将附上一张现场照片的说明，工单会重新进入核实环节。',
      success: function (res) {
        if (!res.confirm) { return; }
        api.authed('POST', '/api/customer/guest/complaints/' + id + '/supplement', {
          guest_key: api.getGuestKey(),
          evidences: [{ kind: 'photo', content: '补充现场照片（演示占位）' }],
          expected_version: version
        }).then(function () {
          wx.showToast({ title: '已重新提交', icon: 'success' });
          that.refresh();
        }).catch(function (err) {
          wx.showModal({ title: '提交失败', content: err.message || '请刷新后重试', showCancel: false });
        });
      }
    });
  },

  retry: function () { this.refresh(); },
  goOrders: function () { wx.switchTab({ url: '/pages/orders/orders' }); },
  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); }
});
