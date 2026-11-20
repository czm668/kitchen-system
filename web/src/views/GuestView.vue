<script setup>
/**
 * A11 扩展模块：客诉与补偿审批台 + 顾客画像 + AI 调用审计。
 *
 * ## 这一页刻意不做的三件事
 *
 * 1. **不显示「退款」按钮**：项目无支付链路，审批通过只生成补偿凭证。
 *    按钮写「终审批准」而不是「同意退款」，避免操作员和答辩老师误解。
 * 2. **不让 AI 结论直接驱动按钮**：`ai_suggestion` 只作为参考卡片展示，
 *    核实/批准/驳回三个动作完全由人按当前 state 决定。
 * 3. **不把金额做成可编辑框**：金额来自订单下单时的单价快照，改了就等于
 *    事后篡改凭证基数。界面只读展示。
 *
 * 权限：`guest.review`（lead/manager）核证据，`guest.approve`（仅 manager）
 * 终审；双人复核（核实人 ≠ 终审人）由后端硬校验，前端只做按钮可见性。
 */
import { computed, onMounted, ref } from 'vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import StateBlock from '../components/StateBlock.vue'
import { can } from '../stores/auth'
import { toastError, toastOk, toastWarn } from '../composables/useToast'
import { fmtDateTime } from '../utils/format'

const overview = ref({ by_state: {}, ai: {}, overdue: [] })
const complaints = ref([])
const runs = ref([])
const filter = ref('all')
const selectedId = ref(0)
const loading = ref(true)
const busy = ref(false)
const checked = ref({})
const compensation = ref('redo')
const rejectReason = ref('')
const rejectCode = ref('evidence_missing')
const showReject = ref(false)
const followupNote = ref('')
const profile = ref(null)
const quality = ref({ csat: 0, first_response_min: 0, feedback_count: 0, reject_rate: 0,
  followup_total: 0, followup_done: 0, followup_pending: 0, followup_overdue: 0,
  followup_late_done: 0, followup_on_time_rate: 0 })
// 质检评分卡：自动分只覆盖可取证的 70 分，服务态度 30 分必须人工补评
const qa = ref({ count: 0, avg: 0, pass_rate: 0, excellent_rate: 0, coverage: 0.7, grades: {}, top_deductions: [], by_actor: {} })
const qaGradeClass = { '优秀': 'ok', '良好': 'ok', '合格': 'info', '待改进': 'warn', '不合格': 'danger' }

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'submitted', label: '待核实' },
  { key: 'evidence_ok', label: '待终审' },
  { key: 'approved', label: '已批准' },
  { key: 'rejected', label: '已驳回' },
]
const STATE = {
  submitted: { label: '待组长核实证据', tone: 'warn' },
  evidence_ok: { label: '待管理人员终审', tone: 'info' },
  approved: { label: '已批准 · 凭证已生成', tone: 'ok' },
  rejected: { label: '已驳回', tone: 'danger' },
  auto_closed: { label: 'AI 即时答复结案', tone: 'ok' },
}
const COMPENSATIONS = [
  { key: 'redo', label: '重做一份' },
  { key: 'waive', label: '本单免单' },
  { key: 'coupon', label: '补偿优惠券' },
  { key: 'dish_gift', label: '赠送菜品' },
  { key: 'voucher', label: '代金券' },
  { key: 'priority', label: '加急重做' },
  { key: 'apology', label: '致歉说明' },
  // 项目无会员体系，积分批了也兑不了；保留展示但禁用，与后端硬拦一致
  { key: 'points', label: '积分补偿（不可用）', disabled: true },
]
// 驳回原因码：内部原因填在输入框，顾客只看到对应标准说明
const REJECT_CODES = [
  { key: 'evidence_missing', label: '缺少证据' },
  { key: 'evidence_unclear', label: '证据无法辨认' },
  { key: 'amount_exceeded', label: '诉求金额超出本单范围' },
  { key: 'duplicate', label: '重复提交' },
  { key: 'not_eligible', label: '不符合补偿条件' },
  { key: 'other', label: '其他' },
]

