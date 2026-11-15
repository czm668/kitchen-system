// wx.request 封装 + 顾客令牌管理。
//
// ## 令牌放哪
//
// `wx.setStorageSync('customer_token')`。顾客通道的令牌**不是登录态**：
// 不存在过期刷新的问题（服务端重启即失效，届时统一弹「请重新开桌」）。
// 401 时清掉本地令牌并重开桌，这是唯一的恢复路径。
//
// ## 三个容易踩的坑（都是实测踩出来的）
//
// 1. 并发 401：订单页 2s 轮询 + 通知页 4s 轮询同时失效，会同时触发多次
//    openSession，后写的 token 把先发的请求作废 → 必须加重开锁（_reopening）。
// 2. 写操作禁止 401 自动重试：下单重试可能造成重复下单（旧令牌的单已经
//    不属于新令牌），只有 GET 能安全重试。
// 3. 网络层失败（status=0）绝不能和 401 混为一谈：前者是服务没起，
//    提示里要带 BASE_URL 和修复动作，后者才该走重开桌。
var config = require('../config.js');
var store = require('./store.js');

var _pending = 0;        // 全局请求计数（页面可据此显示顶部加载态）
var _reopening = null;   // 并发 401 的唯一重开 Promise

function pendingCount() { return _pending; }

function request(method, path, data, options) {
  var opts = options || {};
  _pending += 1;
  return new Promise(function (resolve, reject) {
    wx.request({
      url: config.BASE_URL + path,
      method: method,
      data: data || {},
      timeout: opts.timeout || config.REQUEST_TIMEOUT_MS,
      success: function (res) {
        _pending -= 1;
        // 约定：后端全部 {ok: true, data: ...} / {ok: false, detail, status}
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve((res.data && res.data.data) || {});
          return;
        }
        var detail = (res.data && res.data.detail) || '';
        var err = new Error(detail || ('HTTP ' + res.statusCode));
        err.status = res.statusCode;
        err.detail = detail;
        reject(err);
      },
      fail: function (e) {
        _pending -= 1;
        // 「网络不可用」是最没用的报错：不说连的是哪个地址，也不说怎么修。
        // 现场 90% 是后端没起（8848 无监听），把地址和修复动作直接写进提示。
        var err = new Error(
          '连不上后厨服务 ' + config.BASE_URL +
          '；请确认电脑上的服务已启动（tools/start_miniapp_server.bat），' +
          '真机还需把 config.js 的 BASE_URL 改成电脑的局域网 IP'
        );
        err.status = 0;
        err.detail = e.errMsg || '';
        err.baseUrl = config.BASE_URL;
        reject(err);
      }
    });
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
    return token;
  }).catch(function (err) {
    _reopening = null;
    throw err;
  });
  return _reopening;
}

// 带令牌的请求；401 → 自动重开桌一次再重试读操作
function authed(method, path, data, options) {
  var payload = Object.assign({ customer_token: getToken() }, data || {});
  return request(method, path, payload, options).catch(function (err) {
    if (err.status !== 401) { throw err; }
    return reopenSession().then(function () {
      if (method !== 'GET') { throw err; }   // 写操作不重试，见文件头
      var retry = Object.assign({ customer_token: getToken() }, data || {});
      return request(method, path, retry, options);
    });
  });
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
  getCustomerRef: function () { return store.getCustomerRef(); },
  getGuestKey: function () { return store.getGuestKey(); }
};
