<script setup>
/**
 * 页面分包加载失败的兜底。
 *
 * 这个组件存在的理由很具体：视图改成按需加载后，**首次访问某个页面就要联网
 * 拿一个 chunk**。如果那一刻断网（现场 wifi 掉线是常态），Vue 的默认行为是
 * 控制台报错 + 整块空白，用户看到的是「点了菜单什么都没发生」——比报错更糟。
 *
 * 这里给三件东西：发生了什么、别处还能不能用（侧栏还在，切回去就行）、
 * 一个「重试」按钮（重新执行 import，不需要刷新整页丢掉登录态）。
 *
 * 注：Vue 的 `errorComponent` 只传 `error`，不传 `retry`，所以重试靠自己
 * 重新挂载——`reload` 用自定义事件抛给外层是做不到的（外层是 Vue 内部渲染），
 * 所以退化为整页刷新。整页刷新会重放登录态（token 在内存里，刷新后要重新
 * 登录），因此文案里必须先说「切到别的页面不用刷新」。
 */
defineProps({
  error: { type: [Error, Object, String], default: null },
})

function retry() {
  window.location.reload()
}
</script>

<template>
  <div class="view-error" role="alert">
    <div class="we-title">这一页没能载入</div>
    <p class="we-text">
      页面文件没有下载成功，通常是网络中断。左侧菜单仍然可用，切换到其他页面不需要刷新；
      也可以点下面的按钮重新加载（需要重新登录）。
    </p>
    <!-- 原始报错是英文的 "Failed to fetch dynamically imported module: http://…"。
         直接摆在正文里像系统崩了；折起来，运维要看时能展开。 -->
    <details v-if="error" class="we-detail">
      <summary>技术细节（给维护人员）</summary>
      <p class="mono">{{ error.message || error }}</p>
    </details>
    <button type="button" class="btn" @click="retry">重新加载</button>
  </div>
</template>

<style scoped>
.view-error {
  max-width: 520px;
  padding: 22px 24px;
  border-radius: var(--radius);
  border: 1px solid var(--line);
  background: var(--panel);
  box-shadow: var(--shadow);
}
.we-title { font-size: var(--fs-md); font-weight: 600; margin-bottom: 8px; }
.we-text { margin: 0 0 12px; font-size: var(--fs-sm); color: var(--text-2); line-height: 1.65; }
.we-detail {
  margin: 0 0 14px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  color: var(--muted);
  font-size: var(--fs-xs);
}
.we-detail summary { cursor: pointer; color: var(--text-2); }
.we-detail p { margin: 6px 0 0; word-break: break-all; line-height: 1.5; }
</style>
