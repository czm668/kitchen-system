<script setup>
/**
 * 指标卡。`value` 为空时显示破折号并标注「无样本」——**不用 0 冒充**。
 *
 * 卡片上「趋势/副标题」刻意只接一段文本而不是自己算：所有派生口径都该由后端
 * 给定（后端知道样本量、时间窗口），前端再算一遍就会出现「卡片说 12、表格说 13」。
 */
import Icon from './Icon.vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: null },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
  note: { type: String, default: '' },
  tone: { type: String, default: 'plain' }, // plain | ok | warn | danger | info
  icon: { type: String, default: '' },
  samples: { type: [Number, String], default: null },
  loading: { type: Boolean, default: false },
})

const shown = () => {
  if (props.value === null || props.value === undefined || props.value === '') return '—'
  return props.value
}
</script>

<template>
  <div class="stat" :class="tone">
    <div class="top">
      <span class="label">{{ label }}</span>
      <Icon v-if="icon" :name="icon" :size="14" class="mark" />
    </div>
    <div v-if="loading" class="skeleton value-skeleton"></div>
    <div v-else class="value num">
      {{ shown() }}<span v-if="unit && shown() !== '—'" class="unit">{{ unit }}</span>
    </div>
    <div class="bottom">
      <span v-if="hint" class="hint">{{ hint }}</span>
      <span v-if="note" class="note">{{ note }}</span>
      <span v-if="samples !== null" class="samples tiny muted">样本 {{ samples }}</span>
    </div>
  </div>
</template>

<style scoped>
.stat {
  background: var(--panel);
  border: 1px solid transparent;
  border-radius: var(--radius);
  /* 阴影与 .panel 同源（--shadow），浮起感和外层面板一致 */
  box-shadow: var(--shadow);
  padding: 13px 15px 11px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
  overflow: hidden;
}
:root[data-theme="dark"] .stat { border-color: var(--line); }
/* 左侧色条：一眼分辨「这格是不是出事了」，比只改数字颜色更容易扫到 */
.stat::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 2px;
  background: var(--line-strong);
}
.stat.ok::before { background: var(--ok); }
.stat.warn::before { background: var(--warn); }
.stat.danger::before { background: var(--danger); }
.stat.info::before { background: var(--accent); }

.top { display: flex; align-items: center; justify-content: space-between; }
.label { font-size: 11.5px; color: var(--muted); letter-spacing: 0.02em; }
.mark { color: var(--muted); }

.value {
  font-size: 27px;
  font-weight: 600;
  line-height: 1.15;
  letter-spacing: -0.02em;
}
.value-skeleton { height: 31px; width: 60%; }
.unit { font-size: 13px; font-weight: 400; color: var(--muted); margin-left: 3px; }

.stat.danger .value { color: var(--danger); }
.stat.warn .value { color: var(--warn); }

.bottom { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.hint { font-size: 11.5px; color: var(--text-2); }
.note { font-size: 11.5px; color: var(--muted); }
.samples { margin-left: auto; }
</style>
