<script setup>
/**
 * A7-08 总览与 KPI 看板（登录后首页）。
 *
 * ## 页面职责
 *
 * 回答「现在后厨出了什么事、今天做得怎么样」两个问题。所以它只放**聚合结果**：
 * 四个 KPI + 三通道状态 + 保护状态 + 最近事件。明细都在其它页，这里不做。
 *
 * ## 为什么 KPI 卡在没样本时显示破折号
 *
 * 「出餐时长 0 分钟」和「还没有出过餐」是两件完全不同的事。后端已经把样本数
 * 一起给过来（`samples`），前端负责把两者分开显示——这是答辩时最容易被抓的
 * 一个细节（「你们出餐只要 0 分钟？」）。
 *
 * ## 为什么「视觉违规数」没有数字
 *
 * 视觉模型还没训练（等 GPU/硬件），后端 `violations.available` 为 false。
 * 这里就显示「未接入」，**不显示 0**：0 的意思是「检测过、没有违规」。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ChartPanel from '../components/ChartPanel.vue'
import StatCard from '../components/StatCard.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { live, channels, telemetry, trippedGuards } from '../stores/live'
import { chartTheme, hexA } from '../utils/chart'
import { fmt, fmtDuration, fmtInt, fmtTime, CHANNEL_STATE, describe } from '../utils/format'
import { toastError, toastOk } from '../composables/useToast'

const kpi = ref(null)
const loading = ref(true)
const seeding = ref(false)

const GUARD_LABEL = {
  'guard:gas_surge': '① 燃气突变立即关阀',
  'guard:thermal_fan': '② 排烟/消毒超温联动风机',
  'guard:flame_unattended': '③ 明火长时无人看管',
}
const GUARDS = Object.keys(GUARD_LABEL)

/** 关键遥测：安全类的四个先放，剩下的按分组排。 */
const KEY_METRICS = ['gas_level', 'hood_temp', 'fridge_temp', 'freezer_temp',
  'sterilize_temp', 'sample_temp', 'sct_current', 't']

const keyTelemetry = computed(() => {
  const map = Object.fromEntries(telemetry.value.map((item) => [item.id, item]))
  return KEY_METRICS.map((id) => map[id]).filter(Boolean)
})

const metrics = computed(() => live.overview?.telemetry || {})
const counts = computed(() => live.overview?.counts || {})

const cookOption = computed(() => {
  const theme = chartTheme()
  const rows = kpi.value?.history || []
  if (!rows.length) {
    // 没有分档数据时用当前均值画一根柱，总比空白好读
    const value = kpi.value?.cook_duration?.value
    return {
      xAxis: { type: 'category', data: value == null ? [] : ['当前均值'] },
      yAxis: { type: 'value', name: '秒' },
      series: value == null ? [] : [{
        type: 'bar', data: [value], barWidth: 40,
        itemStyle: { color: hexA(theme.accent, 0.75), borderRadius: [4, 4, 0, 0] },
      }],
    }
  }
  return {
    xAxis: { type: 'category', data: rows.map((row) => row.label) },
    yAxis: { type: 'value', name: '秒' },
    series: [{ type: 'bar', data: rows.map((row) => row.value), barWidth: '52%' }],
  }
})

const disinfectOption = computed(() => {
  const theme = chartTheme()
  const data = kpi.value?.disinfection
  const rate = data?.value
  const has = rate !== null && rate !== undefined
  return {
    series: [{
      type: 'gauge',
      startAngle: 210, endAngle: -30,
      min: 0, max: 100,
      radius: '96%',
      center: ['50%', '62%'],
      progress: {
        show: true, width: 12,
        itemStyle: { color: has && rate >= 90 ? theme.ok : theme.warn },
      },
      axisLine: { lineStyle: { width: 12, color: [[1, theme.line]] } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: false },
      title: { show: false },
      detail: {
        offsetCenter: [0, '-2%'],
        fontSize: 24,
        fontFamily: theme.mono,
        color: theme.text,
        formatter: has ? `{v|${fmt(rate, { digits: 1 })}%}` : '{v|—}',
        rich: { v: { fontSize: 24, fontFamily: theme.mono } },
      },
      data: [{ value: has ? rate : 0 }],
    }],
  }
})

async function load() {
  loading.value = true
  try {
    kpi.value = await api.get('/api/kpi')
  } catch (err) {
    toastError(err, 'KPI 加载失败')
  } finally {
    loading.value = false
  }
}

