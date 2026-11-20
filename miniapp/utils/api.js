// wx.request 封装 + 顾客令牌管理。
//
// ## 令牌放哪
//
// `wx.setStorageSync('customer_token')`。顾客通道的令牌**不是登录态**：
// 不存在过期刷新的问题（服务端重启即失效，届时统一弹「请重新开桌」）。
// 401 时清掉本地令牌并重开桌，这是唯一的恢复路径。
//
// ## 五个容易踩的坑（都是实测踩出来的）
//
// 1. 并发 401：订单页 2s 轮询 + 通知页 4s 轮询同时失效，会同时触发多次
//    openSession，后写的 token 把先发的请求作废 → 必须加重开锁（_reopening）。
// 2. 写操作禁止 401 自动重试：下单重试可能造成重复下单（旧令牌的单已经
//    不属于新令牌），只有 GET 能安全重试。
// 3. 网络层失败（status=0）绝不能和 401 混为一谈：前者是服务没起，
//    提示里要带 BASE_URL 和修复动作，后者才该走重开桌。
// 4. BASE_URL 编译进代码包，改电脑 IP 就得重新编译+重新扫码。所以请求地址
//    统一走 getBaseUrl()：手机上「我的 → 服务地址」设的覆盖值优先，
//    换 WiFi 后在手机上改一行即可继续演示。
// 5. 覆盖值跨冷启动、跨重编译都保留。电脑换 WiFi 后 config.BASE_URL 会被
//    启动脚本自动写回新 IP，但手机上那条旧覆盖值优先级更高，于是请求一直
//    静默打向旧 IP 直到超时：控制台刷屏、页面只说「暂不可达」。见下方自愈。
//
// ## 服务地址自愈
//
// 网络层失败且当前用的是覆盖值时：探测代码包里的默认地址 → 通了就丢弃旧
// 覆盖值、弹一次提示、GET 用新地址重试一次；不通就冷却 15s，错误信息里
// 带上「重跑服务 + 重新编译」的动作。并发失败共享一次探测（_fallbackChecking），
// 写操作不自动重试（同第 2 条），探测本身走 doRequest 不会递归。
var config = require('../config.js');
var store = require('./store.js');

var _pending = 0;        // 全局请求计数（页面可据此显示顶部加载态）
var _reopening = null;   // 并发 401 的唯一重开 Promise
var _sessionNotified = false;  // 重试成功只提示一次，避免轮询页反复弹窗

// 自愈相关状态（模块级，整个小程序生命周期共享）
var _fallbackChecking = null;    // 并发失败共享的探测 Promise
var _fallbackCooldownUntil = 0;  // 探测失败后的冷却截止时刻，避免每轮轮询都探
var _fallbackNotified = false;   // 地址切换提示整个 App 生命周期只弹一次
var _baseUrlListeners = [];      // 地址变更订阅（「我的」页要同步显示）
var FALLBACK_COOLDOWN_MS = 15000;

function pendingCount() { return _pending; }

// -------------------------------------------------------------- 服务地址
// 覆盖值存在 storage 里，跨冷启动保留；留空/非法时回落到编译进包的默认值。
function getBaseUrl() {
  var override = '';
  try { override = wx.getStorageSync(config.KEYS.BASE_URL_OVERRIDE) || ''; }
  catch (e) { /* storage 不可用就用默认值 */ }
  return override || config.BASE_URL;
}

function setBaseUrl(url) {
  wx.setStorageSync(config.KEYS.BASE_URL_OVERRIDE, url);
  // 手动改过地址后，允许下一次自愈再提示一次，并立刻解除探测冷却
  _fallbackNotified = false;
  _fallbackCooldownUntil = 0;
  notifyBaseUrlChanged(url);
}

function clearBaseUrl() {
  wx.removeStorageSync(config.KEYS.BASE_URL_OVERRIDE);
  _fallbackCooldownUntil = 0;
  notifyBaseUrlChanged(config.BASE_URL);
}

function hasBaseUrlOverride() {
  try { return !!wx.getStorageSync(config.KEYS.BASE_URL_OVERRIDE); }
  catch (e) { return false; }
}

// 地址变更时通知页面刷新显示（返回退订函数，页面 onUnload 里调）。
function onBaseUrlChanged(fn) {
  if (typeof fn !== 'function' || _baseUrlListeners.indexOf(fn) >= 0) {
    return function () {};
  }
  _baseUrlListeners.push(fn);
  return function () {
    var i = _baseUrlListeners.indexOf(fn);
    if (i >= 0) { _baseUrlListeners.splice(i, 1); }
  };
}

