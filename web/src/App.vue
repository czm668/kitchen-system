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
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './components/Icon.vue'
import DegradeBanner from './components/DegradeBanner.vue'
import ToastHost from './components/ToastHost.vue'
import ViewLoading from './components/ViewLoading.vue'
import ViewError from './components/ViewError.vue'
import LoginView from './views/LoginView.vue'
// 总览是登录后的落地页，**保持静态引入**：把它也做成异步，等于让首屏多等
// 一次「下载页面自身」的往返，而它本来就要立刻渲染。
import KpiView from './views/KpiView.vue'
import { auth, bootstrap, can, login, logout, whoami } from './stores/auth'
import { live, startLive, stopLive, connectionText, trippedGuards, injecting, lastInject } from './stores/live'
import { fmtInt } from './utils/format'

/**
 * 路由表。`group` 只影响侧栏归组，不参与路由匹配。
 *
 * ## 为什么要分组
 *
 * 12 项平铺时，操作员的扫读方式是「从上往下找」，每找一个功能都要过一遍全表；
 * 分成 4 组后变成「先定组、再找项」，平均扫读距离从 6 项降到 3 项。分组依据
 * 是**使用场景**而不是系统模块：现场监控的放第一组（最高频），一天几次的放
 * 第二组，合规留档的放第三组，配置类放最后（低频且含危险操作）。
 *
 * `risk: true` 的项在侧栏带一枚小圆点——把「能停掉灶台的东西」和「看数据的
 * 页面」在入口处就分开，不靠用户记住哪个页面有阀门。
 */
const ROUTES = [
  { key: 'kpi', path: '#/kpi', label: '总览', icon: 'chart', view: KpiView, group: '运行监控',
    desc: '保护状态 · 关键读数 · 实时事件' },
  { key: 'stations', path: '#/stations', label: '工位看板', icon: 'gantt', load: () => import('./views/StationsView.vue'), group: '运行监控',
    desc: '各工位任务与占用情况' },
  { key: 'environment', path: '#/environment', label: '环境与设备', icon: 'env', load: () => import('./views/EnvironmentView.vue'), group: '运行监控',
    desc: '温湿度 · 燃气 · 排烟 · 消毒' },

  { key: 'orders', path: '#/orders', label: '订单与调度', icon: 'orders', load: () => import('./views/OrdersView.vue'), group: '业务执行',
    desc: '下单 · 派单 · 出餐全流程' },
  { key: 'recipes', path: '#/recipes', label: '配方与工艺', icon: 'recipe', load: () => import('./views/RecipesView.vue'), group: '业务执行',
    desc: '菜品配方与工艺参数' },
  { key: 'tickets', path: '#/tickets', label: '工单中心', icon: 'ticket', load: () => import('./views/TicketsView.vue'), group: '业务执行',
    desc: '告警工单的开单与处置' },

  { key: 'compliance', path: '#/compliance', label: '批次与资质', icon: 'shield', load: () => import('./views/ComplianceView.vue'), group: '食安合规',
    desc: '批次追溯 · 留样 · 资质证照' },
  { key: 'credential', path: '#/credential', label: '合规凭证', icon: 'file', load: () => import('./views/ComplianceCredentialView.vue'), group: '食安合规',
    desc: '每日检查表 · 合规日报 · 导出' },

  { key: 'finance', path: '#/finance', label: '经营账本', icon: 'chart', load: () => import('./views/FinanceView.vue'), group: '经营与配置',
    desc: '营收 · 成本 · 毛利' },
  { key: 'guest', path: '#/guest', label: '客诉与画像', icon: 'chat', load: () => import('./views/GuestView.vue'), group: '经营与配置',
    desc: '客诉归因与常客画像' },
  { key: 'ai', path: '#/ai', label: '多融合 AI', icon: 'spark', load: () => import('./views/AiView.vue'), group: '经营与配置',
    desc: '降级链路与模型调用审计' },
  { key: 'control', path: '#/control', label: '远程控制与参数', icon: 'sliders', load: () => import('./views/ControlView.vue'),
    group: '经营与配置', risk: true, desc: '阀门 · 风机 · 消毒 · 演示加速' },
]

/** 侧栏分组：按 ROUTES 里出现的顺序去重，避免再维护一份清单。 */
const NAV_GROUPS = ROUTES.reduce((acc, item) => {
  const last = acc[acc.length - 1]
  if (last && last.name === item.group) last.items.push(item)
  else acc.push({ name: item.group, items: [item] })
  return acc
}, [])

