/**
 * 轻量提示条。
 *
 * 不引 toast 库：一个队列 + 一个 `<Teleport>` 就够。关键是**成功的操作也要给
 * 反馈**——「点了没反应」在现场会被理解成「没生效」，于是有人连点五次，
 * 把一个复位动作发出五遍。
 */
import { reactive } from 'vue'

export const toasts = reactive([])

let seq = 0
const MAX = 4

/**
 * @param {string} message
 * @param {'ok'|'warn'|'danger'|'info'} tone
 * @param {number} ttl 毫秒；0 表示手动关闭
 */
export function toast(message, tone = 'info', ttl = 3400) {
  const id = ++seq
  toasts.push({ id, message: String(message || ''), tone })
  // 上限保护：一次批量操作可能连出十几条，全堆在右下角会挡住按钮
  while (toasts.length > MAX) toasts.shift()
  if (ttl > 0) {
    setTimeout(() => dismiss(id), ttl)
  }
  return id
}

export function dismiss(id) {
  const index = toasts.findIndex((item) => item.id === id)
  if (index >= 0) toasts.splice(index, 1)
}

/** 把异常统一显示成一条红色提示，附上后端给的 `detail`。 */
export function toastError(err, fallback = '操作失败') {
  const detail = err?.message || (typeof err === 'string' ? err : '') || fallback
  return toast(detail, 'danger', 5200)
}

export const toastOk = (msg) => toast(msg, 'ok')
export const toastWarn = (msg) => toast(msg, 'warn', 4400)
