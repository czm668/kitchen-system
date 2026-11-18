<script setup>
/**
 * A7-06 批次与人员资质。
 *
 * ## 四个页签而不是一页四块
 *
 * 批次、资质、健康证、晨检是四种**不同节奏**的工作：批次是「来货时看一次」，
 * 健康证是「每周扫一遍到期」，晨检是「每天开档前处理」。混在一页会让每天的
 * 例行操作要在一屏里找位置。分页签后，晨检页签每天打开就是待办清单。
 *
 * ## 为什么晨检复核要选「组长卡号」
 *
 * 复核是双通道留痕：TFT 刷组长的卡 + Web 二次确认（`web_confirmed=true`）。
 * 页面上必须真的传一张组长卡号，否则后端会拒绝——这是**故意**的，让「点一下
 * 就放行」这条路走不通。这里从人员表里筛出有组长资质的人自动带出，省掉手输。
 *
 * ## 健康证为什么按剩余天数排序
 *
 * 台账的价值在于「最先过期的排最前」。按姓名排的话，运维每看一次都要自己
 * 找哪个快到期，等于没有台账。
 *
 * ## 为什么还要一个「刷卡准入自检」入口
 *
 * 现场准入发生在 TFT 刷卡的瞬间，管理端只是旁观者。但**出了问题没人能复现**：
 * 「老王说刷不上卡」时，如果页面上不能立刻用同一张卡重放一次，就只能等下次
 * 他再刷。这个入口调的是同一条 `AdmissionController.scan()`，返回的
 * `describes` 就是 TFT 上那行提示——能直接对照现场看到的那句话。
 */
import { computed, onMounted, ref } from 'vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { toastError, toastOk, toastWarn } from '../composables/useToast'
import { fmt, fmtInt } from '../utils/format'

const tab = ref('morning')
const batches = ref([])
const suppliers = ref([])
const ingredientBatches = ref([])
const traceCodes = ref([])
const dishOptions = ref([])
const staff = ref([])
const certs = ref([])
const morning = ref(null)
const loading = ref(true)
const reviewerCard = ref('')
const busy = ref(0)
/** 刷卡自检：卡号 + 工位（工位可空 = 只验准入资格，不判灶位匹配）。 */
const scanCard = ref('')
const scanStation = ref('')
const scanResult = ref(null)
const scanning = ref(false)
const stationOptions = ref([])

/** 溯源录入草稿：null = 未展开编辑，对象 = 正在录入/编辑。 */
const supplierDraft = ref(null)
const batchDraft = ref(null)
const ingredientDraft = ref(null)
const ingredientDraftText = ref('')
const traceDraft = ref(null)
const traceDraftText = ref('')
const savingTrace = ref(false)

const writable = computed(() => can('recipe.write'))

function blankSupplier() {
  return { id: null, name: '', license_no: '', contact: '',
           qualifications: [], state: 'active' }
}
function blankBatch() {
  return { id: null, batch_no: '', dish_id: dishOptions.value[0]?.id ?? null,
           produced_at: '', expire_at: '', state: 'produced' }
}
function blankTrace() {
  return { id: null, code: '', batch_id: null, supplier_id: null }
}
function blankIngredient() {
  return { id: null, batch_no: '', ingredient_name: '',
           supplier_id: suppliers.value[0]?.id ?? null,
           received_at: '', expire_at: '', state: 'received' }
}

const TABS = [
  { key: 'morning', label: '晨检复核' },
  { key: 'certs', label: '健康证台账' },
  { key: 'staff', label: '人员资质' },
  { key: 'batches', label: '批次与溯源' },
]

const leads = computed(() => staff.value.filter((person) =>
  (person.qualifications || []).some((q) => String(q).includes('组长')) &&
  person.card_uid))

const pending = computed(() => (morning.value?.staff || []).filter(
  (row) => row.state === 'locked'))

const missing = computed(() => (morning.value?.staff || []).filter(
  (row) => row.state === 'missing'))

const expiring = computed(() => certs.value.filter((row) => row.warn))

