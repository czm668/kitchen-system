/**
 * ECharts 公共配置：主题取色 + 坐标轴基线。
 *
 * 单独放一个模块而不是写在 `ChartPanel.vue` 的 `<script setup>` 里，是因为
 * 页面如果想在别处复用一个纯函数（比如导出图片时算色），从 `.vue` 里拿不到
 * 具名导出。放这里，组件和页面都能 import。
 */

/** 从 CSS 变量取当前主题的颜色。 */
export function chartTheme() {
  const css = getComputedStyle(document.documentElement)
  const read = (name, fallback) => (css.getPropertyValue(name) || fallback).trim() || fallback
  return {
    text: read('--text', '#d5d9de'),
    dim: read('--text-2', '#a6adb5'),
    muted: read('--muted', '#7b828b'),
    line: read('--line', '#272b30'),
    panel: read('--panel', '#1b1e22'),
    bg: read('--bg', '#141619'),
    accent: read('--accent', '#7e9ab5'),
    ok: read('--ok', '#83a98c'),
    warn: read('--warn', '#c4a05e'),
    danger: read('--danger', '#c5827a'),
    mono: read('--mono', 'ui-monospace, monospace'),
  }
}

function axisCommon(theme) {
  return {
    axisLine: { lineStyle: { color: theme.line } },
    axisTick: { show: false },
    axisLabel: { color: theme.muted, fontSize: 10.5, fontFamily: theme.mono },
    splitLine: { lineStyle: { color: theme.line, type: 'dashed', opacity: 0.7 } },
  }
}

function applyAxis(axis, common) {
  if (!axis) return undefined
  const list = Array.isArray(axis) ? axis : [axis]
  const out = list.map((item) => ({
    ...common,
    ...item,
    axisLine: { ...common.axisLine, ...(item.axisLine || {}) },
    axisLabel: { ...common.axisLabel, ...(item.axisLabel || {}) },
    splitLine: { ...common.splitLine, ...(item.splitLine || {}) },
  }))
  return Array.isArray(axis) ? out : out[0]
}

/**
 * 把页面的 option 叠到主题基线上。
 *
 * 合并策略是「深一层」：`grid` / `tooltip` / 坐标轴各字段单独合，其余整体覆盖。
 * 不做递归深合并——ECharts 的 `series` 是数组，深合并会按索引把两个序列混在
 * 一起，症状是「图例多了一条不存在的数据」。
 */
export function buildOption(option, theme = chartTheme()) {
  const base = {
    // canvas 自带底色（同面板色）：哪怕浏览器合成层出幺蛾子把透明 canvas
    // 画成白底，深色画布也会把它盖掉——实测踩过的坑，别改回 transparent。
    backgroundColor: theme.panel,
    textStyle: { fontFamily: 'system-ui, sans-serif', color: theme.dim },
    animationDuration: 260,
    animationEasing: 'cubicOut',
    grid: { top: 26, right: 16, bottom: 24, left: 48, containLabel: false },
    tooltip: {
      backgroundColor: theme.panel,
      borderColor: theme.line,
      borderWidth: 1,
      padding: [7, 10],
      textStyle: { color: theme.text, fontSize: 11.5 },
      axisPointer: { lineStyle: { color: theme.muted, type: 'dashed' } },
    },
  }
  const merged = { ...base, ...option }
  merged.grid = { ...base.grid, ...(option.grid || {}) }
  merged.tooltip = { ...base.tooltip, ...(option.tooltip || {}) }
  merged.xAxis = applyAxis(option.xAxis, axisCommon(theme))
  merged.yAxis = applyAxis(option.yAxis, axisCommon(theme))
  return merged
}

/** 折线图的一条序列。默认 `smooth` + 细线 + 不显示散点。 */
export function lineSeries(name, points, color, opts = {}) {
  return {
    name,
    type: 'line',
    data: points,
    smooth: opts.smooth ?? 0.25,
    symbol: 'none',
    lineStyle: { width: opts.width ?? 1.6, color },
    itemStyle: { color },
    areaStyle: opts.area
      ? {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: hexA(color, 0.22) },
            { offset: 1, color: hexA(color, 0.01) },
          ],
        },
      }
      : undefined,
    markLine: opts.markLine,
    markArea: opts.markArea,
  }
}

/** `#rrggbb` → `rgba(r,g,b,a)`。ECharts 的渐变要 rgba，不吃 opacity。 */
export function hexA(hex, alpha) {
  const text = String(hex || '').trim()
  if (!text.startsWith('#')) return `rgba(126,154,181,${alpha})`
  const raw = text.length === 4
    ? text.slice(1).split('').map((ch) => ch + ch).join('')
    : text.slice(1)
  const num = parseInt(raw, 16)
  if (Number.isNaN(num)) return `rgba(126,154,181,${alpha})`
  const r = (num >> 16) & 255
  const g = (num >> 8) & 255
  const b = num & 255
  return `rgba(${r},${g},${b},${alpha})`
}
