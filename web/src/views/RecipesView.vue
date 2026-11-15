<script setup>
/**
 * A7-05 配方与工艺维护（版本化）。
 *
 * ## 为什么只能「另存为新版本」不能「原地改」
 *
 * 下单那一刻会把 `recipe_id` 快照到子项上。原地改配方会让「正在做的单」和
 * 「刚下的单」按两套重量判据走：昨天的单说 150g、今天的单说 180g，两个都判
 * 不合格，而现场完全看不出为什么。版本化之后旧单继续用旧版本，新单用新版本。
 *
 * ## 编辑能力（全部可自定义，保存即生成新版本）
 *
 * 配料名/克重/容差、工序名/说明/火候/时长、CCP 与需刷卡标记都开放编辑，
 * 配料行与工序行可增删可排序；无配方的菜直接录入 v1，「新增菜品」按钮
 * 建菜后同样直录 v1。批次快照机制保证了自由度不会伤害在做的订单：
 * 已开的单继续按旧版本称重，怎么改都不影响现场。
 */
import { computed, onMounted, ref } from 'vue'
import Icon from '../components/Icon.vue'
import { api } from '../api/client'
import { can } from '../stores/auth'
import { toastError, toastOk } from '../composables/useToast'
import { fmt } from '../utils/format'

const recipes = ref([])
const selectedDish = ref(null)
const loading = ref(true)
const saving = ref(false)
const draft = ref(null)

const writable = computed(() => can('recipe.write'))

const HEAT_LABEL = { none: '无火', low: '小火', medium: '中火', high: '大火' }

function blankItem() {
  return { ingredient: '', weight_g: null, tolerance_pct: 5 }
}

function blankStep() {
  return { title: '', instruction: '', heat_level: 'none', duration_sec: 0,
           ccp: false, requires_card: false }
}

const current = computed(
  () => recipes.value.find((row) => row.dish_id === selectedDish.value) || null,
)

function select(dishId) {
  selectedDish.value = dishId
  const row = recipes.value.find((item) => item.dish_id === dishId)
  if (!row?.recipe) {
    // 无配方菜也直接给空白草稿，录入后保存即 v1（后端本就支持从零建版本）
    draft.value = writable.value
      ? { items: [blankItem()], steps: [blankStep()] }
      : null
    return
  }
  draft.value = {
    items: row.recipe.items.map((item) => ({ ...item })),
    steps: row.recipe.steps.map((step) => ({ ...step })),
  }
}

async function load() {
  loading.value = true
  try {
    const data = await api.get('/api/recipes')
    recipes.value = data.recipes || []
    if (!selectedDish.value && recipes.value.length) {
      select(recipes.value[0].dish_id)
    } else if (selectedDish.value) {
      select(selectedDish.value)
    }
  } catch (err) {
    toastError(err, '配方加载失败')
  } finally {
    loading.value = false
  }
}

async function saveVersion() {
  if (!current.value || !draft.value) return
  const items = draft.value.items
    .filter((row) => String(row.ingredient || '').trim() || Number(row.weight_g) > 0)
    .map((item, index) => ({
      ingredient: String(item.ingredient || '').trim(),
      weight_g: Number(item.weight_g) || 0,
      tolerance_pct: Number(item.tolerance_pct) || 5,
      sort_no: index + 1,
    }))
  if (!items.length) {
    toastError(new Error('至少要有一条配料（名称 + 克重），否则称重核验无从谈起'), '无法保存')
    return
  }
  const steps = draft.value.steps
    .filter((row) => String(row.title || '').trim() || String(row.instruction || '').trim())
    .map((step, index) => ({
      step_no: index + 1,
      title: String(step.title || '').trim() || `步骤 ${index + 1}`,
      instruction: String(step.instruction || '').trim(),
      heat_level: String(step.heat_level || 'none'),
      duration_sec: Number(step.duration_sec) || 0,
      ccp: !!step.ccp,
      requires_card: !!step.requires_card,
    }))
  saving.value = true
  try {
    const data = await api.post(`/api/recipes/${current.value.dish_id}/versions`, { items, steps })
    toastOk(`已保存为 v${data.version}：${data.items} 条配料 / ${data.steps} 道工序`)
    await load()
  } catch (err) {
    toastError(err, '保存配方失败')
  } finally {
    saving.value = false
  }
}

