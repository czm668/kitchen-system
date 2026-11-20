<script setup>
/**
 * A13 食安合规凭证：每日检查表 / 合规日报 / 批次追溯一页纸 / CSV 导出。
 *
 * ## 这一页刻意不做的三件事
 *
 * 1. **不做「合规分数」**：6 项量纲不同（人数 / 证数 / 批次数 / 会话数），
 *    加权就是拍脑袋。只给逐项「事实数 / 达标数 / 缺口」和整体结论。
 * 2. **不把「无数据」显示成绿色**：后端 `na` 项 rate 为 null，这里明确标
 *    「无数据 · 不计入达标率」。演示环境经常没跑消毒，把它算通过就是假合格证。
 * 3. **不做「一键补录」**：检查表是已有事实的只读物化，页面改不了结论。
 *    要达标得去晨检 / 留样 / 消毒的实际业务页面完成动作。
 *
 * 权限：`compliance.read` = manager + lead。观察员/操作员看到提示卡而不是 403 报错。
 */
import { computed, onMounted, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { api } from '../api/client'
import { can, whoami } from '../stores/auth'
import { toastError, toastOk } from '../composables/useToast'

const date = ref('')
const loading = ref(true)
const busy = ref(false)
const check = ref(null)
const report = ref(null)
const audit = ref({ runs: [], provider: {}, snapshots: [], journal: {} })
const batches = ref([])
const batchId = ref('')
const dossier = ref(null)

const STATE_LABEL = { pass: '达标', warn: '提醒', fail: '未达标', na: '无数据' }
const OVERALL_COLOR = { pass: 'ok', warn: 'warn', fail: 'bad', no_data: 'muted' }

const canRead = computed(() => can('compliance.read'))
// 打印抬头要写明「谁出具的」，这份表是要签字留档的
const operator = whoami()
// 查询串必须写成显式的 `?${...}`：契约扫描器靠 `?` 截断路径，
// 把 `?` 藏进变量里会被识别成不存在的接口（A13 首跑被这条测试拦下）。
const qs = () => (date.value ? `date=${encodeURIComponent(date.value)}` : '')
const pct = (rate) => (rate === null || rate === undefined ? '—' : `${(rate * 100).toFixed(1)}%`)

async function load() {
  if (!canRead.value) { loading.value = false; return }
  loading.value = true
  try {
    const [c, a, b] = await Promise.all([
      api.get(`/api/compliance/checklist?${qs()}`),
      api.get('/api/compliance/runs'),
      api.get('/api/batches'),
    ])
    check.value = c
    audit.value = a
    batches.value = b.batches || []
    if (!batchId.value && batches.value.length) batchId.value = batches.value[0].id
  } catch (err) {
    toastError(err, '合规数据加载失败')
  } finally {
    loading.value = false
  }
}

async function makeReport() {
  busy.value = true
  try {
    report.value = await api.get(`/api/compliance/report?${qs()}`)
    audit.value = await api.get('/api/compliance/runs')
    toastOk('合规日报已生成并归档')
  } catch (err) {
    toastError(err, '日报生成失败')
  } finally {
    busy.value = false
  }
}

async function exportCsv() {
  busy.value = true
  try {
    const out = await api.get(`/api/compliance/export?${qs()}`)
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
    toastOk(`已导出 ${out.rows} 项检查`)
  } catch (err) {
    toastError(err, '导出失败')
  } finally {
    busy.value = false
  }
}

async function loadDossier() {
  if (!batchId.value) return
  busy.value = true
  try {
    dossier.value = await api.get(`/api/compliance/dossier?batch_id=${encodeURIComponent(batchId.value)}`)
  } catch (err) {
    toastError(err, '追溯一页纸加载失败')
  } finally {
    busy.value = false
  }
}

/**
 * 首屏加载。
 *
 * ⚠ 这一行曾经是缺的：`load()` 定义好了却没人调用，而 `loading` 初值为 `true`，
 * 于是整页永远停在加载态——表现是「除了一行灰字什么都没有」，且没有任何超时提示。
 * 所有异步视图都必须在这里挂一次首屏请求。
 */
onMounted(load)
</script>

<template>
  <section class="view">
    <!-- 只出纸的抬头：打印出来要一眼看出这是哪一天、谁出具、什么时候生成的。
         屏幕上不占位（display:none），不会挤占首屏。 -->
    <div class="print-only print-head">
      <div class="print-head__brand">后厨智能调度 · 食安合规管理系统</div>
      <div class="print-head__title">每日合规检查与合规日报</div>
      <div class="print-head__meta">
        <span>检查日期 {{ check?.date || date }}</span>
        <span v-if="check">生成时间 {{ check.generated_at }}</span>
        <span>出具 {{ operator.name || '—' }}{{ operator.roleLabel ? '（' + operator.roleLabel + '）' : '' }}</span>
      </div>
    </div>

    <!-- 操作条：页面标题由顶栏统一承担，这里只放范围说明与动作 -->
    <div class="page-head">
      <p class="lede">按日汇总当日实际发生的检查事实，只读留档，不能在这里改结论。</p>
      <div class="row wrap gap-8">
        <input v-model="date" class="input date-input" type="date" :max="'2099-12-31'" aria-label="选择检查日期" />
        <button class="btn ghost sm" :disabled="loading || busy" @click="load">刷新</button>
        <button class="btn primary sm" :disabled="busy" @click="makeReport">生成日报</button>
        <button class="btn ghost sm" :disabled="busy" @click="exportCsv">导出 CSV</button>
      </div>
    </div>

    <div v-if="!canRead" class="notice">
      <strong>无查看权限</strong>
      当前账号无合规凭证查看权限，需店长或组长角色。
    </div>

    <template v-else>
      <!-- 口径条：任何时候都不能藏起来 -->
      <div class="notice" v-if="check">
        <strong>口径说明</strong> · {{ check.discipline_note }}
        <div class="tiny muted" style="margin-top: 2px">
          生成时间 {{ check.generated_at }} · 空数据项标记「无数据」，不计入达标率
        </div>
      </div>

      <!-- 加载态：骨架形状与真实内容一致（结论条 → 说明 → 检查网格），
           换内容时视觉跳变最小。走 StateBlock 统一入口，读屏器能听到「正在加载」。 -->
      <StateBlock v-if="loading" class="card" title="正在汇总当日合规事实">
        <template #skeleton>
          <div class="panel-body" style="display: flex; flex-direction: column; gap: 14px">
            <div class="skeleton" style="height: 58px; width: 100%"></div>
            <div class="skeleton" style="height: 16px; width: 42%"></div>
            <div class="skeleton" style="height: 92px; width: 100%"></div>
          </div>
        </template>
      </StateBlock>

      <template v-if="check && !loading">
        <!-- 总体结论 -->
        <div class="card overall" :class="OVERALL_COLOR[check.overall]">
          <div>
            <div class="tiny muted">{{ check.date }} 总体结论</div>
            <div class="h1">{{ check.overall_label }}</div>
          </div>
          <div class="overall__nums">
            <div><b>{{ check.summary.passed }}</b><span>达标</span></div>
            <div><b>{{ check.summary.warn }}</b><span>提醒</span></div>
            <div><b>{{ check.summary.failed }}</b><span>未达标</span></div>
            <div><b>{{ check.summary.no_data }}</b><span>无数据</span></div>
          </div>
        </div>

        <!-- 六项检查 -->
        <div class="card">
          <div class="section-title">每日合规检查表（{{ check.items.length }} 项）</div>
          <div class="items">
            <div v-for="row in check.items" :key="row.key" class="item" :class="row.state">
              <div class="item__head">
                <span class="h2">{{ row.label }}</span>
                <span class="badge" :class="row.state">{{ STATE_LABEL[row.state] }}</span>
              </div>
              <div class="item__num">{{ row.passed }} / {{ row.checked }}</div>
              <div class="tiny muted">达标率 {{ pct(row.rate) }}</div>
              <div class="tiny muted" style="margin-top: 6px">{{ row.basis }}</div>
              <ul v-if="row.gaps.length" class="gaps">
                <li v-for="(gap, i) in row.gaps" :key="i">{{ gap }}</li>
              </ul>
              <div v-if="row.gap_total > row.gaps.length" class="tiny muted">
                另有 {{ row.gap_total - row.gaps.length }} 条缺口未展开
              </div>
              <div v-if="row.running" class="tiny muted" style="margin-top: 4px">
                另有 {{ row.running }} 个会话运行中，不计入判定
              </div>
            </div>
          </div>
        </div>

        <!-- 合规日报 -->
        <div class="card">
          <div class="row gap-8" style="justify-content: space-between">
            <div class="section-title">合规日报与生成留痕</div>
            <span class="tiny muted">
              provider {{ (audit.provider && audit.provider.mode) || 'rule' }} ·
              累计 {{ (audit.runs && audit.runs.length) || 0 }} 次
            </span>
          </div>
          <div v-if="report" class="report">
            <div class="tiny muted" style="margin-bottom: 8px">
              {{ report.title }} · 来源 {{ report.source }} · 模型 {{ report.model }} ·
              风险 {{ report.risk_level }} · {{ report.latency_ms }}ms
              <span v-if="report.degraded_reason"> · 降级：{{ report.degraded_reason }}</span>
            </div>
            <pre>{{ report.content_md }}</pre>
          </div>
          <div v-else class="tiny muted" style="margin-top: 10px">
            尚未生成本页日报。点击「生成日报」会归档一份按日快照，并留下 LLM 调用审计；
            离线规则层会如实标注「非大模型生成」。
          </div>
          <div v-if="audit.snapshots && audit.snapshots.length" class="tiny muted" style="margin-top: 10px">
            已归档快照：{{ audit.snapshots.map(s => s.date + '（' + s.overall + '）').join('、') }}
          </div>
        </div>

        <!-- 批次追溯一页纸 -->
        <div class="card">
          <div class="row gap-8" style="justify-content: space-between">
            <div class="section-title">批次追溯一页纸</div>
            <div class="row gap-8">
              <select v-model="batchId" class="input">
                <option v-for="b in batches" :key="b.id" :value="b.id">
                  {{ b.batch_no }} · {{ b.dish_name || ('#' + b.id) }}
                </option>
              </select>
              <button class="btn ghost sm" :disabled="busy" @click="loadDossier">查看证据链</button>
            </div>
          </div>

          <div v-if="dossier" class="dossier">
            <div class="kv"><span class="kv__k">批次</span><span class="kv__v">{{ dossier.batch.batch_no }}</span></div>
            <div class="kv"><span class="kv__k">生产时间</span><span class="kv__v">{{ dossier.batch.produced_at || '—' }}</span></div>
            <div class="kv"><span class="kv__k">供应商</span><span class="kv__v">{{ (dossier.supplier && dossier.supplier.name) || '未登记' }}</span></div>
            <div class="kv"><span class="kv__k">溯源码</span><span class="kv__v">{{ dossier.trace_codes.join('、') || '无' }}</span></div>
            <div class="kv"><span class="kv__k">留样记录</span><span class="kv__v">{{ dossier.retention.length }} 条</span></div>
            <div class="kv"><span class="kv__k">消毒会话</span><span class="kv__v">{{ dossier.disinfection.length }} 条</span></div>
            <div class="kv"><span class="kv__k">出库门禁</span><span class="kv__v">{{ dossier.forbid_outbound ? '禁止出库' : '无阻塞' }}</span></div>
            <div class="kv"><span class="kv__k">审计留痕</span><span class="kv__v">{{ dossier.audit.length }} 条</span></div>

            <div v-if="dossier.missing.length" class="notice" style="margin-top: 12px">
              <strong>缺失环节（如实列出）</strong> · {{ dossier.missing.join('、') }}
            </div>
            <div v-for="r in dossier.retention" :key="'r' + r.id" class="tiny muted" style="margin-top: 8px">
              留样 #{{ r.id }} · {{ r.weight_g }}g · {{ r.qualified ? '达标' : '未达标' }} · {{ r.sampled_at }}
            </div>
            <div v-for="r in dossier.disinfection" :key="r.session_uid" class="tiny muted" style="margin-top: 4px">
              消毒 {{ r.session_uid }} · {{ r.state }} · 累计 {{ r.cumulative_sec }}s · 峰值 {{ r.peak_temp }}℃
            </div>
            <div v-for="o in dossier.outbound" :key="o.code" class="tiny muted" style="margin-top: 4px">
              出库 {{ o.code }} · {{ o.blocked ? o.blockers.join('；') : '无阻塞' }}
            </div>
            <div class="tiny muted" style="margin-top: 12px">{{ dossier.evidence_note }}</div>
          </div>
          <div v-else class="tiny muted" style="margin-top: 10px">
            选择批次后查看「原料 → 留样 → 消毒 → 出库门禁 → 工单 → 审计」的合并证据链。
          </div>
        </div>

        <!-- 只出纸的签署栏：这份表打印出来是要签字留档的，
             没有签字位的「合规日报」在实际场景里是废纸 -->
        <div class="print-only print-sign">
          <div class="print-sign__row">
            <span>店长签字：</span>
            <span>复核日期：</span>
          </div>
          <div class="print-sign__note">
            本表由系统按当日实际发生的检查事实自动生成，未采集到的检查项标记「无数据」，
            不计入达标率，亦不视为通过。本表只读留档，不能在系统内改写结论。
          </div>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
/* 这一页的 .card 直接放内容（不套 panel-body），所以内边距落在卡片身上 */
.card { padding: 18px; }
.lede { margin: 0; font-size: var(--fs-sm); color: var(--muted); }
.date-input { width: 168px; }
.gap-8 { gap: 8px; }
.overall { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 20px; }
/* 结论卡按结论染色：达标绿 / 提醒橙 / 未达标红，左侧色条承载语义 */
.overall.ok { border-left: 3px solid var(--ok); }
.overall.warn { border-left: 3px solid var(--warn); }
.overall.bad { border-left: 3px solid var(--danger); }
.overall.muted { border-left: 3px solid var(--line-strong); }
.overall__nums { display: flex; gap: 26px; }
.overall__nums div { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.overall__nums b { font-size: 24px; font-variant-numeric: tabular-nums; font-weight: 640; letter-spacing: -0.02em; }
.overall__nums span { font-size: var(--fs-xs); color: var(--muted); }
.items { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 12px; margin-top: 14px; }
/* 六项检查：中性底 + 左色条，不用整块染色，六张卡并排时不至于花 */
.item {
  padding: 14px 16px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--line-strong);
}
.item.pass { border-left-color: var(--ok); }
.item.warn { border-left-color: var(--warn); }
.item.fail { border-left-color: var(--danger); }
.item.na { border-left-color: var(--line-strong); }
.item__head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.item__num { font-size: 25px; margin-top: 8px; font-variant-numeric: tabular-nums; font-weight: 620; letter-spacing: -0.02em; }
.gaps { margin: 8px 0 0; padding-left: 17px; font-size: var(--fs-xs); line-height: 1.65; color: var(--warn-ink); }
.report pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; font-size: var(--fs-sm); line-height: 1.7; margin: 0; }
.dossier { margin-top: 14px; }
.kv {
  display: flex; justify-content: space-between; gap: 16px;
  padding: 9px 0; border-bottom: 1px solid var(--line); font-size: var(--fs-sm);
}
.kv__k { color: var(--muted); flex: none; }
.kv__v { text-align: right; font-variant-numeric: tabular-nums; }
</style>
