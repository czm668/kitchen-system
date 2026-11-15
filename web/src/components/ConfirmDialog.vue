<script setup>
/**
 * 危险动作二次确认。
 *
 * ## 为什么是「输入动作名」而不是「点确定」
 *
 * 「点确定」只挡住手滑，挡不住「看错行」：三通道按钮排在一起，点错一格就是
 * 关掉正在炒菜的灶。要求把动作名**打一遍**，等于强制操作者读一遍自己要做
 * 什么——现场测试里这一步能把误操作降到基本为零。
 *
 * ## 为什么确认词由后端给
 *
 * 前端写死一份动作名字符串，后端改名后两边就不一致（确认了却调不动）。所以
 * 确认词从 `/api/meta` 的 `actions` 里取，就是服务端登记的那个 `action`。
 *
 * ## 为什么还要传 `manual_confirmed`
 *
 * 这个弹窗只是 UI 层的防误点，**真正的判据在服务端**：`InterlockController`
 * 收到 `manual_confirmed=false` 的恢复动作会直接抛 `InterlockBlocked`。
 * 有人绕过页面直接调接口时，服务端仍在把关。
 */
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '确认操作' },
  target: { type: String, default: '' },
  /** 需要原样输入的确认词，留空则只需要点确定 */
  phrase: { type: String, default: '' },
  description: { type: String, default: '' },
  warning: { type: String, default: '' },
  confirmText: { type: String, default: '确认执行' },
  danger: { type: Boolean, default: true },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'cancel'])

const typed = ref('')

watch(() => props.open, (open) => {
  if (open) typed.value = ''
})

const ready = computed(() => {
  if (!props.phrase) return true
  return typed.value.trim() === props.phrase
})

function confirm() {
  if (!ready.value || props.busy) return
  emit('confirm', { typed: typed.value.trim() })
}

function cancel() {
  if (props.busy) return
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="open" class="mask" @click.self="cancel">
        <div class="dialog" role="dialog" aria-modal="true">
          <header class="head">
            <Icon :name="danger ? 'alert' : 'check'" :size="16" />
            <h3>{{ title }}</h3>
            <span class="spacer"></span>
            <button class="btn ghost sm" type="button" @click="cancel" :disabled="busy">
              <Icon name="close" :size="13" />
            </button>
          </header>

          <div class="body">
            <p v-if="target" class="target mono">{{ target }}</p>
            <p v-if="description" class="desc">{{ description }}</p>
            <p v-if="warning" class="warn-line">{{ warning }}</p>

            <div v-if="phrase" class="field">
              <label>请输入 <code>{{ phrase }}</code> 以确认</label>
              <input
                v-model="typed"
                class="input mono"
                :placeholder="phrase"
                autocomplete="off"
                spellcheck="false"
                @keydown.enter="confirm"
              />
            </div>
          </div>

          <footer class="foot">
            <button class="btn" type="button" @click="cancel" :disabled="busy">取消</button>
            <button
              class="btn"
              :class="danger ? 'danger' : 'primary'"
              type="button"
              :disabled="!ready || busy"
              @click="confirm"
            >
              {{ busy ? '执行中…' : confirmText }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  background: rgba(1, 4, 9, 0.62);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 90;
  padding: 20px;
}
.dialog {
  width: min(440px, 100%);
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  box-shadow: var(--shadow);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 15px;
  border-bottom: 1px solid var(--line);
  color: var(--danger);
}
.head h3 { color: var(--text); font-size: 13.5px; }

.body { padding: 15px; display: flex; flex-direction: column; gap: 10px; }
.target {
  margin: 0;
  padding: 7px 10px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-size: 12px;
  word-break: break-all;
}
.desc { margin: 0; font-size: 12.5px; color: var(--text-2); }
.warn-line {
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
  border: 1px solid var(--danger);
}
.field label code {
  font-family: var(--mono);
  background: var(--panel-2);
  padding: 1px 5px;
  border-radius: 4px;
  color: var(--accent);
}

.foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 11px 15px;
  border-top: 1px solid var(--line);
  background: var(--panel-2);
}
</style>
