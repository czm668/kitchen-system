<script setup>
/**
 * 加载 / 空 / 错误三态的统一容器。
 *
 * ## 为什么要有这个组件
 *
 * 改造前同一个后台里并存三种「正在加载」：
 * · 骨架屏（合规凭证、远程控制）—— 有形状，最好
 * · 一行灰字「加载中…」（批次、订单、工单、配方、客诉五处）
 * · 一个转圈（启动屏）
 *
 * 对用户来说这是三种不同的东西，但其实表达的是同一件事。更麻烦的是那五处
 * 灰字在 1440 屏上只占一行，下方是整片空白——看起来像页面坏了，而不是在加载。
 *
 * ## 设计取舍：形状化骨架优先于通用骨架
 *
 * 通用骨架（几根等宽灰条）只在「不知道内容长什么样」时才是对的。这里大多数
 * 页面都知道：合规凭证是三块（结论/网格/日报），控制页是两块面板。让骨架
 * 长得像真正要来的内容，视觉跳变小得多。所以本组件留了 `#skeleton` 插槽，
 * 调用方可传入自己形状的骨架；不传时才退回默认的三行灰条。
 *
 * ## 无障碍
 *
 * loading 态下容器是 `role="status" aria-live="polite"`，内含一段 `.sr-only`
 * 文本。这解决了一个容易被忽略的问题：屏幕上有一堆灰条，读屏器进入这块区域
 * 却什么都读不到，用户会以为页面是空的。
 */
import { computed, useSlots } from 'vue'

const props = defineProps({
  /** loading | empty | error */
  state: { type: String, default: 'loading' },
  /** 一句话说明现状，如「正在汇总当日合规事实」「还没有完成出餐的订单」 */
  title: { type: String, default: '' },
  /** 补充说明：为什么空、空的时候能做什么 */
  hint: { type: String, default: '' },
  /** 默认骨架的行数 */
  rows: { type: Number, default: 3 },
  /** 紧凑模式：用在卡片内部或窄栏，减少上下留白 */
  compact: { type: Boolean, default: false },
})

const slots = useSlots()
const SKEL_WIDTHS = ['100%', '88%', '72%', '94%', '64%']

const skelWidth = (i) => SKEL_WIDTHS[(i - 1) % SKEL_WIDTHS.length]
const hasCustomSkeleton = computed(() => !!slots.skeleton)
</script>

<template>
  <div
    class="state-block"
    :class="[state, { compact, 'has-custom': hasCustomSkeleton }]"
    :role="state === 'loading' ? 'status' : undefined"
    :aria-live="state === 'loading' ? 'polite' : undefined"
    :aria-busy="state === 'loading' ? 'true' : undefined"
  >
    <template v-if="state === 'loading'">
      <slot name="skeleton">
        <div class="state-skel">
          <span
            v-for="i in rows"
            :key="i"
            class="skeleton state-skel-bar"
            :style="{ width: skelWidth(i) }"
          ></span>
        </div>
      </slot>
      <span class="sr-only">{{ title || '正在加载' }}</span>
    </template>

    <template v-else>
      <span class="state-mark" aria-hidden="true">{{ state === 'error' ? '!' : '—' }}</span>
      <p v-if="title" class="state-title">{{ title }}</p>
      <p v-if="hint" class="state-hint">{{ hint }}</p>
      <slot />
    </template>
  </div>
</template>

<style scoped>
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: 40px var(--sp-4);
  text-align: center;
  color: var(--muted);
  font-size: var(--fs-sm);
  line-height: 1.65;
}
.state-block.compact { padding: 20px var(--sp-3); }

/* 自定义骨架由调用方给形状，容器不再加内边距，避免套两层留白 */
.state-block.has-custom { padding: 0; align-items: stretch; }

.state-skel {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
}
.state-skel-bar { height: 14px; display: block; }
.state-skel-bar:first-child { height: 20px; }

.state-mark {
  font-size: 18px;
  line-height: 1;
  opacity: 0.45;
  margin-bottom: 2px;
}
.state-title {
  margin: 0;
  color: var(--text-2);
  font-weight: 550;
  font-size: var(--fs-base);
}
.state-hint { margin: 0; max-width: 46ch; }

/* 错误态：标记换成红色，但不用整块红底——错误提示条已经承担了强提醒，
   这里再铺一层红会把整页气氛压死 */
.state-block.error .state-mark { color: var(--danger); opacity: 1; font-weight: 700; }
.state-block.error .state-title { color: var(--danger-ink); }
</style>
