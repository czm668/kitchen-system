<script setup>
/**
 * A7-02 订单与调度看板。
 *
 * ## 一个表格同时承担「看」和「操作」
 *
 * 后厨的场景是「边看边点」，把操作拆到详情弹窗会多两次点击、还容易点错单。
 * 所以每一行就是「这一单的当前状态 + 下一步能做的动作」，动作按钮由子项状态
 * 决定（`pending` 才能派单、`cooking` 才能出餐）。
 *
 * ## 为什么派单必须选工位而不是「自动派」
 *
 * 自动派单需要读资质、读工位忙闲、读排班——后端 `DispatchBoard` 都能算，但
 * 现场的真实情况是「A 灶正在收汁，B 灶刚腾出来」这类信息只有厨师知道。
 * 所以这里给人选，选完由后端做**硬校验**（资质不符、工位被占都会拒绝）。
 *
 * ## 为什么「退菜」要二次确认
 *
 * 退菜不可逆（`cancelled` 是终态），而且直接影响 KPI。其它动作都能被后续操作
 * 修正，只有它是单向的。
 */
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import StateBlock from '../components/StateBlock.vue'
import { can } from '../stores/auth'
import { toastError, toastOk, toastWarn } from '../composables/useToast'
import {
  ITEM_STATE, ORDER_STATE, describe, fmtCountdown, fmtDateTime, fmtTime,
} from '../utils/format'

const orders = ref([])
const dishes = ref([])
const stations = ref([])
const loading = ref(true)
const filter = ref('open')
const stationChoice = ref({})
const busyItem = ref(0)
const seeding = ref(false)

const cancelTarget = ref(null)

const writable = computed(() => can('order.write'))

const FILTERS = [
  { key: 'open', label: '未完成' },
  { key: 'all', label: '全部' },
  { key: 'cooking', label: '制作中' },
  { key: 'ready', label: '待出餐' },
]

// 来源渠道在界面上要能直接读懂，不把 web/miniapp/tft 这类内部值扔给操作员
const SOURCE_LABEL = {
  miniapp: '小程序点单', web: '管理端', tft: '后厨屏',
  customer: '顾客端', demo: '演示数据',
}
function sourceLabel(source) { return SOURCE_LABEL[source] || source || '-' }

const shown = computed(() => {
  if (filter.value === 'all') return orders.value
  if (filter.value === 'open') {
    return orders.value.filter((order) =>
      !['served', 'cancelled'].includes(order.state))
  }
  return orders.value.filter((order) =>
    order.items.some((item) => item.state === filter.value))
})

const idleStations = computed(() => stations.value.filter(
  (station) => station.state === 'idle'))

async function load() {
  loading.value = true
  try {
    const [orderData, dishData, stationData] = await Promise.all([
      api.get('/api/orders'),
      api.get('/api/dishes'),
      api.get('/api/stations'),
    ])
    orders.value = orderData.orders || []
    dishes.value = dishData.dishes || []
    stations.value = stationData.stations || []
    // 默认选中第一个空闲工位，少一次点击
    const fallback = idleStations.value[0]?.id
    if (fallback) {
      for (const order of orders.value) {
        for (const item of order.items) {
          if (!stationChoice.value[item.id]) stationChoice.value[item.id] = fallback
        }
      }
    }
  } catch (err) {
    toastError(err, '订单加载失败')
  } finally {
    loading.value = false
  }
}

async function dispatchItem(item) {
  const stationId = Number(stationChoice.value[item.id])
  if (!stationId) {
    toastWarn('请先选择工位')
    return
  }
  busyItem.value = item.id
  try {
    await api.post(`/api/orders/items/${item.id}/dispatch`, { station_id: stationId })
    toastOk(`已派到 ${stationById(stationId)}`)
    await load()
  } catch (err) {
    toastError(err, '派单失败')
  } finally {
    busyItem.value = 0
  }
}

async function urge(item) {
  busyItem.value = item.id
  try {
    await api.post(`/api/orders/items/${item.id}/urge`, {})
    toastWarn(`已催菜（第 ${item.urge_count + 1} 次），交期提前`)
    await load()
  } catch (err) {
    toastError(err, '催菜失败')
  } finally {
    busyItem.value = 0
  }
}

async function move(item, state) {
  busyItem.value = item.id
  try {
    await api.post(`/api/orders/items/${item.id}/state`, { state })
    toastOk(`子项 #${item.id} → ${describe(ITEM_STATE, state).label}`)
    await load()
  } catch (err) {
    toastError(err, '状态推进失败')
  } finally {
    busyItem.value = 0
  }
}

