/**
 * 数值/时间格式化。**所有展示口径都收在这一个文件**。
 *
 * 为什么值得单独抽一层：同一个温度，订单页想显示 `78.4℃`，看板想要 `78`，
 * 曲线图的 tooltip 想要 `78.40`。如果每页各写一遍 `toFixed`，一定会出现
 * 「两个页面同一个读数不一样」——评审第一眼就会问「哪个是真的」。
 */

const DASH = '—'

/** null / undefined / NaN / 空串 一律显示破折号，**不显示 0**。 */
export function isBlank(value) {
  return value === null || value === undefined || value === '' ||
    (typeof value === 'number' && Number.isNaN(value))
}

/**
 * 数值格式化。
 * @param {*} value
 * @param {{digits?: number, unit?: string, fallback?: string, sign?: boolean}} opts
 */
export function fmt(value, opts = {}) {
  const { digits = 1, unit = '', fallback = DASH, sign = false } = opts
  if (isBlank(value)) return fallback
  const num = Number(value)
  if (!Number.isFinite(num)) return fallback
  const text = num.toFixed(digits)
  const body = sign && num > 0 ? `+${text}` : text
  return unit ? `${body}${unit}` : body
}

export function fmtInt(value, opts = {}) {
  return fmt(value, { digits: 0, ...opts })
}

export function fmtPct(value, opts = {}) {
  return fmt(value, { digits: 1, unit: '%', ...opts })
}

/** 秒 → 「1 小时 20 分」/「3 分 12 秒」/「8 秒」。 */
export function fmtDuration(seconds) {
  if (isBlank(seconds)) return DASH
  const total = Math.max(0, Number(seconds))
  if (!Number.isFinite(total)) return DASH
  if (total < 1) return `${(total * 1000).toFixed(0)} 毫秒`
  if (total < 60) return `${total.toFixed(0)} 秒`
  if (total < 3600) {
    const min = Math.floor(total / 60)
    const sec = Math.round(total % 60)
    return sec ? `${min} 分 ${sec} 秒` : `${min} 分`
  }
  const hours = Math.floor(total / 3600)
  const min = Math.round((total % 3600) / 60)
  /* 超过一天改用「天」：传感器离线久了的工单 age 可能是几万小时，
     `498657 小时 48 分` 这种串在表格里会折成三行，把行高撑到看不下去。 */
  if (total >= 86400) {
    const days = Math.floor(total / 86400)
    if (days >= 30) return `${days} 天`
    const restHour = Math.round((total % 86400) / 3600)
    return restHour ? `${days} 天 ${restHour} 小时` : `${days} 天`
  }
  return min ? `${hours} 小时 ${min} 分` : `${hours} 小时`
}

/** ISO 字符串或 epoch 秒 → `HH:MM:SS`。 */
export function fmtTime(value, { seconds = true } = {}) {
  const date = toDate(value)
  if (!date) return DASH
  const pad = (n) => String(n).padStart(2, '0')
  const head = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return seconds ? `${head}:${pad(date.getSeconds())}` : head
}

export function fmtDateTime(value) {
  const date = toDate(value)
  if (!date) return DASH
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 「已经过去多久」。用于「12 秒前开的单」这种判断。 */
export function fmtAgo(value) {
  const date = toDate(value)
  if (!date) return DASH
  const diff = (Date.now() - date.getTime()) / 1000
  if (diff < 5) return '刚刚'
  if (diff < 60) return `${diff.toFixed(0)} 秒前`
  if (diff < 3600) return `${(diff / 60).toFixed(0)} 分钟前`
  if (diff < 86400) return `${(diff / 3600).toFixed(0)} 小时前`
  return `${(diff / 86400).toFixed(0)} 天前`
}

/** 倒计时：正数 = 还剩，负数 = 已超。 */
export function fmtCountdown(iso) {
  const date = toDate(iso)
  if (!date) return DASH
  const diff = (date.getTime() - Date.now()) / 1000
  return diff >= 0 ? `剩 ${fmtDuration(diff)}` : `超 ${fmtDuration(-diff)}`
}

function toDate(value) {
  if (isBlank(value)) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    // 后端的时间戳是 epoch 秒；毫秒级会是 13 位，顺手兼容
    return new Date(value > 1e12 ? value : value * 1000)
  }
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/* ------------------------------------------------------------------ 状态词表 */

/** 订单/子项状态 → 中文 + 语义色（`tag` 的类名）。 */
export const ITEM_STATE = {
  pending: { label: '待派单', tone: 'plain' },
  dispatched: { label: '已派单', tone: 'info' },
  cooking: { label: '烹饪中', tone: 'warn' },
  ready: { label: '待出餐', tone: 'ok' },
  served: { label: '已上桌', tone: 'ok' },
  cancelled: { label: '已退菜', tone: 'danger' },
}

export const ORDER_STATE = {
  created: { label: '已创建', tone: 'plain' },
  queued: { label: '排队中', tone: 'info' },
  cooking: { label: '制作中', tone: 'warn' },
  ready: { label: '可出餐', tone: 'ok' },
  served: { label: '已完成', tone: 'ok' },
  cancelled: { label: '已取消', tone: 'danger' },
}

export const STATION_STATE = {
  idle: { label: '空闲', tone: 'plain' },
  reserved: { label: '已预约', tone: 'info' },
  busy: { label: '占用中', tone: 'warn' },
}

export const CHANNEL_STATE = {
  normal: { label: '正常', tone: 'ok' },
  latched: { label: '已锁定', tone: 'danger' },
  releasing: { label: '恢复中', tone: 'warn' },
  manual_reset_required: { label: '待人工复位', tone: 'danger' },
  degraded: { label: '降级', tone: 'warn' },
  signal: { label: '已触发', tone: 'info' },
}

export const TICKET_STATE = {
  open: { label: '待处理', tone: 'danger' },
  acknowledged: { label: '处理中', tone: 'warn' },
  running: { label: '进行中', tone: 'info' },
  closed: { label: '已关闭', tone: 'plain' },
}

export function describe(table, key, fallbackLabel) {
  return table[key] || { label: fallbackLabel || key || DASH, tone: 'plain' }
}

/** 遥测分组的中文名（环境页按组折叠用）。 */
export const METRIC_GROUP = {
  safety: '安全类',
  env: '环境类',
  cold: '冷链类',
  equipment: '设备类',
  process: '工艺类',
}
