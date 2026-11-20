<script setup>
/**
 * 登录页。
 *
 * 这一屏有一条别的页面没有的约束：**它没有任何数据可展示**。后台其余 12 页靠
 * 数字、表格、状态色撑起信息密度，登录页只有两个输入框和一颗按钮，所以它只能
 * 靠构图取胜。早期版本是一张 440px 的卡片居中——在 1440×900 上它只占屏幕宽度
 * 的 30%，四周大片空白，像一张飘着的纸条，也是全站视觉最弱的一屏。
 *
 * 现在改成左右分栏：左栏是深色品牌墙，承载「这套系统解决什么问题」；右栏是
 * 表单，视线终点。这样一屏里既有视觉重量，又能让评委在输入账号之前就读到三个
 * 技术卖点——答辩时第一眼看到的东西，比翻到第 8 页再讲要有效。
 *
 * 演示账号直接列在页面上——这是**故意的**：答辩/验收时如果需要翻文档找账号，
 * 现场那两分钟极其难受。生产部署时把 `KITCHEN_ACCOUNTS` 设上，这四条提示就
 * 不成立了，但那时也不会有人在答辩。
 *
 * 口令错误只显示一句「账号或口令不正确」：后端刻意不区分「用户不存在」与
 * 「口令错」，前端也不该自作聪明地猜。
 */
import { onMounted, ref } from 'vue'
import Icon from '../components/Icon.vue'
import { auth, login } from '../stores/auth'

const username = ref('')
const password = ref('')
const busy = ref(false)
const localError = ref('')
const showPwd = ref(false)

const DEMO = [
  { user: 'admin', role: '系统管理员', note: '全部权限，能复位联锁' },
  { user: 'lead01', role: '后厨组长', note: '改阈值、复核晨检、给 CH3 投一票' },
  { user: 'chef01', role: '操作员', note: '下单、派单、刷卡，改不了阈值' },
  { user: 'guest', role: '只读观察', note: '所有写操作按钮都会置灰' },
]

/**
 * 明暗切换。后台顶栏有这颗按钮，但登录页在它之前——不登录就没法切主题，
 * 只能吃上次留下的 localStorage。夜班接口人第一次打开只会看到浅色。
 * 这里直接读写同一个 key，和 App.vue 的 applyTheme 保持同一套约定。
 */
const dark = ref(localStorage.getItem('kitchen.theme') === 'dark')

function toggleTheme() {
  dark.value = !dark.value
  localStorage.setItem('kitchen.theme', dark.value ? 'dark' : 'light')
  if (dark.value) document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
}

/** 三条卖点。图标用已有的线性图标，不新增资源。 */
const FEATURES = [
  {
    icon: 'shield',
    title: '本地保护优先',
    text: '燃气、超温、长时无人三条通道在设备端闭环，不排队等云端。',
  },
  {
    icon: 'link',
    title: '断网可运行',
    text: '网络中断时保护逻辑照常执行，恢复后补齐数据，不丢一段曲线。',
  },
  {
    icon: 'sliders',
    title: '全程留痕',
    text: '每次阈值变更、远程动作、人工复核都写审计，能追溯到人。',
  },
]

async function submit() {
  if (busy.value) return
  localError.value = ''
  auth.error = ''
  if (!username.value.trim() || !password.value) {
    localError.value = '请填写账号与口令'
    return
  }
  busy.value = true
  try {
    await login(username.value.trim(), password.value)
  } catch (err) {
    localError.value = err.message || '登录失败'
  } finally {
    busy.value = false
  }
}

function fill(user) {
  username.value = user
  password.value = 'kitchen2026'
  localError.value = ''
  auth.error = ''
  // 填完直接把焦点送到按钮：现场点两下就能进系统
  document.querySelector('.login-submit')?.focus()
}

onMounted(() => {
  // 焦点直接落在账号框：现场演示时少点一次是一次
  document.querySelector('input[name=username]')?.focus()
})
</script>

