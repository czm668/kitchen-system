<script setup>
/**
 * A12 扩展模块：经营账本（流水 / 成本 / 毛利 / 净利润 / 日报 / 导出）。
 *
 * ## 这一页刻意不做的三件事
 *
 * 1. **不叫「实收 / 银行流水」**：项目无支付链路，营业额是「已上桌子项 ×
 *    下单快照价」确认的经营口径，页面顶部的口径条必须一直可见。
 * 2. **不把净利润做成可编辑框**：所有金额由后端整数分算出来，前端只展示。
 *    能改的只有「标准成本」和「期间费用」两个**配置项**，且都要 manager 权限。
 * 3. **不硬画 ECharts**：趋势用纯 CSS 柱状，避免深色主题下画布白底和
 *    dispose/rebuild 时序问题（KpiView 踩过）。七根柱子足够看日趋势。
 *
 * 权限：`finance.read` / `finance.write` 仅 manager。非店长看到的是提示卡，
 * 不是一堆 403 报错。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { toastError, toastOk, toastWarn } from '../composables/useToast'
import { fmtDateTime } from '../utils/format'

const days = ref(7)
const start = ref('')
const end = ref('')
const loading = ref(true)
const busy = ref(false)
const overview = ref({ trend: [], dishes: [], cost_coverage: {}, settlement_note: '', cost_note: '' })
const entries = ref([])
const dishes = ref([])
const overhead = ref({})
const report = ref(null)
const runs = ref([])
const entryKind = ref('')

const RANGES = [
  { days: 1, label: '今日' },
  { days: 7, label: '近 7 日' },
  { days: 30, label: '近 30 日' },
]
const KINDS = [
  { key: '', label: '全部' },
  { key: 'revenue', label: '营业收入' },
  { key: 'food_cost', label: '食材成本' },
  { key: 'compensation', label: '补偿冲减' },
  { key: 'overhead', label: '期间费用' },
]
const KIND_LABEL = {
  revenue: '营业收入', food_cost: '食材成本',
  compensation: '补偿冲减', overhead: '期间费用',
}
const CAT_LABEL = {
  sales: '销售', food: '食材', waive: '免单', coupon: '优惠券',
  redo: '重做损耗', labor: '人工', rent: '房租',
  utilities: '水电', other: '其他',
}

const canRead = computed(() => can('finance.read'))
const canWrite = computed(() => can('finance.write'))
const money = (value) => `¥${Number(value || 0).toFixed(2)}`
const pct = (value) => `${(Number(value || 0) * 100).toFixed(1)}%`
const kindLabel = (key) => KIND_LABEL[key] || key
const catLabel = (key) => CAT_LABEL[key] || key

/** 只把用户真正改过的范围传给后端；空的让服务端按今天算，避免时区错位。 */
function rangeParams() {
  const p = { days: days.value }
  if (start.value) p.start = start.value
  if (end.value) p.end = end.value
  return p
}
function qs(params) {
  return new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v !== undefined && v !== null)
  ).toString()
}

async function load() {
  if (!canRead.value) { loading.value = false; return }
  loading.value = true
  try {
    const q = qs(rangeParams())
    const [stat, list, costRows, fee, llm] = await Promise.all([
      api.get(`/api/finance/overview?${q}`),
      api.get(`/api/finance/entries?${qs({ ...rangeParams(), kind: entryKind.value })}`),
      api.get('/api/finance/dishes'),
      api.get(`/api/finance/overhead?${qs({ date: end.value })}`),
      api.get('/api/finance/ai-runs?limit=20'),
    ])
    overview.value = stat
    entries.value = list.entries || []
    dishes.value = costRows.dishes || []
    overhead.value = fee
    runs.value = (llm.runs || []).slice().reverse()
  } catch (err) {
    toastError(err, '经营账本加载失败')
  } finally {
    loading.value = false
  }
}

function setRange(value) {
  days.value = value
  start.value = ''
  end.value = ''
  load()
}

/** 趋势柱高按区间内最大营业额归一，零数据时不画 0 高度以外的东西。 */
const trendMax = computed(() => Math.max(
  1, ...overview.value.trend.map((row) => Math.max(0, row.revenue))))
function barHeight(row) {
  return `${Math.max(3, Math.round(Math.max(0, row.revenue) / trendMax.value * 100))}%`
}

