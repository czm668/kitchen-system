<script setup>
/**
 * A7-03 工位甘特。
 *
 * ## 为什么用 ECharts 的 `custom` 序列而不是堆叠柱状图
 *
 * 堆叠柱要先把时间轴切成等宽桶（比如每分钟一格），那么「3 分 40 秒的占用」
 * 要么被四舍五入到 4 格、要么得用小数宽度——两种都会让甘特看起来「对不上」。
 * `custom` 直接按真实起止坐标画矩形，宽度就是真实时长。
 *
 * ## 未结束的段为什么要虚线
 *
 * 正在炒的菜和已经出餐的菜在图上如果长得一样，看一眼不知道「现在灶上有没有人」。
 * 未结束的段延伸到「此刻」，并加虚线描边 + 右侧箭头形状，一眼区分。
 *
 * ## 为什么甘特下面要单独列转派留痕
 *
 * 甘特只能看出「换过人」，看不出「为什么换、谁决定的」。评审和复盘问的恰恰
 * 是后者，所以把 `handovers` 单列一张表。
 *
 * ## 为什么要在这页放「上灶 / 释放」按钮
 *
 * 真实流程里上灶是刷卡的（RC522 → TFT），但**答辩现场只有一块开发板**，读卡器
 * 未必在手边。没有这两个按钮，甘特永远是空的，图上画得再好也说不清「这页能看
 * 什么」。所以管理端保留同一条写入路径（`POST /api/stations/{occupy,release}`），
 * 走的还是 `DispatchBoard` 的同一套资质/忙闲判定——不是演示专用的假数据。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ChartPanel from '../components/ChartPanel.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { chartTheme, hexA } from '../utils/chart'
import { fmtDuration, fmtTime, STATION_STATE, describe } from '../utils/format'
import { toastError, toastOk } from '../composables/useToast'

const gantt = ref(null)
const stations = ref([])
const staffList = ref([])
const loading = ref(true)
const busy = ref(0)
/** 每个工位当前选中的上灶人。按工位记住选择，免得连续给几个灶上灶要反复点。 */
const picks = ref({})

const writable = computed(() => can('order.write'))

const segments = computed(() => gantt.value?.segments || [])
const handovers = computed(() => gantt.value?.handovers || [])

/** 工位列表：图上纵轴顺序固定按 id，不随时间跳。 */
const stationRows = computed(() => {
  const seen = new Map()
  for (const station of stations.value) {
    seen.set(station.id, station)
  }
  for (const seg of segments.value) {
    if (!seen.has(seg.station_id)) {
      seen.set(seg.station_id, { id: seg.station_id, code: `ST${seg.station_id}`, name: '' })
    }
  }
  return [...seen.values()].sort((a, b) => a.id - b.id)
})

const nowSec = computed(() => Date.now() / 1000)

