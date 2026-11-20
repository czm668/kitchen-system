<script setup>
import { toasts, dismiss } from '../composables/useToast'
import Icon from './Icon.vue'

const ICONS = { ok: 'check', warn: 'alert', danger: 'alert', info: 'link' }
</script>

<template>
  <Teleport to="body">
    <!-- aria-live：读屏用户不该靠「看见右下角闪了一下」才知道操作失败了 -->
    <div class="toast-host" role="status" aria-live="polite">
      <TransitionGroup name="toast">
        <div
          v-for="item in toasts" :key="item.id" class="toast" :class="item.tone"
          :role="item.tone === 'danger' ? 'alert' : undefined"
        >
          <Icon :name="ICONS[item.tone] || 'link'" :size="15" class="ico" />
          <span class="msg">{{ item.message }}</span>
          <button class="close" type="button" @click="dismiss(item.id)" aria-label="关闭提示">
            <Icon name="close" :size="13" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-host {
  position: fixed;
  right: 20px;
  bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 9px;
  z-index: 95;
  pointer-events: none;
}
/* 左侧 3px 色条 + 中性卡片底：一排三条 toast 同时出现时，只有色条在竞争注意力 */
.toast {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 240px;
  max-width: 400px;
  padding: 11px 12px 11px 13px;
  border-radius: 10px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--line-strong);
  background: var(--panel);
  box-shadow: var(--shadow-lg);
  font-size: var(--fs-sm);
  pointer-events: auto;
}
.toast.ok { border-left-color: var(--ok); }
.toast.warn { border-left-color: var(--warn); }
.toast.danger { border-left-color: var(--danger); }
.toast.info { border-left-color: var(--accent); }
.ico { flex: none; margin-top: 1px; }
.toast.ok .ico { color: var(--ok-ink); }
.toast.warn .ico { color: var(--warn-ink); }
.toast.danger .ico { color: var(--danger-ink); }
.toast.info .ico { color: var(--info-ink); }
.msg { color: var(--text); flex: 1; line-height: 1.55; }
.close {
  border: none;
  background: none;
  color: var(--muted);
  cursor: pointer;
  padding: 2px;
  display: flex;
  border-radius: 4px;
  flex: none;
}
.close:hover { color: var(--text); background: var(--panel-2); }

/* 显式列出属性，不写 `transition: all`：all 会把后续任何一次样式改动（哪怕
   是无关的 color）都挂上 220ms 的过渡，调试时很难判断"为什么这里也在动" */
.toast-enter-active, .toast-leave-active {
  transition: opacity 220ms cubic-bezier(0.22, 0.61, 0.36, 1),
              transform 220ms cubic-bezier(0.22, 0.61, 0.36, 1);
}
.toast-enter-from { opacity: 0; transform: translateY(10px) scale(0.97); }
.toast-leave-to { opacity: 0; transform: translateX(24px); }
</style>