async function load() {
  loading.value = true
  try {
    const [batchData, staffData, certData, morningData, stationData, dishData] = await Promise.all([
      api.get('/api/batches'),
      api.get('/api/staff'),
      api.get('/api/staff/certificates'),
      api.get('/api/morning'),
      api.get('/api/stations'),
      api.get('/api/dishes'),
    ])
    batches.value = batchData.batches || []
    suppliers.value = batchData.suppliers || []
    ingredientBatches.value = batchData.ingredient_batches || []
    traceCodes.value = batchData.trace_codes || []
    dishOptions.value = dishData.dishes || []
    staff.value = staffData.staff || []
    certs.value = certData.certificates || []
    morning.value = morningData
    stationOptions.value = stationData.stations || []
    if (!scanCard.value && staff.value.length) {
      // 默认选中第一个有卡的人：现场自检多半是「刚刷不上卡的那位」
      const withCard = staff.value.find((person) => person.card_uid)
      scanCard.value = withCard ? withCard.card_uid : ''
    }
    if (!reviewerCard.value && leads.value.length) {
      reviewerCard.value = leads.value[0].card_uid
    }
  } catch (err) {
    toastError(err, '合规数据加载失败')
  } finally {
    loading.value = false
  }
}

async function review(row) {
  if (!reviewerCard.value) {
    toastWarn('请先选择复核组长的卡号')
    return
  }
  busy.value = row.staff_id
  try {
    const data = await api.post('/api/morning/review', {
      staff_id: row.staff_id,
      reviewer_card_uid: reviewerCard.value,
      web_confirmed: true,
    })
    if (data.ok) toastOk(`${row.name} 已复核放行`)
    else toastWarn(data.message || '复核未通过')
    await load()
  } catch (err) {
    toastError(err, '复核失败')
  } finally {
    busy.value = 0
  }
}

async function scan() {
  if (!scanCard.value) {
    toastWarn('请先选择要自检的卡号')
    return
  }
  scanning.value = true
  try {
    const data = await api.post('/api/admission/scan', {
      card_uid: scanCard.value,
      station_id: scanStation.value ? Number(scanStation.value) : null,
    })
    scanResult.value = data
    if (data.ok) toastOk(`${data.name || '—'}：${data.detail}`)
    else toastWarn(`${data.name || '—'}：${data.detail}`)
    await load()
  } catch (err) {
    toastError(err, '刷卡自检失败')
  } finally {
    scanning.value = false
  }
}

function editSupplier(row) {
  supplierDraft.value = { ...row, qualifications: [...(row.qualifications || [])] }
  traceDraft.value = null; batchDraft.value = null; ingredientDraft.value = null
}
function editBatch(row) {
  batchDraft.value = { ...row }
  supplierDraft.value = null; traceDraft.value = null; ingredientDraft.value = null
}
function editTrace(row) {
  traceDraft.value = { ...row }
  traceDraftText.value = row.payload ? JSON.stringify(row.payload, null, 2) : ''
  supplierDraft.value = null; batchDraft.value = null; ingredientDraft.value = null
}
function editIngredient(row) {
  ingredientDraft.value = { ...row,
    received_at: row.received_at || '', expire_at: row.expire_at || '' }
  ingredientDraftText.value = row.inspection ? JSON.stringify(row.inspection, null, 2) : ''
  supplierDraft.value = null; batchDraft.value = null; traceDraft.value = null
}

async function saveSupplier() {
  const d = supplierDraft.value
  if (!d) return
  const quals = Array.isArray(d.qualifications)
    ? d.qualifications
    : String(d.qualifications || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean)
  savingTrace.value = true
  try {
    await api.post('/api/trace/suppliers', { ...d, qualifications: quals })
    toastOk(`供应商「${d.name}」已保存`)
    supplierDraft.value = null
    await load()
  } catch (err) {
    toastError(err, '保存供应商失败')
  } finally {
    savingTrace.value = false
  }
}