const option = computed(() => {
  const theme = chartTheme()
  const rows = stationRows.value
  const index = new Map(rows.map((row, i) => [row.id, i]))
  const data = segments.value
    .filter((seg) => seg.start_epoch)
    .map((seg) => {
      const open = !seg.end_epoch
      const start = seg.start_epoch * 1000
      const end = (seg.end_epoch || nowSec.value) * 1000
      const cooking = seg.state === 'open'
      const color = cooking ? theme.warn : theme.accent
      return {
        value: [index.get(seg.station_id) ?? 0, start, Math.max(end, start + 1000)],
        itemStyle: {
          color: hexA(color, cooking ? 0.42 : 0.26),
          borderColor: color,
          borderWidth: 1,
          borderType: cooking ? 'dashed' : 'solid',
        },
        // 供 tooltip 用
        meta: seg,
      }
    })

  const span = data.length
    ? [Math.min(...data.map((d) => d.value[1])), Math.max(...data.map((d) => d.value[2]))]
    : [Date.now() - 600000, Date.now()]

  return {
    grid: { top: 22, right: 18, bottom: 28, left: 62 },
    tooltip: {
      formatter: (params) => {
        const seg = params.data.meta
        const head = `<b>${seg.dish || '（未挂单）'}</b>`
        const lines = [
          `工位 ${seg.station_id} · ${seg.staff_name || '未指派'}`,
          seg.order_no ? `订单 ${seg.order_no}` : '',
          `${fmtTime(seg.start_epoch)} → ${seg.end_epoch ? fmtTime(seg.end_epoch) : '进行中'}`,
          `时长 ${fmtDuration(seg.elapsed_sec)}`,
          seg.flame_verified ? '点火已确认' : '⚠ 未确认点火',
        ].filter(Boolean)
        return `${head}<br/>${lines.join('<br/>')}`
      },
    },
    xAxis: {
      type: 'time',
      min: span[0],
      max: Math.max(span[1], span[0] + 60000),
      splitLine: { show: true },
    },
    yAxis: {
      type: 'category',
      data: rows.map((row) => `${row.code}${row.name ? ' ' + row.name : ''}`),
      inverse: true,
      splitLine: { show: false },
    },
    series: [{
      type: 'custom',
      // 画矩形：x 起止用真实时间坐标，高度取类别带宽的 56%
      renderItem(params, api) {
        const categoryIndex = api.value(0)
        const start = api.coord([api.value(1), categoryIndex])
        const end = api.coord([api.value(2), categoryIndex])
        const bandHeight = api.size([0, 1])[1]
        const height = bandHeight * 0.56
        const width = Math.max(end[0] - start[0], 2)
        return {
          type: 'rect',
          shape: { x: start[0], y: start[1] - height / 2, width, height, r: 3 },
          style: api.style(),
        }
      },
      encode: { x: [1, 2], y: 0 },
      data,
    }],
  }
})

async function load() {
  loading.value = true
  try {
    const [ganttData, stationData, staffData] = await Promise.all([
      api.get('/api/stations/gantt'),
      api.get('/api/stations'),
      api.get('/api/staff'),
    ])
    gantt.value = ganttData
    stations.value = stationData.stations || []
    staffList.value = staffData.staff || []
  } catch (err) {
    toastError(err, '工位数据加载失败')
  } finally {
    loading.value = false
  }
}

const exhaust = computed(() => gantt.value?.exhaust || null)

/** 在岗的人排前面：上灶是「现在站在灶前的人」的动作，不在岗的排在后面。 */
const candidates = computed(() => [...staffList.value]
  .sort((a, b) => Number(b.on_duty) - Number(a.on_duty) || a.id - b.id))

function pickOf(station) {
  if (picks.value[station.id]) return picks.value[station.id]
  const first = candidates.value.find((row) => row.on_duty) || candidates.value[0]
  return first ? first.id : ''
}

async function occupy(station) {
  const staffId = Number(pickOf(station))
  if (!staffId) {
    toastError('还没选上灶的人')
    return
  }
  busy.value = station.id
  try {
    // `item_id` 不传：现场是「先上灶、再挂单」，而挂在哪个子项由派单决定。
    // 强行让页面选子项会多一步且容易选错灶。
    await api.post('/api/stations/occupy',
      { station_id: station.id, staff_id: staffId })
    toastOk(`${station.code} 已上灶`)
    await load()
  } catch (err) {
    // 资质不符 / 已在别的灶上，后端会给具体原因，原样显示
    toastError(err, '上灶被拒绝')
  } finally {
    busy.value = 0
  }
}

async function release(station) {
  busy.value = station.id
  try {
    await api.post('/api/stations/release',
      { station_id: station.id, reason: '管理端释放' })
    toastOk(`${station.code} 已释放`)
    await load()
  } catch (err) {
    toastError(err, '释放失败')
  } finally {
    busy.value = 0
  }
}

