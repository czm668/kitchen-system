<script setup>
/**
 * A7-04 环境与设备状态：12 遥测曲线 + 阈值基线带 + 互锁状态机。
 *
 * ## 为什么曲线按「4 组」拆开而不是一张 12 线图
 *
 * 12 条线画在一起，量纲从 0.05（燃气归一化）到 260（罩温）跨了四个数量级，
 * 要么燃气那条贴在零轴上永远看不见，要么罩温把纵轴拉到 300 导致其它全压平。
 * 按物理含义分组（安全/冷链/设备/环境）后，每组内部量纲接近，`y` 轴才有意义。
 *
 * ## 基线带是「配置值」不是「判定值」
 *
 * 图上画出上下限只是给人对照用的。真正判定用的是端侧规则引擎，配置一改立刻
 * 生效（`apply_config` 热更新），所以这里每 10 秒重新取一次阈值。
 *
 * ## 为什么互锁面板要列出「恢复需要什么」
 *
 * 现场最常见的操作事故是「阀关了打不开」——原因通常是触发源还没恢复就点恢复。
 * 把前置条件（人工确认 / 全部触发源已注销 / CH3 双通道）摆在按钮旁边，
 * 比让操作员去猜为什么按钮点了没反应要好。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ChartPanel from '../components/ChartPanel.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { live, telemetry, trippedGuards } from '../stores/live'
import { chartTheme, hexA, lineSeries } from '../utils/chart'
import { METRIC_GROUP, fmt, fmtTime, CHANNEL_STATE, describe } from '../utils/format'
import { toastError } from '../composables/useToast'

const series = ref({})
const params = ref(null)
const interlock = ref(null)
const loading = ref(true)
const points = ref(180)

const GUARD_LABEL = {
  'guard:gas_surge': '① 燃气突变',
  'guard:thermal_fan': '② 超温联动风机',
  'guard:flame_unattended': '③ 长时无人看管',
}

/** 分组：每组内的指标量纲接近。 */
const GROUPS = [
  { key: 'safety', label: '安全类', metrics: ['gas_level', 'hood_temp'] },
  { key: 'cold', label: '冷链类', metrics: ['fridge_temp', 'freezer_temp', 'sample_temp'] },
  { key: 'equipment', label: '设备类', metrics: ['sterilize_temp', 'sct_current'] },
  { key: 'env', label: '环境类', metrics: ['t', 'h', 'flame_a', 'flame_b', 'weight_g'] },
]

const metaById = computed(() => {
  const map = {}
  for (const item of telemetry.value) map[item.id] = item
  return map
})

/** 阈值行按 metric 聚合，用于画基线带。 */
const bandsByMetric = computed(() => {
  const out = {}
  for (const row of params.value?.rules || []) {
    if (row.enabled === false || row.enabled === 0) continue
    const metric = row.metric
    if (!metric) continue
    out[metric] = out[metric] || []
    out[metric].push(row)
  }
  return out
})

function groupOption(group) {
  const theme = chartTheme()
  const palette = [theme.accent, theme.warn, theme.ok, theme.danger, theme.muted]
  const list = group.metrics.filter((id) => series.value[id]?.length)
  const allStamps = list.flatMap((id) => series.value[id].map((p) => p.ts))
  const minTs = allStamps.length ? Math.min(...allStamps) : 0

  const chartSeries = list.map((id, i) => {
    const rows = series.value[id]
    const data = rows.map((p) => [p.ts * 1000, p.v])
    const color = palette[i % palette.length]
    const seriesItem = lineSeries(metaById.value[id]?.label || id, data, color, {
      area: list.length === 1,
    })
    // 基线带：把该指标所有启用的上下限画成横向色带
    const bands = bandsByMetric.value[id] || []
    const areas = []
    for (const row of bands) {
      const low = row.lower_bound
      const high = row.upper_bound
      if (low != null && high != null) {
        areas.push([{ yAxis: low, itemStyle: { color: hexA(theme.ok, 0.06) } },
          { yAxis: high }])
      } else if (high != null) {
        areas.push([{ yAxis: high, itemStyle: { color: hexA(theme.danger, 0.1) } },
          { yAxis: high * 1.4 }])
      } else if (low != null) {
        areas.push([{ yAxis: low * 0.6, itemStyle: { color: hexA(theme.danger, 0.1) } },
          { yAxis: low }])
      }
    }
    if (areas.length) {
      seriesItem.markArea = { silent: true, data: areas }
    }
    return seriesItem
  })

  return {
    grid: { top: 30, right: 14, bottom: 22, left: 44 },
    legend: {
      show: list.length > 1,
      top: 0, right: 6,
      itemWidth: 10, itemHeight: 8,
      textStyle: { color: theme.muted, fontSize: 10.5 },
    },
    xAxis: {
      type: 'time',
      min: minTs ? minTs * 1000 : undefined,
      axisLabel: { formatter: '{HH}:{mm}:{ss}' },
    },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v) => fmt(v, { digits: 1 }) } },
    tooltip: { trigger: 'axis' },
    series: chartSeries,
  }
}

const channelCards = computed(() => {
  const rows = interlock.value?.channels || []
  return rows.map((row) => ({
    ...row,
    requires: [
      { label: '人工确认', done: true, note: '恢复动作必须带 manual_confirmed' },
      {
        label: '触发源全部注销',
        done: Object.keys(row.sources || {}).length === 0,
        note: Object.keys(row.sources || {}).length
          ? `仍有 ${Object.keys(row.sources).length} 个源`
          : '已清空',
      },
      {
        label: 'CH3 双通道复核',
        done: row.channel !== 'CH3' || (row.reviewers || []).length >= 2,
        note: row.channel === 'CH3'
          ? `已复核 ${(row.reviewers || []).join(' + ') || '无'}`
          : '不适用',
      },
    ],
  }))
})

