<script setup>
/**
 * ECharts 容器。**所有图表都走这一个组件**。
 *
 * 四个必须处理的问题：
 *
 * 1. **实例必须 `dispose()`**。ECharts 内部有动画帧循环与事件监听，不 dispose
 *    的话切九次页面就有九个实例在跑，表现是「用久了越来越卡」而不是明显报错。
 * 2. **尺寸用 `ResizeObserver` 而不是 `window.resize`**。侧栏折叠、表格加载完成
 *    后高度变化都不会触发 window 的 resize，图表会停在旧尺寸（被裁掉一半）。
 * 3. **主题色从 CSS 变量读**。硬编码颜色会让浅色主题下的坐标轴变成白底白字。
 * 4. **图表库必须是动态 import**。见 `utils/echarts-setup.js` 里的长注释：
 *    静态 import 会让首屏先下完 181KB(gzip) 的图表库才轮到渲染。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { buildOption, chartTheme } from '../utils/chart'

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: [Number, String], default: 240 },
  empty: { type: String, default: '暂无数据' },
  isEmpty: { type: Boolean, default: false },
  /**
   * 读屏器读到的图表说明。
   *
   * canvas 对辅助技术是完全不透明的——不加这个，Tab 到图表位置只会听到
   * 「图形」或者干脆一片空白。而这张图往往承载的是「出餐时长有没有恶化」
   * 这类结论，纯视觉用户一眼看到、读屏用户完全拿不到，是不对等的。
   *
   * 传入时请写成「结论 + 口径」，不要只写图名，例如
   * 「出餐时长：派单到出餐的耗时分布，当前均值 6 分 20 秒」。
   */
  label: { type: String, default: '' },
})

/** 没传 label 时退回用 series 名拼一个最小可读描述，总比没有强 */
const ariaLabel = computed(() => {
  if (props.label) return props.label
  const names = (props.option?.series || []).map((s) => s?.name).filter(Boolean)
  const title = props.option?.title?.text
  return [title, names.length ? names.join('、') : ''].filter(Boolean).join('，') || '数据图表'
})

const host = ref(null)
let chart = null
let observer = null
let themeWatcher = null

/**
 * 图表库的加载状态。
 *
 * `loading` 期间画布是空的，必须有点东西告诉用户「图在来的路上」——
 * 尤其在 2MB 的现场 wifi 上，这 300ms 的空白看起来就是「加载失败」。
 * `failed` 只在动态 import 真的失败时出现（首次访问就断网、chunk 被拦）。
 */
const phase = ref('loading')

/** 图表库只加载一次，多个图表共用同一个模块实例 */
let libPromise = null
function loadLib() {
  if (!libPromise) {
    libPromise = import('../utils/echarts-setup.js')
      .then((mod) => mod.default)
      .catch((err) => {
        // 失败后把 promise 清掉，下次（比如重试/切页回来）还能再试
        libPromise = null
        throw err
      })
  }
  return libPromise
}

/**
 * 空态时把画布高度收敛到 88px。
 *
 * 原先无数据也保留 180px 画布，两条空图表把「最近事件」挤到折叠线以下——
 * 而空态本身没有任何信息量。88px 是 ECharts 仍能正常 init 的最小安全高度
 * （再矮会触发「Can't get dom width or height」告警），同时把垂直空间还给内容。
 * 数据回来后高度复原，ResizeObserver 会自动触发 resize。
 */
const EMPTY_HEIGHT = 88
const wrapStyle = computed(() => {
  const h = props.isEmpty ? EMPTY_HEIGHT : props.height
  return { height: typeof h === 'number' ? `${h}px` : h }
})

function render() {
  if (!chart) return
  chart.setOption(buildOption(props.option, chartTheme()), { notMerge: true })
}

/**
 * 主题切换/首挂后整机重建：setOption 换不掉的部分（层叠合成、GPU 纹理缓存）
 * 在个别 Chromium 版本上会残留初始化时的浅色绘制，重建实例是唯一彻底的手段。
 *
 * `gen` 是防竞态的版本号：动态 import 是异步的，用户快速切主题时会有多个
 * rebuild 并发，晚到的那个不能把已经重建好的实例又 dispose 一次。
 */
let gen = 0
async function rebuild() {
  if (!host.value) return
  const mine = ++gen
  let echarts
  try {
    echarts = await loadLib()
  } catch (err) {
    console.error('图表库加载失败:', err)
    if (mine === gen) phase.value = 'failed'
    return
  }
  // 等待期间组件可能已卸载（切页），或已有更新的一次 rebuild 在路上
  if (mine !== gen || !host.value) return
  if (chart) {
    chart.dispose()
    chart = null
  }
  chart = echarts.init(host.value, null, { renderer: 'canvas' })
  phase.value = 'ready'
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
  gen++                        // 让还在飞的 rebuild 回来时直接放弃
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
  <div class="chart-wrap" :class="{ 'is-empty': isEmpty }" :style="wrapStyle">
    <!-- 空态时让出 role，避免和下方那段可见的空态文案被读屏器重复播报 -->
    <div
      ref="host"
      class="chart"
      :role="isEmpty ? undefined : 'img'"
      :aria-label="isEmpty ? undefined : ariaLabel"
    ></div>
    <!-- 图表库还在下载：给一句可读说明，而不是一块空白。
         空态下不显示载入提示（两句话会叠在一起），但**失败提示要压过空态**：
         库没加载出来时，下面的「暂无数据」是一句假话——数据可能是有的。 -->
    <div v-if="phase === 'loading' && !isEmpty" class="chart-hint" aria-hidden="true">
      <span class="hint-dot"></span>正在载入图表…
    </div>
    <div v-else-if="phase === 'failed'" class="chart-hint is-failed" role="status">
      图表未能载入，读数不受影响
    </div>
    <div v-if="isEmpty && phase !== 'failed'" class="chart-empty">
      <span class="empty-mark">—</span>
      <span>{{ empty }}</span>
    </div>
  </div>
</template>

<style scoped>
.chart-wrap { position: relative; width: 100%; }
/* 显式跟 panel 同色：canvas 之下永远不存在浏览器默认白底 */
.chart { width: 100%; height: 100%; background: var(--panel); }

/* 载入提示压在画布中央：不改变高度、不推挤下面的内容 */
.chart-hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  font-size: var(--fs-xs);
  color: var(--muted);
  pointer-events: none;
}
.chart-hint.is-failed { color: var(--warn-ink); }
.hint-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.5;
  animation: hint-pulse 1.1s ease-in-out infinite;
}
@keyframes hint-pulse {
  0%, 100% { opacity: 0.25; transform: scale(0.85); }
  50% { opacity: 0.7; transform: scale(1); }
}

.chart-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 12px;
  border-radius: var(--radius-sm);
  border: 1px dashed var(--line-strong);
  background: var(--panel-2);
  color: var(--muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: center;
  pointer-events: none;
}
.empty-mark { font-size: 15px; opacity: 0.6; }
</style>
