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
 *
 * ## 键盘行为
 *
 * 打开 → 焦点落在输入框（无需输入时落在确认按钮）；`Esc` 取消；`Tab` 只在
 * 弹窗内循环；关闭 → 焦点回到触发它的那个按钮。少了这一段，键盘用户打开
 * 弹窗后焦点还留在背后的页面上，回车可能触发页面上的另一个危险动作。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
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
const dialogRef = ref(null)
const inputRef = ref(null)
let lastFocused = null

const ready = computed(() => {
  if (!props.phrase) return true
  return typed.value.trim() === props.phrase
})

/** 焦点锁在弹窗内，Tab / Shift+Tab 在首尾之间循环 */
function onKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    cancel()
    return
  }
  if (event.key !== 'Tab' || !dialogRef.value) return
  const nodes = Array.from(
    dialogRef.value.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])'),
  ).filter((node) => !node.disabled)
  if (!nodes.length) return
  const first = nodes[0]
  const last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(() => props.open, async (open) => {
  if (open) {
    typed.value = ''
    lastFocused = document.activeElement
    await nextTick()
    if (props.phrase && inputRef.value) inputRef.value.focus()
    else dialogRef.value?.querySelector('[data-confirm]')?.focus()
    document.addEventListener('keydown', onKeydown)
  } else {
    document.removeEventListener('keydown', onKeydown)
    // 焦点归还触发按钮：读屏用户回到「我刚才在哪儿」
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus()
    lastFocused = null
  }
})

onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown))

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
        <div
          ref="dialogRef"
          class="dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-title"
          :aria-describedby="description ? 'confirm-desc' : undefined"
        >
          <header class="head" :class="{ danger }">
            <span class="head-icon">
              <Icon :name="danger ? 'alert' : 'check'" :size="17" />
            </span>
            <h3 id="confirm-title">{{ title }}</h3>
            <span class="spacer"></span>
            <button class="btn ghost sm" type="button" @click="cancel" :disabled="busy" aria-label="关闭">
              <Icon name="close" :size="14" />
            </button>
          </header>

          <div class="body">
            <p v-if="target" class="target mono">{{ target }}</p>
            <p v-if="description" id="confirm-desc" class="desc">{{ description }}</p>
            <p v-if="warning" class="warn-line">{{ warning }}</p>

            <div v-if="phrase" class="field">
              <label for="confirm-phrase">
                请输入 <code>{{ phrase }}</code> 以确认
              </label>
              <input
                id="confirm-phrase"
                ref="inputRef"
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
              data-confirm
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
  background: rgba(1, 4, 9, 0.55);
  backdrop-filter: blur(3px);
  /* 弹层里的滚动不该把背后的页面一起带着滚（触屏上尤其明显：
     在弹层里滑到底，页面会跟着滑走，关掉弹层时位置已经变了） */
  overscroll-behavior: contain;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 90;
  padding: 20px;
}
.dialog {
  width: min(460px, 100%);
  background: var(--panel);
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
:root[data-theme="dark"] .dialog { border-color: var(--line-strong); }
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 15px 18px;
  border-bottom: 1px solid var(--line);
}
.head-icon {
  width: 30px; height: 30px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  flex: none;
  background: var(--panel-2);
  color: var(--text-2);
}
.head.danger .head-icon { background: var(--danger-soft); color: var(--danger-ink); }
.head h3 { color: var(--text); font-size: var(--fs-md); }

.body { padding: 18px; display: flex; flex-direction: column; gap: 12px; }
.target {
  margin: 0;
  padding: 9px 12px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  color: var(--text-2);
  word-break: break-all;
}
.desc { margin: 0; font-size: var(--fs-sm); color: var(--text-2); line-height: 1.65; }
.warn-line {
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger-ink);
  font-size: var(--fs-sm);
  line-height: 1.6;
  border: 1px solid var(--danger);
}
.field label code {
  font-family: var(--mono);
  background: var(--panel-2);
  padding: 2px 6px;
  border-radius: 4px;
  color: var(--accent-ink);
  font-size: var(--fs-xs);
}

.foot {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  padding: 14px 18px;
  border-top: 1px solid var(--line);
  background: var(--panel-2);
}
</style>