<template>
  <div class="login-page">
    <!-- ---------------------------------------------------------- 品牌墙 -->
    <aside class="brand-panel">
      <div class="bp-grid" aria-hidden="true"></div>
      <div class="bp-glow" aria-hidden="true"></div>

      <div class="bp-main">
        <header class="bp-top">
          <div class="bp-mark"><Icon name="brand" :size="20" /></div>
          <div class="bp-wordmark">
            <strong>后厨智能调度</strong>
            <span>食安合规管理系统</span>
          </div>
        </header>

        <div class="bp-body">
          <p class="bp-eyebrow">KITCHEN OPERATIONS CONSOLE</p>
          <h2 class="bp-title">让每一口灶<br />都在该在的状态里</h2>
          <p class="bp-sub">面向连锁后厨的实时排产、三通道联锁与食安合规控制台。</p>

          <ul class="bp-feats">
            <li v-for="f in FEATURES" :key="f.title">
              <span class="bp-ico"><Icon :name="f.icon" :size="15" /></span>
              <div class="bp-feat-text">
                <b>{{ f.title }}</b>
                <p>{{ f.text }}</p>
              </div>
            </li>
          </ul>
        </div>
      </div>

      <!-- 这里只说「覆盖哪三条通道」这一事实，不冒充实时在线状态：
           未登录时前端拿不到遥测，任何一个「在线」小圆点都是假的。 -->
      <footer class="bp-foot">
        <div class="bp-chans">
          <span class="bp-chan"><Icon name="flame" :size="13" /><b>CH1</b> 燃气阀</span>
          <span class="bp-chan"><Icon name="fan" :size="13" /><b>CH2</b> 排风</span>
          <span class="bp-chan"><Icon name="plug" :size="13" /><b>CH3</b> 工位电</span>
        </div>
        <p class="bp-note">本地保护不依赖网络与登录，断网时仍在设备端执行。</p>
      </footer>
    </aside>

    <!-- ---------------------------------------------------------- 表单 -->
    <main class="form-panel">
      <button
        type="button" class="theme-toggle"
        :aria-label="dark ? '切换到浅色主题' : '切换到深色主题'"
        :title="dark ? '切换到浅色主题' : '切换到深色主题'"
        @click="toggleTheme"
      >
        <Icon :name="dark ? 'sun' : 'moon'" :size="16" />
      </button>

      <div class="form-inner">
        <div class="fp-head">
          <h1>登录</h1>
          <p class="muted">使用账号与口令进入控制台</p>
        </div>

        <form class="form" @submit.prevent="submit" novalidate>
          <div class="field">
            <label for="u">账号</label>
            <input
              id="u" name="username" v-model="username" class="input"
              autocomplete="username" spellcheck="false" :disabled="busy"
              placeholder="admin"
            />
          </div>

          <div class="field">
            <label for="p">口令</label>
            <div class="pwd">
              <input
                id="p" :type="showPwd ? 'text' : 'password'" v-model="password" class="input"
                autocomplete="current-password" :disabled="busy"
                placeholder="kitchen2026"
              />
              <button
                type="button" class="pwd-toggle"
                :aria-label="showPwd ? '隐藏口令' : '显示口令'"
                :aria-pressed="showPwd"
                :disabled="busy"
                @click="showPwd = !showPwd"
              >
                <Icon :name="showPwd ? 'eye-off' : 'eye'" :size="16" />
              </button>
            </div>
          </div>

          <p v-if="localError || auth.error" class="error" role="alert">
            <Icon name="alert" :size="14" />
            <span>{{ localError || auth.error }}</span>
          </p>

          <button class="btn primary block login-submit" type="submit" :disabled="busy">
            <span v-if="busy" class="btn-spinner" aria-hidden="true"></span>
            {{ busy ? '正在登录…' : '登录' }}
          </button>
        </form>

        <div class="demo">
          <div class="demo-head">
            <span class="demo-title">演示账号</span>
            <span class="tiny muted">口令 kitchen2026 · 点一行填入</span>
          </div>
          <div class="demo-list">
            <button
              v-for="item in DEMO" :key="item.user" type="button"
              class="demo-row" @click="fill(item.user)"
            >
              <span class="mono user">{{ item.user }}</span>
              <span class="role">{{ item.role }}</span>
              <span class="note">{{ item.note }}</span>
            </button>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
/* ============================================================================
   两栏：左品牌墙（深色，給重量）+ 右表单（浅色，收视线）
   50/50 在 1440 下是 720/720，右栏内容定宽 408，两侧各留 156，不空。
   ========================================================================== */
.login-page {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  background: var(--bg);
}

/* ------------------------------------------------------------ 品牌墙 */
.brand-panel {
  /* 局部变量：这一块始终是深色，不随主题翻转，只是深色主题下再压暗一档，
     免得和右侧 --bg(#1a1a1c) 贴在同一个明度上分不开。 */
  --bp-bg: #15171d;
  --bp-bg-2: #0d0f13;
  --bp-text: #f5f5f7;
  --bp-text-2: #a6abb8;
  --bp-line: rgba(255, 255, 255, 0.09);
  --bp-fill: rgba(255, 255, 255, 0.05);

  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 44px 52px 40px;
  background: linear-gradient(180deg, var(--bp-bg) 0%, var(--bp-bg-2) 100%);
  color: var(--bp-text);
  isolation: isolate;
  /* 深色主题下右侧是 #1a1a1c、左栏 #101216，纯靠明度差只剩 10 级，两栏几乎连成
     一片。补一根发丝线给边界。 */
  border-right: 1px solid var(--bp-line);
}

