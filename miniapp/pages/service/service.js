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

var COMPENSATIONS = config.COMPENSATION_OPTIONS || [
  { key: 'redo', label: '重做一份' },
  { key: 'waive', label: '本单免单' },
  { key: 'coupon', label: '补偿优惠券' }
];

// 后端证据 url 是相对路径（/api/...），小程序 <image>/previewImage 不认相对路径，
// 会当成代码包内文件而加载失败。显示时用绝对地址，提交时再还原成相对地址
// （后端 _build_evidences 明确要求 photo.url 以 /api/ 开头）。
function absEvidenceUrl(url) {
  if (!url) { return ''; }
  if (/^https?:\/\//i.test(url)) { return url; }
  return api.getBaseUrl() + url;
}
function relEvidenceUrl(url) {
  if (!url) { return ''; }
  var base = api.getBaseUrl();
  if (url.indexOf(base) === 0) { return url.slice(base.length); }
  return url;
}

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
    quickPhrases: [],
    orderIndex: 0,
    orderLabels: [],
    orderIds: [],
    wantsRefund: false,
    compensationIndex: 0,
    compensationLabels: [],
    evidenceText: '',
    evidenceImages: [],
    uploading: false,
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
      compensationLabels: comp,
      quickPhrases: config.COMPLAINT_QUICK_PHRASES || []
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
      ai_reply: row.ai_reply || '',
      ai_mode_label: ai.mode === 'live' ? '在线模型' : '离线规则引擎',
      degraded: ai.degraded_reason || '',
      stage_label: row.stage_label || meta.label,
      blocker: row.blocker || '',
      blocker_owner: row.blocker_owner || '',
      overdue: !!row.overdue,
      sla_remain_min: row.sla_remain_min || 0,
      actions_text: (row.actions || []).map(function (a) {
        return a.detail || a.type;
      }).join('；'),
      urgent_text: (row.urgent || []).join('；'),
      can_supplement: !!row.can_supplement,
      compensation_note: row.compensation_note || '',
      history_count: (row.history || []).length,
      photo_urls: (row.evidences || []).filter(function (ev) {
        return ev.kind === 'photo' && ev.url;
      }).map(function (ev) { return absEvidenceUrl(ev.url); })
    };
  },

  onTextInput: function (e) { this.setData({ text: e.detail.value }); },
  // 常用语句只填入、不自动提交：顾客还能补上菜名/时间等细节，
  // 后端拿到的信息越具体，阶段与卡点判断越准。
  onQuickPhrase: function (e) {
    var phrase = String(e.currentTarget.dataset.phrase || '');
    if (!phrase) { return; }
    var old = (this.data.text || '').trim();
    var next = old ? (old + '\n' + phrase) : phrase;
    this.setData({ text: next.slice(0, 500) });
    wx.showToast({ title: '已填入，可补充细节后提交', icon: 'none' });
  },
  onEvidenceInput: function (e) { this.setData({ evidenceText: e.detail.value }); },

  // 选择并上传图片证据：wx.chooseMedia → 服务端二进制端点 → 拿到 url 存本地
  chooseImage: function () {
    var that = this;
    var remain = 5 - this.data.evidenceImages.length;
    if (remain <= 0) {
      wx.showToast({ title: '最多 5 张图片', icon: 'none' });
      return;
    }
    wx.chooseMedia({
      count: remain, mediaType: ['image'], sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
      success: function (res) {
        var files = (res.tempFiles || []).map(function (f) { return f.tempFilePath; });
        that.uploadImages(files);
      }
    });
  },

  uploadImages: function (files) {
    var that = this;
    if (!files || !files.length) { return; }
    this.setData({ uploading: true });
    var base = api.getBaseUrl();
    var token = wx.getStorageSync(config.KEYS.TOKEN) || '';
    var guestKey = api.getGuestKey();
    var done = 0, okCount = 0, failCount = 0, lastError = '';
    var urls = this.data.evidenceImages.slice();
    files.forEach(function (filePath) {
      wx.uploadFile({
        url: base + '/api/customer/guest/evidence-upload',
        filePath: filePath,
        name: 'image',
        header: { 'X-Customer-Token': token, 'X-Guest-Key': guestKey },
        success: function (res) {
          var body = null;
          try { body = JSON.parse(res.data || '{}'); } catch (e) { body = null; }
          if (res.statusCode === 200 && body && body.ok && body.url) {
            okCount += 1;
            // 存绝对地址供 <image>/previewImage 显示；提交时再还原相对地址
            urls.push(absEvidenceUrl(body.url));
          } else {
            failCount += 1;
            lastError = (body && (body.detail || body.error)) ||
                        ('HTTP ' + res.statusCode);
          }
        },
        fail: function (e) {
          failCount += 1;
          lastError = (e && e.errMsg) || '网络异常';
        },
        complete: function () {
          done += 1;
          if (done !== files.length) { return; }
          that.setData({ evidenceImages: urls.slice(0, 5), uploading: false });
          // 如实反馈：有几张成功/失败，绝不在全失败时谎报成功
          if (failCount === 0) {
            wx.showToast({ title: '已上传 ' + okCount + ' 张', icon: 'success' });
          } else if (okCount > 0) {
            wx.showToast({
              title: okCount + ' 张成功，' + failCount + ' 张失败', icon: 'none'
            });
          } else {
            wx.showModal({
              title: '图片上传失败',
              content: (lastError || '请稍后重试') +
                '。请确认已开桌、图片小于 2MB。',
              showCancel: false
            });
          }
        }
      });
    });
  },

  removeImage: function (e) {
    var idx = Number(e.currentTarget.dataset.index);
    var urls = this.data.evidenceImages.slice();
    urls.splice(idx, 1);
    this.setData({ evidenceImages: urls });
  },

  previewImage: function (e) {
    var url = e.currentTarget.dataset.url;
    wx.previewImage({ current: url, urls: this.data.evidenceImages });
  },

  previewTicketImage: function (e) {
    var d = e.currentTarget.dataset;
    wx.previewImage({ current: d.url, urls: d.urls || [d.url] });
  },
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
    var evidences = [];
    if (this.data.evidenceText.trim()) {
      evidences.push({ kind: 'text', content: this.data.evidenceText.trim() });
    }
    (this.data.evidenceImages || []).forEach(function (url) {
      // 本地存的是绝对地址（为了能显示），提交还原成 /api/... 相对地址
      evidences.push({ kind: 'photo', content: '', url: relEvidenceUrl(url) });
    });
    var payload = {
      guest_key: api.getGuestKey(),
      text: text,
      wants_refund: this.data.wantsRefund,
      compensation: COMPENSATIONS[this.data.compensationIndex].key,
      evidences: evidences
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
        data.action_text = (data.actions || []).map(function (a) {
          return a.detail || a.type;
        }).join('；');
        data.urgent_text = (data.urgent || []).join('；');
        that.setData({
          submitting: false, reply: data, text: '', evidenceText: '',
          evidenceImages: [], wantsRefund: false
        });
        wx.showToast({
          title: (data.actions && data.actions.length) ? '已受理并处理'
                 : (data.state === 'auto_closed' ? 'AI 已答复' : '已受理'),
          icon: 'success'
        });
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
  goChat: function () { wx.navigateTo({ url: '/pages/chat/chat' }); },
  goOrders: function () { wx.switchTab({ url: '/pages/orders/orders' }); },
  goMenu: function () { wx.switchTab({ url: '/pages/menu/menu' }); }
});
