<script setup>
/**
 * A7-09 远程控制与参数页。
 *
 * 这一页是全系统**唯一能真正动硬件**的地方，所以它的设计取向和其它页面不同：
 * 别的页面追求「一眼看清」，这一页追求「不可能点错」。
 *
 * ## 三通道动作为什么要输动作名才给执行
 *
 * 燃气阀、排风、工位电三个按钮排在一起，误点的代价完全不同：关排风只是油烟大，
 * 关工位电是断掉正在炒菜的灶，关燃气阀会让整条线停下来。`ConfirmDialog` 要求
 * 把服务端登记的动作名（`gas_valve_close` 这种）**原样打一遍**，等于强制操作者
 * 读一遍自己要做什么。这一层只是 UI 防误触，**真正的闸门在服务端**：
 * `InterlockController.request()` 收到 `manual_confirmed=false` 会直接抛错，
 * 绕过页面调接口一样拦得住。
 *
 * ## 为什么阈值只提交「改过的行」
 *
 * 后端 `PUT /api/params` 是按 `id` 合并字段的。整表提交会把「别人刚在另一台
 * 电脑上改的那一行」用我这边的旧值覆盖回去——两个人同时维护阈值时，丢失修改
 * 是必然的，而且没有任何提示。所以这里只发 dirty 行、只发改过的字段。
 *
 * ## 为什么要单列一张「演示值 ↔ 真实值」表
 *
 * 答辩时最常见的质疑是「你 60 秒就报警，真实场景是 60 分钟，是不是没做」。
 * 这张表把每一项的**设计值**、**当前生效值**、**换算规则**并排放在同一屏，
 * 并且明确写「只压缩时间轴，判定逻辑与阈值不变」——不用解释，看表即可。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { channels, live, refresh } from '../stores/live'
import { toastError, toastOk, toastWarn } from '../composables/useToast'
import { fmtDateTime, fmtInt, CHANNEL_STATE } from '../utils/format'

/* ------------------------------------------------------------------ 状态 */
const meta = ref(null)
const params = ref(null)
const audit = ref([])
const loading = ref(true)
const busy = ref('')

/** 阈值编辑副本：`rows` 是用户正在改的，`baseline` 是服务端返回的原值。 */
const rows = ref([])
const baseline = reactive({})

/** 二次确认的目标动作。 */
const pending = ref(null)

/** 演示加速滑杆。 */
const scaleDraft = ref(1)

const canTrip = computed(() => can('channel.trip'))
const canRestore = computed(() => can('channel.restore'))
const canReview = computed(() => can('channel.review'))
const canParam = computed(() => can('param.write'))

const online = computed(() => live.overview?.online !== false)

/** 动作按通道分组；声光/语音是独立 GPIO，不能混进三通道卡片。 */
const actionsByChannel = computed(() => {
  const out = { CH1: [], CH2: [], CH3: [], GPIO_SIGNAL: [] }
  for (const row of meta.value?.actions || []) {
    if (!out[row.channel]) out[row.channel] = []
    out[row.channel].push(row)
  }
  return out
})

const CHANNEL_META = {
  CH1: { label: '燃气电磁阀', hint: '常开触点 + 常闭阀体：断电即关，得电才开', icon: 'flame' },
  CH2: { label: '排风扇', hint: '与罩温/消毒柜温度联动，超温强制开启', icon: 'fan' },
  CH3: { label: '工位插座', hint: '行为类违规切断；复位需 Web + TFT 双通道复核', icon: 'plug' },
}

const GUARD_LABEL = {
  'guard:gas_surge': '燃气突变',
  'guard:thermal_fan': '超温',
  'guard:flame_unattended': '长时无人',
}

const stateLabel = (state) => CHANNEL_STATE[state]?.label || state
const stateTone = (state) => CHANNEL_STATE[state]?.tone || 'plain'

/** 三通道 + 复核票数，全部来自 `overview` 的聚合快照。 */
const channelRows = computed(() => channels.value
  .filter((row) => CHANNEL_META[row.name])
  .map((row) => ({
    ...row,
    ...(CHANNEL_META[row.name] || {}),
    stateLabel: stateLabel(row.state),
    tone: stateTone(row.state),
    sources: Object.keys(row.sources || {}),
    reviews: row.reviewers?.length || 0,
  })))

