// 食品溯源：扫码 / 手输 → 批次、供应商资质、检测记录、二维码标签。
// 字段来自 TraceView.to_dict()，页面不臆造字段；查询历史只存本地。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var fmt = require('../../utils/format.js');

// 种子里固定存在的 24 个示例码（与 fixtures/seed_kitchen.sql 对齐），
// 一道菜一个码；前三码覆盖绿/橙/红三种保质期状态，其余均为新鲜批次。
var DEMO_CODES = [
  { code: 'TC-DEMO-0001', dish: '青椒肉丝', category: '热菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0002', dish: '宫保鸡丁', category: '热菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0003', dish: '清炒时蔬', category: '素菜', supplier: '绿源蔬菜基地', tag: '临期', tone: 'warn' },
  { code: 'TC-DEMO-0004', dish: '酸辣土豆丝', category: '素菜', supplier: '绿源蔬菜基地', tag: '已过期', tone: 'danger' },
  { code: 'TC-DEMO-0005', dish: '红烧肉', category: '热菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0006', dish: '水煮鱼', category: '热菜', supplier: '滨海水产有限公司', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0007', dish: '烤鸭', category: '热菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0008', dish: '木须肉', category: '热菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0009', dish: '凉拌海带丝', category: '凉菜', supplier: '滨海水产有限公司', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0010', dish: '凉拌黄瓜', category: '凉菜', supplier: '绿源蔬菜基地', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0011', dish: '酱牛肉', category: '凉菜', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0012', dish: '皮蛋豆腐', category: '凉菜', supplier: '金穗粮油贸易', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0013', dish: '地三鲜', category: '素菜', supplier: '绿源蔬菜基地', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0014', dish: '手撕包菜', category: '素菜', supplier: '绿源蔬菜基地', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0015', dish: '西红柿蛋汤', category: '汤羹', supplier: '绿源蔬菜基地', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0016', dish: '紫菜蛋花汤', category: '汤羹', supplier: '滨海水产有限公司', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0017', dish: '玉米排骨汤', category: '汤羹', supplier: '鲜丰肉联厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0018', dish: '米饭', category: '主食', supplier: '金穗粮油贸易', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0019', dish: '饺子', category: '主食', supplier: '金穗粮油贸易', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0020', dish: '炒面', category: '主食', supplier: '金穗粮油贸易', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0021', dish: '红豆沙', category: '甜品', supplier: '金穗粮油贸易', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0022', dish: '拔丝香蕉', category: '甜品', supplier: '绿源蔬菜基地', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0023', dish: '鲜榨橙汁', category: '饮品', supplier: '清源饮品厂', tag: '新鲜', tone: 'ok' },
  { code: 'TC-DEMO-0024', dish: '酸梅汤', category: '饮品', supplier: '清源饮品厂', tag: '新鲜', tone: 'ok' }
];

