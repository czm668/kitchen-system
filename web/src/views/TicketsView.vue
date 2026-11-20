<script setup>
/**
 * A7-07 工单中心。
 *
 * ## 三类工单合流而不是分三个页签
 *
 * 现场运维关心的是「今天有几件事要处理」，不关心它来自传感器、消毒柜还是
 * 规则引擎。分页签会让「消毒返工」和「探头坏了」各自躺在不同页签里，交接班
 * 时漏掉一个。所以一张表，用 `source` 列溯源。
 *
 * ## 为什么消毒类工单不能在这里接单
 *
 * 消毒返工单的闭环是「重新消毒一次并达标」，跑一遍 `DisinfectionService` 才会
 * 自动关单。在页面上点「关单」等于跳过复消——批次照样出库，合规记录却是假的。
 * 所以后端对 `dis-*` / `rw-*` 直接拒绝，页面上也把按钮置灰并写明原因。
 *
 * ## 「超时」为什么单独一列而不是只改颜色
 *
 * 超时工单要触发升级（`due_escalations`）。只把行染红，接班的第二个人复制
 * 表格到聊天工具里就丢了信息。单列一栏 `超时` 能被筛、能被引用。
 */
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import StateBlock from '../components/StateBlock.vue'
import { can } from '../stores/auth'
import { toastError, toastOk } from '../composables/useToast'
import { TICKET_STATE, describe, fmtDuration } from '../utils/format'

const tickets = ref([])
const loading = ref(true)
const filter = ref('open')
const busy = ref('')
const closeTarget = ref(null)
const closeNote = ref('')

const SOURCE_LABEL = {
  sensor: '传感器故障',
  disinfection: '消毒',
  rule: '规则告警',
}
const TYPE_LABEL = {
  sensor_fault: '传感器故障',
  disinfection_rework: '复消',
  rework: '复消',
  unattended: '长时无人',
}
const LEVEL_LABEL = { 1: '提示', 2: '警告', 3: '严重' }

const writable = computed(() => can('ticket.write'))

const shown = computed(() => {
  if (filter.value === 'all') return tickets.value
  return tickets.value.filter((row) => row.state !== 'closed')
})

const openCount = computed(
  () => tickets.value.filter((row) => row.state !== 'closed').length)

const overdueCount = computed(
  () => tickets.value.filter((row) => row.overdue).length)

function closable(row) {
  // 字符串 id（`dis-1` / `rw-1`）由消毒域闭环，管理端不能直接关
  return typeof row.id === 'number'
}

async function load() {
  loading.value = true
  try {
    const data = await api.get('/api/tickets')
    tickets.value = data.tickets || []
  } catch (err) {
    toastError(err, '工单加载失败')
  } finally {
    loading.value = false
  }
}

async function ack(row) {
  busy.value = String(row.id)
  try {
    await api.post(`/api/tickets/${row.id}/ack`, {})
    toastOk(`工单 #${row.id} 已接单`)
    await load()
  } catch (err) {
    toastError(err, '接单失败')
  } finally {
    busy.value = ''
  }
}

async function confirmClose() {
  const row = closeTarget.value
  if (!row) return
  busy.value = String(row.id)
  try {
    await api.post(`/api/tickets/${row.id}/close`, { note: closeNote.value })
    toastOk(`工单 #${row.id} 已关闭`)
    closeTarget.value = null
    closeNote.value = ''
    await load()
  } catch (err) {
    toastError(err, '关单失败')
  } finally {
    busy.value = ''
  }
}