async function saveBatch() {
  const d = batchDraft.value
  if (!d) return
  savingTrace.value = true
  try {
    await api.post('/api/trace/batches', {
      ...d,
      dish_id: Number(d.dish_id),
      produced_at: d.produced_at || null,
      expire_at: d.expire_at || null,
    })
    toastOk(`批次「${d.batch_no}」已保存`)
    batchDraft.value = null
    await load()
  } catch (err) {
    toastError(err, '保存批次失败')
  } finally {
    savingTrace.value = false
  }
}

async function saveIngredient() {
  const d = ingredientDraft.value
  if (!d) return
  let inspection = {}
  const text = ingredientDraftText.value.trim()
  if (text) {
    try {
      inspection = JSON.parse(text)
      if (!inspection || typeof inspection !== 'object' || Array.isArray(inspection)) {
        throw new Error('检疫信息必须是 JSON 对象')
      }
    } catch (err) {
      toastWarn(`检疫信息不是合法 JSON：${err.message}`)
      return
    }
  }
  savingTrace.value = true
  try {
    await api.post('/api/trace/ingredient-batches', {
      ...d,
      supplier_id: Number(d.supplier_id),
      received_at: d.received_at || null,
      expire_at: d.expire_at || null,
      inspection,
    })
    toastOk(`原料批次「${d.batch_no}」已保存`)
    ingredientDraft.value = null
    ingredientDraftText.value = ''
    await load()
  } catch (err) {
    toastError(err, '保存原料批次失败')
  } finally {
    savingTrace.value = false
  }
}

async function saveTrace() {
  const d = traceDraft.value
  if (!d) return
  let payload = {}
  const text = traceDraftText.value.trim()
  if (text) {
    try {
      payload = JSON.parse(text)
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('检测信息必须是 JSON 对象')
      }
    } catch (err) {
      toastWarn(`检测信息不是合法 JSON：${err.message}`)
      return
    }
  }
  savingTrace.value = true
  try {
    await api.post('/api/trace/codes', {
      ...d,
      batch_id: d.batch_id === '' || d.batch_id == null ? null : Number(d.batch_id),
      supplier_id: d.supplier_id === '' || d.supplier_id == null ? null : Number(d.supplier_id),
      payload,
    })
    toastOk(`溯源码「${d.code}」已保存`)
    traceDraft.value = null
    traceDraftText.value = ''
    await load()
  } catch (err) {
    toastError(err, '保存溯源码失败')
  } finally {
    savingTrace.value = false
  }
}