const DEFAULT_ROUTE = ROUTES[0]
const activePath = ref(normalizeHash())
const booting = ref(true)
const theme = ref(localStorage.getItem('kitchen.theme') === 'dark' ? 'dark' : 'light')
const nowTick = ref(Date.now())

const active = computed(
  () => ROUTES.find((item) => item.path === activePath.value) || DEFAULT_ROUTE,
)

/**
 * 页面组件缓存表：**每个 key 只创建一次** `defineAsyncComponent`。
 *
 * 如果在 computed 里现造一个异步组件，每次重渲染都会拿到新对象，Vue 会当成
 * 不同组件整块卸载重挂——表现就是「切一下主题，页面数据也跟着重拉一遍」。
 */
const asyncViews = new Map()

function asyncView(item) {
  if (item.view) return item.view        // 总览是静态引入的，直接用
  if (!asyncViews.has(item.key)) {
    asyncViews.set(item.key, defineAsyncComponent({
      loader: item.load,
      loadingComponent: ViewLoading,
      errorComponent: ViewError,
      // 120ms 内到货就不闪骨架：本地/内网通常在这个量级以内
      delay: 120,
      timeout: 20000,
      onError(err, retry, fail, attempts) {
        // 现场 wifi 抖一下很常见，前三次自动重试，之后才交给错误兜底
        if (attempts <= 3) retry()
        else fail()
      },
    }))
  }
  return asyncViews.get(item.key)
}

const activeView = computed(() => asyncView(active.value))

/** 悬停/聚焦即预取：鼠标移到菜单上就开始下载，点下去时通常已经到货 */
function prefetch(item) {
  if (item.load) item.load().catch(() => {})
}

/**
 * 跳转链接的落点处理。
 *
 * 不去改 URL：这个应用的 hash 就是路由，跳转链接一旦把 hash 改成 `#main`，
 * 用户再刷新就回不到原来的页面了。所以自己接管焦点，让地址栏保持 `#/finance`。
 * `href="#main"` 仍然保留——禁用 JS 或中键打开时它是有效的页内锚点。
 */
function focusMain(event) {
  event.preventDefault()
  const main = document.getElementById('main')
  if (main) main.focus({ preventScroll: false })
}

const me = computed(() => whoami())
const signedIn = computed(() => !!auth.session)

/**
 * 标签页标题跟着路由走。
 *
 * 不这么做的话，同时开三个标签页（现场很常见：总览 / 订单 / 凭证）在标签栏上
 * 是三个一模一样的长标题，靠点进去才能分辨。顺带让浏览器历史记录里也带着
 * 页面名——用后退键时至少知道回到的是哪一页。
 *
 * 退出登录后要把标题改回「登录」：否则页面上已经是登录框，标签页还写着
 * 「经营账本」，演示时最容易被评委指出来。
 */