function openClose(row) {
  closeTarget.value = row
  closeNote.value = ''
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <div class="toolbar">
      <div class="seg">
        <button type="button" class="seg-btn" :class="{ active: filter === 'open' }"
                @click="filter = 'open'">待处理 {{ openCount }}</button>
        <button type="button" class="seg-btn" :class="{ active: filter === 'all' }"
                @click="filter = 'all'">全部 {{ tickets.length }}</button>
      </div>
      <span v-if="overdueCount" class="tag danger">
        <Icon name="alert" :size="11" />{{ overdueCount }} 张已超时，需升级
      </span>
      <span class="spacer"></span>
      <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
        <Icon name="refresh" :size="12" />刷新
      </button>
    </div>

    <section class="panel">
      <div class="panel-body tight">
        <StateBlock v-if="loading" />
        <div v-else-if="!shown.length" class="empty">
          没有待处理工单。传感器卡死/超量程/心跳丢失会自动开单；消毒不达标会开复消单。
        </div>
        <table aria-label="表格：来源、级别、内容、状态、已开" v-else class="table cards">
          <thead>
            <tr>
              <th scope="col" style="width: 106px">来源</th>
              <th scope="col" style="width: 64px">级别</th>
              <th scope="col">内容</th>
              <th scope="col" style="width: 98px">状态</th>
              <th scope="col" class="num" style="width: 104px">已开</th>
              <th scope="col" style="width: 176px"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in shown" :key="String(row.id)" :class="{ over: row.overdue }">
              <td data-label="来源">
                <div class="small">{{ SOURCE_LABEL[row.source] || row.source || '—' }}</div>
                <div class="tiny muted mono">{{ TYPE_LABEL[row.type] || row.type || '' }}</div>
              </td>
              <td data-label="级别">
                <span class="tag" :class="(row.level || 1) >= 2 ? 'danger' : 'warn'">
                  {{ LEVEL_LABEL[row.level] || `L${row.level || 1}` }}
                </span>
              </td>
              <td data-label="内容">
                <div class="pre-wrap">{{ row.message || row.detail || '—' }}</div>
                <div v-if="row.metric" class="tiny muted mono">指标 {{ row.metric }}</div>
                <div v-if="row.forbid_outbound" class="tiny" style="color: var(--danger)">
                  已阻止出库
                </div>
              </td>
              <td data-label="状态">
                <span class="tag" :class="describe(TICKET_STATE, row.state).tone">
                  {{ describe(TICKET_STATE, row.state).label }}
                </span>
                <div v-if="row.overdue" class="tiny" style="color: var(--danger)">超时</div>
              </td>
              <td data-label="已开" class="num mono small">{{ fmtDuration(row.age_sec) }}</td>
              <td class="num">
                <div class="row" style="justify-content: flex-end; gap: 5px">
                  <button
                    v-if="row.state === 'open' && closable(row)"
                    class="btn sm" :disabled="!writable || busy === String(row.id)"
                    @click="ack(row)"
                  >接单</button>
                  <button
                    v-if="closable(row) && row.state !== 'closed'"
                    class="btn ghost sm"
                    :disabled="!writable || busy === String(row.id)"
                    @click="openClose(row)"
                  >关单</button>
                  <span
                    v-if="!closable(row)"
                    class="tiny muted nowrap"
                    title="复消工单由消毒域闭环：重新消毒达标后自动关单"
                  >由消毒域闭环</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <p class="tiny muted">
      超时升级：`due_escalations()` 只回答「谁超时了」，由服务端调度器推送给组长；
      接单（acknowledged）后不再升级，避免「运维已经在修了还被一直捅上去」。
    </p>

    <ConfirmDialog
      :open="!!closeTarget"
      title="关闭工单"
      :target="closeTarget ? `#${closeTarget.id} ${closeTarget.message || ''}` : ''"
      description="关单表示现场已处置完毕。故障不会自动关单，因为「自动关了」会让记录里看不出这次故障发生过。"
      :phrase="closeTarget ? `关单 #${closeTarget.id}` : ''"
      confirm-text="确认关单"
      :busy="busy === String(closeTarget?.id)"
      @confirm="confirmClose"
      @cancel="closeTarget = null"
    >
      <template #extra>
        <div class="field">
          <label>处置说明（会写进工单备注）</label>
          <input v-model="closeNote" class="input" placeholder="例：更换 DS18B20 探头后恢复" />
        </div>
      </template>
    </ConfirmDialog>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.seg {
  display: inline-flex;
  flex-wrap: wrap;
  padding: 3px;
  border-radius: 8px;
  background: var(--panel);
  border: 1px solid var(--line);
  gap: 2px;
}
.seg-btn {
  border: none;
  background: none;
  color: var(--muted);
  padding: 5px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: var(--fs-xs);
  font-weight: 500;
  white-space: nowrap;
  transition: background 130ms ease, color 130ms ease;
}
.seg-btn:hover { color: var(--text); }
.seg-btn.active { background: var(--accent-soft); color: var(--accent-ink); font-weight: 600; }
/* 固定列宽：自动布局会让「内容」列抢走来源/状态的宽度，
   结果每个单元格都在折行，整张表被撑成一条条高栅栏。 */
.table { table-layout: fixed; }
.table td { overflow: hidden; text-overflow: ellipsis; }
tr.over td { background: var(--danger-soft); }
</style>
