<script setup>
/**
 * 应用外壳：侧栏 + 顶栏 + 路由 + 会话。
 *
 * ## 为什么自己写 hash 路由（不引 vue-router）
 *
 * 8 个扁平页面、无嵌套、无路由守卫需求（只有一个「未登录就显示登录页」的判断）。
 * `vue-router` 会带来 30KB 和一个 `RouterView` 概念层，而这里 20 行就能覆盖。
 * 用 hash 而不是 history 模式还有个实际好处：**不需要服务端配 fallback**，
 * 双击 `dist/index.html` 用 `file://` 打开也能用（现场兜底演示很有用）。
 *
 * ## 为什么顶栏要显示服务器时间而不是本地时间
 *
 * 后厨的判据全部基于服务端时钟（档位切换、超时、留档 48 小时）。操作员看到
 * 的时间和设备判事的时间不一致时，「明明过了半小时为什么还没超时」就成了
 * 无法解释的问题。所以顶栏显示的是 `overview.server_time`，与设备同源。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './components/Icon.vue'
import DegradeBanner from './components/DegradeBanner.vue'
import ToastHost from './components/ToastHost.vue'
import LoginView from './views/LoginView.vue'
import KpiView from './views/KpiView.vue'
import OrdersView from './views/OrdersView.vue'
import StationsView from './views/StationsView.vue'
import EnvironmentView from './views/EnvironmentView.vue'
import RecipesView from './views/RecipesView.vue'
import ComplianceView from './views/ComplianceView.vue'
import ComplianceCredentialView from './views/ComplianceCredentialView.vue'
import TicketsView from './views/TicketsView.vue'
import GuestView from './views/GuestView.vue'
import FinanceView from './views/FinanceView.vue'
import AiView from './views/AiView.vue'
import ControlView from './views/ControlView.vue'
import { auth, bootstrap, can, login, logout, whoami } from './stores/auth'
import { live, startLive, stopLive, connectionText, trippedGuards, injecting, lastInject } from './stores/live'
import { fmtInt } from './utils/format'

/** 路由表。`perm` 为空表示只要登录就能看。 */
const ROUTES = [
  { key: 'kpi', path: '#/kpi', label: '总览', icon: 'chart', view: KpiView },
  { key: 'orders', path: '#/orders', label: '订单与调度', icon: 'orders', view: OrdersView },
  { key: 'stations', path: '#/stations', label: '工位看板', icon: 'gantt', view: StationsView },
  { key: 'environment', path: '#/environment', label: '环境与设备', icon: 'env', view: EnvironmentView },
  { key: 'recipes', path: '#/recipes', label: '配方与工艺', icon: 'recipe', view: RecipesView },
  { key: 'compliance', path: '#/compliance', label: '批次与资质', icon: 'shield', view: ComplianceView },
  { key: 'credential', path: '#/credential', label: '合规凭证', icon: 'shield', view: ComplianceCredentialView },
  { key: 'tickets', path: '#/tickets', label: '工单中心', icon: 'ticket', view: TicketsView },
  { key: 'guest', path: '#/guest', label: '客诉与画像', icon: 'chat', view: GuestView },
  { key: 'finance', path: '#/finance', label: '经营账本', icon: 'chart', view: FinanceView },
  { key: 'ai', path: '#/ai', label: '多融合 AI', icon: 'chat', view: AiView },
  { key: 'control', path: '#/control', label: '远程控制与参数', icon: 'sliders', view: ControlView },
]

const DEFAULT_ROUTE = ROUTES[0]
const activePath = ref(normalizeHash())
const booting = ref(true)
const theme = ref(localStorage.getItem('kitchen.theme') === 'dark' ? 'dark' : 'light')
const nowTick = ref(Date.now())

const active = computed(
  () => ROUTES.find((item) => item.path === activePath.value) || DEFAULT_ROUTE,
)
const me = computed(() => whoami())
const signedIn = computed(() => !!auth.session)

/** 顶栏时间：优先用服务端时间（与设备判据同源），没连上时退本地时间。 */
const clockText = computed(() => {
  const server = live.overview?.server_time
  if (server) {
    const date = new Date(server)
    if (!Number.isNaN(date.getTime())) {
      // 服务端时间只精确到秒，用本地流逝补足秒内的跳动
      const drift = Math.floor((nowTick.value - live.lastUpdate) / 1000)
      const shifted = new Date(date.getTime() + drift * 1000)
      return shifted.toTimeString().slice(0, 8)
    }
  }
  return new Date(nowTick.value).toTimeString().slice(0, 8)
})

