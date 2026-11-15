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
import { api } from '../api/client'
import { can } from '../stores/auth'
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
</script>

<template>
  <section class="view">
    <div class="page-head">
      <div>
        <h1>合规凭证</h1>
        <p class="muted">每日检查表 · 合规日报 · 批次追溯一页纸</p>
      </div>
      <div class="row gap-8">
        <input v-model="date" class="input" type="date" :max="'2099-12-31'" />
        <button class="btn ghost sm" :disabled="loading || busy" @click="load">刷新</button>
        <button class="btn sm" :disabled="busy" @click="makeReport">生成日报</button>
        <button class="btn ghost sm" :disabled="busy" @click="exportCsv">导出检查表 CSV</button>
      </div>
    </div>

    <div v-if="!canRead" class="card notice">
      当前账号无合规凭证查看权限（需要店长或组长）。
    </div>

    <template v-else>
      <!-- 口径条：任何时候都不能藏起来 -->
      <div class="notice" v-if="check">
        <strong>口径说明</strong> · {{ check.discipline_note }}
        <div class="tiny muted" style="margin-top: 4px">
          生成时间 {{ check.generated_at }} · 空数据项标记「无数据」，不计入达标率
        </div>
      </div>

      <div v-if="loading" class="card muted">正在汇总当日合规事实…</div>

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
      </template>
    </template>
  </section>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.gap-8 { gap: 8px; }
.overall { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; }
.overall__nums { display: flex; gap: 22px; }
.overall__nums div { display: flex; flex-direction: column; align-items: center; }
.overall__nums b { font-size: 22px; font-variant-numeric: tabular-nums; }
.overall__nums span { font-size: 12px; opacity: .7; }
.items { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-top: 14px; }
.item { padding: 14px; border-radius: 12px; background: var(--surface-2, rgba(127,127,127,.06)); }
.item__head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.item__num { font-size: 24px; margin-top: 10px; font-variant-numeric: tabular-nums; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(127,127,127,.16); }
.badge.pass { color: #1a7f37; background: rgba(26,127,55,.12); }
.badge.warn { color: #9a6700; background: rgba(154,103,0,.12); }
.badge.fail { color: #c0392b; background: rgba(192,57,43,.12); }
.gaps { margin: 8px 0 0; padding-left: 18px; font-size: 12px; line-height: 1.6; opacity: .85; }
.report pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; font-size: 13px; line-height: 1.65; margin: 0; }
.dossier { margin-top: 14px; }
.kv { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(127,127,127,.12); font-size: 13px; }
.select input, .input { min-height: 32px; }
</style>
