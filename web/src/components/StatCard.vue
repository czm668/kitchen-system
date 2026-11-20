<script setup>
/**
 * 指标卡。`value` 为空时显示破折号并标注「无样本」——**不用 0 冒充**。
 *
 * 卡片上「趋势/副标题」刻意只接一段文本而不是自己算：所有派生口径都该由后端
 * 给定（后端知道样本量、时间窗口），前端再算一遍就会出现「卡片说 12、表格说 13」。
 *
 * ## 视觉上的两个取舍
 *
 * 1. **数字放大到 34px**：KPI 卡的全部价值就是那个数字，其余都是注解。数字与
 *    标签的字号比从 27:11.5 拉到 34:12.5，一眼先看到数、再看到它是什么。
 * 2. **值是文字时自动降字号**：「未接入」「无样本」这类词按 34px 排会撑破卡片，
 *    并且它们表达的是「这里没有数」，视觉重量本就不该和真实读数一样。
 */
import { computed } from 'vue'
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

/** 纯数字才用大号读数；「未接入」这类文字降一档。 */
const isNumeric = computed(() => {
  const v = props.value
  if (v === null || v === undefined || v === '') return true
  return !Number.isNaN(Number(v))
})
</script>

<template>
  <div class="stat" :class="tone">
    <div class="top">
      <span class="label">{{ label }}</span>
      <Icon v-if="icon" :name="icon" :size="15" class="mark" />
    </div>
    <div v-if="loading" class="skeleton value-skeleton"></div>
    <div v-else class="value num" :class="{ text: !isNumeric }">
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
  border: 1px solid rgba(0, 0, 0, 0.05);
  border-radius: var(--radius);
  /* 阴影与 .panel 同源（--shadow），浮起感和外层面板一致 */
  box-shadow: var(--shadow);
  padding: 15px 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
  overflow: hidden;
  transition: box-shadow 160ms ease, border-color 160ms ease;
}
:root[data-theme="dark"] .stat { border-color: var(--line); }
.stat:hover { box-shadow: var(--shadow-lg); }

/* 左侧色条：一眼分辨「这格是不是出事了」，比只改数字颜色更容易扫到 */
.stat::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 3px;
  background: var(--line-strong);
  opacity: 0.7;
}
.stat.ok::before { background: var(--ok); opacity: 1; }
.stat.warn::before { background: var(--warn); opacity: 1; }
.stat.danger::before { background: var(--danger); opacity: 1; }
.stat.info::before { background: var(--accent); opacity: 1; }

.top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.label {
  font-size: var(--fs-xs);
  color: var(--muted);
  letter-spacing: 0.01em;
  font-weight: 500;
  line-height: 1.4;
}
.mark { color: var(--muted); opacity: 0.75; flex: none; }

.value {
  font-size: 34px;
  font-weight: 640;
  line-height: 1.1;
  letter-spacing: -0.035em;
  margin-top: 2px;
}
.value.text { font-size: 21px; letter-spacing: -0.01em; font-weight: 600; }
.value-skeleton { height: 38px; width: 58%; }
.unit {
  font-size: var(--fs-sm);
  font-weight: 450;
  color: var(--muted);
  margin-left: 4px;
  letter-spacing: 0;
}

.stat.danger .value { color: var(--danger-ink); }
.stat.warn .value { color: var(--warn-ink); }

.bottom { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-top: auto; }
.hint { font-size: var(--fs-xs); color: var(--text-2); }
.note { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; }
.samples { margin-left: auto; }
</style>