const shown = computed(() => filter.value === 'all'
  ? complaints.value
  : complaints.value.filter((row) => row.state === filter.value))
const selected = computed(() => complaints.value.find((row) => row.id === selectedId.value) || null)
const canReview = computed(() => can('guest.review'))
const canApprove = computed(() => can('guest.approve'))
const canResolve = computed(() => can('guest.resolve'))
const canFollowup = computed(() => can('guest.followup'))

function stateOf(value) { return STATE[value] || { label: value, tone: 'plain' } }

function actionText(handling) {
  return ((handling && handling.actions) || []).map((a) => a.detail || a.type).join('；')
}

async function load() {
  loading.value = true
  try {
    const [stat, list, llm, qc] = await Promise.all([
      api.get('/api/guest/overview'),
      api.get('/api/guest/complaints'),
      api.get('/api/guest/llm-runs?limit=20'),
      api.get('/api/guest/quality'),
    ])
    overview.value = stat
    complaints.value = list.complaints || []
    runs.value = (llm.runs || []).slice().reverse()
    quality.value = qc
    qa.value = qc.qa || qa.value
    if (!complaints.value.some((row) => row.id === selectedId.value)) {
      selectedId.value = complaints.value.length ? complaints.value[0].id : 0
    }
  } catch (err) {
    toastError(err, '客诉数据加载失败')
  } finally {
    loading.value = false
  }
  await loadProfile()
}

async function loadProfile() {
  const row = selected.value
  if (!row || !row.guest_key) { profile.value = null; return }
  try {
    profile.value = await api.get(`/api/guest/profiles/${encodeURIComponent(row.guest_key)}`)
  } catch {
    profile.value = null
  }
}

async function refreshPortrait() {
  const row = selected.value
  if (!row) return
  busy.value = true
  try {
    profile.value = await api.post(
      `/api/guest/profiles/${encodeURIComponent(row.guest_key)}/portrait`, {})
    toastOk('画像已重新生成')
  } catch (err) {
    toastError(err, '画像生成失败')
  } finally {
    busy.value = false
  }
}

function pick(row) {
  selectedId.value = row.id
  checked.value = {}
  // 默认选中矩阵给出的标准方案，减少「凭感觉选」——管理员可改，偏离会进审计
  const advice = row.compensation || {}
  compensation.value = row.method && row.method !== 'none'
    ? row.method
    : (advice.suggest || 'redo')
  showReject.value = false
  followupNote.value = ''
  loadProfile()
}

async function act(path, body, okText) {
  busy.value = true
  try {
    await api.post(path, body)
    toastOk(okText)
    await load()
  } catch (err) {
    toastError(err, '操作失败')
  } finally {
    busy.value = false
  }
}

function review() {
  const row = selected.value
  if (!row) return
  const ids = Object.keys(checked.value).filter((key) => checked.value[key]).map(Number)
  if (!ids.length) { toastWarn('请先勾选已核实的证据'); return }
  act(`/api/guest/complaints/${row.id}/review`,
    { evidence_ids: ids, expected_version: row.version, note: '已核对订单与出餐记录' },
    '证据已核实，等待管理人员终审')
}

function approve() {
  const row = selected.value
  if (!row) return
  act(`/api/guest/complaints/${row.id}/approve`,
    { compensation: compensation.value, expected_version: row.version,
      note: '核实无误，同意补偿' },
    `终审通过，凭证已生成（不发生实际退款）`)
}

function resolve() {
  const row = selected.value
  if (!row) return
  act(`/api/guest/complaints/${row.id}/resolve`,
    { compensation: compensation.value, expected_version: row.version,
      note: '管理员单人直通处理' },
    '已直接处理，凭证已生成（不发生实际退款）')
}

function doFollowup() {
  const row = selected.value
  if (!row) return
  if (!followupNote.value.trim()) { toastWarn('请填写回访联系情况'); return }
  act(`/api/guest/complaints/${row.id}/followup`,
    { note: followupNote.value.trim(), expected_version: row.version },
    '回访已记录并关闭任务（回访不改评分与工单状态）')
  followupNote.value = ''
}