function notifyBaseUrlChanged(url) {
  try {
    var app = getApp();
    if (app && app.globalData) { app.globalData.baseUrl = url; }
  } catch (e) { /* app 尚未初始化时不强求 */ }
  _baseUrlListeners.slice().forEach(function (fn) {
    try { fn(url); } catch (e) { /* 单个页面回调出错不影响其它页面 */ }
  });
}

// 只接受 http(s)://host[:port]，不带路径：request 会直接拼 /api/... 路径。
function normalizeBaseUrl(input) {
  var url = String(input || '').trim().replace(/\/+$/, '');
  return /^https?:\/\/[^\s/]+$/.test(url) ? url : null;
}

// ---------------------------------------------------------- 底层单次请求
// 不含任何重试/自愈逻辑：request() 在它外面编排，探测也直接用它（不递归）。
// opts.silent 的请求不计入 _pending，避免后台探测让页面顶部 loading 闪一下。
function doRequest(base, method, path, data, opts) {
  if (!opts.silent) { _pending += 1; }
  return new Promise(function (resolve, reject) {
    wx.request({
      url: base + path,
      method: method,
      data: data || {},
      timeout: opts.timeout || config.REQUEST_TIMEOUT_MS,
      success: function (res) {
        if (!opts.silent) { _pending -= 1; }
        // 约定：后端全部 {ok: true, data: ...} / {ok: false, detail, status}
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve((res.data && res.data.data) || {});
          return;
        }
        var detail = (res.data && res.data.detail) || '';
        var err = new Error(detail || ('HTTP ' + res.statusCode));
        err.status = res.statusCode;
        err.detail = detail;
        err.baseUrl = base;
        reject(err);
      },
      fail: function (e) {
        if (!opts.silent) { _pending -= 1; }
        // 「网络不可用」是最没用的报错：不说连的是哪个地址，也不说怎么修。
        // 现场 90% 是后端没起（8848 无监听）或还在打换 WiFi 前的旧 IP。
        var err = new Error(
          '连不上后厨服务 ' + base +
          '；请确认电脑上的服务已启动（tools/start_miniapp_server.bat）。' +
          '若电脑 IP 变了，重跑服务后还要在开发者工具点「编译」（真机重新扫码）；' +
          '真机也可在「我的 → 服务地址」直接改成当前局域网 IP'
        );
        err.status = 0;
        err.detail = e.errMsg || '';
        err.baseUrl = base;
        reject(err);
      }
    });
  });
}

// 覆盖值连不上时，探测一次代码包里的默认地址；通了就丢弃旧覆盖值。
// 探测接口用匿名只读的 /api/customer/menu，无副作用。
function tryFallbackBaseUrl(stale) {
  if (_fallbackChecking) { return _fallbackChecking; }
  var candidate = config.BASE_URL;
  if (!candidate || candidate === stale || Date.now() < _fallbackCooldownUntil) {
    return Promise.reject(new Error('no usable fallback'));
  }
  var probe = doRequest(candidate, 'GET', '/api/customer/menu', {},
                        { timeout: config.PROBE_TIMEOUT_MS, silent: true });
  _fallbackChecking = probe.then(
    function () {
      _fallbackChecking = null;
      clearBaseUrl();                     // 旧覆盖值作废，后续请求都走新地址
      if (!_fallbackNotified) {
        _fallbackNotified = true;
        wx.showModal({
          title: '服务地址已自动修正',
          content: '原来保存的 ' + stale + ' 已连不上，已切回 ' + candidate +
                   '。若这个地址仍是旧 IP，请在电脑重新运行 start_miniapp_server.bat，' +
                   '并在微信开发者工具点「编译」（真机需重新扫码预览）。',
          showCancel: false
        });
      }
      return candidate;
    },
    function (err) {
      _fallbackChecking = null;
      _fallbackCooldownUntil = Date.now() + FALLBACK_COOLDOWN_MS;
      throw err;
    }
  );
  return _fallbackChecking;
}

