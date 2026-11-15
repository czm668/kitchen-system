// 食品溯源：扫码 / 手输 → 批次、供应商资质、检测记录、二维码标签。
// 字段来自 TraceView.to_dict()，页面不臆造字段；查询历史只存本地。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var config = require('../../config.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    code: '',
    view: null,
    error: '',
    loading: false,
    labelUrl: '',
    labelFailed: false,
    history: [],
    historyTexts: {}
  },

  onLoad: function () { this.syncHistory(); },
  onShow: function () { this.syncHistory(); },

  syncHistory: function () {
    var rows = store.getTraceHistory();
    var texts = {};
    rows.forEach(function (row) {
      var d = new Date(row.time);
      texts[row.code] = (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
        ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
    });
    this.setData({ history: rows, historyTexts: texts });
  },

  // wx.scanCode 扫到的是标签 URL（…/label.png?code=XXX 或短链 /t/XXX）
  scan: function () {
    var that = this;
    wx.scanCode({
      onlyFromCamera: false,
      success: function (res) {
        var code = that.extractCode(res.result || '');
        if (!code) {
          wx.showToast({ title: '码里没有溯源码，请手动输入', icon: 'none' });
          return;
        }
        that.query(code);
      },
      fail: function () { /* 用户取消扫码，静默 */ }
    });
  },

  extractCode: function (text) {
    var match = /[?&]code=([^&]+)/.exec(text);
    if (match) { return decodeURIComponent(match[1]); }
    match = /\/t\/([A-Za-z0-9-]+)/.exec(text);
    if (match) { return match[1]; }
    return text.indexOf('http') === 0 ? '' : text.trim();
  },

  onInput: function (e) { this.setData({ code: e.detail.value }); },

  submit: function () {
    var code = (this.data.code || '').trim();
    if (!code) {
      wx.showToast({ title: '请输入或扫描溯源码', icon: 'none' });
      return;
    }
    this.query(code);
  },

  queryFromHistory: function (e) { this.query(e.currentTarget.dataset.code); },

  clearHistory: function () {
    var that = this;
    wx.showModal({
      title: '清除查询记录', content: '只清本机的历史，不影响后厨数据',
      success: function (res) {
        if (res.confirm) { store.clearTraceHistory(); that.syncHistory(); }
      }
    });
  },

  query: function (code) {
    var that = this;
    this.setData({ loading: true, error: '', view: null, labelFailed: false });
    api.request('GET', '/api/customer/trace', { code: code }).then(function (data) {
      var view = {};
      Object.keys(data).forEach(function (k) { view[k] = data[k]; });
      view.produce_text = fmt.timeText(data.produced_at);
      view.expire_text = fmt.timeText(data.expire_at);
      // seed 里 qualifications 存的是 JSON 数组字符串，直接渲染会带引号和方括号
      var quals = data.qualifications;
      if (typeof quals === 'string' && quals.charAt(0) === '[') {
        try {
          var parsed = JSON.parse(quals);
          if (Array.isArray(parsed)) { quals = parsed.join('、'); }
        } catch (e) { /* 解析不了就按原文显示 */ }
      }
      view.qualification_text = quals || '未登记';
      // WXML 遍历对象只能拿到 value，转成 [{key,value}] 才能同时显示检测项名
      var inspection = data.inspection || {};
      view.inspectionRows = Object.keys(inspection).map(function (key) {
        var value = inspection[key];
        return { key: key, value: typeof value === 'object' ? JSON.stringify(value) : String(value) };
      });
      view.hasInspection = view.inspectionRows.length > 0;
      store.pushTraceHistory(code, data.dish || '');
      that.setData({
        loading: false, view: view, code: code,
        labelUrl: config.BASE_URL + (data.qr_url || '')
      });
      that.syncHistory();
    }).catch(function (err) {
      that.setData({
        loading: false,
        error: err.status === 404 ? ('溯源码不存在：' + code) : (err.message || '查询失败')
      });
    });
  },

  onImageError: function () { this.setData({ labelFailed: true }); },

  copyCode: function () {
    if (!this.data.view) { return; }
    wx.setClipboardData({
      data: this.data.view.code || '',
      success: function () { wx.showToast({ title: '溯源码已复制', icon: 'none' }); }
    });
  },

  retry: function () {
    if (this.data.code) { this.query(this.data.code); }
  },
  emptyAction: function () { /* 溯源页无空态按钮 */ }
});