watch([active, signedIn], ([item, signed]) => {
  document.title = signed
    ? `${item.label} · 后厨智能调度与食安合规管理系统`
    : '登录 · 后厨智能调度与食安合规管理系统'
}, { immediate: true })

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
  const raw = window.location.hash || ''
  /**
   * 页内锚点不是路由。
   *
   * 「跳到主内容」链接指向 `#main`，点它会把 hash 改成 `#main`；照原来的逻辑
   * 这里会判定为「未知路由」并回落到总览——用户按了跳转链接，页面却跳回首页。
   * 所以凡是不以 `#/` 开头的 hash 一律忽略，保持当前页不动。
   */
  if (raw && !raw.startsWith('#/')) return
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
    <!-- 键盘用户每切一页都要 Tab 过 12 个菜单项才能到内容区，这个链接让他们一步跳过去。
         它平时不可见（不在 tab 序列进入前是挪出屏幕的），一按 Tab 就出现在左上角。 -->
    <a class="skip-link" href="#main" @click="focusMain">跳到主内容</a>

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

      <nav class="nav scroll" aria-label="主导航">
        <div v-for="group in NAV_GROUPS" :key="group.name" class="nav-group">
          <div class="nav-group-title" aria-hidden="true">{{ group.name }}</div>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            class="nav-item"
            :class="{ active: item.path === activePath, risk: item.risk }"
            :aria-current="item.path === activePath ? 'page' : undefined"
            :title="item.label"
            :aria-label="item.label"
            @click="go(item.path)"
            @mouseenter="prefetch(item)"
            @focus="prefetch(item)"
          >
            <Icon :name="item.icon" :size="16" />
            <span class="nav-label">{{ item.label }}</span>
            <span v-if="item.risk" class="nav-risk" aria-hidden="true"></span>
          </button>
        </div>
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
          <div class="title-text">
            <h1>{{ active.label }}</h1>
            <p class="title-desc">{{ active.desc }}</p>
          </div>
          <span v-if="guardText" class="tag danger">
            <span class="dot pulse"></span>保护已触发：{{ guardText }}
          </span>
        </div>

        <div class="meta">
          <span class="clock mono" :title="'服务端时间（与设备判据同源）'">
            {{ clockText }}
          </span>
          <span v-if="live.overview?.time_scale > 1" class="tag warn mono">
            <span class="speed-word">演示加速</span> ×{{ live.overview.time_scale }}
          </span>
          <span v-if="injecting" class="tag warn mono" :title="'控制页触发的演示信号（' + (lastInject?.by || '') + '）；报警由模拟产生，非真实传感器'">
            ⚡ 正在模拟演示信号
          </span>
          <button class="btn ghost sm" type="button" @click="toggleTheme"
                  :title="theme === 'dark' ? '切到浅色' : '切到深色'"
                  :aria-label="theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'">
            <Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="14" />
          </button>
          <div class="user">
            <div class="user-info">
              <strong class="small">{{ me.name }}</strong>
              <span class="tiny muted">{{ me.roleLabel }}</span>
            </div>
            <button class="btn ghost sm" type="button" @click="onLogout" title="退出登录" aria-label="退出登录">
              <Icon name="logout" :size="14" />
            </button>
          </div>
        </div>
      </header>

      <DegradeBanner />

      <main id="main" class="content scroll" tabindex="-1">
        <component :is="activeView" :key="active.key" />
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
  gap: 11px;
  padding: 18px 16px 16px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  width: 34px; height: 34px;
  border-radius: 9px;
  /* 黑白灰：面板灰底 + 细线框 + 灰字，随主题自动切换深浅 */
  background: var(--panel-2);
  border: 1px solid var(--line);
  color: var(--text-2);
  display: flex; align-items: center; justify-content: center;
  flex: none;
}
.brand-text { display: flex; flex-direction: column; line-height: 1.35; min-width: 0; }
.brand-text strong { font-size: 13.5px; letter-spacing: -0.01em; }

/* 导航：分组 + 组内紧凑。组标题 11px 大写字距，只在展开态出现。 */
.nav {
  padding: 10px 8px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-height: 0;
}
.nav-group { display: flex; flex-direction: column; gap: 1px; }
.nav-group + .nav-group { margin-top: 14px; }
.nav-group-title {
  padding: 0 10px 6px;
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
  opacity: 0.75;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 34px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  border: none;
  background: none;
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  font-size: var(--fs-sm);
  font-weight: 500;
  transition: background 130ms ease, color 130ms ease;
}
.nav-item:hover { background: var(--panel-2); color: var(--text); }
/* 选中态：浅蓝底 + 左侧 2.5px 竖条。竖条比整块染色更容易在余光里定位。 */
.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent-ink);
  font-weight: 600;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: -8px;
  top: 7px;
  bottom: 7px;
  width: 2.5px;
  border-radius: 0 2px 2px 0;
  background: var(--accent);
}
.nav-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
/* 危险入口标记：能停灶台的页面，在入口就与看数据的页面区分开 */
.nav-risk {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--warn);
  flex: none;
  opacity: 0.8;
}