// 对外请求：网络层失败 → 尝试自愈 → GET 用新地址重试一次。
function request(method, path, data, options) {
  var opts = options || {};
  var base = getBaseUrl();
  return doRequest(base, method, path, data, opts).catch(function (err) {
    // 只有「网络连不上 + 未显式禁用自愈」才值得自愈；
    // HTTP 4xx/5xx 说明地址是对的，走原错误处理（401 由 authed 处理）。
    if (err.status !== 0 || opts.noFallback) { throw err; }
    // 等待期间已有别的请求把地址修好（或用户手动改了）：直接换新地址重试，
    // 不再重复探测。2s/4s 双轮询并发时很常见。
    var current = getBaseUrl();
    if (current !== base) {
      if (method !== 'GET') { err.baseUrl = current; throw err; }
      return doRequest(current, method, path, data, opts);
    }
    if (!hasBaseUrlOverride()) { throw err; }
    return tryFallbackBaseUrl(base).then(
      function (newBase) {
        if (method !== 'GET') {
          // 写操作绝不自动重试（可能重复下单）；地址已切好，请用户重试本次操作
          err.message += '；已自动切到 ' + newBase + '，请重试本次操作';
          err.baseUrl = newBase;
          throw err;
        }
        return doRequest(newBase, method, path, data, opts);
      },
      function () {
        // 默认地址也不通（或在冷却期）：保留原始地址，补一句最准的排错方向
        err.message += config.BASE_URL === base
          ? '；代码包里的地址也是 ' + base + '，请重跑服务并重新编译代码包'
          : '；代码包地址 ' + config.BASE_URL + ' 也不通，请重跑服务并重新编译代码包';
        throw err;
      }
    );
  });
}

function getToken() {
  return wx.getStorageSync(config.KEYS.TOKEN) || '';
}

function setToken(token) {
  wx.setStorageSync(config.KEYS.TOKEN, token);
}

function clearToken() {
  wx.removeStorageSync(config.KEYS.TOKEN);
}

// 开桌：没有令牌或令牌失效（401）时调用。
// 带上本机桌号，服务端才好生成稳定的顾客编号（T03-XXXX）并落到订单上；
// 不带桌号会被当散客（G-XXXX），后厨屏就少了「送到哪桌」的信息。
function openSession(tableId) {
  var table = tableId === undefined || tableId === null ? store.getTable() : tableId;
  // guest_key 跨开桌不变，服务端据此聚合顾客习惯；tags 是用户自报的忌口标签
  return request('POST', '/api/customer/session', {
    table_id: table,
    guest_key: store.getGuestKey(),
    tags: store.getGuestTags()
  })
    .then(function (data) {
      setToken(data.customer_token);
      store.setCustomerRef(data.customer_ref || '');
      return data.customer_token;
    });
}

// 带锁的重开桌：多个并发 401 只发一次 session 请求
function reopenSession() {
  if (_reopening) { return _reopening; }
  clearToken();
  _reopening = openSession().then(function (token) {
    _reopening = null;
    // 401 在 Console 里看着吓人，但恢复是自动的。弹一次提示，
    // 让演示者知道「页面数据回来了」而不是以为只是缓存没刷。
    if (!_sessionNotified) {
      _sessionNotified = true;
      wx.showToast({ title: '已重新开桌，继续演示', icon: 'none' });
    }
    return token;
  }).catch(function (err) {
    _reopening = null;
    throw err;
  });
  return _reopening;
}

// 带令牌的请求；401 → 自动重开桌一次再重试读操作
function authed(method, path, data, options) {
  var opts = options || {};
  function send() {
    var payload = Object.assign({ customer_token: getToken() }, data || {});
    return request(method, path, payload, opts).catch(function (err) {
      if (err.status !== 401) { throw err; }
      return reopenSession().then(function () {
        if (method !== 'GET') { throw err; }   // 写操作不重试，见文件头
        var retry = Object.assign({ customer_token: getToken() }, data || {});
        return request(method, path, retry, opts);
      });
    });
  }
  // 冷启动首屏：app.onLaunch 可能正在开桌。等它落盘再发请求，
  // 否则页面 onLoad 必然先吃一次 401，Console 里像故障其实是竞态。
  if (getToken()) { return send(); }
  var app = null;
  try { app = getApp(); } catch (e) { /* 尚未初始化 */ }
  var ready = app && app.globalData && app.globalData.sessionReady;
  if (!ready) { return send(); }
  return Promise.resolve(ready).catch(function () { return null; }).then(send);
}

// 「我的」页的服务连通性探测：匿名、只读、无副作用
function probe() {
  return request('GET', '/api/customer/menu', {},
                 { timeout: config.PROBE_TIMEOUT_MS });
}

module.exports = {
  request: request,
  authed: authed,
  openSession: openSession,
  reopenSession: reopenSession,
  probe: probe,
  pendingCount: pendingCount,
  getToken: getToken,
  setToken: setToken,
  clearToken: clearToken,
  getBaseUrl: getBaseUrl,
  setBaseUrl: setBaseUrl,
  clearBaseUrl: clearBaseUrl,
  hasBaseUrlOverride: hasBaseUrlOverride,
  onBaseUrlChanged: onBaseUrlChanged,
  normalizeBaseUrl: normalizeBaseUrl,
  getCustomerRef: function () { return store.getCustomerRef(); },
  getGuestKey: function () { return store.getGuestKey(); }
};