async function saveCost(row) {
  const raw = window.prompt(
    `设置「${row.name}」的标准成本（元/份）\n当前：${money(row.cost)}（${row.cost_source === 'manual' ? '手工录入' : '按售价 35% 估算'}）\n只影响之后确认的账目，历史账面不重算。`,
    String(row.cost))
  if (raw === null) return
  const value = Number(raw)
  if (!Number.isFinite(value) || value < 0) { toastWarn('请输入不小于 0 的金额'); return }
  busy.value = true
  try {
    await api.post('/api/finance/costs', { dish_id: row.dish_id, cost: value })
    toastOk('标准成本已保存，之后确认的账目按新成本计')
    await load()
  } catch (err) {
    toastError(err, '保存失败')
  } finally {
    busy.value = false
  }
}

async function saveOverhead() {
  busy.value = true
  try {
    const payload = { date: overhead.value.date }
    for (const key of ['labor', 'rent', 'utilities', 'other']) {
      payload[key] = Number(overhead.value[key] || 0)
    }
    await api.post('/api/finance/overhead', payload)
    toastOk('期间费用已录入（同日重复录入为覆盖）')
    await load()
  } catch (err) {
    toastError(err, '保存失败')
  } finally {
    busy.value = false
  }
}

async function buildReport() {
  busy.value = true
  try {
    report.value = await api.get(
      `/api/finance/report?${qs({ date: end.value || overview.value.to })}`)
    toastOk(report.value.source === 'rule' ? '日报已生成（离线规则点评）' : '日报已生成')
  } catch (err) {
    toastError(err, '日报生成失败')
  } finally {
    busy.value = false
  }
}