.side-foot {
  padding: 12px 14px 14px;
  border-top: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 7px;
  background: var(--panel-2);
}
.dev { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.conn { display: flex; align-items: center; gap: 6px; color: var(--muted); margin-top: 2px; }
.conn.live { color: var(--ok-ink); }
.conn.polling { color: var(--warn-ink); }
.conn.reconnecting { color: var(--warn-ink); }
.conn.offline { color: var(--danger-ink); }

/* ------------------------------------------------------------ 跳转链接 */
/* 用「移出视口」而不是 `display:none`：被 display 掉的可聚焦元素拿不到焦点，
   键盘用户会永远碰不到这个链接。聚焦时滑回来。 */
.skip-link {
  position: fixed;
  top: 8px;
  left: 8px;
  z-index: 60;
  padding: 8px 14px;
  border-radius: var(--radius-sm);
  background: var(--accent-solid);
  color: #fff;
  font-size: var(--fs-sm);
  font-weight: 500;
  text-decoration: none;
  /* 它是浮在品牌区之上的一层，必须明确"我在上面"——否则看起来像标签被压花了 */
  box-shadow: var(--shadow-lg);
  transform: translateY(-200%);
  transition: transform 140ms ease;
}
.skip-link:focus,
.skip-link:focus-visible { transform: none; color: #fff; }

/* ------------------------------------------------------------ 主区 */
.main {
  display: grid;
  /* 隐式列也必须写 minmax(0,1fr)。回车后 grid 会给出一条 auto 列，
     auto 的自动最小尺寸 = 内容最小宽度，一张宽表格就能把整列顶出父容器。 */
  grid-template-rows: auto auto minmax(0, 1fr);
  grid-template-columns: minmax(0, 1fr);
  min-height: 0;
  min-width: 0;
  background: var(--bg);
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: 12px 24px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  min-height: 64px;
}
.title { display: flex; align-items: center; gap: 14px; min-width: 0; }
.title-text { display: flex; flex-direction: column; min-width: 0; }
/* 省略号兜底：窄屏下标题与右侧标签争位置时，宁可截断标题也不要压在标签上 */
.title h1 {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: var(--fs-xl);
  line-height: 1.25;
}
/* 副标题说明「这页能干什么」，比只给名词更快定位 */
.title-desc {
  margin: 1px 0 0;
  font-size: var(--fs-xs);
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta { display: flex; align-items: center; gap: 10px; flex: none; }
.clock {
  font-size: var(--fs-sm);
  color: var(--text-2);
  letter-spacing: 0.04em;
  padding: 4px 9px;
  border-radius: var(--radius-sm);
  background: var(--panel-2);
}
.user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--line);
}
.user-info { display: flex; flex-direction: column; line-height: 1.3; text-align: right; }
.user-info strong { font-weight: 600; }

/* min-width: 0 不能省。作为 grid item 时它的默认值是 auto，会被内部宽表格的
   最小内容宽度顶开：实测 375px 下宽 351 撑破 309 的父列，整块右移 42px 再被
   html 的 overflow: clip 裁掉，用户既看不到右边也拿不到滚动条。
   加上之后 .content 老实收缩到父列宽度，横向滚动交给它自己的 overflow-x。 */
.content {
  padding: 20px 24px 40px;
  min-height: 0;
  min-width: 0;
  background: var(--bg);
  overflow-x: auto;
  /* 内容滚到底后不要再把滚动传给外层：桌面浏览器会把整页往上顶一下，
     移动端表现为地址栏收起，两种都让人误以为"点错了" */
  overscroll-behavior: contain;
}

/* 跳转链接的落点：整块内容区会拿到焦点。这里不给它画焦点环——环会框住整个
   主区域，而用户的目光此时已经在内容上了；真正的替代反馈是"页面滚到了顶部"。 */
.content:focus,
.content:focus-visible { outline: none; box-shadow: none; }

/* 切页入场：3px 上移 + 淡入，只动 opacity/transform（合成层，不触发重排）。
   刻意压到 180ms——超过 250ms 就会从"顺滑"变成"卡"。reduced-motion 下
   theme.css 的全局规则会把它降到 0.01ms，等于不播。 */
.content > * {
  animation: view-in 180ms cubic-bezier(0.22, 0.61, 0.36, 1) both;
}
@keyframes view-in {
  from { opacity: 0; transform: translateY(3px); }
  to { opacity: 1; transform: none; }
}

/* ------------------------------------------------------------ 启动 */
.boot-screen {
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--muted);
  font-size: var(--fs-sm);
}
.spinner {
  width: 30px; height: 30px;
  border-radius: 50%;
  border: 2px solid var(--line);
  border-top-color: var(--accent);
  animation: spin 900ms linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 折叠态：只留图标。title + aria-label 双保险，鼠标悬停/读屏都能拿到名称。 */
@media (max-width: 1000px) {
  .shell { grid-template-columns: 66px minmax(0, 1fr); }
  .brand-text, .nav-label, .nav-risk, .nav-group-title, .side-foot { display: none; }
  .brand { justify-content: center; padding: 16px 0; }
  .nav { padding: 10px 6px; align-items: center; gap: 10px; }
  .nav-group { gap: 2px; align-items: center; width: 100%; }
  .nav-group + .nav-group { margin-top: 4px; padding-top: 10px; border-top: 1px solid var(--line); }
  .nav-item { justify-content: center; padding: 0; width: 40px; height: 38px; }
  .nav-item.active::before { left: -6px; }
}
@media (max-width: 720px) {
  .title-desc, .clock, .user-info { display: none; }
  .topbar { padding: 10px 14px; min-height: 56px; gap: 8px; }
  /* 375px 下顶栏可用宽度约 309px：标题 + 加速标签 + 两颗图标按钮已经放满。
     「演示加速」四个字在此时没有信息增量（×60 本身就是标记），把它收掉，
     保留橙色和倍数，既不误导也不挤掉标题。 */
  .speed-word { display: none; }
  .meta { gap: 6px; }
  .content { padding: 14px 14px 32px; }
}
</style>
