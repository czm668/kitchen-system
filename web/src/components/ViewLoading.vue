<script setup>
/**
 * 页面分包加载中的占位。
 *
 * 为什么不是转圈：切页时用户已经知道自己点了哪个菜单，需要的不是「在转」，
 * 而是「内容马上填到这里」。所以用与页面同构的灰块（标题行 + 两列卡片 +
 * 一块大面板），骨架出现在最终内容的位置上，内容到达时不会有跳动。
 *
 * **只在动态 import 的 chunk 还没到的那几百毫秒出现**（`:delay="120"`），
 * 网络快时用户根本看不到这一屏。
 */
</script>

<template>
  <div class="view-skel" aria-busy="true">
    <span class="sr-only">正在载入页面…</span>
    <div class="skel-head" aria-hidden="true">
      <span class="bar w-30"></span>
      <span class="bar w-18 dim"></span>
    </div>
    <div class="skel-grid" aria-hidden="true">
      <span class="blk"></span>
      <span class="blk"></span>
      <span class="blk"></span>
      <span class="blk"></span>
    </div>
    <div class="skel-panel" aria-hidden="true"></div>
  </div>
</template>

<style scoped>
.view-skel { display: flex; flex-direction: column; gap: 14px; }

.skel-head { display: flex; align-items: center; gap: 10px; }
.bar { height: 12px; border-radius: 6px; background: var(--panel-2); }
.bar.dim { opacity: 0.6; height: 10px; }
.w-30 { width: 30%; min-width: 120px; }
.w-18 { width: 18%; min-width: 80px; }

.skel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.blk {
  height: 84px;
  border-radius: var(--radius);
  background: var(--panel);
  border: 1px solid var(--line);
}
.skel-panel {
  height: 260px;
  border-radius: var(--radius);
  background: var(--panel);
  border: 1px solid var(--line);
}

/* 骨架的呼吸感只在允许动效时给；reduced-motion 下 theme.css 会统一关掉动画 */
.blk, .skel-panel, .bar { animation: skel-in 160ms ease-out both; }
@keyframes skel-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@media (max-width: 720px) {
  .skel-panel { height: 200px; }
}
</style>
