/**
 * 实时数据 store：一份全局面板快照 + 一路事件流。
 *
 * ## 为什么用「一个 overview 接口 + 事件流」而不是每个页面自己拉
 *
 * 现场 wifi 通常只有 2~5Mbps 且延迟抖动大。九个页面各打三四个请求，
 * 切一次页面就是十几次往返，页面会明显「一顿一顿」。所以：
 *
 * - `GET /api/overview` 一次给出九个页面都要的聚合数据（12 遥测 + 三通道 +
 *   降级 + 计数 + 保护状态），约 5KB；
 * - 页面各自需要的明细（订单列表、配方、人员）再单独拉，且**只在进入页面时拉一次**；
 * - 实时变化靠事件流，事件到达后**合并刷新**（debounce 400ms），而不是一条一拉。
 *
 * ## 事件为什么要有上限
 *
 * 后厨跑一整天，事件能有几万条。无上限的数组会让浏览器内存一路涨到卡死。
 * 保留最近 200 条足够「看一眼刚才发生了什么」，历史查询走工单/审计接口。
 */
import { computed, reactive } from 'vue'
import { api } from '../api/client'
import { createStream } from '../api/stream'

const MAX_EVENTS = 200
const REFRESH_DEBOUNCE_MS = 400

export const live = reactive({
  overview: null,
  events: [],
  connection: 'idle',       // idle | live | reconnecting | polling | offline | closed
  connectionNote: '',
  error: '',
  lastUpdate: 0,
  refreshCount: 0,
})

let stream = null
let refreshTimer = null
let inflight = false

/** 合并多次刷新请求：一个 400ms 窗口内只发一次。 */
function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    refresh({ silent: true })
  }, REFRESH_DEBOUNCE_MS)
}

export async function refresh({ silent = false } = {}) {
  if (inflight) return
  inflight = true
  try {
    const data = await api.get('/api/overview')
    live.overview = data
    live.lastUpdate = Date.now()
    live.refreshCount += 1
    live.error = ''
  } catch (err) {
    // 静默刷新失败不打断用户正在看的页面，只把错误挂到状态栏
    live.error = err.message || '刷新失败'
    if (!silent) throw err
  } finally {
    inflight = false
  }
}

function pushEvent(event) {
  live.events.unshift(event)
  if (live.events.length > MAX_EVENTS) live.events.length = MAX_EVENTS
  // 任何事件都可能改变聚合快照（新订单、通道变化、降级），统一合并刷新
  scheduleRefresh()
}

export function startLive() {
  if (stream) return
  stream = createStream({
    onEvent: (event) => pushEvent(event),
    onState: (state, note) => {
      live.connection = state
      live.connectionNote = note
    },
    onPoll: async () => {
      await refresh({ silent: true })
    },
  })
  stream.start()
  refresh({ silent: true }).catch(() => {})
}

export function stopLive() {
  if (stream) {
    stream.stop()
    stream = null
  }
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
  live.connection = 'closed'
  live.overview = null
  live.events.length = 0
}

/* ------------------------------------------------------------------ 派生 */

/** 降级报告（三端文案里的 web 一栏）。 */
export const degrade = computed(() => live.overview?.degrade || null)

/** A10-06：最近的演示信号注入（30 秒内算「注入中」）。全站角标只从这里取——
 * 事件流里 kind=inject 的帧就是服务端打过的 demo 标记，前端不另算一套。 */
export const lastInject = computed(() => {
  const row = live.events.find((e) => e.kind === 'inject')
  return row || null
})
export const injecting = computed(() => {
  const row = lastInject.value
  if (!row) return false
  return Date.now() - (row.ts || 0) * 1000 < 30_000
})

export const isDegraded = computed(() => {
  const report = degrade.value
  return !!report && report.severity !== 'normal'
})

/** 不可用能力清单：只列 `broken` 的，页面用来解释「哪些功能没了」。 */
export const brokenCapabilities = computed(() => {
  const caps = degrade.value?.capabilities || []
  return caps.filter((cap) => cap.state !== 'full')
})

/** 当前触发的保护（燃气/超温/长烧）。 */
export const trippedGuards = computed(
  () => live.overview?.guards?.tripped || [],
)

/** 三通道状态数组，带中文标签。 */
export const channels = computed(() => {
  const raw = live.overview?.channels || {}
  const labels = { CH1: '燃气阀', CH2: '排风', CH3: '工位电', GPIO_SIGNAL: '声光' }
  return Object.entries(raw).map(([name, body]) => ({
    name,
    label: labels[name] || name,
    state: body.state || 'normal',
    sources: body.sources || {},
    reviewers: body.reset_reviewers || [],
  }))
})

/** 12 遥测的当前值数组。 */
export const telemetry = computed(() => {
  const meta = live.overview?.telemetry || {}
  return Object.entries(meta).map(([id, body]) => ({ id, ...body }))
})

export const connectionText = computed(() => {
  switch (live.connection) {
    case 'live': return '实时通道已连接'
    case 'reconnecting': return live.connectionNote || '实时通道重连中'
    case 'polling': return '轮询模式（实时通道不可用）'
    case 'offline': return '未连接到设备'
    case 'closed': return '已断开'
    default: return '正在连接…'
  }
})