const dirtyRows = computed(() => rows.value.filter((row) => isDirty(row)))

/* ------------------------------------------------------------------ 载入 */
async function load() {
  loading.value = true
  try {
    const [metaData, paramData] = await Promise.all([
      api.get('/api/meta'),
      api.get('/api/params'),
    ])
    meta.value = metaData
    applyParams(paramData)
    await loadAudit()
  } catch (err) {
    toastError(err, '参数载入失败')
  } finally {
    loading.value = false
  }
}

function applyParams(data) {
  params.value = data
  rows.value = (data.rules || []).map((row) => ({ ...row }))
  Object.keys(baseline).forEach((key) => delete baseline[key])
  rows.value.forEach((row) => { baseline[String(row.id)] = { ...row } })
  scaleDraft.value = data.demo_map?.time_scale ?? data.time_scale ?? 1
}

async function loadAudit() {
  try {
    const data = await api.get('/api/audit?limit=12')
    audit.value = data.entries || []
  } catch {
    // 审计读不到不影响主功能，静默即可（读失败仍要能操作设备）
  }
}

/* ------------------------------------------------------------------ 通道 */
function actionsOf(channel, kind) {
  return (actionsByChannel.value[channel] || [])
    .filter((row) => (kind === 'signal' ? row.kind === 'signal' : row.kind !== 'signal'))
    .filter((row) => (row.kind === 'restore' ? canRestore.value : canTrip.value))
}

function askAction(action, label) {
  pending.value = { action, label, kind: 'channel' }
}

async function runChannelAction({ typed }) {
  const target = pending.value
  if (!target) return
  busy.value = target.action
  try {
    const data = await api.post(
      `/api/channels/${channelOfAction(target.action)}/actions`,
      { action: target.action, manual_confirmed: Boolean(typed) },
    )
    // 动作用返回值即时更新，不等 400ms 合并刷新——现场要看到「按下去就变了」
    toastOk(`${target.label}：${data.state === 'signal' ? '已下发' : stateLabel(data.state)}`)
    pending.value = null
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '动作被拒绝')
    pending.value = null
    await refresh({ silent: true })
  } finally {
    busy.value = ''
  }
}

function channelOfAction(action) {
  const found = (meta.value?.actions || []).find((row) => row.action === action)
  return found?.channel || 'CH1'
}

async function reviewChannel(name) {
  busy.value = `review:${name}`
  try {
    const data = await api.post(`/api/channels/${name}/review`, { channel: name })
    toastOk(`已登记本端复核（${data.reviewers.length}/2）：${data.reviewers.join(' + ')}`)
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '复核登记失败')
  } finally {
    busy.value = ''
  }
}

/* ------------------------------------------------------------------ 阈值 */
const EDIT_FIELDS = ['lower_bound', 'upper_bound', 'duration_sec', 'window_sec', 'level', 'enabled']

function isDirty(row) {
  const origin = baseline[String(row.id)]
  if (!origin) return false
  return EDIT_FIELDS.some((field) => norm(row[field]) !== norm(origin[field]))
}

function norm(value) {
  if (value === '' || value === undefined) return null
  return value
}

function metricLabel(id) {
  return meta.value?.metrics?.find((row) => row.id === id)?.label || id
}

function unitOf(id) {
  return meta.value?.metrics?.find((row) => row.id === id)?.unit || ''
}

const KIND_LABEL = {
  range: '区间',
  duration: '持续时长',
  window: '时间窗',
  tier: '分档',
}

async function saveParams() {
  const patch = dirtyRows.value.map((row) => {
    const origin = baseline[String(row.id)]
    const body = { id: row.id }
    EDIT_FIELDS.forEach((field) => {
      if (norm(row[field]) !== norm(origin[field])) body[field] = norm(row[field])
    })
    return body
  })
  if (!patch.length) {
    toastWarn('没有改动')
    return
  }
  busy.value = 'save'
  try {
    const data = await api.put('/api/params', { rules: patch })
    const count = Object.keys(data.changed || {}).length
    toastOk(`已保存 ${patch.length} 行 / ${count} 处改动，热更新生效（不重启、不重置去抖计时）`)
    applyParams(await api.get('/api/params'))
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '保存失败')
  } finally {
    busy.value = ''
  }
}

