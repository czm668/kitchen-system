// 跨页面本地状态：购物车、桌号、通知已读、溯源历史、订单详情中转。
//
// 为什么不用 globalData：购物车要跨「退出小程序再进来」保留（演示中途
// 切真机调试很常见），storage 是唯一能落盘的地方。订单详情不额外请求
// 接口（后端只有「订单列表」没有单笔详情），从列表带整单数据过来即可。
var config = require('../config.js');
var K = config.KEYS;

function read(key, fallback) {
  try {
    var value = wx.getStorageSync(key);
    return value === '' || value === null || value === undefined ? fallback : value;
  } catch (e) {
    return fallback;
  }
}

function write(key, value) {
  try { wx.setStorageSync(key, value); } catch (e) { /* 存储满不阻塞主流程 */ }
}

function remove(key) {
  try { wx.removeStorageSync(key); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------- 购物车
// 结构：[{code, name, price, qty, estimate_minutes, category}]
// 用数组而不是对象：WXML 的 wx:for 直接渲染，不用再做 Object.keys 转换。
function getCart() {
  var rows = read(K.CART, []);
  return Array.isArray(rows) ? rows : [];
}

function setCart(rows) {
  write(K.CART, rows || []);
  return rows || [];
}

function addToCart(dish) {
  var rows = getCart();
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].code === dish.code) {
      rows[i].qty += 1;
      return setCart(rows);
    }
  }
  rows.push({
    code: dish.code, name: dish.name, price: Number(dish.price || 0),
    qty: 1, category: dish.category || '其他',
    estimate_minutes: dish.estimate_minutes || 0
  });
  return setCart(rows);
}

function changeQty(code, delta) {
  var rows = getCart().map(function (row) {
    if (row.code === code) { row.qty += delta; }
    return row;
  }).filter(function (row) { return row.qty > 0; });
  return setCart(rows);
}

function removeFromCart(code) {
  return setCart(getCart().filter(function (row) { return row.code !== code; }));
}

function clearCart() {
  remove(K.CART);
  return [];
}

function cartSummary(rows) {
  var qty = 0, money = 0, minutes = 0;
  (rows || []).forEach(function (row) {
    qty += row.qty;
    money += row.qty * Number(row.price || 0);
    minutes = Math.max(minutes, Number(row.estimate_minutes || 0));
  });
  return { qty: qty, money: money, money_text: money.toFixed(2), minutes: minutes };
}

// 与菜单实时沽清对齐：菜单刷新后把已沽清项从购物车剔掉，避免下单才被 409 拒绝
function pruneSoldOut(dishes) {
  var soldOut = {};
  (dishes || []).forEach(function (dish) {
    if (dish.sold_out) { soldOut[dish.code] = true; }
  });
  var before = getCart();
  var after = before.filter(function (row) { return !soldOut[row.code]; });
  if (after.length !== before.length) { setCart(after); }
  return after;
}

// ---------------------------------------------------------------- 桌号
function getTable() {
  return Number(read(K.TABLE, config.DEFAULT_TABLE)) || config.DEFAULT_TABLE;
}

function setTable(table) {
  write(K.TABLE, Number(table));
}

// ---------------------------------------------------------------- 顾客编号
// 服务端下发的脱敏会话编号（T03-A1B2）。只用于展示与对单，不是登录态；
// 重新开桌会换一个，所以必须和 TOKEN 一起清。
function getCustomerRef() { return read(K.CUSTOMER_REF, ''); }
function setCustomerRef(ref) { write(K.CUSTOMER_REF, ref || ''); }

// ---------------------------------------------------------------- 匿名档案
// 设备级 guest_key：小程序首次启动生成并落盘，跨开桌不变。
// 服务端只把它当「聚合键」，查数据仍要同时校验当前令牌，见 api/customer.py
// 的 require_guest()——所以这个 key 泄露也不等于别人能看你的画像。
function getGuestKey() {
  var key = read(K.GUEST_KEY, '');
  if (!key) {
    key = 'g-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
    write(K.GUEST_KEY, key);
  }
  return key;
}

function getGuestTags() {
  var rows = read(K.GUEST_TAGS, []);
  return Array.isArray(rows) ? rows : [];
}

function setGuestTags(rows) {
  var clean = (rows || []).filter(function (t) {
    return config.GUEST_TAG_OPTIONS.indexOf(t) >= 0;
  });
  write(K.GUEST_TAGS, clean);
  return clean;
}

// ---------------------------------------------------------------- 通知已读
function getReadSeq() { return Number(read(K.READ_SEQ, 0)) || 0; }
function setReadSeq(seq) { write(K.READ_SEQ, Number(seq) || 0); }

// ---------------------------------------------------------------- 溯源历史
function getTraceHistory() {
  var rows = read(K.TRACE_HISTORY, []);
  return Array.isArray(rows) ? rows : [];
}

function pushTraceHistory(code, dish) {
  var rows = getTraceHistory().filter(function (row) { return row.code !== code; });
  rows.unshift({ code: code, dish: dish || '', time: Date.now() });
  return setCartLike(K.TRACE_HISTORY, rows.slice(0, 8));
}

function setCartLike(key, rows) { write(key, rows); return rows; }

function clearTraceHistory() { remove(K.TRACE_HISTORY); }

// ---------------------------------------------------------------- 详情中转
function setDetailOrder(order) { write(K.DETAIL_ORDER, order || null); }
function getDetailOrder() { return read(K.DETAIL_ORDER, null); }

// 重新开桌：只清「属于旧会话」的东西，购物车/桌号/溯源历史保留
function resetSession() {
  remove(K.TOKEN);
  remove(K.CUSTOMER_REF);
  remove(K.READ_SEQ);
  remove(K.DETAIL_ORDER);
  // 刻意保留 GUEST_KEY / GUEST_TAGS：画像与客诉历史跨开桌沉淀，
  // 清掉就等于每次开桌都是新顾客，习惯永远攒不起来。
}

module.exports = {
  getCart: getCart, setCart: setCart, addToCart: addToCart,
  changeQty: changeQty, removeFromCart: removeFromCart,
  clearCart: clearCart, cartSummary: cartSummary, pruneSoldOut: pruneSoldOut,
  getTable: getTable, setTable: setTable,
  getCustomerRef: getCustomerRef, setCustomerRef: setCustomerRef,
  getGuestKey: getGuestKey, getGuestTags: getGuestTags, setGuestTags: setGuestTags,
  getReadSeq: getReadSeq, setReadSeq: setReadSeq,
  getTraceHistory: getTraceHistory, pushTraceHistory: pushTraceHistory,
  clearTraceHistory: clearTraceHistory,
  setDetailOrder: setDetailOrder, getDetailOrder: getDetailOrder,
  resetSession: resetSession
};
