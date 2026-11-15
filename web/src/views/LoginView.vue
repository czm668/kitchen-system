<script setup>
/**
 * 登录页。
 *
 * 演示账号直接列在页面上——这是**故意的**：答辩/验收时如果需要翻文档找账号，
 * 现场那两分钟极其难受。生产部署时把 `KITCHEN_ACCOUNTS` 设上，这四条提示就不
 * 成立了，但那时也不会有人在答辩。
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

const DEMO = [
  { user: 'admin', role: '系统管理员', note: '全部权限，能复位联锁' },
  { user: 'lead01', role: '后厨组长', note: '改阈值、复核晨检、给 CH3 投一票' },
  { user: 'chef01', role: '操作员', note: '下单、派单、刷卡，改不了阈值' },
  { user: 'guest', role: '只读观察', note: '所有写操作按钮都会置灰' },
]

async function submit() {
  if (busy.value) return
  localError.value = ''
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
}

onMounted(() => {
  // 焦点直接落在账号框：现场演示时少点一次是一次
  document.querySelector('input[name=username]')?.focus()
})
</script>

<template>
  <div class="login">
    <div class="card">
      <div class="head">
        <div class="mark">厨</div>
        <h1>后厨智能调度与食安合规管理系统</h1>
        <p class="muted small">
          基于 ESP32 · 本地保护优先 · 断网可运行
        </p>
      </div>

      <form @submit.prevent="submit" class="form">
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
          <input
            id="p" type="password" v-model="password" class="input"
            autocomplete="current-password" :disabled="busy"
            placeholder="kitchen2026"
          />
        </div>

        <p v-if="localError || auth.error" class="error">
          <Icon name="alert" :size="13" />
          <span>{{ localError || auth.error }}</span>
        </p>

        <button class="btn primary block" type="submit" :disabled="busy">
          {{ busy ? '正在登录…' : '登录' }}
        </button>
      </form>

      <div class="demo">
        <div class="demo-head">
          <span class="tiny muted">演示账号（口令统一 kitchen2026）</span>
        </div>
        <button
          v-for="item in DEMO" :key="item.user" type="button"
          class="demo-row" @click="fill(item.user)"
        >
          <span class="mono user">{{ item.user }}</span>
          <span class="role">{{ item.role }}</span>
          <span class="note tiny muted">{{ item.note }}</span>
        </button>
      </div>
    </div>

    <p class="foot tiny muted">
      本地保护（燃气/超温/长时无人）不依赖网络与登录，断网时仍在设备端执行。
    </p>
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 24px;
  background:
    radial-gradient(1000px 480px at 50% -10%, var(--accent-soft), transparent 70%),
    var(--bg);
}
.card {
  width: min(420px, 100%);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: var(--shadow);
  overflow: hidden;
}
.head {
  padding: 24px 24px 18px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 7px;
  text-align: center;
  border-bottom: 1px solid var(--line);
}
.mark {
  width: 42px; height: 42px;
  border-radius: 11px;
  background: linear-gradient(140deg, var(--accent), var(--info));
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 21px; font-weight: 700;
  margin-bottom: 4px;
}
.head h1 { font-size: 15px; }
.head p { margin: 0; }

.form { padding: 18px 24px 20px; display: flex; flex-direction: column; gap: 13px; }
.form .field > label { font-size: 11.5px; }

.error {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  border: 1px solid var(--danger);
  color: var(--danger);
  font-size: 12px;
}

.demo { border-top: 1px solid var(--line); padding: 12px 16px 14px; background: var(--panel-2); }
.demo-head { padding: 0 4px 7px; }
.demo-row {
  display: grid;
  grid-template-columns: 62px 74px minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: var(--radius-sm);
  background: none;
  cursor: pointer;
  text-align: left;
}
.demo-row:hover { background: var(--accent-soft); }
.user { font-size: 11.5px; color: var(--accent); }
.role { font-size: 11.5px; color: var(--text-2); }
.note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.foot { margin: 0; text-align: center; max-width: 460px; }
</style>