async function seedDemo() {
  seeding.value = true
  try {
    const data = await api.post('/api/demo/orders', {})
    toastOk(`已生成 ${data.created} 单${data.skipped?.length ? `，跳过 ${data.skipped.length} 项（沽清/停售）` : ''}`)
    await load()
  } catch (err) {
    toastError(err, '生成演示订单失败')
  } finally {
    seeding.value = false
  }
}

/* ---------------------------------------------------------------- 事件文案 */
const KIND_LABEL = {
  rule: '规则', guard: '保护', order: '订单', station: '工位', ticket: '工单',
  channel: '通道', network: '网络', param: '参数', admission: '准入',
  dish: '菜品', inject: '注入', 'fault-escalate': '升级', hello: '连接', bye: '重连',
}
const KIND_TONE = {
  guard: 'danger', rule: 'warn', ticket: 'warn', 'fault-escalate': 'danger',
  channel: 'info', network: 'info', param: 'info', admission: 'info',
  order: 'ok', station: 'plain', dish: 'plain', inject: 'warn',
  hello: 'plain', bye: 'plain',
}

function kindLabel(kind) {
  return KIND_LABEL[kind] || kind
}

function toneOf(kind) {
  return KIND_TONE[kind] || 'plain'
}

function describeEvent(ev) {
  if (ev.message) return ev.message
  if (ev.kind === 'order') {
    if (ev.order_no) return `订单 ${ev.order_no} → ${ev.state || ''}`
    if (ev.item_id) return `子项 #${ev.item_id} → ${ev.state || ''}`
    return '订单状态变化'
  }
  if (ev.kind === 'channel') return `${ev.action}（${ev.channel}）${ev.label || ''}`
  if (ev.kind === 'network') {
    return ev.online ? '网络恢复' : `网络断开，本地排队 ${ev.queued ?? 0} 条`
  }
  if (ev.kind === 'admission') return `刷卡 ${ev.card || ''}：${ev.ok ? '通过' : '拒绝'}`
  if (ev.kind === 'param') {
    return `参数变更：${(ev.changed || []).join('、') || ev.time_scale || ''}`
  }
  if (ev.kind === 'ticket') return `工单 #${ev.id} → ${ev.state}`
  if (ev.kind === 'inject') return `演示注入：${Object.keys(ev.metrics || {}).join(',')}`
  return ev.kind
}

