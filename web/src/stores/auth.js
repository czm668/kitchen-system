/**
 * 登录态。手写 store（不引 Pinia）：一个 token + 一个 session，用 `reactive`
 * 就够，多一层依赖反而要在 `main.js` 里多装一次插件。
 */
import { reactive } from 'vue'
import { api, getToken, onUnauthorized, setToken } from '../api/client'

export const auth = reactive({
  token: getToken(),
  session: null,
  /** 是否已经尝试过用 token 换 session（避免刷新页面时闪一下登录框） */
  ready: false,
  loading: false,
  error: '',
})

// 401 统一踢回登录：由 client 广播，这里负责清状态。
// 页面不各自处理 401，否则「有 9 个页面就有 9 处忘记处理」。
onUnauthorized(() => {
  auth.token = ''
  auth.session = null
  auth.error = '登录已过期，请重新登录'
})

export function can(permission) {
  const list = auth.session?.permissions || []
  return list.includes(permission)
}

/** 当前会话的显示名与角色中文名。 */
export function whoami() {
  if (!auth.session) return { name: '', role: '', roleLabel: '' }
  return {
    name: auth.session.subject,
    role: auth.session.role,
    roleLabel: ROLE_LABEL[auth.session.role] || auth.session.role,
  }
}

const ROLE_LABEL = {
  viewer: '只读观察',
  operator: '操作员',
  lead: '后厨组长',
  manager: '系统管理员',
}

export async function login(username, password) {
  auth.loading = true
  auth.error = ''
  try {
    const data = await api.post('/api/auth/login', { username, password })
    setToken(data.token)
    auth.token = data.token
    auth.session = data.session
    auth.ready = true
    return data
  } catch (err) {
    auth.error = err.message || '登录失败'
    throw err
  } finally {
    auth.loading = false
  }
}

export async function logout() {
  try {
    await api.post('/api/auth/logout', {})
  } catch {
    /* 服务端只是留一条审计，失败了也要清本地状态 */
  }
  setToken('')
  auth.token = ''
  auth.session = null
}

/**
 * 用已存的令牌换一次 session。刷新页面时调用。
 *
 * **必须真的请求一次**而不是「有 token 就认为已登录」：令牌可能已过期，
 * 也可能因为服务端换了密钥而失效。前者会让页面先渲染再集体 401，
 * 那个闪烁比多一次请求难看得多。
 */
export async function bootstrap() {
  if (!auth.token) {
    auth.ready = true
    return false
  }
  try {
    const data = await api.get('/api/auth/me')
    auth.session = data.session
    return true
  } catch {
    setToken('')
    auth.token = ''
    auth.session = null
    return false
  } finally {
    auth.ready = true
  }
}