async function confirmCancel() {
  const item = cancelTarget.value
  if (!item) return
  busyItem.value = item.id
  try {
    await api.post(`/api/orders/items/${item.id}/state`, { state: 'cancelled' })
    toastOk(`子项 #${item.id} 已退菜`)
    cancelTarget.value = null
    await load()
  } catch (err) {
    toastError(err, '退菜失败')
  } finally {
    busyItem.value = 0
  }
}

async function toggleSoldOut(dish) {
  try {
    await api.post(`/api/dishes/${dish.id}/soldout`, { sold_out: !dish.sold_out })
    toastOk(`${dish.name} ${dish.sold_out ? '恢复供应' : '已沽清'}`)
    await load()
  } catch (err) {
    toastError(err, '沽清操作失败')
  }
}

async function seed() {
  seeding.value = true
  try {
    const data = await api.post('/api/demo/orders', {})
    toastOk(`已生成 ${data.created} 单`)
    await load()
  } catch (err) {
    toastError(err, '生成演示订单失败')
  } finally {
    seeding.value = false
  }
}

function stationById(id) {
  const found = stations.value.find((station) => station.id === id)
  return found ? `${found.code}（${found.name || found.kind}）` : `#${id}`
}

function nextActions(item) {
  const list = []
  if (item.state === 'pending') list.push('dispatch')
  if (item.state === 'dispatched') list.push('cooking')
  if (item.state === 'cooking') list.push('ready')
  if (item.state === 'ready') list.push('served')
  return list
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <div class="toolbar">
      <div class="seg">
        <button
          v-for="item in FILTERS" :key="item.key" type="button"
          class="seg-btn" :class="{ active: filter === item.key }"
          @click="filter = item.key"
        >{{ item.label }}</button>
      </div>
      <span class="spacer"></span>
      <span class="tiny muted">{{ idleStations.length }} 个工位空闲</span>
      <button class="btn sm" type="button" :disabled="seeding || !writable" @click="seed">
        <Icon name="play" :size="12" />{{ seeding ? '生成中…' : '生成演示订单' }}
      </button>
      <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
        <Icon name="refresh" :size="12" />刷新
      </button>
    </div>

    <div class="layout">
      <!-- 订单表 -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">订单流<span class="panel-sub"> · {{ shown.length }} 单</span></span>
          <span v-if="!writable" class="tag plain">只读</span>
        </div>
        <div class="panel-body tight">
          <StateBlock v-if="loading" />
          <div v-else-if="!shown.length" class="empty">
            没有符合条件的订单。点「生成演示订单」造几单，或从小程序下单。
          </div>
          <table aria-label="表格：订单、状态、子项、交期" v-else class="table cards">
            <thead>
              <tr>
                <th scope="col" style="width: 112px">订单</th>
                <th scope="col" style="width: 78px">状态</th>
                <th scope="col">子项</th>
                <th scope="col" style="width: 108px" class="num">交期</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="order in shown" :key="order.id">
                <td data-label="订单">
                  <div class="mono bold">{{ order.order_no }}</div>
                  <div class="tiny muted">
                    {{ fmtTime(order.created_at) }} · {{ sourceLabel(order.source) }}
                    <template v-if="order.customer_ref">
                      · {{ order.table_label }} · 顾客 {{ order.customer_ref }}
                    </template>
                  </div>
                </td>
                <td data-label="状态">
                  <span class="tag" :class="describe(ORDER_STATE, order.state).tone">
                    {{ describe(ORDER_STATE, order.state).label }}
                  </span>
                </td>
                <td data-label="子项">
                  <div v-for="item in order.items" :key="item.id" class="item-row">
                    <div class="item-main">
                      <span class="dish">{{ item.dish_name }}×{{ item.qty }}</span>
                      <span class="tag" :class="describe(ITEM_STATE, item.state).tone">
                        {{ describe(ITEM_STATE, item.state).label }}
                      </span>
                      <span v-if="item.station_id" class="tiny muted mono">
                        → {{ stationById(item.station_id) }}
                      </span>
                      <span v-if="item.urge_count" class="tag warn">
                        催 {{ item.urge_count }}
                      </span>
                    </div>
                    <div class="item-actions">
                      <template v-if="writable">
                        <template v-for="action in nextActions(item)" :key="action">
                          <select
                            v-if="action === 'dispatch'"
                            v-model="stationChoice[item.id]"
                            class="select mini"
                          >
                            <option
                              v-for="station in stations.filter((s) => s.state === 'idle' || s.id === item.station_id)"
                              :key="station.id" :value="station.id"
                            >{{ station.code }}</option>
                          </select>
                          <button
                            v-if="action === 'dispatch'" class="btn sm"
                            :disabled="busyItem === item.id"
                            @click="dispatchItem(item)"
                          >派单</button>
                          <button
                            v-else class="btn sm"
                            :disabled="busyItem === item.id"
                            @click="move(item, action)"
                          >
                            {{ { cooking: '上灶', ready: '出餐', served: '上桌' }[action] }}
                          </button>
                        </template>
                        <button
                          v-if="item.state !== 'served' && item.state !== 'cancelled'"
                          class="btn ghost sm" :disabled="busyItem === item.id"
                          @click="urge(item)"
                        >催菜</button>
                        <button
                          v-if="['pending', 'dispatched', 'cooking'].includes(item.state)"
                          class="btn ghost sm danger-text"
                          :disabled="busyItem === item.id"
                          @click="cancelTarget = item"
                        >退菜</button>
                      </template>
                    </div>
                  </div>
                </td>
                <td data-label="交期" class="num">
                  <div class="mono small">{{ fmtDateTime(order.due_at) }}</div>
                  <div class="tiny muted">{{ fmtCountdown(order.due_at) }}</div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 沽清 -->
      <section class="panel dishes">
        <div class="panel-head">
          <span class="panel-title">菜品与沽清</span>
          <span class="panel-sub">{{ dishes.filter((d) => d.sold_out).length }} 项沽清</span>
        </div>
        <div class="panel-body tight scroll">
          <table class="table" aria-label="表格：菜品与沽清状态">
            <tbody>
              <tr v-for="dish in dishes" :key="dish.id" :class="{ soldout: dish.sold_out }">
                <td>
                  <div class="bold small">{{ dish.name }}</div>
                  <div class="tiny muted mono">{{ dish.code }} · {{ dish.estimate_minutes }} 分钟</div>
                </td>
                <td class="num" style="width: 78px">
                  <button
                    class="btn sm" :class="dish.sold_out ? 'danger' : 'ghost'"
                    :disabled="!can('recipe.write')"
                    @click="toggleSoldOut(dish)"
                  >{{ dish.sold_out ? '恢复' : '沽清' }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="panel-foot tiny muted">
          沽清状态立即影响下单（`SoldOutError`），不需要重启。
        </div>
      </section>
    </div>

    <ConfirmDialog
      :open="!!cancelTarget"
      title="确认退菜"
      :target="cancelTarget ? `#${cancelTarget.id} ${cancelTarget.dish_name} ×${cancelTarget.qty}` : ''"
      description="退菜是终态，无法撤销。已经消耗的原料仍会计入成本。"
      warning="确属顾客取消或做废才退；只是「慢了」应该催菜。"
      :phrase="cancelTarget ? `退菜 #${cancelTarget.id}` : ''"
      confirm-text="确认退菜"
      :busy="busyItem === cancelTarget?.id"
      @confirm="confirmCancel"
      @cancel="cancelTarget = null"
    />
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.seg {
  display: inline-flex;
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
  transition: background 130ms ease, color 130ms ease;
}
.seg-btn:hover { color: var(--text); }
.seg-btn.active { background: var(--accent-soft); color: var(--accent-ink); font-weight: 600; }

/* 右栏 268px 装不下「菜名 + 编码 + 时长 + 沽清按钮」，字号上调后必须放量 */
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: var(--sp-4); align-items: start; }
@media (max-width: 1120px) { .layout { grid-template-columns: minmax(0, 1fr); } }

.item-row {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  padding: 4px 0;
}
.item-main { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: 0; }
.dish { font-weight: 500; }
.item-actions { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-wrap: wrap; justify-content: flex-end; }
.select.mini { height: 28px; width: 80px; padding: 0 6px; font-size: var(--fs-xs); }
.danger-text { color: var(--danger-ink); }

/* 沽清表不再框死 420px：字号上调后 6 个菜品会溢出，最后一行被裁一半。
   菜品总数有限，让它自然展开比固定高度更稳。 */
.dishes .panel-body { max-height: min(520px, 56vh); }
tr.soldout td { opacity: 0.55; }
tr.soldout .bold { text-decoration: line-through; }
</style>
