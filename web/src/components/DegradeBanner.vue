<script setup>
/**
 * 降级横幅：**整站唯一一处**说「什么能力没了」的地方。
 *
 * 后端 `build_degrade()` 已经把三端文案、受影响能力、严重度都算好了，这里只
 * 负责渲染。页面上不允许再各自判断「视觉离线了所以……」——那会造出三种说法。
 *
 * 交互：默认折叠成一行（不挡内容），点开看逐条能力。`offline` 时不可折叠，
 * 因为是全站失效，必须一直可见。
 */
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import { brokenCapabilities, degrade, isDegraded } from '../stores/live'

const open = ref(false)

const severity = computed(() => degrade.value?.severity || 'normal')
const headline = computed(() => degrade.value?.headline || '')
const forcedOpen = computed(() => severity.value === 'offline')

const tone = computed(() => {
  if (severity.value === 'offline') return 'danger'
  if (severity.value === 'degraded') return 'warn'
  return 'ok'
})

const webText = computed(() => degrade.value?.channels?.web || {})
const pending = computed(() => degrade.value?.queue_pending || 0)
const broken = computed(() => brokenCapabilities.value)
</script>

<template>
  <div v-if="isDegraded" class="banner" :class="tone">
    <button class="head" type="button" @click="open = !open" :disabled="forcedOpen">
      <Icon :name="severity === 'offline' ? 'ban' : 'alert'" :size="15" />
      <span class="headline">{{ headline }}</span>
      <span v-if="pending" class="pill">待补传 {{ pending }} 条</span>
      <span class="spacer"></span>
      <span v-if="!forcedOpen" class="toggle tiny muted">
        {{ open ? '收起' : `查看 ${broken.length} 项受影响能力` }}
      </span>
    </button>
    <div v-if="open || forcedOpen" class="detail">
      <p v-if="webText.detail" class="web-line">{{ webText.detail }}</p>
      <ul class="caps">
        <li v-for="cap in broken" :key="cap.key">
          <span class="tag" :class="cap.state === 'off' ? 'danger' : 'warn'">
            {{ cap.state === 'off' ? '不可用' : '降级' }}
          </span>
          <strong>{{ cap.label }}</strong>
          <span class="muted">{{ cap.reason }}</span>
        </li>
      </ul>
      <p class="hint tiny muted">
        本地保护（燃气/超温/长时无人）不依赖网络，仍然生效；数据会在恢复后自动补传。
      </p>
    </div>
  </div>
</template>

<style scoped>
.banner {
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.banner.warn { background: var(--warn-soft); border-bottom-color: var(--warn); }
.banner.danger { background: var(--danger-soft); border-bottom-color: var(--danger); }

.head {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 9px 18px;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: 12.5px;
}
.head[disabled] { cursor: default; }
.banner.warn .head { color: var(--warn); }
.banner.danger .head { color: var(--danger); }

.headline { font-weight: 600; }

.pill {
  padding: 1px 7px;
  border-radius: 20px;
  border: 1px solid currentColor;
  font-size: 10.5px;
  opacity: 0.85;
}

.detail {
  padding: 0 18px 13px 42px;
  font-size: 12px;
  color: var(--text-2);
}
.web-line { margin: 0 0 9px; }

.caps { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
.caps li { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.caps strong { font-weight: 600; color: var(--text); }

.hint { margin: 10px 0 0; }
</style>