function resetDraft() {
  if (selectedDish.value) select(selectedDish.value)
}

function addItem() { draft.value?.items.push(blankItem()) }
function removeItem(index) { draft.value?.items.splice(index, 1) }
function addStep() { draft.value?.steps.push(blankStep()) }
function removeStep(index) { draft.value?.steps.splice(index, 1) }
function moveStep(index, delta) {
  const steps = draft.value?.steps
  const target = index + delta
  if (!steps || target < 0 || target >= steps.length) return
  const [row] = steps.splice(index, 1)
  steps.splice(target, 0, row)
}

const nextVersion = computed(() => (current.value?.recipe?.version || 0) + 1)

// ---- 新增菜品 ----
const showNewDish = ref(false)
const newDish = ref({ code: '', name: '', category: '', price: '' })
const creatingDish = ref(false)

async function createDish() {
  creatingDish.value = true
  try {
    const data = await api.post('/api/recipes/dishes', {
      code: newDish.value.code, name: newDish.value.name,
      category: newDish.value.category,
      price: newDish.value.price === '' ? undefined : Number(newDish.value.price),
    })
    toastOk(`已新建菜品「${data.name}」，录入配料与工序后保存 v1`)
    showNewDish.value = false
    newDish.value = { code: '', name: '', category: '', price: '' }
    await load()
    select(data.dish_id)
  } catch (err) {
    toastError(err, '新建菜品失败')
  } finally {
    creatingDish.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="layout">
    <!-- 菜品列表 -->
    <section class="panel list">
      <div class="panel-head">
        <span class="panel-title">菜品</span>
        <button class="btn ghost sm" type="button" @click="load" :disabled="loading">
          <Icon name="refresh" :size="12" />
        </button>
      </div>
      <div class="panel-body tight scroll">
        <div v-if="loading" class="empty">加载中…</div>
        <button
          v-for="row in recipes" :key="row.dish_id" type="button"
          class="dish-item" :class="{ active: row.dish_id === selectedDish }"
          @click="select(row.dish_id)"
        >
          <div class="dish-top">
            <span class="bold small">{{ row.dish }}</span>
            <span v-if="row.recipe" class="tag info mono">v{{ row.recipe.version }}</span>
            <span v-else class="tag danger">无配方</span>
          </div>
          <div class="tiny muted mono">{{ row.dish_code }}</div>
        </button>
      </div>
      <div v-if="writable" class="new-dish">
        <button v-if="!showNewDish" class="btn ghost sm" type="button" @click="showNewDish = true">
          ＋ 新增菜品
        </button>
        <div v-else class="stack" style="gap: 6px">
          <input v-model="newDish.code" class="input" placeholder="编码，如 SuanLaTD" />
          <input
            v-model="newDish.name" class="input" placeholder="菜名，如 酸辣土豆丝"
            @keyup.enter="createDish"
          />
          <div class="row" style="gap: 6px">
            <input v-model="newDish.category" class="input" placeholder="分类，如 热菜" />
            <input v-model="newDish.price" class="input" type="number" min="0" step="0.5" placeholder="价格，如 18" />
          </div>
          <div class="row">
            <button
              class="btn primary sm" type="button"
              :disabled="creatingDish || !newDish.code || !newDish.name" @click="createDish"
            >{{ creatingDish ? '创建中…' : '创建' }}</button>
            <button class="btn ghost sm" type="button" @click="showNewDish = false">取消</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 配方详情：有配方展示当前版，无配方直接进入 v1 录入 -->
    <div class="stack detail">
      <div v-if="!current" class="panel">
        <div class="empty">左侧选一道菜开始。</div>
      </div>

      <template v-else>
        <section class="panel">
          <div class="panel-head">
            <span class="panel-title">
              {{ current.dish }}
              <span v-if="current.recipe" class="panel-sub">
                · 当前 v{{ current.recipe.version }} · {{ current.recipe.ccp_count }} 个 CCP
              </span>
              <span v-else class="panel-sub"> · 还没有配方，录入后保存为 v1</span>
            </span>
            <div class="row">
              <span v-if="current.recipe" class="tag ok">启用中</span>
              <span v-else class="tag warn">无配方</span>
            </div>
          </div>

          <div class="panel-body">
            <h3 class="section-title">配料与称重区间</h3>
            <table class="table">
              <thead>
                <tr>
                  <th>配料</th>
                  <th class="num" style="width: 120px">目标 (g)</th>
                  <th class="num" style="width: 96px">容差 (%)</th>
                  <th class="num" style="width: 130px">合格区间</th>
                  <th v-if="writable" style="width: 40px"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, index) in draft?.items || []" :key="index">
                  <td>
                    <input
                      v-if="writable" v-model="item.ingredient"
                      class="input" style="width: 100%" placeholder="配料名，如 猪里脊"
                    />
                    <span v-else>{{ item.ingredient }}</span>
                  </td>
                  <td class="num">
                    <input
                      v-if="writable" v-model.number="item.weight_g"
                      class="input num mini" type="number" min="1" step="1"
                    />
                    <span v-else class="mono">{{ fmt(item.weight_g, { digits: 0 }) }}</span>
                  </td>
                  <td class="num">
                    <input
                      v-if="writable" v-model.number="item.tolerance_pct"
                      class="input num mini" type="number" min="1" max="50" step="1"
                    />
                    <span v-else class="mono">{{ fmt(item.tolerance_pct, { digits: 0 }) }}</span>
                  </td>
                  <td class="num mono tiny muted">
                    {{ item.min_g != null
                      ? `${fmt(item.min_g, { digits: 1 })} ~ ${fmt(item.max_g, { digits: 1 })}` : '—' }}
                  </td>
                  <td v-if="writable" class="num">
                    <button
                      class="btn ghost sm row-del" type="button" title="删除该配料"
                      @click="removeItem(index)"
                    >✕</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="writable" style="margin-top: 8px">
              <button class="btn ghost sm" type="button" @click="addItem">＋ 加一条配料</button>
            </div>

            <h3 class="section-title" style="margin-top: 18px">制作工序</h3>
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 52px" class="num">序</th>
                  <th>工序</th>
                  <th style="width: 92px">火候</th>
                  <th class="num" style="width: 96px">时长 (s)</th>
                  <th style="width: 130px">标记</th>
                  <th v-if="writable" style="width: 96px"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(step, index) in draft?.steps || []" :key="index">
                  <td class="num mono tiny">{{ index + 1 }}</td>
                  <td>
                    <template v-if="writable">
                      <input
                        v-model="step.title" class="input" style="width: 100%"
                        placeholder="工序名，如 切配"
                      />
                      <input
                        v-model="step.instruction" class="input" style="width: 100%; margin-top: 4px"
                        placeholder="做法说明，如 里脊切丝 3mm"
                      />
                    </template>
                    <template v-else>
                      <div class="bold small">{{ step.title }}</div>
                      <div class="tiny muted pre-wrap">{{ step.instruction }}</div>
                    </template>
                  </td>
                  <td>
                    <select v-if="writable" v-model="step.heat_level" class="input" style="width: 100%">
                      <option value="none">无火</option>
                      <option value="low">小火</option>
                      <option value="medium">中火</option>
                      <option value="high">大火</option>
                    </select>
                    <span v-else class="small">{{ HEAT_LABEL[step.heat_level] || '—' }}</span>
                  </td>
                  <td class="num">
                    <input
                      v-if="writable" v-model.number="step.duration_sec"
                      class="input num mini" type="number" min="0" step="5"
                    />
                    <span v-else class="mono">{{ step.duration_sec }}</span>
                  </td>
                  <td>
                    <template v-if="writable">
                      <label class="tiny" style="display: flex; align-items: center; gap: 4px">
                        <input v-model="step.ccp" type="checkbox" /> CCP
                      </label>
                      <label class="tiny" style="display: flex; align-items: center; gap: 4px">
                        <input v-model="step.requires_card" type="checkbox" /> 刷卡
                      </label>
                    </template>
                    <template v-else>
                      <span v-if="step.ccp" class="tag danger">CCP</span>
                      <span v-if="step.requires_card" class="tag info" style="margin-left: 4px">
                        刷卡确认
                      </span>
                    </template>
                  </td>
                  <td v-if="writable">
                    <div class="row step-actions">
                      <button
                        class="btn ghost sm row-del" type="button" title="上移"
                        :disabled="index === 0" @click="moveStep(index, -1)"
                      >↑</button>
                      <button
                        class="btn ghost sm row-del" type="button" title="下移"
                        :disabled="index === (draft?.steps.length || 0) - 1" @click="moveStep(index, 1)"
                      >↓</button>
                      <button
                        class="btn ghost sm row-del" type="button" title="删除该工序"
                        @click="removeStep(index)"
                      >✕</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="writable" style="margin-top: 8px">
              <button class="btn ghost sm" type="button" @click="addStep">＋ 加一道工序</button>
            </div>

            <div class="actions">
              <span class="tiny muted">
                保存会生成 <strong>v{{ nextVersion }}</strong>，旧版本对已在做的订单继续生效。
              </span>
              <span class="spacer"></span>
              <button class="btn sm" type="button" @click="resetDraft" :disabled="saving">
                还原
              </button>
              <button
                class="btn primary sm" type="button"
                :disabled="!writable || saving" @click="saveVersion"
              >{{ saving ? '保存中…' : (current.recipe ? `另存为 v${nextVersion}` : '保存为 v1') }}</button>
            </div>
          </div>
        </section>

        <!-- 版本历史：新菜无版本可看，先不展示 -->
        <section v-if="current.recipe" class="panel">
          <div class="panel-head">
            <span class="panel-title">版本历史</span>
            <span class="panel-sub">{{ (current.versions || []).length }} 个版本</span>
          </div>
          <div class="panel-body tight">
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 70px">版本</th>
                  <th class="num" style="width: 70px">配料</th>
                  <th class="num" style="width: 70px">工序</th>
                  <th style="width: 90px">状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="ver in [...(current.versions || [])].reverse()" :key="ver.id">
                  <td class="mono">v{{ ver.version }}</td>
                  <td class="num mono">{{ ver.items }}</td>
                  <td class="num mono">{{ ver.steps }}</td>
                  <td>
                    <span class="tag" :class="ver.id === current.recipe.id ? 'ok' : 'plain'">
                      {{ ver.id === current.recipe.id ? '当前生效' : '历史' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 主-详布局：左列面板不拉伸、保持自然高度，吸顶（sticky）跟随滚动——
   右列配方再长，往下滚时菜品列表也一直钉在可视区里。
   sticky 相对滚动容器 .content 生效，top 对齐内容区内边距（16px）；
   列表超过一屏时面板内部自滚（.scroll），文档永不滚。
   窄屏单列堆叠时关闭吸顶（否则列表会压住下面的详情）。 */
.layout { display: grid; grid-template-columns: 236px minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 1000px) { .layout { grid-template-columns: minmax(0, 1fr); } .list { position: static; } }

.list { position: sticky; top: 16px; }
.list .panel-body { max-height: calc(100vh - 130px); }
/* 列表面板底部的「新增菜品」入口 */
.new-dish { padding: 10px 12px; border-top: 1px solid var(--line); }
/* 工序行的上移/下移/删除按钮组 */
.step-actions { gap: 4px; justify-content: flex-end; }
.step-actions .btn { padding: 0 7px; }
.dish-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 9px 12px;
  border: none;
  border-bottom: 1px solid var(--line);
  background: none;
  text-align: left;
  cursor: pointer;
  color: var(--text);
}
.dish-item:hover { background: var(--panel-2); }
.dish-item.active { background: var(--accent-soft); border-left: 2px solid var(--accent); }
.dish-top { display: flex; align-items: center; gap: 7px; }

.section-title { font-size: 12px; color: var(--muted); margin: 0 0 7px; letter-spacing: 0.03em; }
.input.mini { height: 26px; padding: 0 6px; font-size: 12px; width: 100%; text-align: right; }

.actions { display: flex; align-items: center; gap: 9px; margin-top: 16px; flex-wrap: wrap; }
.actions .spacer { flex: 1; }
</style>