let timer = null
onMounted(() => {
  load()
  // KPI 是全站唯一需要「定期重算」的页面：其它页靠事件推送就够，
  // 但出餐时长/达标率是聚合值，只能重新查
  timer = setInterval(load, 20000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="stack">
    <!-- 保护状态：三秒能看清有没有事 -->
    <div class="guards">
      <div
        v-for="key in GUARDS" :key="key"
        class="guard" :class="{ on: trippedGuards.includes(key) }"
      >
        <Icon :name="trippedGuards.includes(key) ? 'alert' : 'check'" :size="14" />
        <span>{{ GUARD_LABEL[key] }}</span>
        <span class="spacer"></span>
        <span class="tag" :class="trippedGuards.includes(key) ? 'danger' : 'ok'">
          {{ trippedGuards.includes(key) ? '已触发' : '正常' }}
        </span>
      </div>
    </div>

    <!-- 四个 KPI -->
    <div class="grid grid-4">
      <StatCard
        label="出餐时长（派单→出餐）"
        :value="kpi?.cook_duration?.value == null ? null : fmt(kpi.cook_duration.value, { digits: 0 })"
        unit="秒"
        :hint="kpi?.cook_duration?.p90 != null ? `P90 ${fmt(kpi.cook_duration.p90, { digits: 0 })} 秒` : ''"
        :note="kpi?.cook_duration?.note || ''"
        :samples="kpi?.cook_duration?.samples ?? null"
        :loading="loading"
        :tone="kpi?.cook_duration?.value == null ? 'plain' : 'info'"
        icon="orders"
      />
      <StatCard
        label="视觉违规数"
        :value="kpi?.violations?.available ? fmtInt(kpi.violations.value) : '未接入'"
        :unit="kpi?.violations?.available ? '次' : ''"
        :note="kpi?.violations?.available ? '' : '视觉模型未训练，不下发违规判定'"
        :loading="loading"
        :tone="kpi?.violations?.available ? 'warn' : 'plain'"
        icon="shield"
      />
      <StatCard
        label="告警处置时长（开单→关单）"
        :value="kpi?.alarm_handle?.value == null ? null : fmt(kpi.alarm_handle.value, { digits: 0 })"
        unit="秒"
        :note="kpi?.alarm_handle?.note || ''"
        :samples="kpi?.alarm_handle?.samples ?? null"
        :loading="loading"
        :tone="kpi?.alarm_handle?.value == null ? 'plain' : 'warn'"
        icon="ticket"
      />
      <StatCard
        label="消毒达标率"
        :value="kpi?.disinfection?.value == null ? null : fmt(kpi.disinfection.value, { digits: 1 })"
        unit="%"
        :hint="kpi?.disinfection?.samples ? `${kpi.disinfection.passed}/${kpi.disinfection.samples} 次达标` : ''"
        :note="kpi?.disinfection?.note || ''"
        :samples="kpi?.disinfection?.samples ?? null"
        :loading="loading"
        :tone="kpi?.disinfection?.value == null ? 'plain' : (kpi.disinfection.value >= 90 ? 'ok' : 'warn')"
        icon="shield"
      />
    </div>

    <div class="grid grid-2 mid">
      <!-- 三通道 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">三通道运行状态</span>
          <span class="panel-sub mono">{{ counts.busy_stations ?? 0 }}/{{ counts.stations ?? 0 }} 工位占用</span>
        </div>
        <div class="panel-body chan-body">
          <div class="chans">
            <div v-for="ch in channels" :key="ch.name" class="chan" :class="ch.state">
              <div class="chan-top">
                <span class="mono chan-name">{{ ch.name }}</span>
                <span class="tag" :class="describe(CHANNEL_STATE, ch.state).tone">
                  {{ describe(CHANNEL_STATE, ch.state).label }}
                </span>
              </div>
              <div class="chan-label">{{ ch.label }}</div>
              <div v-if="Object.keys(ch.sources).length" class="sources">
                <span v-for="(level, name) in ch.sources" :key="name" class="src mono">
                  {{ name }}·P{{ level }}
                </span>
              </div>
              <div v-else class="tiny muted">无触发源</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 关键遥测 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">关键读数</span>
          <span class="panel-sub mono">
            {{ live.overview?.profile ? `档位 ${live.overview.profile}` : '' }}
          </span>
        </div>
        <div class="panel-body tight">
          <!-- 两列读数网格：8 项排成 4 行，与左侧「三通道」高度接近。
               单列 8 行时右栏比左栏高出 130px，左栏下方就是一大片空白。 -->
          <div class="readings">
            <div v-for="item in keyTelemetry" :key="item.id" class="reading">
              <span class="r-label">{{ item.label }}</span>
              <span class="r-value">
                <span class="num">
                  {{ fmt(item.v, { digits: item.id === 'gas_level' ? 3 : 1, fallback: '—' }) }}
                  <em v-if="item.unit">{{ item.unit }}</em>
                </span>
                <span class="r-time tiny muted">{{ item.ts ? fmtTime(item.ts) : '未采样' }}</span>
              </span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <div class="grid grid-2">
      <!-- 出餐时长 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">出餐时长<span class="panel-sub"> · 派单到出餐，单位秒</span></span>
        </div>
        <div class="panel-body">
          <ChartPanel
            :option="cookOption" :height="180"
            :is-empty="kpi?.cook_duration?.samples === 0"
            empty="还没有完成出餐的订单——去订单页造一单并走完流程"
            label="出餐时长：每单从派单到出餐的耗时，横轴为完成时间，纵轴为秒"
          />
        </div>
      </section>

      <!-- 消毒达标率 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">消毒达标率<span class="panel-sub"> · 已结束会话中达标占比</span></span>
        </div>
        <div class="panel-body">
          <ChartPanel
            :option="disinfectOption" :height="180"
            :is-empty="kpi?.disinfection?.samples === 0"
            empty="还没有已结束的消毒会话"
            label="消毒达标率：已结束消毒会话中达到标准的占比，横轴为日期"
          />
        </div>
      </section>
    </div>

    <!-- 事件流 -->
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">最近事件<span class="panel-sub"> · 实时推送，最多保留 200 条</span></span>
        <div class="row">
          <button class="btn sm" type="button" @click="seedDemo" :disabled="seeding">
            <Icon name="play" :size="12" />{{ seeding ? '生成中…' : '生成演示订单' }}
          </button>
          <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
            <Icon name="refresh" :size="12" />刷新
          </button>
        </div>
      </div>
      <div class="panel-body tight">
        <div v-if="!live.events.length" class="empty">
          还没有事件。设备每秒采一帧，有判定结果时会出现在这里。
        </div>
        <ul v-else class="events">
          <li v-for="(ev, index) in live.events.slice(0, 24)" :key="ev.seq || index" class="event">
            <span class="mono tiny muted time">{{ ev.time || fmtTime(ev.ts) }}</span>
            <span class="tag" :class="toneOf(ev.kind)">{{ kindLabel(ev.kind) }}</span>
            <span class="text truncate">{{ describeEvent(ev) }}</span>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 保护状态条：整页最高优先级的信息。正常时低调成三条中性条，被触发时
   整条翻成红底——「有没有事」这一眼不该需要读文字才能判断。 */
.guards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.guard {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--radius);
  border: 1px solid rgba(0, 0, 0, 0.05);
  background: var(--panel);
  box-shadow: var(--shadow);
  color: var(--text-2);
  font-size: var(--fs-sm);
  font-weight: 500;
  transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
}
:root[data-theme="dark"] .guard { border-color: var(--line); }
.guard .spacer { flex: 1; }
/* 正常态图标压暗，避免三行绿勾抢走视觉重心 */
.guard > svg { color: var(--ok); opacity: 0.55; flex: none; }
.guard.on {
  border-color: var(--danger);
  background: var(--danger-soft);
  color: var(--danger-ink);
  font-weight: 600;
}
.guard.on > svg { color: var(--danger); opacity: 1; }

.chans { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
/* 这一行两栏强行等高：左侧只有三张短卡片，右侧是 4 行读数表。
   等高的收益是「面板底边对齐」，代价是左栏卡片内部要拉开分布——
   卡片内用 space-between 把状态、名称、触发源分到上中下，看起来是排版，
   而不是「一块面板下面空了一截」。 */
.mid { align-items: stretch; }
.mid > .panel { display: flex; flex-direction: column; }
.mid > .panel > .chan-body { flex: 1; display: flex; }
.mid .chans { flex: 1; align-items: stretch; }
/* 读数网格：4 行 × 2 列。分割线用 border 画在单元格上，
   不用表格是为了让「标签 / 数值 + 采样时刻」能在窄格子里两行排下。 */
.readings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.reading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px var(--sp-4);
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  min-width: 0;
}
.reading:nth-child(2n) { border-right: none; }
.reading:nth-last-child(-n + 2) { border-bottom: none; }
.r-label {
  font-size: var(--fs-xs);
  color: var(--muted);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.r-value { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; flex: none; }
.r-value .num { font-size: var(--fs-base); font-weight: 600; letter-spacing: -0.01em; }
.r-value em { font-style: normal; font-size: var(--fs-2xs); font-weight: 400; color: var(--muted); margin-left: 2px; }
.r-time { font-size: var(--fs-2xs); opacity: 0.8; }
.chan {
  border: 1px solid var(--line);
  border-left: 3px solid var(--line-strong);
  border-radius: var(--radius-sm);
  padding: 11px 13px;
  background: var(--surface-2);
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
  transition: border-color 160ms ease, background 160ms ease;
}
/* 状态只靠左色条 + 右上标签表达，不整块染色：三张卡片并排时，
   整块淡橙/淡红在深色主题下会糊成一片土褐，反而看不出哪张是坏的。 */
.chan.releasing { border-left-color: var(--warn); }
.chan.latched, .chan.manual_reset_required { border-left-color: var(--danger); }
.chan-top { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.chan-name { font-size: var(--fs-xs); color: var(--muted); font-weight: 600; letter-spacing: 0.02em; }
.chan-label { font-size: var(--fs-sm); font-weight: 600; line-height: 1.4; }
.sources { display: flex; flex-wrap: wrap; gap: 4px; color: var(--danger-ink); }
.src {
  font-size: var(--fs-2xs);
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--danger-soft);
  font-weight: 500;
}

.events { list-style: none; margin: 0; padding: 4px 0; max-height: 320px; overflow: auto; }.event {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px var(--sp-4);
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--line);
  transition: background 120ms ease;
}
.event:hover { background: var(--panel-2); }
.event:last-child { border-bottom: none; }
.event .time { width: 66px; flex: none; font-variant-numeric: tabular-nums; }
.event .text { color: var(--text-2); }

@media (max-width: 1100px) {
  .guards, .chans { grid-template-columns: minmax(0, 1fr); }
}
</style>