/* 品牌标记和主文案收成一组再整体居中：早先是「logo 钉顶 + 文案居中」，
   900px 高时中间会空出 250px，logo 孤零零挂在上面，读起来像两个不相干的区块。 */
.bp-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: clamp(36px, 7vh, 64px);
  min-height: 0;
}

/* 细网格：36px 一格，从左上向右下淡出。给纯色底一点「仪表台」的质感，
   又不至于像贴了一张壁纸。 */
.bp-grid {
  position: absolute;
  inset: 0;
  z-index: -1;
  background-image:
    linear-gradient(to right, rgba(255, 255, 255, 0.045) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255, 255, 255, 0.045) 1px, transparent 1px);
  background-size: 36px 36px;
  -webkit-mask-image: radial-gradient(118% 105% at 24% 0%, #000 0%, rgba(0, 0, 0, 0.5) 46%, transparent 84%);
  mask-image: radial-gradient(118% 105% at 24% 0%, #000 0%, rgba(0, 0, 0, 0.5) 46%, transparent 84%);
  pointer-events: none;
}
/* 光晕必须**在元素内部就走完衰减**：早先写成 `at 32% 100%`，渐变中心正好落在
   盒子下边缘，颜色还没到透明就被裁掉，屏幕上出现一条水平的硬切线。 */
.bp-glow {
  position: absolute;
  z-index: -1;
  left: -20%;
  right: -20%;
  top: -26%;
  height: 94%;
  background: radial-gradient(46% 50% at 34% 42%, rgba(10, 132, 255, 0.30), rgba(10, 132, 255, 0) 74%);
  pointer-events: none;
}

.bp-top { display: flex; align-items: center; gap: 12px; }
.bp-mark {
  width: 38px; height: 38px;
  flex: none;
  border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bp-fill);
  border: 1px solid var(--bp-line);
  color: #fff;
}
.bp-wordmark { display: flex; flex-direction: column; line-height: 1.35; }
.bp-wordmark strong { font-size: 14px; letter-spacing: 0.01em; }
.bp-wordmark span { font-size: 12px; color: var(--bp-text-2); }

.bp-body { max-width: 440px; }
.bp-eyebrow {
  margin: 0 0 14px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.14em;
  color: rgba(122, 173, 255, 0.92);
}
.bp-title {
  margin: 0;
  font-size: clamp(26px, 2.35vw, 34px);
  line-height: 1.28;
  font-weight: 620;
  letter-spacing: -0.018em;
}
.bp-sub {
  margin: 14px 0 0;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--bp-text-2);
  max-width: 380px;
}

.bp-feats { list-style: none; margin: 30px 0 0; padding: 0; display: grid; gap: 16px; }
.bp-feats li { display: flex; gap: 12px; align-items: flex-start; }
.bp-ico {
  width: 32px; height: 32px;
  flex: none;
  border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bp-fill);
  border: 1px solid var(--bp-line);
  color: #cfe2ff;
}
.bp-feat-text b { font-size: 13.5px; font-weight: 600; }
.bp-feat-text p {
  margin: 3px 0 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--bp-text-2);
}

.bp-foot { display: flex; flex-direction: column; gap: 10px; }
.bp-chans { display: flex; flex-wrap: wrap; gap: 8px; }
.bp-chan {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid var(--bp-line);
  background: var(--bp-fill);
  font-size: 12px;
  color: var(--bp-text-2);
}
.bp-chan b { color: var(--bp-text); font-weight: 600; font-size: 11px; letter-spacing: 0.03em; }
.bp-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: rgba(166, 171, 184, 0.8);
}

/* 深色主题下右侧是 #1a1a1c，左栏再压暗一档才能把两栏分开 */
:root[data-theme="dark"] .brand-panel {
  --bp-bg: #101216;
  --bp-bg-2: #0a0b0e;
  --bp-line: rgba(255, 255, 255, 0.08);
  --bp-fill: rgba(255, 255, 255, 0.045);
}

/* ------------------------------------------------------------ 表单 */
.form-panel {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 40px;
  background: var(--bg);
}
.form-inner { width: 100%; max-width: 408px; }