const guardText = computed(() => {
  const list = trippedGuards.value
  if (!list.length) return ''
  const names = { 'guard:gas_surge': '燃气突变', 'guard:thermal_fan': '超温',
    'guard:flame_unattended': '长时无人' }
  return list.map((key) => names[key] || key).join(' / ')
})

function normalizeHash() {
  const raw = window.location.hash || ''
  const found = ROUTES.find((item) => item.path === raw)
  return found ? found.path : DEFAULT_ROUTE.path
}

function go(path) {
  if (window.location.hash === path) return
  window.location.hash = path
}

function onHashChange() {
  activePath.value = normalizeHash()
}

function applyTheme(value) {
  theme.value = value
  localStorage.setItem('kitchen.theme', value)
  const root = document.documentElement
  // 浅色是默认（米绿底）：不设属性；只有深色才挂 data-theme="dark"
  if (value === 'dark') root.setAttribute('data-theme', 'dark')
  else root.removeAttribute('data-theme')
}

function toggleTheme() {
  applyTheme(theme.value === 'dark' ? 'light' : 'dark')
}

async function onLogout() {
  stopLive()
  await logout()
}

let clockTimer = null

onMounted(async () => {
  applyTheme(theme.value)
  window.addEventListener('hashchange', onHashChange)
  clockTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
  // 开发/演示口子（仅限本机访问）：?autologin=1 自动登录演示账号，
  // ?theme=dark|light 覆盖主题——供无头浏览器截图、演示快进、自动化点检用。
  const params = new URLSearchParams(window.location.search)
  if (['127.0.0.1', 'localhost'].includes(window.location.hostname)) {
    const themeParam = params.get('theme')
    if (themeParam === 'dark' || themeParam === 'light') {
      localStorage.setItem('kitchen.theme', themeParam)
      applyTheme(themeParam)
    }
    if (params.has('autologin')) {
      try { await login('admin', 'kitchen2026') } catch { /* 失败就走正常登录页 */ }
    }
  }
  // bootstrap 失败也必须落骨架屏，否则启动页永远挂着（网络异常/令牌换失败都会走到这）
  try {
    await bootstrap()
  } catch (err) {
    console.error('启动校验失败:', err)
  } finally {
    booting.value = false
  }
  if (auth.session) {
    if (!window.location.hash) go(DEFAULT_ROUTE.path)
    startLive()
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', onHashChange)
  if (clockTimer) clearInterval(clockTimer)
  stopLive()
})

// 登录成功后起实时通道（登录页只负责拿 token，不碰数据层）
watch(signedIn, (signed) => {
  if (signed) {
    if (!window.location.hash) go(DEFAULT_ROUTE.path)
    startLive()
  } else {
    stopLive()
  }
})
</script>

<template>
  <div v-if="booting" class="boot-screen">
    <div class="spinner"></div>
    <p>正在校验登录态…</p>
  </div>

  <LoginView v-else-if="!signedIn" />

  <div v-else class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">
          <Icon name="brand" :size="19" />
        </div>
        <div class="brand-text">
          <strong>后厨智能调度</strong>
          <span class="tiny muted">食安合规管理系统</span>
        </div>
      </div>

      <nav class="nav">
        <button
          v-for="item in ROUTES"
          :key="item.key"
          type="button"
          class="nav-item"
          :class="{ active: item.path === activePath }"
          @click="go(item.path)"
        >
          <Icon :name="item.icon" :size="15" />
          <span>{{ item.label }}</span>
        </button>
      </nav>

      <div class="side-foot">
        <div class="dev">
          <span class="tiny muted">设备</span>
          <span class="mono small truncate">
            {{ live.overview?.device_id || '未连接' }}
          </span>
        </div>
        <div class="dev">
          <span class="tiny muted">采集</span>
          <span class="mono small">
            {{ fmtInt(live.overview?.ticks, { fallback: '0' }) }} 帧
          </span>
        </div>
        <div class="conn" :class="live.connection">
          <span class="dot" :class="{ pulse: live.connection === 'live' }"></span>
          <span class="tiny">{{ connectionText }}</span>
        </div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="title">
          <h1>{{ active.label }}</h1>
          <span v-if="guardText" class="tag danger">
            <span class="dot pulse"></span>保护已触发：{{ guardText }}
          </span>
        </div>

        <div class="meta">
          <span class="clock mono" :title="'服务端时间（与设备判据同源）'">
            {{ clockText }}
          </span>
          <span v-if="live.overview?.time_scale > 1" class="tag warn mono">
            演示加速 ×{{ live.overview.time_scale }}
          </span>
          <span v-if="injecting" class="tag warn mono" :title="'控制页触发的演示信号（' + (lastInject?.by || '') + '）；报警由模拟产生，非真实传感器'">
            ⚡ 正在模拟演示信号
          </span>
          <button class="btn ghost sm" type="button" @click="toggleTheme"
                  :title="theme === 'dark' ? '切到浅色' : '切到深色'">
            <Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="14" />
          </button>
          <div class="user">
            <div class="user-info">
              <strong class="small">{{ me.name }}</strong>
              <span class="tiny muted">{{ me.roleLabel }}</span>
            </div>
            <button class="btn ghost sm" type="button" @click="onLogout" title="退出登录">
              <Icon name="logout" :size="14" />
            </button>
          </div>
        </div>
      </header>

      <DegradeBanner />

      <main class="content scroll">
        <component :is="active.view" :key="active.key" />
      </main>
    </div>

    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: var(--sidebar) minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
  /* 兑底层：侧栏外的全部缝隙都由这层主题底色填满，不允许任何透明区透出画布 */
  background: var(--bg);
}