Page({
  data: {
    code: '',
    view: null,
    error: '',
    loading: false,
    labelUrl: '',
    labelFailed: false,
    history: [],
    historyTexts: {},
    demoCodes: DEMO_CODES
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

  // 一键查询种子示例码：空态给「点了就能看到效果」的四个入口。
  // 不传 data-code 时默认查第一个，兼容旧调用。
  demoCode: function (e) {
    var code = (e && e.currentTarget && e.currentTarget.dataset.code) || 'TC-DEMO-0001';
    this.query(code);
  },

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
      // ---- 保质期进度：用生产/到期/当前时刻算一条可视进度条 ----
      // 注意用 that（闭包捕获的 page 实例）：这里是 .then 的普通函数回调，
      // this 不指向 page，写 this.buildShelf 会抛错被 catch 吞掉显示「查询失败」。
      view.shelf = that.buildShelf(data.produced_at, data.expire_at);
      // ---- 溯源时间线：生产 → 检测 → 到期，串成一条可读的链 ----
      view.timeline = that.buildTimeline(data);
      // ---- 安全核验清单：资质/检测/保质期三项一眼看清 ----
      view.checks = that.buildChecks(data, view);
      // ---- 原料溯源：这盘菜用了什么、每样从哪来、检疫结果 ----
      view.ingredients = that.buildIngredients(data.ingredients || []);
      view.proof = that.buildProof(data.proof, data.recipe);
      store.pushTraceHistory(code, data.dish || '');
      that.setData({
        loading: false, view: view, code: code,
        labelUrl: api.getBaseUrl() + (data.qr_url || '')
      });
      that.syncHistory();
    }).catch(function (err) {
      that.setData({
        loading: false,
        error: err.status === 404 ? ('溯源码不存在：' + code) : (err.message || '查询失败')
      });
    });
  },

  // ISO 时间 → 毫秒；兼容 iOS 对 "YYYY-MM-DDTHH:mm:ss+08:00" 的解析差异
  toMillis: function (iso) {
    if (!iso) { return null; }
    var s = String(iso).replace('T', ' ').replace(/([+-]\d{2}:\d{2})$/, '');
    var t = Date.parse(s.replace(/-/g, '/'));
    return isNaN(t) ? null : t;
  },

  // 保质期进度条：已过天数/总天数，颜色随新鲜度变化。
  // 用服务端时间基准不那么容易，这里用本机时钟；演示足够，且过期判定后端为准（view.expired）。
  buildShelf: function (producedAt, expireAt) {
    var start = this.toMillis(producedAt);
    var end = this.toMillis(expireAt);
    if (start === null || end === null || end <= start) {
      return { available: false };
    }
    var now = Date.now();
    var total = end - start;
    var used = Math.max(0, Math.min(total, now - start));
    var percent = Math.round(used * 100 / total);
    var days = Math.ceil((end - now) / 86400000);
    var expired = now > end;
    var tone = expired ? 'danger' : (days <= 1 ? 'warn' : 'ok');
    var hint = expired ? '已过保质期' : (days <= 0 ? '今日到期' :
               (days === 1 ? '明天到期' : '还剩 ' + days + ' 天'));
    return {
      available: true, percent: percent, days: days,
      expired: expired, tone: tone, hint: hint,
      barTone: expired ? 'bar--danger' : (days <= 1 ? 'bar--warn' : 'bar--ok')
    };
  },

  // 溯源时间线：每个节点 {title, sub, tone, done}，前端照着画圆点+连线
  buildTimeline: function (data) {
    var inspection = data.inspection || {};
    var checked = inspection['检测'] || inspection['检验结果'] || '';
    var inspector = inspection['检验员'] || '';
    var qualified = String(checked).indexOf('合格') >= 0;
    var nodes = [];
    nodes.push({
      title: '生产入库', sub: fmt.timeText(data.produced_at) || '未登记',
      tone: 'info', done: !!data.produced_at
    });
    nodes.push({
      title: '检验检测',
      sub: checked ? (checked + (inspector ? ' · ' + inspector : '')) : '暂无检测记录',
      tone: qualified ? 'ok' : 'plain', done: !!checked
    });
    nodes.push({
      title: '保质到期', sub: fmt.timeText(data.expire_at) || '未标保质期',
      tone: data.expired ? 'danger' : 'plain', done: !!data.expire_at
    });
    return nodes;
  },

  // 安全核验：三张状态卡，每张 {label, state, tone, detail}
  buildChecks: function (data, view) {
    var inspection = data.inspection || {};
    var checked = inspection['检测'] || inspection['检验结果'] || '';
    var qualified = String(checked).indexOf('合格') >= 0;
    var hasQual = !!(data.qualifications && data.qualifications !== '[]' &&
                     data.qualifications !== '未登记');
    return [
      {
        label: '供应商资质',
        state: hasQual ? '已登记' : '未登记',
        tone: hasQual ? 'ok' : 'warn',
        detail: data.supplier || '无供应商信息'
      },
      {
        label: '检验检测',
        state: qualified ? '合格' : (checked ? String(checked) : '无记录'),
        tone: qualified ? 'ok' : 'plain',
        detail: checked ? checked : '这批尚未登记检测'
      },
      {
        label: '保质期',
        state: view.shelf && view.shelf.available ? view.shelf.hint : '未标保质期',
        tone: view.shelf && view.shelf.tone ? view.shelf.tone : 'plain',
        detail: view.expire_text
      }
    ];
  },

  // 原料溯源列表：每样原料的到货批次、供应商、检疫项目与结果。
  // 未登记原料批次的如实标注，不臆造「合格」。
  buildIngredients: function (rows) {
    return (rows || []).map(function (ing) {
      var ins = ing.inspection || {};
      var items = [];
      Object.keys(ins).forEach(function (key) {
        if (key === 'ingredients') { return; }
        var val = ins[key];
        items.push(key + '：' + (typeof val === 'object' ? JSON.stringify(val) : String(val)));
      });
      var weight = ing.weight_g ? ing.weight_g + 'g' : '';
      return {
        name: ing.name || '未命名原料',
        meta: [weight, ing.batch_no ? '批次 ' + ing.batch_no : ''].filter(Boolean).join(' · '),
        supplier: ing.supplier || (ing.note || '供应商未登记'),
        license: ing.license_no || '',
        inspectText: items.join(' · ') || '检疫记录未登记',
        tone: ing.expired ? 'danger' : (ing.batch_no ? 'ok' : 'warn'),
        expired: !!ing.expired
      };
    });
  },

  // 现做证据：SOP 工序/称重窗口是「标准」，执行记录查不到就如实标未记录。
  buildProof: function (proof, recipe) {
    if (!proof) { return null; }
    var steps = (proof.sop_steps || []).map(function (s) {
      return {
        title: '#' + s.step_no + ' ' + s.title,
        detail: (s.heat_level ? s.heat_level + ' · ' : '') +
                (s.duration_sec ? s.duration_sec + 's' : '') +
                (s.ccp ? ' · 关键控制点' : '') + (s.requires_card ? ' · 需刷卡' : ''),
        ccp: !!s.ccp
      };
    });
    return {
      steps: steps,
      stepCount: steps.length,
      ccpCount: proof.ccp_count || 0,
      weighCount: (proof.weigh_items || []).length,
      executed: !!(proof.weigh_executed || proof.retention_executed),
      note: proof.note || '',
      hasRecipe: !!recipe
    };
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
  // 从结果页回到初始查询态（空态引导 + 示例码列表重新出现）。
  // 用 that 风格无关，这里是事件回调直接绑定在 page 上，this 即 page。
  backToList: function () {
    this.setData({ view: null, error: '', code: '', labelUrl: '', labelFailed: false });
  },
  emptyAction: function () { /* 溯源页无空态按钮 */ }
});