function fmtTime(v) {
  if (!v) return '—'
  return String(v).replace('T', ' ').slice(0, 16)
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <!-- 顶部提示 -->
    <div class="grid grid-3">
      <div class="mini-stat">
        <span class="tiny muted">晨检待复核</span>
        <strong class="num" :class="{ danger: pending.length }">{{ pending.length }}</strong>
        <span class="tiny muted">人</span>
      </div>
      <div class="mini-stat">
        <span class="tiny muted">健康证 30 天内到期</span>
        <strong class="num" :class="{ warn: expiring.length }">{{ expiring.length }}</strong>
        <span class="tiny muted">张</span>
      </div>
      <div class="mini-stat">
        <span class="tiny muted">在库批次 / 溯源码</span>
        <strong class="num">{{ batches.length }}</strong>
        <span class="tiny muted">批</span>
      </div>
    </div>

    <section class="panel">
      <div class="panel-head">
        <div class="seg">
          <button
            v-for="item in TABS" :key="item.key" type="button"
            class="seg-btn" :class="{ active: tab === item.key }"
            @click="tab = item.key"
          >{{ item.label }}</button>
        </div>
        <div class="row">
          <select v-if="leads.length" v-model="reviewerCard" class="select mini">
            <option v-for="lead in leads" :key="lead.id" :value="lead.card_uid">
              复核人：{{ lead.name }}
            </option>
          </select>
          <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
            <Icon name="refresh" :size="12" />刷新
          </button>
        </div>
      </div>

      <div class="panel-body tight">
        <!-- 晨检 -->
        <template v-if="tab === 'morning'">
          <!-- 刷卡准入自检：与 TFT 走同一个 scan()，可以把现场那句话复现出来 -->
          <div class="scan">
            <span class="tiny muted">刷卡准入自检</span>
            <select v-model="scanCard" class="select mini">
              <option value="">选择要自检的卡</option>
              <option v-for="person in staff" :key="person.id" :value="person.card_uid || ''"
                      :disabled="!person.card_uid">
                {{ person.name }}{{ person.card_uid ? '' : '（未绑卡）' }}
              </option>
            </select>
            <select v-model="scanStation" class="select mini">
              <option value="">不指定工位</option>
              <option v-for="st in stationOptions" :key="st.id" :value="String(st.id)">
                {{ st.code }}{{ st.name ? ' ' + st.name : '' }}
              </option>
            </select>
            <button type="button" class="btn ghost sm"
                    :disabled="scanning || !can('order.write')" @click="scan">
              {{ scanning ? '检查中…' : '模拟刷卡' }}
            </button>
            <span v-if="scanResult" class="scan-out small" :class="scanResult.ok ? 'ok-text' : 'danger-text'">
              {{ scanResult.ok ? '通过' : '拒绝' }} · {{ scanResult.detail }}
              <span v-if="scanResult.needs_morning_check" class="tag warn">需晨检</span>
            </span>
          </div>

          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!morning?.staff?.length" class="empty">还没有人员数据</div>
          <template v-else>
            <div v-if="!pending.length" class="empty ok-empty">
              今天没有待复核的晨检。
              <span v-if="missing.length">（{{ missing.length }} 人还没提交问卷）</span>
            </div>
            <table v-else class="table">
              <thead>
                <tr>
                  <th>姓名</th>
                  <th>状态</th>
                  <th>问卷结论</th>
                  <th style="width: 96px"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in pending" :key="row.staff_id">
                  <td class="bold">{{ row.name }}</td>
                  <td><span class="tag danger">{{ row.state_label }}</span></td>
                  <td class="small pre-wrap">{{ row.describe }}</td>
                  <td class="num">
                    <button
                      class="btn primary sm" :disabled="!can('ticket.write') || busy === row.staff_id"
                      @click="review(row)"
                    >{{ busy === row.staff_id ? '复核中…' : '复核放行' }}</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <table v-if="missing.length" class="table" style="margin-top: 14px">
              <thead>
                <tr><th>未提交问卷</th><th style="width: 110px">状态</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="row in missing" :key="row.staff_id">
                  <td>{{ row.name }}</td>
                  <td><span class="tag plain">未提交</span></td>
                  <td class="tiny muted">开档前须在 TFT 刷卡填问卷，否则无法上灶</td>
                </tr>
              </tbody>
            </table>
          </template>
        </template>

        <!-- 健康证 -->
        <template v-else-if="tab === 'certs'">
          <table class="table">
            <thead>
              <tr>
                <th>姓名</th>
                <th>证号</th>
                <th style="width: 108px">有效期至</th>
                <th class="num" style="width: 86px">剩余</th>
                <th style="width: 88px">状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in certs" :key="i" :class="{ warnrow: row.warn }">
                <td class="bold">{{ row.name }}</td>
                <td class="mono small">{{ row.cert_no || '—' }}</td>
                <td class="mono small">{{ row.valid_to || '—' }}</td>
                <td class="num mono">
                  {{ row.days_left == null ? '—' : fmtInt(row.days_left) }}
                  <span class="tiny muted">天</span>
                </td>
                <td>
                  <span class="tag" :class="row.valid_today ? (row.warn ? 'warn' : 'ok') : 'danger'">
                    {{ row.valid_today ? (row.warn ? '即将到期' : '有效') : '已过期' }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </template>

        <!-- 人员资质 -->
        <template v-else-if="tab === 'staff'">
          <table class="table">
            <thead>
              <tr>
                <th style="width: 70px">工号</th>
                <th style="width: 80px">姓名</th>
                <th>资质</th>
                <th style="width: 120px">卡号</th>
                <th style="width: 84px">健康证</th>
                <th style="width: 70px">在岗</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="person in staff" :key="person.id">
                <td class="mono small">{{ person.code }}</td>
                <td class="bold">{{ person.name }}</td>
                <td>
                  <span v-for="q in person.qualifications" :key="q" class="tag plain" style="margin-right: 4px">
                    {{ q }}
                  </span>
                </td>
                <td class="mono tiny">{{ person.card_uid || '未绑卡' }}</td>
                <td>
                  <span class="tag" :class="person.valid_certificates ? 'ok' : 'danger'">
                    {{ person.valid_certificates }}/{{ person.certificates }}
                  </span>
                </td>
                <td>
                  <span class="tag" :class="person.on_duty ? 'ok' : 'plain'">
                    {{ person.on_duty ? '在岗' : '不在岗' }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </template>

        <!-- 批次与溯源：四类资料的录入/编辑 -->
        <template v-else>
          <div class="trace-hint tiny muted">
            这里的改动会即时生效并写入配置归档，服务重启后仍在；顾客扫码读的就是这份数据。
          </div>

          <!-- 原料批次：溯源主体 -->
          <div class="trace-block">
            <div class="trace-head">
              <h3>原料批次 <span class="tiny muted">（溯源主体：这批肉/菜从哪来）</span></h3>
              <button v-if="writable && !ingredientDraft" class="btn sm" @click="ingredientDraft = blankIngredient(); ingredientDraftText = ''">+ 新增原料批次</button>
            </div>
            <table class="table">
              <thead>
                <tr>
                  <th>原料</th><th>到货批次</th><th>供应商</th><th>到货 / 到期</th><th>检疫</th><th v-if="writable" style="width:56px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in ingredientBatches" :key="row.id">
                  <td class="small">{{ row.ingredient_name }}</td>
                  <td class="mono small">{{ row.batch_no }}</td>
                  <td class="small">{{ row.supplier_name || row.supplier_id }}</td>
                  <td class="mono tiny">{{ row.received_at || '—' }} → {{ row.expire_at || '—' }}</td>
                  <td class="tiny muted">{{ Object.entries(row.inspection || {}).map(([k, v]) => k + ':' + v).join('、') || '—' }}</td>
                  <td v-if="writable"><button class="linkbtn" @click="editIngredient(row)">编辑</button></td>
                </tr>
                <tr v-if="!ingredientBatches.length"><td colspan="6" class="empty">还没有原料批次</td></tr>
              </tbody>
            </table>
            <div v-if="ingredientDraft" class="form-grid">
              <label>原料名称 <input v-model="ingredientDraft.ingredient_name" class="input" placeholder="五花肉" /></label>
              <label>到货批次号 <input v-model="ingredientDraft.batch_no" class="input" placeholder="IB-001" /></label>
              <label>供应商
                <select v-model.number="ingredientDraft.supplier_id" class="input">
                  <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
                </select>
              </label>
              <label>到货时间 <input v-model="ingredientDraft.received_at" class="input" placeholder="2026-11-16 08:00" /></label>
              <label>到期时间 <input v-model="ingredientDraft.expire_at" class="input" placeholder="2026-11-23 08:00" /></label>
              <label>检疫信息（JSON）
                <textarea v-model="ingredientDraftText" class="input" rows="3" placeholder='{"项目":"兽药残留","结果":"合格","报告":"JY-001"}'></textarea>
              </label>
              <div class="form-actions">
                <button class="btn sm primary" :disabled="savingTrace" @click="saveIngredient">保存原料批次</button>
                <button class="btn sm ghost" @click="ingredientDraft = null; ingredientDraftText = ''">取消</button>
              </div>
            </div>
          </div>

          <!-- 溯源码 -->
          <div class="trace-block">
            <div class="trace-head">
              <h3>溯源码 <span class="tiny muted">（顾客扫码查询的码）</span></h3>
              <button v-if="writable && !traceDraft" class="btn sm" @click="traceDraft = blankTrace(); traceDraftText = ''">+ 新增溯源码</button>
            </div>
            <table class="table">
              <thead>
                <tr>
                  <th>溯源码</th><th>菜品批次</th><th>供应商</th><th>检测信息</th><th v-if="writable" style="width:56px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in traceCodes" :key="row.code">
                  <td class="mono small">{{ row.code }}</td>
                  <td class="small">{{ batches.find((b) => b.id === row.batch_id)?.batch_no || row.batch_id || '—' }}</td>
                  <td class="small">{{ suppliers.find((s) => s.id === row.supplier_id)?.name || row.supplier_id || '—' }}</td>
                  <td class="tiny muted pre-wrap">{{ Object.entries(row.payload || {}).map(([k, v]) => `${k}:${v}`).join('，') || '—' }}</td>
                  <td v-if="writable"><button class="linkbtn" @click="editTrace(row)">编辑</button></td>
                </tr>
                <tr v-if="!traceCodes.length"><td colspan="5" class="empty">还没有溯源码</td></tr>
              </tbody>
            </table>
            <div v-if="traceDraft" class="trace-form">
              <div class="form-grid">
                <label>溯源码 <input v-model="traceDraft.code" class="input" placeholder="TC-DEMO-0005" /></label>
                <label>关联批次
                  <select v-model="traceDraft.batch_id" class="select">
                    <option :value="null">— 不关联 —</option>
                    <option v-for="b in batches" :key="b.id" :value="b.id">{{ b.batch_no }}</option>
                  </select>
                </label>
                <label>供应商
                  <select v-model="traceDraft.supplier_id" class="select">
                    <option :value="null">— 不关联 —</option>
                    <option v-for="s in suppliers" :key="s.id" :value="s.id">{{ s.name }}</option>
                  </select>
                </label>
                <label class="full">检测信息（JSON 对象，扫码后逐条展示）
                  <textarea v-model="traceDraftText" class="input" rows="3" placeholder='{"dish":"红烧肉","检测":"合格","检验员":"王组长","报告":"JC-2026-1121"}'></textarea>
                </label>
              </div>
              <div class="form-actions">
                <button class="btn sm primary" :disabled="savingTrace" @click="saveTrace">保存溯源码</button>
                <button class="btn sm ghost" @click="traceDraft = null; traceDraftText = ''">取消</button>
              </div>
            </div>
          </div>

          <!-- 批次 -->
          <div class="trace-block">
            <div class="trace-head">
              <h3>食材批次</h3>
              <button v-if="writable && !batchDraft" class="btn sm" @click="batchDraft = blankBatch()">+ 新增批次</button>
            </div>
            <table class="table">
              <thead>
                <tr>
                  <th>批次号</th><th>菜品</th><th>生产时间</th><th>到期时间</th><th v-if="writable" style="width:56px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in batches" :key="row.id">
                  <td class="mono small">{{ row.batch_no }}</td>
                  <td class="small">{{ dishOptions.find((d) => d.id === row.dish_id)?.name || row.dish_id }}</td>
                  <td class="mono small">{{ fmtTime(row.produced_at) }}</td>
                  <td class="mono small">{{ fmtTime(row.expire_at) }}</td>
                  <td v-if="writable"><button class="linkbtn" @click="editBatch(row)">编辑</button></td>
                </tr>
                <tr v-if="!batches.length"><td colspan="5" class="empty">还没有批次</td></tr>
              </tbody>
            </table>
            <div v-if="batchDraft" class="trace-form">
              <div class="form-grid">
                <label>批次号 <input v-model="batchDraft.batch_no" class="input" placeholder="B-DEMO-005" /></label>
                <label>菜品
                  <select v-model="batchDraft.dish_id" class="select">
                    <option v-for="d in dishOptions" :key="d.id" :value="d.id">{{ d.name }}</option>
                  </select>
                </label>
                <label>生产时间 <input v-model="batchDraft.produced_at" class="input" placeholder="2026-11-17 09:00:00" /></label>
                <label>到期时间 <input v-model="batchDraft.expire_at" class="input" placeholder="2026-11-19 09:00:00" /></label>
              </div>
              <div class="form-actions">
                <button class="btn sm primary" :disabled="savingTrace" @click="saveBatch">保存批次</button>
                <button class="btn sm ghost" @click="batchDraft = null">取消</button>
              </div>
            </div>
          </div>

          <!-- 供应商 -->
          <div class="trace-block">
            <div class="trace-head">
              <h3>供应商</h3>
              <button v-if="writable && !supplierDraft" class="btn sm" @click="supplierDraft = blankSupplier()">+ 新增供应商</button>
            </div>
            <table class="table">
              <thead>
                <tr>
                  <th>名称</th><th>许可证号</th><th>联系人</th><th>资质</th><th v-if="writable" style="width:56px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in suppliers" :key="row.id">
                  <td class="small">{{ row.name }}</td>
                  <td class="mono small">{{ row.license_no || '—' }}</td>
                  <td class="small">{{ row.contact || '—' }}</td>
                  <td class="tiny muted">{{ (row.qualifications || []).join('、') || '—' }}</td>
                  <td v-if="writable"><button class="linkbtn" @click="editSupplier(row)">编辑</button></td>
                </tr>
                <tr v-if="!suppliers.length"><td colspan="5" class="empty">还没有供应商</td></tr>
              </tbody>
            </table>
            <div v-if="supplierDraft" class="trace-form">
              <div class="form-grid">
                <label>名称 <input v-model="supplierDraft.name" class="input" placeholder="XX食品厂" /></label>
                <label>许可证号 <input v-model="supplierDraft.license_no" class="input" /></label>
                <label>联系人 <input v-model="supplierDraft.contact" class="input" /></label>
                <label class="full">资质（逗号分隔） <input v-model="supplierDraft.qualifications" class="input" placeholder="营业执照,食品生产许可证" /></label>
              </div>
              <div class="form-actions">
                <button class="btn sm primary" :disabled="savingTrace" @click="saveSupplier">保存供应商</button>
                <button class="btn sm ghost" @click="supplierDraft = null">取消</button>
              </div>
            </div>
          </div>
        </template>
      </div>
    </section>

    <p class="tiny muted">
      留样（#14）与消毒（#15）的判定结果在「工单中心」与「总览」里看：
      ≥125g 才算有效留样，只有 100~120℃ 的累计时长计入消毒达标。
    </p>
  </div>
</template>

<style scoped>
.scan {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 0 12px; border-bottom: 1px dashed var(--line); margin-bottom: 10px;
}
.scan-out { display: inline-flex; align-items: center; gap: 6px; }
.ok-text { color: var(--ok); }
.danger-text { color: var(--danger); }

.seg {
  display: inline-flex;
  padding: 2px;
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  border: 1px solid var(--line);
}
.seg-btn {
  border: none;
  background: none;
  color: var(--muted);
  padding: 4px 11px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.seg-btn.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }

.mini-stat {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 11px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
}
.mini-stat strong { font-size: 20px; }
.mini-stat strong.danger { color: var(--danger); }
.mini-stat strong.warn { color: var(--warn); }

.ok-empty { color: var(--ok); }
.warnrow td { background: var(--warn-soft); }

.select.mini { height: 26px; width: auto; padding: 0 7px; font-size: 11.5px; }

/* 溯源录入 */
.trace-hint { padding: 6px 0 12px; border-bottom: 1px dashed var(--line); }
.trace-block { margin-top: 16px; }
.trace-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 7px;
}
.trace-head h3 { margin: 0; font-size: 13px; font-weight: 600; }
.linkbtn {
  border: none; background: none; color: var(--accent); cursor: pointer;
  font-size: 12px; padding: 2px 4px;
}
.linkbtn:hover { text-decoration: underline; }
.trace-form {
  margin-top: 8px; padding: 12px; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--panel);
}
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-2); }
.form-grid label.full { grid-column: 1 / -1; }
.form-grid .input, .form-grid .select { width: 100%; }
.form-grid textarea.input { height: auto; resize: vertical; font-family: var(--mono); }
.form-actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