.theme-toggle {
  position: absolute;
  top: 20px; right: 20px;
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--panel);
  color: var(--text-2);
  cursor: pointer;
  transition: color 130ms ease, border-color 130ms ease, background 130ms ease;
}
.theme-toggle:hover { color: var(--text); border-color: var(--line-strong); }

.fp-head { margin-bottom: 26px; }
.fp-head h1 {
  margin: 0;
  font-size: 27px;
  line-height: 1.25;
  letter-spacing: -0.015em;
}
.fp-head p { margin: 7px 0 0; font-size: 13.5px; }

.form { display: flex; flex-direction: column; gap: 16px; }
.form .field > label { font-size: 12.5px; }
.form .input {
  height: 46px;
  font-size: 14.5px;
  border-radius: 10px;
  background: var(--panel);
}

/* 口令显隐：按钮压在输入框右侧内缘，不额外占一行高度 */
.pwd { position: relative; }
.pwd .input { padding-right: 44px; }
.pwd-toggle {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  border: none;
  border-radius: 8px;
  background: none;
  color: var(--muted);
  cursor: pointer;
  transition: background 130ms ease, color 130ms ease;
}
.pwd-toggle:hover { background: var(--panel-2); color: var(--text-2); }
.pwd-toggle[disabled] { opacity: 0.4; cursor: not-allowed; }

.error {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 10px 12px;
  border-radius: 9px;
  background: var(--danger-soft);
  border-left: 3px solid var(--danger);
  color: var(--danger-ink);
  font-size: var(--fs-sm);
  line-height: 1.5;
}

.login-submit {
  height: 46px;
  margin-top: 2px;
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.btn-spinner {
  width: 14px; height: 14px;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.35);
  border-top-color: #fff;
  animation: lp-spin 700ms linear infinite;
}
@keyframes lp-spin { to { transform: rotate(360deg); } }

/* ------------------------------------------------------------ 演示账号 */
.demo { margin-top: 26px; padding-top: 20px; border-top: 1px solid var(--line); }
.demo-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 0 2px 8px;
}
.demo-title { font-size: 12.5px; font-weight: 600; color: var(--text-2); }

.demo-list { display: flex; flex-direction: column; gap: 1px; margin: 0 -8px; }
.demo-row {
  display: grid;
  grid-template-columns: 66px 68px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 8px;
  border: none;
  border-radius: 8px;
  background: none;
  cursor: pointer;
  text-align: left;
  transition: background 130ms ease, box-shadow 130ms ease;
}
/* hover 用投影浮起，键盘焦点必须另给焦点环：两条规则合写会让 Tab 过来时只剩
   一根 2px 竖条，键盘用户看不出自己在哪一行。 */
.demo-row:hover {
  background: var(--panel);
  box-shadow: var(--shadow-1), inset 2px 0 0 var(--accent);
}
.demo-row:focus-visible {
  background: var(--panel);
  box-shadow: var(--focus-ring), inset 2px 0 0 var(--accent);
}
.user { font-size: 12.5px; color: var(--accent-ink); font-weight: 600; }
.role { font-size: 12.5px; color: var(--text-2); }
.note {
  font-size: 12px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ------------------------------------------------------------ 响应式
   1000px 以下两栏会压到 480px 一栏，品牌墙的标题开始三行折行，不如折叠成
   一条顶部横条：保留品牌识别，把整块宽度让给表单。 */
@media (max-width: 1000px) {
  /* 单列时必须显式给行高：grid 的 auto 行在 min-height:100vh 下会被拉平，
     顶栏会跟着表单一起撑到 200px 高，变成一条胖横幅。 */
  .login-page { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 1fr; }
  .brand-panel { padding: 18px 24px; border-right: none; }
  .bp-main { flex: none; gap: 0; }
  .bp-body, .bp-foot, .bp-grid { display: none; }
  .bp-glow { top: -160%; height: 340%; opacity: 0.42; }
  .form-panel { align-items: center; padding: 40px 24px 48px; }
}

@media (max-width: 520px) {
  /* 手机上顶对齐：屏幕窄、表单高，居中会让标题离顶栏忽远忽近 */
  .form-panel { align-items: flex-start; padding: 26px 18px 44px; }
  .fp-head h1 { font-size: 24px; }
  /* 一行放不下用户名 + 角色 + 说明时，把说明折到第二行，不截断 */
  .demo-row { grid-template-columns: 66px minmax(0, 1fr); row-gap: 3px; padding: 9px 8px; }
  .note { grid-column: 1 / -1; white-space: normal; }
}
</style>
