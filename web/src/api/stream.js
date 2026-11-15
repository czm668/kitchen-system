/**
 * 实时通道：SSE 优先，失败降级为轮询。
 *
 * ## 为什么必须留一条轮询的退路
 *
 * 现场 wifi 是最大的不确定项。`EventSource` 在网络抖时会不停重连，而重连期间
 * **一个事件都收不到**——页面上的曲线就那么停着，操作员以为设备坏了。更糟的是
 * 有些代理会把 `text/event-stream` 缓冲住，连接看起来是通的，数据永远不来。
 *
 * 所以策略是：
 * 1. 先试 SSE；
 * 2. `onerror` 之后计数，连续 3 次失败 → 判定 SSE 不可用，切轮询；
 * 3. 轮询期间每 60 秒探一次 SSE，能用就切回去（省流量、延迟更低）。
 *
 * ## 为什么 `onerror` 不直接判定失败
 *
 * `EventSource` 在服务端做「连接轮换」（本服务端 5 分钟主动断开一次）时也会
 * 触发 `onerror`，然后自动重连成功。一次就降级会把正常的轮换当成故障。
 */
import { getToken } from './client'

const MAX_FAILURES = 3
const POLL_MS = 5000
const PROBE_MS = 60000

export function createStream({ onEvent, onState, onPoll }) {
  let source = null
  let failures = 0
  let polling = false
  let pollTimer = null
  let probeTimer = null
  let closed = false

  function setState(state, detail) {
    onState?.(state, detail || '')
  }

  function startPolling(reason) {
    if (polling) return
    polling = true
    teardownSource()
    setState('polling', reason)
    const tick = async () => {
      try {
        await onPoll?.()
      } catch {
        /* 轮询失败由 onPoll 自己处理提示，这里不叠加重试逻辑 */
      }
    }
    tick()
    pollTimer = setInterval(tick, POLL_MS)
    // 定期试探 SSE 是否恢复
    probeTimer = setInterval(() => {
      if (closed) return
      if (polling) start()
    }, PROBE_MS)
  }

  function stopPolling() {
    polling = false
    if (pollTimer) clearInterval(pollTimer)
    if (probeTimer) clearInterval(probeTimer)
    pollTimer = null
    probeTimer = null
  }

  function teardownSource() {
    if (source) {
      source.close()
      source = null
    }
  }

  function start() {
    if (closed) return
    teardownSource()
    const token = getToken()
    if (!token) {
      setState('offline', '未登录')
      return
    }
    // `EventSource` 不支持自定义请求头，令牌只能走查询串（服务端专门支持了这条）
    const url = `/api/stream?token=${encodeURIComponent(token)}`
    source = new EventSource(url)

    source.addEventListener('hello', (msg) => {
      failures = 0
      stopPolling()
      setState('live', '')
      onEvent?.({ kind: 'hello', ...safeParse(msg.data) })
    })

    // 业务事件：服务端把所有 kind 都当事件名发，这里统一接
    const kinds = ['rule', 'guard', 'order', 'station', 'ticket', 'channel',
      'network', 'param', 'admission', 'dish', 'inject', 'fault-escalate', 'bye']
    kinds.forEach((kind) => {
      source.addEventListener(kind, (msg) => {
        failures = 0
        const data = safeParse(msg.data)
        if (kind === 'bye') {
          // 服务端主动轮换连接：正常现象，立刻重连但不计失败
          teardownSource()
          setTimeout(start, 200)
          return
        }
        onEvent?.({ kind, ...data })
      })
    })

    source.onerror = () => {
      failures += 1
      if (failures >= MAX_FAILURES) {
        startPolling(`实时通道连续 ${failures} 次失败，已切轮询`)
      } else {
        setState('reconnecting', `第 ${failures} 次重连…`)
      }
    }
  }

  function stop() {
    closed = true
    teardownSource()
    stopPolling()
    setState('closed', '')
  }

  return { start, stop, get state() { return polling ? 'polling' : 'live' } }
}

function safeParse(text) {
  try {
    return JSON.parse(text)
  } catch {
    return { raw: text }
  }
}
