/**
 * ECharts 按需装配。**故意单独成一个模块**。
 *
 * ## 为什么不能把 `import 'echarts/core'` 留在 ChartPanel.vue 里
 *
 * 静态 import 的模块会进**首屏依赖图**：浏览器拿到 index.js 后必须先下完并
 * 求值它 import 的所有模块，Vue 才开始挂载。`manualChunks` 只是把 echarts
 * 拆成独立文件，并没有把它挪出这条关键路径——实测首屏照样先下 181KB(gzip)
 * 的 echarts，才轮到渲染。
 *
 * 只有「被动态 `import()` 的模块」才不进首屏依赖图。所以这里把 echarts 的
 * 全部 import 收进本文件，ChartPanel 用 `await import('./echarts-setup.js')`
 * 拿它。Rollup 会把本文件连同它用到的 echarts 子模块切成一个懒加载 chunk，
 * 未进入图表页的用户一个字节都不下载。
 *
 * ## 代价：每加一种图表类型都要在这里补一行
 *
 * 漏了的表现是「图表空白且不报错」（series.type 找不到实现），所以下面按类型
 * 列清楚，别指望运行时报错提醒你。
 */
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

echarts.use([
  LineChart, BarChart, GaugeChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent,
  MarkLineComponent, MarkAreaComponent, AxisPointerComponent,
  CanvasRenderer,
])

export default echarts