function resetDraft() {
  rows.value = rows.value.map((row) => ({ ...baseline[String(row.id)] }))
}

/* ------------------------------------------------------------------ 演示 */
const SCALE_STEPS = [1, 10, 30, 60, 120]

async function applyScale() {
  busy.value = 'scale'
  try {
    await api.post('/api/params/time-scale', { time_scale: Number(scaleDraft.value) })
    toastOk(`加速因子已设为 ×${scaleDraft.value}（只压缩时间轴，判定逻辑不变）`)
    applyParams(await api.get('/api/params'))
    await loadAudit()
  } catch (err) {
    toastError(err, '设置失败')
    scaleDraft.value = params.value?.demo_map?.time_scale || 1
  } finally {
    busy.value = ''
  }
}

async function setOnline(next) {
  busy.value = 'network'
  try {
    const data = await api.post('/api/network', { online: next })
    if (next) {
      toastOk(`已恢复联网，补传 ${data.replay?.accepted ?? 0} 条（重复 ${data.replay?.duplicates ?? 0} 条已去重）`)
    } else {
      toastWarn('已模拟断网：安全保护继续在端侧执行，业务数据进本地队列')
    }
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '切换失败')
  } finally {
    busy.value = ''
  }
}

async function flushQueue() {
  busy.value = 'flush'
  try {
    const data = await api.post('/api/admin/flush', {})
    toastOk(`补传完成：队列剩 ${data.queued} 条，服务端共 ${fmtInt(data.store_rows)} 行`)
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '补传失败')
  } finally {
    busy.value = ''
  }
}

const INJECT_PRESETS = [
  { key: 'gas', label: '燃气泄漏', metrics: { gas_level: 0.92 }, note: '连续 2 个采样点后关阀' },
  { key: 'flame', label: '明火离人', metrics: { flame_b: 1.0 }, note: '12 秒（演示按加速折算）' },
  { key: 'thermal', label: '排烟超温', metrics: { hood_temp: 292.0 }, note: '罩温 ≥280℃ 强制开风机' },
]

async function inject(preset) {
  busy.value = `inject:${preset.key}`
  try {
    const data = await api.post('/api/demo/inject', { metrics: preset.metrics })
    const guards = (data.guards || []).map((row) => row.rule_id).join('、')
    toastOk(guards ? `已注入${preset.label} → 保护触发：${guards}` : `已注入${preset.label}（本帧未触发保护）`)
    await Promise.all([refresh({ silent: true }), loadAudit()])
  } catch (err) {
    toastError(err, '注入失败')
  } finally {
    busy.value = ''
  }
}

/* ------------------------------------------------------------------ 生命周期 */
onMounted(load)

// 断网/恢复会改 `demo_map` 与阈值版本号；页面停留时跟随后台快照变化重载一次
let lastScale = 1
watch(() => live.overview?.time_scale, (next) => {
  if (!next || next === lastScale) return
  lastScale = next
  if (params.value) load()
})
</script>