async function exportCsv() {
  busy.value = true
  try {
    const out = await api.get(`/api/finance/export?${qs(rangeParams())}`)
    // 后端已在 csv 首部加 UTF-8 BOM，Excel 双击不乱码
    const blob = new Blob([out.csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = out.filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    toastOk(`已导出 ${out.rows} 条流水`)
  } catch (err) {
    toastError(err, '导出失败')
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="finance-view">
    <!-- 权限闸门：非 manager 不给一堆 403，直接说明边界 -->
    <section v-if="!canRead" class="panel">
      <div class="panel-body">
        <div class="h2">经营账本仅店长可见</div>
        <div class="caption" style="margin-top: 8px">
          毛利、净利、标准成本与期间费用属于店内经营数据，组长、操作员与观察员账号无权查看。
          如需访问请用店长账号登录。
        </div>
      </div>
    </section>

    <template v-else>
      <!-- 顶部：区间 + 导出 -->
      <div class="toolbar">
        <div class="segmented">
          <button v-for="r in RANGES" :key="r.days"
                  :class="{ active: days === r.days && !start && !end }"
                  @click="setRange(r.days)">{{ r.label }}</button>
        </div>
        <div class="row gap-8" style="margin-left: auto">
          <input v-model="start" class="input sm" type="date" title="起始日期" @change="load" />
          <span class="tiny muted">至</span>
          <input v-model="end" class="input sm" type="date" title="结束日期" @change="load" />
          <button class="btn ghost sm" :disabled="busy || loading" @click="load">刷新</button>
          <button class="btn sm" :disabled="busy" @click="exportCsv">导出流水 CSV</button>
        </div>
      </div>

      <!-- 口径条：任何时候都不能藏起来 -->
      <div class="notice">
        <strong>口径说明</strong> · {{ overview.settlement_note }}
        <div class="tiny muted" style="margin-top: 4px">{{ overview.cost_note }}</div>
      </div>

      <!-- KPI -->
      <div class="kpi-grid">
        <div class="kpi">
          <div class="tiny muted">营业额（已上桌确认）</div>
          <div class="kpi-num mono">{{ money(overview.revenue) }}</div>
          <div class="tiny muted">{{ overview.order_count || 0 }} 单 · 客单价 {{ money(overview.avg_ticket) }}</div>
        </div>
        <div class="kpi">
          <div class="tiny muted">食材成本</div>
          <div class="kpi-num mono">{{ money(overview.food_cost) }}</div>
          <div class="tiny muted">手工 {{ overview.cost_coverage?.manual || 0 }} 条 / 估算 {{ overview.cost_coverage?.estimated || 0 }} 条</div>
        </div>
        <div class="kpi">
          <div class="tiny muted">毛利</div>
          <div class="kpi-num mono">{{ money(overview.gross) }}</div>
          <div class="tiny muted">毛利率 {{ pct(overview.gross_margin) }}</div>
        </div>
        <div class="kpi">
          <div class="tiny muted">期间费用</div>
          <div class="kpi-num mono">{{ money(overview.overhead) }}</div>
          <div class="tiny muted">人工 / 房租 / 水电 / 其他</div>
        </div>
        <div class="kpi" :class="{ negative: Number(overview.net) < 0 }">
          <div class="tiny muted">净利润</div>
          <div class="kpi-num mono">{{ money(overview.net) }}</div>
          <div class="tiny muted">净利率 {{ pct(overview.net_margin) }}</div>
        </div>
        <div class="kpi">
          <div class="tiny muted">补偿冲减</div>
          <div class="kpi-num mono">{{ money(overview.compensation) }}</div>
          <div class="tiny muted">重做损耗 {{ money(overview.redo_loss) }}</div>
        </div>
      </div>

      <div class="grid-2">
        <!-- 日趋势 -->
        <section class="panel">
          <div class="panel-head"><span class="panel-title">日营业额趋势</span>
            <span class="tiny muted">{{ overview.from }} ~ {{ overview.to }}</span></div>
          <div class="panel-body">
            <div v-if="!overview.trend.length" class="empty">暂无数据</div>
            <div v-else class="bars">
              <div v-for="row in overview.trend" :key="row.date" class="bar-col">
                <div class="bar-val mono">{{ row.revenue ? Number(row.revenue).toFixed(0) : '' }}</div>
                <div class="bar-track">
                  <div class="bar-fill" :style="{ height: barHeight(row) }"></div>
                </div>
                <div class="bar-day tiny muted">{{ row.date.slice(5) }}</div>
              </div>
            </div>
            <div class="tiny muted" style="margin-top: 10px">
              柱高按区间内最高营业额归一；悬停可在下方流水明细中按日期核对。
            </div>
          </div>
        </section>

        <!-- 日报 -->
        <section class="panel">
          <div class="panel-head">
            <span class="panel-title">经营日报</span>
            <button class="btn ghost sm" :disabled="busy" @click="buildReport">生成 / 刷新</button>
          </div>
          <div class="panel-body">
            <div v-if="!report" class="empty">点击「生成」查看当日经营日报与 AI 点评</div>
            <template v-else>
              <div class="row between" style="margin-bottom: 8px">
                <span class="mono bold">{{ report.title }}</span>
                <span class="tag" :class="report.source === 'live' ? 'info' : 'plain'">
                  {{ report.source === 'live' ? '在线模型点评' : '离线规则点评' }}
                </span>
              </div>
              <div class="ai-card">
                <div class="tiny">{{ report.summary }}</div>
                <div v-if="report.degraded_reason" class="tiny muted" style="margin-top: 6px">
                  在线模型不可用，已自动降级：{{ report.degraded_reason }}
                </div>
              </div>
              <pre class="report-md mono">{{ report.content_md }}</pre>
              <div class="tiny muted">
                {{ report.model }} · {{ report.prompt_version }} · {{ report.latency_ms }} ms
                <span v-if="runs.length"> · 本会话日报调用 {{ runs.length }} 次</span>
              </div>
            </template>
          </div>
        </section>
      </div>

      <!-- 菜品毛利排行 -->
      <section class="panel">
        <div class="panel-head"><span class="panel-title">菜品毛利排行</span>
          <span class="tiny muted">按毛利降序 · 成本口径逐行标注</span></div>
        <div class="panel-body">
          <div v-if="!overview.dishes?.length" class="empty">区间内暂无已上桌订单</div>
          <table aria-label="表格：菜品、销量、营业额、食材成本、毛利、毛利率、成本口径" v-else class="table cards">
            <thead><tr>
              <th scope="col">菜品</th><th scope="col">销量</th><th scope="col">营业额</th><th scope="col">食材成本</th>
              <th scope="col">毛利</th><th scope="col">毛利率</th><th scope="col">成本口径</th>
            </tr></thead>
            <tbody>
              <tr v-for="row in overview.dishes" :key="row.dish_id">
                <td data-label="菜品">{{ row.name }}</td>
                <td data-label="销量" class="mono">{{ row.qty }}</td>
                <td data-label="营业额" class="mono">{{ money(row.revenue) }}</td>
                <td data-label="食材成本" class="mono">{{ money(row.cost) }}</td>
                <td data-label="毛利" class="mono">{{ money(row.gross) }}</td>
                <td data-label="毛利率" class="mono">{{ pct(row.margin) }}</td>
                <td data-label="成本口径"><span class="tag" :class="row.cost_source === 'manual' ? 'ok' : 'plain'">
                  {{ row.cost_source === 'manual' ? '手工' : '估算 35%' }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="grid-2">
        <!-- 成本维护 -->
        <section class="panel">
          <div class="panel-head"><span class="panel-title">标准成本维护</span>
            <span class="tiny muted">售价 × 35% 为估算默认值</span></div>
          <div class="panel-body">
            <div class="tiny muted" style="margin-bottom: 8px">
              修改只影响之后确认的账目，历史账面冻结不重算。当前区间按毛利率升序，
              最低的排最前，优先核对这几道菜。
            </div>
            <div v-for="row in dishes.slice(0, 8)" :key="row.dish_id" class="kv-row">
              <span class="tiny grow">{{ row.name }} <span class="muted">· 售价 {{ money(row.price) }}</span></span>
              <span class="mono tiny">{{ money(row.cost) }}</span>
              <span class="tag plain">{{ row.cost_source === 'manual' ? '手工' : '估算' }}</span>
              <button v-if="canWrite" class="btn ghost sm" :disabled="busy"
                      @click="saveCost(row)">设置</button>
            </div>
            <div v-if="!canWrite" class="tiny muted" style="margin-top: 8px">
              仅店长可修改标准成本。
            </div>
          </div>
        </section>

        <!-- 期间费用 -->
        <section class="panel">
          <div class="panel-head"><span class="panel-title">期间费用（按日）</span>
            <span class="tiny muted">{{ overhead.date || '-' }}</span></div>
          <div class="panel-body">
            <div class="tiny muted" style="margin-bottom: 10px">
              未录入时净利润等于毛利，不是完整净利润。同一天重复保存为覆盖，不会累加。
            </div>
            <div v-for="f in [
                   { key: 'labor', label: '人工' }, { key: 'rent', label: '房租' },
                   { key: 'utilities', label: '水电' }, { key: 'other', label: '其他' }]"
                 :key="f.key" class="kv-row">
              <span class="tiny grow">{{ f.label }}</span>
              <input v-model="overhead[f.key]" class="input sm" type="number" min="0"
                     step="0.01" :disabled="!canWrite" style="width: 120px" />
              <span class="tiny muted">元</span>
            </div>
            <div class="row between" style="margin-top: 12px">
              <span class="tiny muted">合计 {{ money(overhead.total) }}</span>
              <button v-if="canWrite" class="btn sm" :disabled="busy" @click="saveOverhead">
                保存期间费用
              </button>
            </div>
          </div>
        </section>
      </div>

      <!-- 流水明细 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">流水明细（{{ entries.length }} 条）</span>
          <div class="segmented sm">
            <button v-for="k in KINDS" :key="k.key"
                    :class="{ active: entryKind === k.key }"
                    @click="entryKind = k.key; load()">{{ k.label }}</button>
          </div>
        </div>
        <div class="panel-body">
          <div v-if="!entries.length" class="empty">区间内暂无账目（下单未上桌的不进流水）</div>
          <table aria-label="表格：时间、流水号、类型、分类、单号 / 菜品、金额、口径、备注" v-else class="table cards">
            <thead><tr>
              <th scope="col">时间</th><th scope="col">流水号</th><th scope="col">类型</th><th scope="col">分类</th>
              <th scope="col">单号 / 菜品</th><th scope="col">金额</th><th scope="col">口径</th><th scope="col">备注</th>
            </tr></thead>
            <tbody>
              <tr v-for="row in entries" :key="row.entry_no">
                <td data-label="时间" class="tiny muted">{{ fmtDateTime(row.occurred_at) }}</td>
                <td data-label="流水号" class="mono tiny">{{ row.entry_no }}</td>
                <td data-label="类型"><span class="tag plain">{{ kindLabel(row.kind) }}</span></td>
                <td data-label="分类" class="tiny">{{ catLabel(row.category) }}</td>
                <td data-label="单号 / 菜品" class="tiny">{{ row.order_no || '-' }}<br />{{ row.dish_name || '-' }}</td>
                <td data-label="金额" class="mono" :class="{ 'danger-text': row.amount < 0 }">
                  {{ row.amount > 0 ? '+' : '' }}{{ Number(row.amount).toFixed(2) }}
                </td>
                <td data-label="口径" class="tiny muted">{{ row.cost_source === 'estimated' ? '估算' : (row.cost_source === 'manual' ? '手工' : '-') }}</td>
                <td data-label="备注" class="tiny muted ellipsis" style="max-width: 220px">{{ row.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* 只保留这一页独有的东西：KPI 网格、自制柱状图、分段控件。
   表格 / 说明条 / grid-2 / 空态一律走 theme.css 的全局实现，
   否则同一个后台里会出现两套表格行高和两套圆角。 */
.finance-view { display: grid; gap: var(--sp-4); align-content: start; }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(186px, 1fr)); gap: 12px; }
/* 与 StatCard 同一套读数比例：34px / 640 字重 / 负字距，两页数字看起来是一个系统 */
.kpi {
  padding: 15px 18px; border-radius: var(--radius); background: var(--panel);
  border: 1px solid rgba(0, 0, 0, 0.05);
  box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 6px;
  transition: box-shadow 160ms ease;
}
:root[data-theme="dark"] .kpi { border-color: var(--line); }
.kpi:hover { box-shadow: var(--shadow-lg); }
.kpi.negative .kpi-num { color: var(--danger-ink); }
.kpi-num {
  font-size: 26px; font-weight: 640; letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums; line-height: 1.15;
}
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: var(--sp-4); }
.ai-card {
  padding: 12px 14px; border-radius: var(--radius-sm);
  background: var(--surface-2); border: 1px solid var(--line);
  font-size: var(--fs-sm); line-height: 1.65;
}
.report-md {
  margin: 10px 0 8px; padding: 12px; border-radius: var(--radius-sm);
  background: var(--surface-2); border: 1px solid var(--line);
  font-size: var(--fs-xs); line-height: 1.75;
  white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow: auto;
}
.bars { display: flex; align-items: flex-end; gap: 8px; height: 156px; }
.bar-col { flex: 1; display: flex; flex-direction: column; align-items: center; min-width: 0; }
.bar-val { font-size: var(--fs-2xs); height: 15px; color: var(--text-2); }
.bar-track { width: 100%; flex: 1; display: flex; align-items: flex-end; }
.bar-fill {
  width: 100%; border-radius: 5px 5px 2px 2px;
  background: var(--accent); opacity: .82; transition: height 240ms ease;
}
.bar-day { margin-top: 6px; font-size: var(--fs-2xs); white-space: nowrap; }
.segmented {
  display: inline-flex; padding: 3px; border-radius: 8px;
  background: var(--panel-2); gap: 2px; border: 1px solid var(--line);
}
.segmented button {
  border: none; background: none; color: var(--text-2); cursor: pointer;
  padding: 6px 12px; border-radius: 6px; font-size: var(--fs-xs); font-weight: 500;
  transition: background 130ms ease, color 130ms ease;
}
.segmented button:hover { color: var(--text); }
.segmented button.active {
  background: var(--panel); color: var(--text);
  box-shadow: var(--shadow-1); font-weight: 600;
}
.segmented.sm button { padding: 4px 9px; font-size: var(--fs-2xs); }
.gap-8 { gap: 8px; }

/* ---------------------------------------------------------------- 窄屏
   这一段必须放在样式表末尾：`.grid-2` / `.input` 的通用规则在上面，同优先级
   下后者胜，写在前面等于没写（踩过一次）。 */
@media (max-width: 720px) {
  /* 本页 `.grid-2` 的下限是 360px，比手机上能用的宽度（299px）还宽——栅格
     不是「排成两列」而是把整个页面撑出屏幕，右侧被切掉。下限给 0，否则
     `1fr` 仍受内容 min-content 托底。 */
  .grid-2 { grid-template-columns: minmax(0, 1fr); }
  .toolbar .row.gap-8 { width: 100%; margin-left: 0 !important; }
  /* 日期框给死宽度：`.input { width: 100% }` 在不定宽的 flex 行里会被解成
     「撑满容器」，而容器宽度又由内容决定，循环下来 Chromium 解成 360px。
     140px 正好放得下「2026/11/19」。 */
  .toolbar .row.gap-8 .input { flex: 0 1 140px; width: 140px; min-width: 0; }
}
</style>
