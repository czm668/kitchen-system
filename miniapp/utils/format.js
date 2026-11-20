// 展示层格式化：WXML 不能调用 JS 函数，所有文案/色彩/进度都在 js 里算好再绑定。
//
// 口径来源：src/kitchen/api/customer.py 的 item.state / eta_state，
// 状态机见 src/kitchen/process/models.py 的 ItemState。字段名一字不改，
// 只做「状态 → 中文 + 阶段 + 色调」的映射，避免前后端各写一套。

var STAGES = ['已受理', '排队中', '已派单', '制作中', '已出锅', '已上桌'];

// 菜品图映射（tools/menu_assets.py 生成）。有 code 就有图，没有就走无图版式。
var dishImages = require('./dish_images.js');

var STATE_MAP = {
  created:    { label: '已受理', stage: 0, tone: 'plain' },
  pending:    { label: '排队中', stage: 1, tone: 'info' },
  queued:     { label: '排队中', stage: 1, tone: 'info' },
  dispatched: { label: '已派单', stage: 2, tone: 'info' },
  cooking:    { label: '制作中', stage: 3, tone: 'warn' },
  cooked:     { label: '已出锅', stage: 4, tone: 'warn' },
  served:     { label: '已上桌', stage: 5, tone: 'ok' },
  done:       { label: '已完成', stage: 5, tone: 'ok' },
  completed:  { label: '已完成', stage: 5, tone: 'ok' },
  cancelled:  { label: '已退菜', stage: 0, tone: 'danger' }
};

function stateOf(state) {
  return STATE_MAP[state] || { label: state || '处理中', stage: 0, tone: 'plain' };
}

// 单个菜品的状态 → 可绑定字段（不改动原对象，避免 wx 的 setData 引用问题）
function decorateItem(item) {
  var meta = stateOf(item.state);
  var copy = {};
  Object.keys(item).forEach(function (k) { copy[k] = item[k]; });
  // 配图走 dish_code（后端查单接口给的）；老接口没这个字段时留空，WXML 退化成纯文字行
  copy.image = dishImages[item.dish_code] || '';
  copy.state_label = meta.label;
  copy.state_tone = meta.tone;
  copy.stage = meta.stage;
  copy.stage_total = STAGES.length;
  copy.stages = STAGES.map(function (name, idx) {
    return { name: name, reached: idx <= meta.stage, current: idx === meta.stage };
  });
  copy.percent = Math.round((meta.stage + 1) * 100 / STAGES.length);
  copy.can_urge = item.state !== 'served' && item.state !== 'done' &&
                  item.state !== 'completed' && item.state !== 'cancelled';
  copy.eta_text = item.eta_state === 'done' ? '已上桌' :
                  item.eta_state === 'soon' ? '快好了' : '制作中';
  copy.eta_tone = item.eta_state === 'done' ? 'ok' :
                  item.eta_state === 'soon' ? 'warn' : 'plain';
  return copy;
}

// 整单进度：取所有菜品阶段的平均值；全单完成才算 100%
function decorateOrder(order) {
  var copy = {};
  Object.keys(order).forEach(function (k) { copy[k] = order[k]; });
  copy.items = (order.items || []).map(decorateItem);
  var total = 0;
  copy.items.forEach(function (item) { total += item.percent; });
  copy.percent = copy.items.length ? Math.round(total / copy.items.length) : 0;
  // 全部菜品都不再可催（已上桌 / 已退菜）即整单结束；
  // cancelled 的 can_urge 也是 false，别把它排除掉，否则订单会永远挂在「进行中」
  copy.finished = copy.items.length > 0 && copy.items.every(function (item) {
    return !item.can_urge;
  });
  copy.created_text = timeText(order.created_at);
  copy.due_text = timeText(order.due_at);
  copy.item_count = copy.items.reduce(function (sum, item) {
    return sum + (item.qty || 1);
  }, 0);
  return copy;
}

// 2026-11-14T21:30:00 → 11-14 21:30（后端 isoformat(timespec='seconds')）
function timeText(iso) {
  if (!iso) { return '—'; }
  var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(iso));
  return m ? (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : String(iso);
}

function money(value) {
  var n = Number(value || 0);
  return n.toFixed(2);
}

// 按菜品编码取包内图；没有图返回空串，调用方用占位块兜底
function imageOf(code) {
  return (code && dishImages[code]) || '';
}

function tokenShort(token) {
  var text = String(token || '');
  return text ? text.slice(0, 8) + '…' + text.slice(-4) : '未开桌';
}

// 通知分类文案（kind 见 CustomerService._CUSTOMER_KINDS）
function kindLabel(kind) {
  var map = {
    order: '订单', cooking: '出餐', customer: '顾客',
    ticket: '异常', param: '系统'
  };
  return map[kind] || '消息';
}

module.exports = {
  STAGES: STAGES,
  stateOf: stateOf,
  decorateItem: decorateItem,
  decorateOrder: decorateOrder,
  imageOf: imageOf,
  timeText: timeText,
  money: money,
  tokenShort: tokenShort,
  kindLabel: kindLabel
};