/* ------------------------------------------------------------ 侧栏 */
.sidebar {
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-right: 1px solid var(--line);
  min-height: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 15px 14px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  width: 30px; height: 30px;
  border-radius: 8px;
  /* 黑白灰：面板灰底 + 细线框 + 灰字，随主题自动切换深浅 */
  background: var(--panel-2);
  border: 1px solid var(--line);
  color: var(--text-2);
  display: flex; align-items: center; justify-content: center;
  flex: none;
}
.brand-text { display: flex; flex-direction: column; line-height: 1.35; }
.brand-text strong { font-size: 12.5px; }

.nav { padding: 9px 8px; display: flex; flex-direction: column; gap: 1px; flex: 1; overflow: auto; }
.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  border: none;
  background: none;
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  font-size: 12.5px;
  transition: background 120ms ease, color 120ms ease;
}
.nav-item:hover { background: var(--panel-2); color: var(--text); }
.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}

.side-foot {
  padding: 11px 13px;
  border-top: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dev { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.conn { display: flex; align-items: center; gap: 6px; color: var(--muted); margin-top: 2px; }
.conn.live { color: var(--ok); }
.conn.polling { color: var(--warn); }
.conn.reconnecting { color: var(--warn); }
.conn.offline { color: var(--danger); }

/* ------------------------------------------------------------ 主区 */
.main {
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  min-height: 0;
  min-width: 0;
  background: var(--bg);
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 11px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  min-height: 54px;
}
.title { display: flex; align-items: center; gap: 11px; min-width: 0; }
.title h1 { white-space: nowrap; }
.meta { display: flex; align-items: center; gap: 10px; }
.clock {
  font-size: 13px;
  color: var(--text-2);
  letter-spacing: 0.02em;
}
.user {
  display: flex;
  align-items: center;
  gap: 7px;
  padding-left: 10px;
  border-left: 1px solid var(--line);
}
.user-info { display: flex; flex-direction: column; line-height: 1.3; text-align: right; }

.content { padding: 16px 18px 28px; min-height: 0; background: var(--bg); }

/* ------------------------------------------------------------ 启动 */
.boot-screen {
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--muted);
  font-size: 12.5px;
}
.spinner {
  width: 30px; height: 30px;
  border-radius: 50%;
  border: 2px solid var(--line);
  border-top-color: var(--accent);
  animation: spin 900ms linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 900px) {
  .shell { grid-template-columns: 64px minmax(0, 1fr); }
  .brand-text, .nav-item span, .side-foot { display: none; }
  .brand { justify-content: center; padding: 14px 0; }
  .nav-item { justify-content: center; padding: 10px 0; }
}
</style>
