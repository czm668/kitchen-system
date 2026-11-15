<script setup>
import { toasts, dismiss } from '../composables/useToast'
import Icon from './Icon.vue'

const ICONS = { ok: 'check', warn: 'alert', danger: 'alert', info: 'link' }
</script>

<template>
  <Teleport to="body">
    <div class="toast-host">
      <TransitionGroup name="toast">
        <div v-for="item in toasts" :key="item.id" class="toast" :class="item.tone">
          <Icon :name="ICONS[item.tone] || 'link'" :size="14" />
          <span class="msg">{{ item.message }}</span>
          <button class="close" type="button" @click="dismiss(item.id)">
            <Icon name="close" :size="12" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-host {
  position: fixed;
  right: 18px;
  bottom: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 95;
  pointer-events: none;
}
.toast {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 220px;
  max-width: 380px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line-strong);
  background: var(--panel);
  box-shadow: var(--shadow);
  font-size: 12.5px;
  pointer-events: auto;
}
.toast.ok { border-color: var(--ok); color: var(--ok); }
.toast.warn { border-color: var(--warn); color: var(--warn); }
.toast.danger { border-color: var(--danger); color: var(--danger); }
.toast.info { border-color: var(--accent); color: var(--accent); }
.msg { color: var(--text); flex: 1; line-height: 1.5; }
.close {
  border: none;
  background: none;
  color: var(--muted);
  cursor: pointer;
  padding: 1px;
  display: flex;
}
.close:hover { color: var(--text); }

.toast-enter-active, .toast-leave-active { transition: all 200ms ease; }
.toast-enter-from { opacity: 0; transform: translateY(8px) scale(0.98); }
.toast-leave-to { opacity: 0; transform: translateX(20px); }
</style>