async function load() {
  try {
    const [tel, param, lock] = await Promise.all([
      api.get(`/api/telemetry?points=${points.value}`),
      api.get('/api/params'),
      api.get('/api/interlock'),
    ])
    series.value = tel.series || {}
    params.value = param
    interlock.value = lock
  } catch (err) {
    toastError(err, '环境数据加载失败')
  } finally {
    loading.value = false
  }
}

let timer = null
onMounted(() => {
  load()
  // 3 秒一次：曲线本身是 1Hz 采样，更密的刷新只会让 ECharts 白重绘
  timer = setInterval(load, 3000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="stack">
    <!-- 三通道状态机 -->
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">安全互锁状态<span class="panel-sub"> · 三通道 + 独立声光</span></span>
        <div class="row">
          <span
            v-for="key in Object.keys(GUARD_LABEL)" :key="key"
            class="tag" :class="trippedGuards.includes(key) ? 'danger' : 'plain'"
          >
            {{ GUARD_LABEL[key] }}{{ trippedGuards.includes(key) ? ' 触发' : '' }}
          </span>
          <span v-if="interlock?.degraded" class="tag warn">降级中</span>
        </div>
      </div>
      <div class="panel-body">
        <div class="chans">
          <div
            v-for="row in channelCards" :key="row.channel"
            class="chan" :class="row.state"
          >
            <div class="chan-top">
              <span class="mono bold">{{ row.channel }}</span>
              <span class="small">{{ row.label }}</span>
              <span class="spacer"></span>
              <span class="tag" :class="describe(CHANNEL_STATE, row.state).tone">
                {{ describe(CHANNEL_STATE, row.state).label }}
              </span>
            </div>
            <div class="src-list">
              <template v-if="Object.keys(row.sources || {}).length">
                <span v-for="(level, name) in row.sources" :key="name" class="src mono">
                  {{ name }} · P{{ level }}
                </span>
              </template>
              <span v-else class="tiny muted">无触发源</span>
            </div>
            <ul class="requires">
              <li v-for="req in row.requires" :key="req.label" :class="{ done: req.done }">
                <Icon :name="req.done ? 'check' : 'close'" :size="11" />
                <span>{{ req.label }}</span>
                <span class="tiny muted">{{ req.note }}</span>
              </li>
            </ul>
          </div>
        </div>
        <p class="tiny muted hint">
          互锁优先级：CH1 燃气（P0）&gt; CH3 工位电（P1）&gt; CH2 排风（P2）&gt; 视觉违规（P3）。
          高优先级占用期间，低优先级恢复一律被拒。
        </p>
      </div>
    </section>

    <!-- 曲线分组 -->
    <section v-for="group in GROUPS" :key="group.key" class="panel">
      <div class="panel-head">
        <span class="panel-title">
          {{ group.label }}
          <span class="panel-sub"> · {{ METRIC_GROUP[group.key] }} · 浅色带为配置上下限</span>
        </span>
        <div class="row">
          <select v-model.number="points" class="select mini" @change="load">
            <option :value="60">最近 1 分钟</option>
            <option :value="180">最近 3 分钟</option>
            <option :value="600">最近 10 分钟</option>
          </select>
        </div>
      </div>
      <div class="panel-body">
        <ChartPanel
          :option="groupOption(group)" :height="188"
          :is-empty="!group.metrics.some((id) => series[id]?.length)"
          empty="这一组还没有采样数据"
          :label="`环境读数趋势：${group.label}，包含 ${group.metrics.map((id) => metaById[id]?.label || id).join('、')}；浅色带为配置上下限`"
        />
        <div class="legend-readout">
          <span v-for="id in group.metrics" :key="id" class="readout">
            <span class="tiny muted">{{ metaById[id]?.label || id }}</span>
            <span class="mono bold">
              {{ fmt(metaById[id]?.v, { digits: id === 'gas_level' ? 3 : 1, fallback: '—' }) }}
              <span class="tiny muted">{{ metaById[id]?.unit }}</span>
            </span>
            <span class="tiny muted">{{ metaById[id]?.ts ? fmtTime(metaById[id].ts) : '未采样' }}</span>
          </span>
        </div>
      </div>
    </section>

    <p class="tiny muted">
      12 遥测口径见 <code>scenarios/kitchen.json</code>；第 13 类信号是 RC522 刷卡事件，
      不是遥测，因此不出现在曲线里。当前档位：
      <strong>{{ live.overview?.profile || '—' }}</strong>。
    </p>
  </div>
</template>

<style scoped>
.chans { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; }
@media (max-width: 1000px) { .chans { grid-template-columns: minmax(0, 1fr); } }

.chan {
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.chan.releasing { border-color: var(--warn); background: var(--warn-soft); }
.chan.latched, .chan.manual_reset_required { border-color: var(--danger); background: var(--danger-soft); }
.chan-top { display: flex; align-items: center; gap: 7px; }
.chan-top .spacer { flex: 1; }

.src-list { display: flex; flex-wrap: wrap; gap: 4px; min-height: 17px; }
.src {
  font-size: 10.5px;
  padding: 0 5px;
  border-radius: 3px;
  background: var(--danger-soft);
  color: var(--danger);
}

.requires { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.requires li {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--danger);
}
.requires li.done { color: var(--ok); }
.requires li .muted { margin-left: auto; }

.hint { margin: 11px 0 0; }

.legend-readout {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  padding-top: 9px;
  margin-top: 6px;
  border-top: 1px solid var(--line);
}
.readout { display: flex; align-items: baseline; gap: 6px; }
.select.mini { height: 26px; width: auto; padding: 0 7px; font-size: 11.5px; }
</style>