let timer = null
onMounted(() => {
  load()
  // 甘特要跟着时间走：不刷新的话「进行中」的段不会延长，看起来像卡住了
  timer = setInterval(load, 8000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="stack">
    <!-- 工位实时卡 -->
    <div class="grid grid-2">
      <section
        v-for="station in stations" :key="station.id" class="panel station"
        :class="station.state"
      >
        <div class="panel-head">
          <span class="panel-title">
            {{ station.code }}
            <span class="panel-sub">{{ station.name || station.kind }}</span>
          </span>
          <span class="tag" :class="describe(STATION_STATE, station.state).tone">
            {{ describe(STATION_STATE, station.state).label }}
          </span>
        </div>
        <div class="panel-body">
          <template v-if="station.occupancy">
            <div class="occ">
              <div class="occ-main">
                <strong>{{ station.occupancy.dish || '（未挂单）' }}</strong>
                <span class="tiny muted mono">
                  {{ station.occupancy.staff_name || `staff#${station.occupancy.staff_id}` }}
                </span>
              </div>
              <div class="occ-meta">
                <span class="mono">{{ fmtDuration(station.occupancy.elapsed_sec) }}</span>
                <span class="tag" :class="station.flame?.ok ? 'ok' : 'warn'">
                  {{ station.flame?.ok ? '点火正常' : '点火未确认' }}
                </span>
              </div>
            </div>
          </template>
          <div v-else class="idle">
            <span class="muted small">空闲</span>
            <span v-if="station.reserved_name" class="tiny muted">
              已预约给 {{ station.reserved_name }}
            </span>
          </div>
          <div v-if="writable" class="st-acts">
            <template v-if="station.occupancy">
              <button type="button" class="btn ghost sm"
                      :disabled="busy === station.id" @click="release(station)">
                释放工位
              </button>
            </template>
            <template v-else>
              <select v-model.number="picks[station.id]" class="input xs">
                <option v-for="person in candidates" :key="person.id" :value="person.id">
                  {{ person.name }}{{ person.on_duty ? '' : '（不在岗）' }}
                </option>
              </select>
              <button type="button" class="btn sm"
                      :disabled="busy === station.id" @click="occupy(station)">
                上灶
              </button>
            </template>
          </div>
          <p class="flame-text tiny muted">{{ station.flame?.text }}</p>
        </div>
      </section>
    </div>

    <!-- 甘特 -->
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">
          工位占用时间轴
          <span class="panel-sub"> · 虚线 = 进行中，实线 = 已出餐</span>
        </span>
        <div class="row">
          <span v-if="exhaust" class="tag" :class="exhaust.running ? 'ok' : 'warn'">
            排风 {{ exhaust.running ? '运行' : '未运行' }}
          </span>
          <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
            <Icon name="refresh" :size="12" />刷新
          </button>
        </div>
      </div>
      <div class="panel-body">
        <ChartPanel
          :option="option" :height="Math.max(160, stationRows.length * 54 + 60)"
          :is-empty="!segments.length"
          empty="还没有上灶记录。派单后刷卡上灶（或点「上灶」）才会出现在这里。"
        />
      </div>
    </section>

    <!-- 转派留痕 -->
    <section class="panel">
      <div class="panel-head">
        <span class="panel-title">转派 / 接管留痕</span>
        <span class="panel-sub">{{ handovers.length }} 条</span>
      </div>
      <div class="panel-body tight">
        <div v-if="!handovers.length" class="empty">
          没有转派记录。离岗转派（#11）触发时，每一次换人都会留一条。
        </div>
        <table v-else class="table">
          <thead>
            <tr>
              <th style="width: 86px">时间</th>
              <th style="width: 110px">订单</th>
              <th style="width: 70px" class="num">子项</th>
              <th>原因</th>
              <th style="width: 96px">操作人</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in handovers" :key="i">
              <td class="mono tiny">{{ fmtTime(row.ts) }}</td>
              <td class="mono small">{{ row.order_no }}</td>
              <td class="num mono tiny">#{{ row.item_id ?? '—' }}</td>
              <td class="pre-wrap">{{ row.reason }}</td>
              <td class="small">{{ row.actor || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.station.busy { border-color: var(--warn); }
.station.reserved { border-color: var(--accent); }

.occ { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.occ-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.occ-meta { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.idle { display: flex; align-items: center; justify-content: space-between; }
.flame-text { margin: 8px 0 0; }
.st-acts { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
.st-acts .input.xs { width: auto; min-width: 104px; flex: 1; text-align: left; }
</style>
