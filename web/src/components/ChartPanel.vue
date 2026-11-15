<script setup>
/**
 * ECharts 容器。**所有图表都走这一个组件**。
 *
 * 三个必须处理的问题：
 *
 * 1. **实例必须 `dispose()`**。ECharts 内部有动画帧循环与事件监听，不 dispose
 *    的话切九次页面就有九个实例在跑，表现是「用久了越来越卡」而不是明显报错。
 * 2. **尺寸用 `ResizeObserver` 而不是 `window.resize`**。侧栏折叠、表格加载完成
 *    后高度变化都不会触发 window 的 resize，图表会停在旧尺寸（被裁掉一半）。
 * 3. **主题色从 CSS 变量读**。硬编码颜色会让浅色主题下的坐标轴变成白底白字。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
// **按需引入**，不用 `import * as echarts from 'echarts'`：
// 全量包 1.03MB（gzip 343KB），按需之后 400KB 左右（gzip 130KB）。现场 wifi
// 通常 2~5Mbps，首屏差 200KB 就是多等一秒；而 ECharts 里绝大部分图表类型
// （树图、地图、桑基图、3D）这个项目一个都用不到。
// 代价是**每加一种图表类型都要在这里补一行 use()**，漏了的表现是
// 「图表空白且不报错」（sereis.type 找不到对应实现），所以下面按类型列清楚。
import * as echarts from 'echarts/core'
import { BarChart, CustomChart, GaugeChart, LineChart } from 'echarts/charts'
import {
  AxisPointerComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { buildOption, chartTheme } from '../utils/chart'

echarts.use([
  LineChart, BarChart, GaugeChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent,
  MarkLineComponent, MarkAreaComponent, AxisPointerComponent,
  CanvasRenderer,
])

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: [Number, String], default: 240 },
  empty: { type: String, default: '暂无数据' },
  isEmpty: { type: Boolean, default: false },
})

const host = ref(null)
let chart = null
let observer = null
let themeWatcher = null

function render() {
  if (!chart) return
  chart.setOption(buildOption(props.option, chartTheme()), { notMerge: true })
}

/** 主题切换/首挂后整机重建：setOption 换不掉的部分（层叠合成、GPU 纹理缓存）
 *  在个别 Chromium 版本上会残留初始化时的浅色绘制，重建实例是唯一彻底的手段。 */
function rebuild() {
  if (!host.value) return
  if (chart) {
    chart.dispose()
    chart = null
  }
  chart = echarts.init(host.value, null, { renderer: 'canvas' })
  render()
}

onMounted(() => {
  if (!host.value) return
  rebuild()
  observer = new ResizeObserver(() => chart && chart.resize())
  observer.observe(host.value)
  // 主题切换（`data-theme` 变化）后整机重建图表
  themeWatcher = new MutationObserver(() => rebuild())
  themeWatcher.observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  })
  // 兜底：首挂后两帧再画一次。App 的 applyTheme（设 data-theme）与本组件
  // 的挂载时序在个别路径下会交错，晚两帧时主题属性已稳定，重画必取到终态色。
  requestAnimationFrame(() => requestAnimationFrame(render))
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  if (themeWatcher) themeWatcher.disconnect()
  if (observer) observer.disconnect()
  observer = null
  themeWatcher = null
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <div
    class="chart-wrap"
    :style="{ height: typeof height === 'number' ? height + 'px' : height }"
  >
    <div ref="host" class="chart"></div>
    <div v-if="isEmpty" class="chart-empty">{{ empty }}</div>
  </div>
</template>

<style scoped>
.chart-wrap { position: relative; width: 100%; }
/* 显式跟 panel 同色：canvas 之下永远不存在浏览器默认白底 */
.chart { width: 100%; height: 100%; background: var(--panel); }
.chart-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 12px;
  pointer-events: none;
}
</style>
