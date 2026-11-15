/**
 * HTTP 客户端：统一令牌、超时、错误语义。
 *
 * ## 令牌放 sessionStorage 而不是 localStorage
 *
 * | | localStorage | sessionStorage |
 * |---|---|---|
 * | XSS 能读 | 能 | 能 |
 * | 关标签后残留 | 会 | 不会 |
 * | 多标签页共享 | 会 | 不会 |
 *
 * XSS 两者都防不住（真正的防线是服务端不返回可执行脚本 + CSP），但管理端是
 * 「一个标签页干完就关」的场景，sessionStorage 少一个「忘关电脑、第二天别人
 * 打开浏览器还是登录态」的风险。刷新页面仍然保留（同标签页），不影响使用。
 *
 * ## 为什么所有错误都收敛成 `ApiError`
 *
 * 页面里如果每处都写 `try/catch` + `if (res.status === 401)`，会有 9 处判断、
 * 9 种写法。统一在这里处理：401 直接踢回登录页，其余抛一个带 `status` 和
 * `detail` 的异常，页面只负责把它显示出来。
 */

const TOKEN_KEY = 'kitchen.token'
const TIMEOUT_MS = 12000

export class ApiError extends Error {
  constructor(status, detail, code) {
    super(detail || `请求失败（${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = code || ''
  }
}

/** 401 时广播出去，由 auth store 决定踢回登录页——client 不认识路由。 */
const listeners = new Set()
export function onUnauthorized(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    // 隐私模式下 sessionStorage 可能抛异常，退化成「不记住令牌」
    return ''
  }
}

export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 忽略：见上 */
  }
}

/**
 * 发一个请求。`body` 为 undefined 时是 GET。
 * 返回解包后的 `payload.data`（后端固定 `{ok, data}` 结构）。
 */
export async function request(method, path, body) {
  const controller = new AbortController()
  // 超时必须自己实现：`fetch` 没有内置超时，网络半死时请求会挂到浏览器放弃为止，
  // 页面表现就是「一直在转圈」
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  const headers = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (err) {
    clearTimeout(timer)
    if (err.name === 'AbortError') {
      throw new ApiError(0, `请求超时（${TIMEOUT_MS / 1000} 秒），设备可能不在线`)
    }
    throw new ApiError(0, '无法连接到服务端，请检查网络或后端是否启动')
  }
  clearTimeout(timer)

  let payload = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      // 非 JSON 响应（比如代理返回的 HTML 错误页）：保留原文前 200 字，
      // 否则排查时只知道「解析失败」，不知道服务端到底说了什么
      throw new ApiError(response.status, `服务端返回了非 JSON 内容：${text.slice(0, 200)}`)
    }
  }

  if (response.status === 401) {
    setToken('')
    listeners.forEach((fn) => fn())
    throw new ApiError(401, payload?.detail || '登录已过期，请重新登录', payload?.error)
  }
  if (!response.ok || payload?.ok === false) {
    throw new ApiError(
      response.status,
      payload?.detail || `请求失败（${response.status}）`,
      payload?.error,
    )
  }
  return payload ? payload.data : null
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  put: (path, body = {}) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
}
