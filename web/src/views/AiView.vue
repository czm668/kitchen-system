<script setup>
/**
 * A28 多融合 AI 中枢面板。
 *
 * ## 这一页的核心责任：把"建议"和"已执行"在视觉上彻底分开
 *
 * 七个 AI 场景共用一套 provider + 护栏 + 审计。AI 只出建议：异常归因不关阀、
 * 派单建议不下发、合规问答不改门禁。所以每个建议卡顶部都带 `advice_only` 警示条，
 * 按钮文案统一用"获取建议"而不是"执行/下发"，避免值班员误以为点了就改了业务状态。
 *
 * 面板（overview）全员可读（ai.read）；三个"获取建议"按钮只在 ai.advise
 * （组长及以上）时出现——防止观察员/脚本刷在线模型调用。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { toastError } from '../composables/useToast'

const loading = ref(true)
const overview = ref({ provider: {}, scenarios: [], totals: {}, guardrails: {}, note: '' })
const runs = ref([])
const busy = ref('')

// 三个建议场景的表单：只收少量结构化字段，隐私字段前端根本不提供。
const forms = ref({
  anomaly: { kind: '燃气', reading: '', threshold: '', detail: '', advice: '' },
  dispatch: { queue: '', idle_stations: '', free_staff: '' },
  compliance: { question: '' },
})
const result = ref(null)
const activeTab = ref('anomaly')

const canAdvise = computed(() => can('ai.advise'))
const totals = computed(() => overview.value.totals || {})
const scenarios = computed(() => overview.value.scenarios || [])
const provider = computed(() => overview.value.provider || {})

const ADVICE_PATHS = {
  anomaly: '/api/ai/advise/anomaly',
  dispatch: '/api/ai/advise/dispatch',
  compliance: '/api/ai/advise/compliance',
}

async function load() {
  loading.value = true
  try {
    const [ov, r] = await Promise.all([
      api.get('/api/ai/overview'),
      api.get('/api/ai/runs?limit=30'),
    ])
    overview.value = ov
    runs.value = r.runs || []
  } catch (err) {
    toastError(err)
  } finally {
    loading.value = false
  }
}

function splitList(text) {
  return String(text || '').split(/[,，\s]+/).filter(Boolean)
}

function buildPayload(kind) {
  const f = forms.value[kind]
  if (kind === 'anomaly') {
    const p = { kind: f.kind }
    if (f.reading !== '') p.reading = Number(f.reading)
    if (f.threshold !== '') p.threshold = Number(f.threshold)
    if (f.detail) p.detail = f.detail
    if (f.advice) p.advice = f.advice
    return p
  }
  if (kind === 'dispatch') {
    return {
      queue: splitList(f.queue),
      idle_stations: splitList(f.idle_stations),
      free_staff: splitList(f.free_staff),
      staff_pool_enabled: true,
    }
  }
  return { question: f.question, overall: '', items: [] }
}

async function requestAdvice(kind) {
  busy.value = kind
  result.value = null
  try {
    result.value = await api.post(ADVICE_PATHS[kind], buildPayload(kind))
    // 建议落一条审计，刷新面板与列表即可（既有场景不会被重复调用）
    await load()
  } catch (err) {
    toastError(err)
  } finally {
    busy.value = ''
  }
}

function modeLabel(mode) {
  return mode === 'live' ? '在线模型' : '离线规则'
}

onMounted(load)
</script>

<template>
  <div class="ai-view">
    <!-- 口径条：每次都可见，明确 AI 的边界 -->
    <div class="boundary-banner">
      <strong>AI 只出建议，不改业务状态</strong>
      <span>异常归因不关阀 · 派单建议不下发 · 合规问答不改门禁。派单/关阀/门禁/退款仍由规则与权限层裁决。</span>
    </div>

    <div v-if="loading" class="loading">正在加载 AI 面板…</div>

    <template v-else>
      <!-- 汇总卡 -->
      <div class="stat-row">
        <div class="stat"><span class="k">AI 场景</span><b class="v">{{ totals.scenario_count || 0 }}</b></div>
        <div class="stat"><span class="k">累计调用</span><b class="v">{{ totals.calls || 0 }}</b></div>
        <div class="stat"><span class="k">在线模型</span><b class="v">{{ totals.live || 0 }}</b></div>
        <div class="stat"><span class="k">离线规则</span><b class="v">{{ totals.rule || 0 }}</b></div>
        <div class="stat"><span class="k">降级次数</span><b class="v">{{ totals.degraded || 0 }}</b></div>
        <div class="stat"><span class="k">当前 provider</span><b class="v small">{{ modeLabel(provider.mode) }}</b></div>
      </div>

      <!-- 七场景一屏 -->
      <section class="panel">
        <h2>七个 AI 场景 <span class="tiny muted">（新增 3 + 既有 4，共用一套护栏与审计）</span></h2>
        <div class="scenario-grid">
          <div v-for="s in scenarios" :key="s.scenario" class="scenario-card"
               :class="{ 'is-new': s.is_new }">
            <div class="sc-head">
              <b>{{ s.title }}</b>
              <span class="tag" :class="s.is_new ? 'accent' : ''">{{ s.is_new ? '新增' : '既有' }}</span>
            </div>
            <div class="sc-meta mono tiny muted">{{ s.scenario }}</div>
            <div class="sc-nums tiny">
              <span>调用 {{ s.calls }}</span>
              <span>在线 {{ s.live }}</span>
              <span>离线 {{ s.rule }}</span>
              <span v-if="s.degraded" class="warn-text">降级 {{ s.degraded }}</span>
            </div>
            <div class="sc-bound tiny" :title="s.slot_boundary">边界：{{ s.slot_boundary }}</div>
            <div v-if="s.prompt_version" class="sc-ver mono tiny muted">版本 {{ s.prompt_version }}</div>
          </div>
        </div>
      </section>

      <!-- 护栏清单 -->
      <section class="panel">
        <h2>统一护栏</h2>
        <div class="guard-row">
          <span class="tag ok">✓ 只出建议</span>
          <span class="tag ok">✓ 离线降级</span>
          <span class="tag ok">✓ 白名单输出</span>
          <span class="tag ok">✓ 逐次审计</span>
          <span class="tag ok">✓ 不喂隐私</span>
          <span class="tag ok">✓ 可复现</span>
        </div>
      </section>

      <!-- 三个建议请求 -->
      <section class="panel">
        <h2>请求一次建议 <span class="tiny muted">（仅组长及以上）</span></h2>
        <div v-if="!canAdvise" class="hint">当前角色无 ai.advise 权限，只能查看面板。请用组长/店长账号登录。</div>
        <template v-else>
          <div class="advice-tabs">
            <button type="button" :class="{ active: activeTab === 'anomaly' }"
                    @click="activeTab='anomaly'; result=null">异常归因</button>
            <button type="button" :class="{ active: activeTab === 'dispatch' }"
                    @click="activeTab='dispatch'; result=null">派单建议</button>
            <button type="button" :class="{ active: activeTab === 'compliance' }"
                    @click="activeTab='compliance'; result=null">合规问答</button>
          </div>
          <div class="advice-forms">
            <div v-if="activeTab === 'anomaly'" class="advice-form">
              <h3>安全异常归因</h3>
              <div class="fields">
                <label>告警类型
                  <select v-model="forms.anomaly.kind">
                    <option>燃气</option><option>排烟积热</option>
                    <option>冷链超温</option><option>明火离人</option>
                  </select>
                </label>
                <label>读数 <input v-model="forms.anomaly.reading" placeholder="如 900" /></label>
                <label>门限 <input v-model="forms.anomaly.threshold" placeholder="如 800" /></label>
                <label class="wide">核查点 <input v-model="forms.anomaly.detail" placeholder="规则引擎给出的核查点（可选）" /></label>
              </div>
              <button class="btn" type="button" :disabled="busy === 'anomaly'"
                      @click="requestAdvice('anomaly')">获取归因建议</button>
            </div>
            <div v-else-if="activeTab === 'dispatch'" class="advice-form">
              <h3>派单优先级建议</h3>
              <div class="fields">
                <label>待派队列 <input v-model="forms.dispatch.queue" placeholder="子项 id，逗号分隔" /></label>
                <label>空闲工位 <input v-model="forms.dispatch.idle_stations" placeholder="工位 id，逗号分隔" /></label>
                <label>空闲人员 <input v-model="forms.dispatch.free_staff" placeholder="人员 id，逗号分隔" /></label>
              </div>
              <button class="btn" type="button" :disabled="busy === 'dispatch'"
                      @click="requestAdvice('dispatch')">获取派单建议</button>
            </div>
            <div v-else class="advice-form">
              <h3>食安合规问答</h3>
              <div class="fields">
                <label class="wide">问题 <input v-model="forms.compliance.question" placeholder="如：为什么这个批次不能出库？" /></label>
              </div>
              <button class="btn" type="button" :disabled="busy === 'compliance'"
                      @click="requestAdvice('compliance')">获取合规解释</button>
            </div>
          </div>
        </template>

        <div v-if="result" class="result-card">
          <div class="result-head">
            <b>{{ result.title }}</b>
            <span class="tag" :class="result.mode === 'live' ? 'accent' : ''">{{ modeLabel(result.mode) }}</span>
            <span class="tag danger">仅建议 · {{ result.slot_boundary }}</span>
          </div>
          <p class="result-summary">{{ result.summary }}</p>
          <p class="result-reply">{{ result.reply }}</p>
          <div class="result-foot tiny muted mono">
            {{ result.scenario }} · {{ result.prompt_version }} · {{ result.latency_ms }}ms
          </div>
        </div>
      </section>

      <!-- 审计 -->
      <section class="panel">
        <h2>调用审计 <span class="tiny muted">（llm_runs，逐次留痕）</span></h2>
        <div class="runs">
          <div v-for="(r, i) in runs" :key="i" class="run-row">
            <span class="mono tiny">{{ r.scenario }}</span>
            <span class="tag" :class="r.mode === 'live' ? 'accent' : ''">{{ modeLabel(r.mode) }}</span>
            <span class="tiny muted">{{ r.model }}</span>
            <span class="tiny muted">{{ r.prompt_version }}</span>
            <span class="tiny" :class="r.status === 'degraded' ? 'warn-text' : ''">{{ r.status }}</span>
          </div>
          <div v-if="!runs.length" class="hint">暂无审计记录。请求一次建议后即可看到。</div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.ai-view { display: flex; flex-direction: column; gap: 14px; }
.boundary-banner {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px;
  padding: 10px 14px; border-radius: var(--radius-sm);
  background: var(--accent-soft); border: 1px solid var(--line);
  color: var(--text); font-size: 12px;
}
.boundary-banner strong { color: var(--accent); }
.boundary-banner span { color: var(--text-2); }
.loading { padding: 40px; text-align: center; color: var(--muted); }
.stat-row { display: flex; flex-wrap: wrap; gap: 10px; }
.stat {
  flex: 1 1 120px; display: flex; flex-direction: column; gap: 4px;
  padding: 12px 14px; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.stat .k { font-size: 11px; color: var(--muted); }
.stat .v { font-size: 20px; }
.stat .v.small { font-size: 13px; }
.panel {
  padding: 14px 16px; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius);
}
.panel h2 { font-size: 14px; margin: 0 0 12px; }
.scenario-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.scenario-card {
  padding: 11px 12px; border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel-2); display: flex; flex-direction: column; gap: 6px;
}
.scenario-card.is-new { border-color: var(--accent); }
.sc-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12.5px; }
.sc-nums { display: flex; flex-wrap: wrap; gap: 10px; color: var(--text-2); }
.sc-bound { color: var(--text-2); }
.sc-ver { color: var(--muted); }
.guard-row { display: flex; flex-wrap: wrap; gap: 8px; }
.hint { padding: 12px; color: var(--muted); font-size: 12px; }
.advice-tabs { display: flex; gap: 6px; margin-bottom: 12px; }
.advice-tabs button {
  padding: 6px 12px; border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel-2); color: var(--text-2); cursor: pointer; font-size: 12px;
}
.advice-tabs button.active, .advice-tabs button:hover { color: var(--accent); border-color: var(--accent); }
.advice-forms { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.advice-form { display: flex; flex-direction: column; gap: 10px; }
.advice-form h3 { font-size: 12.5px; margin: 0; color: var(--text-2); }
.fields { display: grid; gap: 8px; }
.fields label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); }
.fields label.wide { grid-column: 1 / -1; }
.fields input, .fields select {
  padding: 6px 8px; background: var(--panel-2); color: var(--text);
  border: 1px solid var(--line); border-radius: var(--radius-sm); font-size: 12px;
}
.result-card {
  margin-top: 14px; padding: 12px 14px; border-radius: var(--radius-sm);
  border: 1px solid var(--accent); background: var(--panel-2);
}
.result-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.result-summary { font-size: 13px; margin: 0 0 6px; }
.result-reply { font-size: 12px; color: var(--text-2); margin: 0 0 8px; }
.result-foot { color: var(--muted); }
.runs { display: flex; flex-direction: column; gap: 6px; }
.run-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); }
.tag.accent { color: var(--accent); border-color: var(--accent); }
.tag.ok { color: var(--ok); border-color: var(--ok); }
.tag.danger { color: var(--danger); border-color: var(--danger); }
.warn-text { color: var(--warn); }
</style>
