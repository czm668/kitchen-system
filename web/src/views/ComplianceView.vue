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
    const [batchData, staffData, certData, morningData, stationData] = await Promise.all([
      api.get('/api/batches'),
      api.get('/api/staff'),
      api.get('/api/staff/certificates'),
      api.get('/api/morning'),
      api.get('/api/stations'),
    ])
    batches.value = batchData.batches || []
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

        <!-- 批次 -->
        <template v-else>
          <table class="table">
            <thead>
              <tr>
                <th style="width: 80px">批次</th>
                <th>供应商</th>
                <th style="width: 110px">到货日期</th>
                <th>检测 / 备注</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in batches" :key="row.id">
                <td class="mono small">#{{ row.id }}</td>
                <td>{{ row.supplier_name || row.supplier_id || '—' }}</td>
                <td class="mono small">{{ row.received_on || row.created_at || '—' }}</td>
                <td class="tiny muted pre-wrap">
                  {{ row.inspection || row.note || JSON.stringify(row).slice(0, 90) }}
                </td>
              </tr>
            </tbody>
          </table>
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
</style>
