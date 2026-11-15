<script setup>
/**
 * 图标：内置 path，不引图标库。
 *
 * 一个 12 个图标的界面引 `@element-plus/icons-vue` 要多 200KB，而这里只需要
 * 24×24 的线性图标。`stroke="currentColor"` 让图标自动跟随文字颜色，主题切换
 * 不用改任何一条 path。
 */
const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 16 },
})

const PATHS = {
  // 品牌图标：锅 + 蒸汽，纯线性黑白灰，不携帮任何主题色
  brand: 'M4 12h16M5 12v2.5A5.5 5.5 0 0 0 10.5 20h3A5.5 5.5 0 0 0 19 14.5V12M8 8c0-1.8 1-1.8 1-4M12 8c0-1.8 1-1.8 1-4M16 8c0-1.8 1-1.8 1-4',
  gauge: 'M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0-10a9 9 0 1 0 9 9M12 4v4m6.4-1.6L15 9.8',
  orders: 'M4 5h16M4 12h10M4 19h7M17 14l3 3-3 3',
  gantt: 'M4 6h9M4 12h14M4 18h6M3 4v16',
  env: 'M12 3v10m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM6 7a4 4 0 0 0 0 8M18 7a4 4 0 0 1 0 8',
  recipe: 'M6 3v8a3 3 0 0 0 6 0V3M9 11v10M15 3c-1 3-1 6 0 8h3V3',
  shield: 'M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6l7-3ZM9 12l2 2 4-4',
  ticket: 'M4 8a2 2 0 0 0 2-2h12a2 2 0 0 0 2 2v3a2 2 0 0 0 0 4v3H6a2 2 0 0 0-2 2V8Zm5 0v8',
  chart: 'M4 20V10m5 10V4m5 16v-7m5 7V8',
  sliders: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4',
  logout: 'M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 16l-4-4 4-4M6 12h9',
  sun: 'M12 4V2m0 20v-2m8-8h2M2 12h2m13.7-5.7 1.4-1.4M4.9 19.1l1.4-1.4m11.4 0 1.4 1.4M4.9 4.9l1.4 1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z',
  moon: 'M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z',
  alert: 'M12 9v4m0 3h.01M10.3 4.3 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0Z',
  check: 'M4 13l5 5L20 7',
  close: 'M6 6l12 12M18 6 6 18',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6',
  play: 'M8 5l11 7-11 7V5Z',
  ban: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM6 6l12 12',
  link: 'M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1',
  chat: 'M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v9ZM8 9h8M8 13h5',
  // 三通道专用：燃气阀 / 排风 / 工位电，点错了就是关掉正在炒菜的灶
  flame: 'M12 3c3 4 5 5.5 5 9a5 5 0 0 1-10 0c0-2 1-3 2-4 .5 2 1.5 2.5 2 2.5 0-3-1-5 1-7.5Z',
  fan: 'M12 12a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 0c0-4 2-7 5-7s2 5-2 6m-3 1c-4 0-7 2-7 5s5 2 6-2m1-3c4 0 7 2 7 5s-5 2-6-2m-1-3c0 4-2 7-5 7s-2-5 2-6',
  plug: 'M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0V8Zm6 9v4',
}
</script>

<template>
  <svg
    :width="size" :height="size" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true"
  >
    <path :d="PATHS[props.name] || PATHS.gauge" />
  </svg>
</template>