function reject() {
  const row = selected.value
  if (!row) return
  if (!rejectReason.value.trim()) { toastWarn('请填写驳回原因'); return }
  act(`/api/guest/complaints/${row.id}/reject`,
    { reason: rejectReason.value.trim(), reason_code: rejectCode.value,
      expected_version: row.version },
    '已驳回，顾客看到的是标准说明（内部原因只进审计）')
  showReject.value = false
  rejectReason.value = ''
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <div class="toolbar">
      <div class="seg">
        <button v-for="item in FILTERS" :key="item.key" type="button"
                class="seg-btn" :class="{ active: filter === item.key }"
                @click="filter = item.key">{{ item.label }}</button>
      </div>
      <span class="spacer"></span>
      <span class="tiny muted">待处理 {{ overview.pending || 0 }} 单</span>
      <span v-if="overview.overdue && overview.overdue.length" class="tag warn">
        SLA 超时 {{ overview.overdue.length }} 单
      </span>
      <!-- 质检口径：低分与慢响应优先于单量被看到 -->
      <span v-if="quality.feedback_count" class="tag"
            :class="quality.csat >= 4 ? 'ok' : 'warn'">
        满意度 {{ quality.csat }} 分（{{ quality.feedback_count }} 条评价）
      </span>
      <span v-if="quality.feedback_count" class="tiny muted">
        首响中位 {{ quality.first_response_min }} 分
      </span>
      <span v-if="quality.followup_pending" class="tag danger">
        低分待回访 {{ quality.followup_pending }} 单
        <template v-if="quality.followup_overdue"> · 超时 {{ quality.followup_overdue }}</template>
      </span>
      <span v-if="quality.followup_total" class="tiny muted">
        回访 {{ quality.followup_done }}/{{ quality.followup_total }}
        <template v-if="quality.followup_done">
          · 及时率 {{ Math.round(quality.followup_on_time_rate * 100) }}%
          <template v-if="quality.followup_late_done">（超时补做 {{ quality.followup_late_done }}）</template>
        </template>
      </span>
      <span v-if="qa.count" class="tag" :class="qa.avg >= 90 ? 'ok' : qa.avg >= 70 ? 'info' : 'warn'">
        质检 {{ qa.avg }} 分 · 合格 {{ Math.round(qa.pass_rate * 100) }}%
      </span>
      <button class="btn ghost sm" type="button" :disabled="loading" @click="load">
        <Icon name="refresh" :size="12" />刷新
      </button>
    </div>

    <div class="notice tiny">
      演示环境无支付链路：审批通过只生成补偿凭证，不发生实际退款。AI 结论仅供参考，
      核实与终审必须由组长 / 管理人员分别完成（双人复核）。
    </div>

    <div class="layout">
      <!-- 工单列表 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">客诉工单<span class="panel-sub"> · {{ shown.length }} 条</span></span>
        </div>
        <div class="panel-body tight">
          <StateBlock v-if="loading" />
          <div v-else-if="!shown.length" class="empty">暂无工单，可从小程序「智能客服」提交一条。</div>
          <div v-for="row in shown" :key="row.id"
               class="row-item" :class="{ active: row.id === selectedId }" @click="pick(row)">
            <div class="row between">
              <span class="mono bold">{{ row.complaint_no }}</span>
              <span class="tag" :class="stateOf(row.state).tone">{{ stateOf(row.state).label }}</span>
            </div>
            <div class="tiny muted">
              {{ row.category }} · {{ row.customer_ref || '散客' }} ·
              {{ fmtDateTime(row.created_at) }}
            </div>
            <div class="tiny ellipsis">{{ row.text }}</div>
            <div v-if="row.wants_refund" class="tiny muted">
              补偿基数 ¥{{ row.amount }} · 需两级审批
            </div>
            <div v-if="row.blocker" class="tiny muted">
              卡点：{{ row.blocker }}
              <span v-if="row.overdue" class="tag warn">已超时</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 详情 -->
      <section class="panel detail">
        <div class="panel-head">
          <span class="panel-title">工单详情</span>
          <span v-if="selected" class="tag plain mono">v{{ selected.version }}</span>
        </div>
        <div class="panel-body" v-if="selected">
          <div class="detail-head">
            <div>
              <div class="mono bold">{{ selected.complaint_no }}</div>
              <div class="tiny muted">{{ selected.category }} · {{ selected.table_id || '-' }} 号桌</div>
            </div>
            <span class="tag" :class="stateOf(selected.state).tone">
              {{ stateOf(selected.state).label }}
            </span>
          </div>
          <p class="text">{{ selected.text }}</p>

          <!-- 处理决策：阶段 / 卡点 / 超时 / 已执行动作（确定性，不代替审批） -->
          <div v-if="selected.handling && Object.keys(selected.handling).length" class="ai-card">
            <div class="row between">
              <span class="label">AI 处理决策</span>
              <span v-if="selected.handling.overdue" class="tag warn">
                SLA 超时 {{ selected.handling.overdue_min }} 分
              </span>
              <span v-else class="tag plain">SLA 剩 {{ selected.handling.sla_remain_min }} 分</span>
            </div>
            <div class="tiny" style="margin-top:6px">
              当前环节：{{ selected.handling.stage_label || selected.state }}
            </div>
            <div class="tiny">
              卡点：{{ selected.handling.blocker || '无' }}
              <span v-if="selected.handling.blocker_owner"> · 责任人 {{ selected.handling.blocker_owner }}</span>
            </div>
            <div v-if="actionText(selected.handling)" class="tiny muted" style="margin-top:6px">
              已执行：{{ actionText(selected.handling) }}
            </div>
            <div v-if="selected.handling.urgent && selected.handling.urgent.length"
                 class="tiny danger-text" style="margin-top:6px">
              待跟进：{{ selected.handling.urgent.join('；') }}
            </div>
          </div>

          <!-- AI 建议：只读参考 -->
          <div class="ai-card">
            <div class="row between">
              <span class="label">AI 辅助建议（仅供参考）</span>
              <span class="tag plain">
                {{ selected.ai_suggestion.mode === 'live' ? '在线模型' : '离线规则引擎' }}
              </span>
            </div>
            <div class="tiny">{{ selected.ai_suggestion.summary || '—' }}</div>
            <div v-if="selected.ai_suggestion.reply" class="tiny" style="margin-top:6px">
              对客回复：{{ selected.ai_suggestion.reply }}
            </div>
            <div v-if="selected.ai_suggestion.degraded_reason" class="tiny muted">
              自动降级：{{ selected.ai_suggestion.degraded_reason }}
            </div>
            <div v-if="selected.ai_suggestion.injection_suspected" class="tiny danger-text">
              检测到疑似指令注入，已强制转人工（原文未被修改）
            </div>
          </div>

          <!-- 证据 -->
          <div class="label" style="margin-top: 14px">证据（{{ selected.verified_count || 0 }} 条已核实）</div>
          <div v-if="!selected.evidences.length" class="tiny muted">顾客未提交证据</div>
          <label v-for="ev in selected.evidences" :key="ev.id" class="ev-row">
            <input v-if="canReview && selected.needs_evidence" v-model="checked[ev.id]"
                   type="checkbox" />
            <span class="tag plain">{{ ev.kind }}</span>
            <a v-if="ev.kind === 'photo' && ev.url" :href="ev.url" target="_blank"
               class="ev-photo" title="查看原图">
              <img :src="ev.url" alt="证据图片" width="44" height="44"
                   loading="lazy" decoding="async" />
            </a>
            <span class="tiny grow">{{ ev.content }}</span>
            <span v-if="ev.verified" class="tag ok">已核实 {{ ev.verified_by }}</span>
          </label>

          <!-- 补偿建议：分类定方案、级别定上限，管理员不用凭感觉选 -->
          <div v-if="selected.compensation" class="ai-card" style="margin-top: 12px">
            <div class="row between">
              <span class="label">补偿建议 · {{ selected.compensation.tier_label }}</span>
              <span class="tag plain mono">{{ selected.compensation.cap_label }}</span>
            </div>
            <div class="tiny" style="margin-top: 6px">
              标准方案：<b>{{ (COMPENSATIONS.find(c => c.key === selected.compensation.suggest) || {}).label || selected.compensation.suggest }}</b>
              <span v-if="selected.compensation.alternatives.length">
                · 可选：{{ selected.compensation.alternatives.map(k => (COMPENSATIONS.find(c => c.key === k) || {}).label || k).join(' / ') }}
              </span>
            </div>
            <div class="tiny muted" style="margin-top: 4px">
              {{ selected.compensation.decide_by }}可决 · {{ selected.compensation.tier_desc }}
            </div>
            <div v-for="(why, i) in selected.compensation.reasons" :key="i" class="tiny muted">
              · {{ why }}
            </div>
            <div v-if="selected.compensation.chosen" class="tiny" style="margin-top: 6px">
              已选方案：{{ selected.compensation.chosen_label || selected.compensation.chosen }}
            </div>
          </div>

          <!-- 质检评分卡：只评可取证项，服务态度明确交人工 -->
          <div v-if="selected.qa" class="ai-card" style="margin-top: 12px">
            <div class="row between">
              <span class="label">质检评分卡 · 机器可判 {{ Math.round(selected.qa.coverage * 100) }}%</span>
              <span class="tag" :class="qaGradeClass[selected.qa.grade] || 'plain'">
                {{ selected.qa.grade }} {{ selected.qa.normalized }} 分
              </span>
            </div>
            <div class="tiny muted" style="margin: 6px 0">
              自动 {{ selected.qa.score }}/{{ selected.qa.applicable_max }} 分 ·
              服务态度 30 分待人工补评 · 不是完整质检分
            </div>
            <div v-if="selected.qa.deductions.length" class="qa-list">
              <div v-for="d in selected.qa.deductions" :key="d.id" class="qa-line">
                <span class="mono">-{{ d.deduct }}</span>
                <span>{{ d.name }}：{{ d.why }}</span>
              </div>
            </div>
            <div v-else class="tiny" style="color: var(--green, #2f7a5a)">
              可判项零扣分
            </div>
            <div class="tiny muted" style="margin-top: 6px">
              人工补评：{{ selected.qa.manual_items.map(x => x.name + '(' + x.max + ')').join('、') }}
            </div>
          </div>

          <!-- 低分当日回访：发事件只是"看到"，这里才是"跟到"的闭环 -->
          <div v-if="selected.followup_due" class="ai-card" style="margin-top: 12px">
            <div class="row between">
              <span class="label">低分当日回访 · {{ selected.feedback_score }} 分</span>
              <span class="tag" :class="selected.followup_late ? 'warn'
                    : (selected.followup_state === 'done' ? 'ok'
                    : (selected.followup_overdue ? 'danger' : 'warn'))">
                {{ selected.followup_late ? '超时补做'
                   : (selected.followup_state === 'done' ? '已完成'
                   : (selected.followup_overdue ? '已超时' : '待回访')) }}
              </span>
            </div>
            <div class="tiny muted" style="margin-top:6px">
              当日截止 {{ selected.followup_due.slice(0, 16).replace('T', ' ') }}
              <template v-if="selected.followup_state === 'done'">
                · {{ selected.followup_by }} 于 {{ selected.followup_at.slice(0, 16).replace('T', ' ') }} 回访
              </template>
            </div>
            <div v-if="selected.followup_state === 'done'" class="tiny" style="margin-top:6px">
              联系情况：{{ selected.followup_note }}
            </div>
            <template v-else>
              <div v-if="selected.followup_script" class="tiny" style="margin-top:8px">
                建议话术：{{ selected.followup_script.text }}
              </div>
              <div class="tiny muted" style="margin-top:4px">{{ selected.followup_script.forbid }}</div>
              <div v-if="canFollowup" class="actions" style="margin-top:8px">
                <input v-model="followupNote" class="input sm grow"
                       placeholder="填写联系情况（如：已电话联系，顾客接受重做方案）" />
                <button class="btn sm primary" :disabled="busy" @click="doFollowup">
                  记录回访并关闭
                </button>
              </div>
            </template>
          </div>

          <!-- 金额：只读 -->
          <div v-if="selected.wants_refund" class="kv-row">
            <span class="tiny muted">补偿基数（下单价格快照）</span>
            <span class="mono bold">¥{{ selected.amount }}</span>
          </div>
          <div v-if="selected.voucher_no" class="kv-row">
            <span class="tiny muted">补偿凭证</span>
            <span class="mono">{{ selected.voucher_no }}</span>
          </div>
          <div class="tiny muted" style="margin-top: 6px">{{ selected.compensation_note }}</div>

          <!-- 动作 -->
          <div class="actions">
            <!-- 管理员单人直通：选方案后一步核实+终审，无需等组长/另一人 -->
            <template v-if="canResolve && (selected.needs_evidence || selected.needs_approval)">
              <select v-model="compensation" class="select sm">
                <option v-for="c in COMPENSATIONS" :key="c.key" :value="c.key"
                        :disabled="c.disabled">{{ c.label }}</option>
              </select>
              <button class="btn sm primary" :disabled="busy" @click="resolve">
                单人直通处理
              </button>
            </template>
            <template v-if="canReview && selected.needs_evidence">
              <button class="btn sm" :disabled="busy" @click="review">核实勾选的证据</button>
            </template>
            <template v-if="canApprove && selected.needs_approval && !canResolve">
              <select v-model="compensation" class="select sm">
                <option v-for="c in COMPENSATIONS" :key="c.key" :value="c.key"
                        :disabled="c.disabled">{{ c.label }}</option>
              </select>
              <button class="btn sm" :disabled="busy" @click="approve">终审批准</button>
            </template>
            <button v-if="canReview && !['approved', 'auto_closed'].includes(selected.state)"
                    class="btn ghost sm danger-text" :disabled="busy"
                    @click="showReject = !showReject">驳回</button>
          </div>
          <div v-if="showReject" class="actions" style="margin-top: 8px">
            <select v-model="rejectCode" class="select sm">
              <option v-for="c in REJECT_CODES" :key="c.key" :value="c.key">{{ c.label }}</option>
            </select>
            <input v-model="rejectReason" class="input sm grow"
                   placeholder="内部原因（管理端与审计可见，不推给顾客）" />
            <button class="btn ghost sm" :disabled="busy" @click="reject">确认驳回</button>
          </div>
          <div v-if="showReject" class="tiny muted" style="margin-top: 6px">
            顾客将收到按「{{
              (REJECT_CODES.find(c => c.key === rejectCode) || {}).label
            }}」生成的标准说明（含补证操作与 7 天期限）；上面的内部原因只进审计。
          </div>
          <div v-if="canResolve && (selected.needs_evidence || selected.needs_approval)"
               class="tiny muted" style="margin-top: 8px">
            管理员可单人直通：选方案后一步完成核实与终审。需要双人复核时设
            KITCHEN_DUAL_REVIEW=1 重启服务，即可恢复「核实人≠终审人」流程。
          </div>
          <div v-else-if="!canApprove && selected.needs_approval" class="tiny muted" style="margin-top: 8px">
            终审仅管理人员可操作。
          </div>

          <!-- 审批轨迹 -->
          <div class="label" style="margin-top: 16px">处理轨迹</div>
          <div v-for="(h, i) in selected.history" :key="i" class="tiny muted">
            {{ fmtDateTime(h.time) }} · {{ h.actor }} · {{ h.action }} → {{ h.state }}
          </div>
        </div>
        <div v-else class="panel-body"><div class="empty">选择左侧工单查看详情</div></div>
      </section>

      <!-- 画像 + AI 审计 -->
      <section class="panel side">
        <div class="panel-head"><span class="panel-title">顾客画像</span></div>
        <div class="panel-body tight">
          <div v-if="!profile" class="tiny muted">选择工单后加载匿名档案</div>
          <template v-else>
            <div class="tiny mono">{{ profile.profile.guest_key }}</div>
            <div class="text" style="margin-top: 8px">
              {{ profile.portrait.portrait || '暂无画像' }}
            </div>
            <div class="tiny muted" style="margin-top: 8px">
              {{ profile.portrait.mode === 'live' ? '在线模型' : '离线规则引擎' }} ·
              {{ profile.portrait.facts.order_count }} 单 ·
              客诉 {{ profile.portrait.facts.complaint_count }} 次
            </div>
            <div class="tiny muted" style="margin-top: 6px">
              标签：{{ profile.profile.tags.join('、') || '无' }}
            </div>
            <div class="tiny muted" style="margin-top: 6px">{{ profile.privacy_note }}</div>
            <button v-if="canReview" class="btn ghost sm" style="margin-top: 10px"
                    :disabled="busy" @click="refreshPortrait">重新生成画像</button>
          </template>
        </div>
        <div class="panel-head" style="border-top: 1px solid var(--line)">
          <span class="panel-title">AI 调用审计</span>
          <span class="panel-sub">{{ overview.ai.calls || 0 }} 次</span>
        </div>
        <div class="panel-body tight">
          <div class="tiny muted">
            在线 {{ overview.ai.live || 0 }} · 规则 {{ overview.ai.rule || 0 }} ·
            降级 {{ overview.ai.degraded || 0 }}
          </div>
          <div v-for="(run, i) in runs.slice(0, 8)" :key="i" class="run-row tiny">
            <span class="mono">{{ run.scenario }}</span>
            <span class="tag plain">{{ run.mode }}</span>
            <span class="muted">{{ run.latency_ms }}ms</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.seg { display: inline-flex; flex-wrap: wrap; padding: 2px; border-radius: var(--radius-sm);
  background: var(--panel); border: 1px solid var(--line); }
.seg-btn { border: none; background: none; color: var(--muted); padding: 4px 11px;
  border-radius: 4px; cursor: pointer; font-size: 12px; white-space: nowrap; }
.seg-btn.active { background: var(--accent-soft); color: var(--accent-ink); font-weight: 600; }
.notice { padding: 9px 14px; border-radius: var(--radius-sm);
  background: var(--accent-soft); color: var(--text); }
.layout { display: grid; grid-template-columns: 280px minmax(0, 1fr) 260px; gap: 14px; align-items: start; }
@media (max-width: 1240px) { .layout { grid-template-columns: 260px minmax(0, 1fr); }
  .side { grid-column: 1 / -1; } }
@media (max-width: 900px) { .layout { grid-template-columns: minmax(0, 1fr); } }

.row-item { padding: 9px 12px; border-bottom: 1px solid var(--line); cursor: pointer; }
.row-item:last-child { border-bottom: none; }
.row-item:hover { background: var(--panel-2); }
.row-item.active { background: var(--accent-soft); }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 3px; }
.detail-head { display: flex; justify-content: space-between; align-items: start; gap: 12px; }
.text { font-size: 13px; line-height: 1.7; margin: 10px 0; }
.label { font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 6px; }
.ai-card { padding: 10px 12px; border-radius: var(--radius-sm);
  background: var(--panel-2); border-left: 3px solid var(--accent); }
.ev-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; }
.ev-photo { flex: none; width: 44px; height: 44px; border-radius: 6px; overflow: hidden; border: 1px solid var(--line); }
.ev-photo img { width: 100%; height: 100%; object-fit: cover; display: block; }
.btn.primary { background: var(--accent, #0071e3); color: #fff; border-color: var(--accent, #0071e3); }
.grow { flex: 1; min-width: 0; }
.kv-row { display: flex; justify-content: space-between; align-items: center;
  margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line); }
.actions { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-top: 14px; }
.danger-text { color: var(--danger); }
.input.sm { height: 28px; padding: 0 8px; font-size: 12px; }
.select.sm { height: 28px; padding: 0 6px; font-size: 12px; }
.run-row { display: flex; align-items: center; gap: 7px; padding: 4px 0; }
/* 质检扣分明细：每行一条可复核的扣分理由 */
.qa-list { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 6px; }
.qa-line { display: flex; gap: 8px; align-items: start; font-size: 12px;
  line-height: 1.6; padding: 3px 0; color: var(--muted); }
.qa-line .mono { color: var(--danger); font-weight: 600; flex: none; }
</style>