<template>
  <div v-if="loading" class="stack">
    <div class="panel skeleton" style="height: 160px"></div>
    <div class="panel skeleton" style="height: 240px"></div>
  </div>

  <div v-else class="stack">
    <!-- ---------------------------------------------------------- 三通道 -->
    <section class="panel">
      <div class="panel-head">
        <div>
          <div class="panel-title">三通道远程控制</div>
          <div class="panel-sub">
            动作先经互锁层裁决再下发；恢复类动作需人工确认，CH3 另需双通道复核
          </div>
        </div>
        <span class="tag" :class="online ? 'ok' : 'danger'">
          <span class="dot" :class="{ pulse: !online }"></span>
          {{ online ? '联网' : '断网（本地保护仍生效）' }}
        </span>
      </div>

      <div class="panel-body grid grid-3">
        <div v-for="row in channelRows" :key="row.name" class="chan" :class="row.tone">
          <div class="chan-head">
            <Icon :name="row.icon" :size="16" />
            <strong>{{ row.name }} · {{ row.label }}</strong>
            <span class="tag" :class="row.tone">{{ row.stateLabel }}</span>
          </div>
          <p class="tiny muted">{{ row.hint }}</p>

          <div class="chan-src tiny">
            <span class="muted">触发源</span>
            <span v-if="!row.sources.length" class="dim">无</span>
            <span v-for="src in row.sources" :key="src" class="chip mono">{{ src }}</span>
          </div>

          <div class="chan-acts">
            <button
              v-for="act in actionsOf(row.name)"
              :key="act.action"
              type="button"
              class="btn sm"
              :class="act.kind === 'restore' ? 'ghost' : 'danger'"
              :disabled="!!busy"
              @click="askAction(act.action, act.label)"
            >
              {{ act.label }}
            </button>
          </div>

          <div v-if="row.name === 'CH3' && row.state !== 'normal'" class="review">
            <div class="tiny">
              复核票 <strong>{{ row.reviews }}/2</strong>
              <span class="dim mono">（{{ row.reviewers.join(' + ') || '暂无' }}）</span>
            </div>
            <button
              type="button"
              class="btn ghost sm"
              :disabled="!!busy || !canReview"
              @click="reviewChannel('CH3')"
            >
              登记本端复核
            </button>
          </div>
        </div>
      </div>

      <div class="panel-body signal-row">
        <span class="tiny muted">独立 GPIO（不占三通道、不受互锁抑制）</span>
        <button
          v-for="act in actionsOf('GPIO_SIGNAL', 'signal')"
          :key="act.action"
          type="button"
          class="btn ghost sm"
          :disabled="!!busy"
          @click="askAction(act.action, act.label)"
        >
          {{ act.label }}
        </button>
        <span class="spacer"></span>
        <span v-if="live.overview?.guards?.tripped?.length" class="tag danger">
          保护触发中：{{ live.overview.guards.tripped.map((k) => GUARD_LABEL[k] || k).join('、') }}
        </span>
      </div>
    </section>

    <!-- ------------------------------------------------- 演示加速 / 网络 -->
    <div class="grid grid-2">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">演示加速因子</div>
            <div class="panel-sub">只压缩时间轴，判定逻辑与阈值常数一律不变</div>
          </div>
          <span class="tag mono" :class="scaleDraft > 1 ? 'warn' : 'plain'">×{{ scaleDraft }}</span>
        </div>
        <div class="panel-body stack">
          <div class="scale-steps">
            <button
              v-for="step in SCALE_STEPS"
              :key="step"
              type="button"
              class="btn sm"
              :class="Number(scaleDraft) === step ? 'primary' : 'ghost'"
              @click="scaleDraft = step"
            >
              ×{{ step }}
            </button>
          </div>
          <label class="tiny muted">自定义（1 ~ 3600）</label>
          <input v-model.number="scaleDraft" type="number" min="1" max="3600" class="input" />
          <p v-if="params?.demo_map" class="tiny dim pre-wrap">{{ params.demo_map.scale_note }}</p>
          <button type="button" class="btn" :disabled="busy === 'scale' || !canParam" @click="applyScale">
            应用加速因子
          </button>
          <p v-if="!canParam" class="tiny warn-text">当前角色无参数写权限</p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">网络与补传</div>
            <div class="panel-sub">断网期间安全保护照常执行，业务数据落本地队列</div>
          </div>
          <span class="tag mono" :class="live.overview?.counts?.queue ? 'warn' : 'plain'">
            队列 {{ fmtInt(live.overview?.counts?.queue, { fallback: '0' }) }} 条
          </span>
        </div>
        <div class="panel-body stack">
          <div class="row">
            <button
              type="button"
              class="btn sm"
              :class="online ? 'danger' : 'primary'"
              :disabled="!!busy || !canParam"
              @click="setOnline(!online)"
            >
              {{ online ? '模拟断网' : '恢复联网' }}
            </button>
            <button
              type="button"
              class="btn ghost sm"
              :disabled="!!busy || !canParam"
              @click="flushQueue"
            >
              手动补传
            </button>
          </div>
          <p class="tiny dim">
            断网时 #1 燃气、#3 明火离人、#17 三条保守保护仍在端侧判定并动作；
            只有上报与跨端同步暂停。恢复联网会自动补传并按
            <code class="mono">(device_id, seq)</code> 去重。
          </p>
          <!-- 逐条能力清单只在顶部 DegradeBanner 里说一次：
               两个地方各写一份，迟早出现「这里说可用、那里说不可用」 -->
          <p v-if="live.overview?.degrade" class="tiny">
            <span class="tag" :class="live.overview.degrade.severity === 'normal' ? 'ok' : 'warn'">
              {{ live.overview.degrade.headline }}
            </span>
            <span class="dim">受影响能力见页面顶部横幅</span>
          </p>
        </div>
      </section>
    </div>

    <!-- --------------------------------------------- 演示值 ↔ 真实值 -->
    <section v-if="params?.demo_map" class="panel">
      <div class="panel-head">
        <div>
          <div class="panel-title">演示阈值 ↔ 真实值（答辩核对用）</div>
          <div class="panel-sub">
            当前加速 ×{{ params.demo_map.time_scale }}：{{ params.demo_map.scale_note }}
          </div>
        </div>
      </div>
      <div class="panel-body">
        <table class="table">
          <thead>
            <tr>
              <th>判据项</th>
              <th class="num">设计值</th>
              <th class="num">当前生效</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in params.demo_map.rows" :key="row.item">
              <td>{{ row.item }}</td>
              <td class="num mono">{{ row.real_text }}</td>
              <td class="num mono" :class="{ 'warn-text': row.effective_sec !== row.real_sec }">
                {{ row.effective_text }}
              </td>
              <td class="tiny muted">{{ row.note }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="params.demo_map.hard_rule" class="tiny dim pre-wrap">
          {{ params.demo_map.hard_rule }}
        </p>
      </div>
    </section>

    <!-- ------------------------------------------------------ 阈值配置 -->
    <section class="panel">
      <div class="panel-head">
        <div>
          <div class="panel-title">阈值配置</div>
          <div class="panel-sub">
            来源 {{ params?.source }} · 版本 v{{ params?.version }} · 当前档位 {{ params?.profile }}
            <span v-if="params?.degraded" class="tag danger">配置降级</span>
          </div>
        </div>
        <div class="row">
          <span v-if="dirtyRows.length" class="tag warn">{{ dirtyRows.length }} 行待保存</span>
          <button type="button" class="btn ghost sm" :disabled="!dirtyRows.length"
                  @click="resetDraft">
            撤销
          </button>
          <button type="button" class="btn sm" :disabled="!canParam || !!busy || !dirtyRows.length"
                  @click="saveParams">
            保存并热更新
          </button>
        </div>
      </div>

      <div class="panel-body">
        <p v-if="!canParam" class="tiny warn-text">当前角色只读，无法修改阈值。</p>
        <table class="table">
          <thead>
            <tr>
              <th>规则</th>
              <th>指标</th>
              <th>类型</th>
              <th class="num">下限</th>
              <th class="num">上限</th>
              <th class="num">时长(s)</th>
              <th class="num">时间窗(s)</th>
              <th class="num">等级</th>
              <th>启用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" :class="{ dirty: isDirty(row) }">
              <td class="mono small">{{ row.id }}</td>
              <td>
                <div class="small">{{ metricLabel(row.metric) }}</div>
                <div class="tiny dim mono">{{ row.metric }} <span v-if="unitOf(row.metric)">{{ unitOf(row.metric) }}</span></div>
              </td>
              <td class="small">{{ KIND_LABEL[row.kind] || row.kind }}</td>
              <td class="num"><input v-model.number="row.lower_bound" type="number" step="0.1"
                                     class="input xs" :disabled="!canParam" /></td>
              <td class="num"><input v-model.number="row.upper_bound" type="number" step="0.1"
                                     class="input xs" :disabled="!canParam" /></td>
              <td class="num"><input v-model.number="row.duration_sec" type="number" step="1"
                                     class="input xs" :disabled="!canParam" /></td>
              <td class="num"><input v-model.number="row.window_sec" type="number" step="1"
                                     class="input xs" :disabled="!canParam" /></td>
              <td class="num">
                <select v-model.number="row.level" class="input xs" :disabled="!canParam">
                  <option :value="1">1</option>
                  <option :value="2">2</option>
                  <option :value="3">3</option>
                </select>
              </td>
              <td>
                <!-- 配置里 enabled 是 0/1（不是 true/false）：用 true-value 保住类型，
                     否则存回去变成布尔值，`changed` 里会多出一条「1 → true」的假改动 -->
                <input v-model="row.enabled" type="checkbox" :true-value="1" :false-value="0"
                       :disabled="!canParam" />
              </td>
            </tr>
          </tbody>
        </table>
        <p class="tiny dim">
          保存走 `KitchenConfig` 热更新：不重启、不重置去抖计时。三通道保守保护
          （`resilience/guards.py`）的阈值**不在本表**，写死在代码里，避免「配置写错
          导致保护静默失效」。
        </p>
      </div>
    </section>

    <!-- ------------------------------------------------------ 信号注入 -->
    <div class="grid grid-2">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">模拟报警演示</div>
            <div class="panel-sub">只影响下一帧采样，不改阈值、不改保护逻辑</div>
          </div>
        </div>
        <div class="panel-body stack">
          <div class="row wrap">
            <button
              v-for="preset in INJECT_PRESETS"
              :key="preset.key"
              type="button"
              class="btn danger sm"
              :disabled="!!busy || !canTrip"
              @click="inject(preset)"
            >
              {{ preset.label }}
            </button>
          </div>
          <p class="tiny dim">
            模拟产生的帧会打 `demo` 标记进事件流并写审计，事后能看出「这次报警是演出来的」；
            生产环境该接口应关闭。
          </p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">最近操作留痕</div>
            <div class="panel-sub">被拒绝的动作也会记一条——它比成功的那次更值得追溯</div>
          </div>
          <button type="button" class="btn ghost sm" @click="loadAudit">刷新</button>
        </div>
        <div class="panel-body">
          <div v-if="!audit.length" class="empty tiny">暂无记录</div>
          <ul v-else class="audit">
            <li v-for="entry in audit" :key="entry.id" class="tiny">
              <span class="mono dim">{{ fmtDateTime(entry.time) }}</span>
              <span class="mono">{{ entry.action }}</span>
              <span class="muted">{{ entry.actor }}</span>
              <span class="tag" :class="entry.result === 'ok' ? 'ok' : 'danger'">
                {{ entry.result }}
              </span>
              <span v-if="entry.detail" class="dim truncate">{{ entry.detail }}</span>
            </li>
          </ul>
        </div>
      </section>
    </div>

    <ConfirmDialog
      :open="!!pending"
      :title="`确认执行：${pending?.label || ''}`"
      :target="pending?.action || ''"
      :phrase="pending?.action || ''"
      :description="`请输入动作名 ${pending?.action || ''} 以确认。执行后立即下发到硬件，无法从页面撤销。`"
      warning="三通道动作会直接影响正在进行的烹饪，请先确认现场已处置。"
      :busy="!!busy"
      @confirm="runChannelAction"
      @cancel="pending = null"
    />
  </div>
</template>

<style scoped>
.stack { display: flex; flex-direction: column; gap: 14px; }

/* ------------------------------------------------------------ 通道卡片 */
.chan {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 12px;
  background: var(--panel-2);
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.chan.danger { border-color: var(--danger); background: var(--danger-soft); }
.chan.warn { border-color: var(--warn); background: var(--warn-soft); }

.chan-head { display: flex; align-items: center; gap: 7px; font-size: 12.5px; }
.chan-head strong { flex: 1; min-width: 0; }
.chan-src { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.chip {
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--panel);
  border: 1px solid var(--line);
  font-size: 10.5px;
}
.chan-acts { display: flex; flex-wrap: wrap; gap: 6px; }
.review {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding-top: 7px; border-top: 1px dashed var(--line);
}

.signal-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  border-top: 1px solid var(--line);
}

/* ------------------------------------------------------------ 其它块 */
.scale-steps { display: flex; flex-wrap: wrap; gap: 6px; }
.warn-text { color: var(--warn); }

.input.xs { width: 78px; padding: 3px 6px; font-size: 11.5px; text-align: right; }
tr.dirty td { background: var(--warn-soft); }

.audit { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.audit li { display: flex; align-items: center; gap: 7px; min-width: 0; }

.row.wrap { flex-wrap: wrap; }
</style>
