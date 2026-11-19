<template>
  <section class="publish-panel" data-testid="publish-panel">
    <header class="publish-head">
      <div>
        <h3>属性发布快照</h3>
        <p class="publish-desc">
          属性登记只使用必填字段齐全的生效快照；切换完成前，已可用快照继续供应查询。
        </p>
      </div>
      <label class="publish-select">
        <span>维度编码</span>
        <select v-model="selected" data-testid="publish-view-select" @change="reloadView">
          <option v-for="item in views" :key="item.dim_code" :value="item.dim_code">
            {{ item.dim_code }} · {{ item.dim_name || '未命名' }}
          </option>
        </select>
      </label>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="view">
      <div class="publish-summary">
        <article class="publish-card">
          <span class="stat-label">生效快照</span>
          <strong class="stat-value" data-testid="active-version">{{ view.active_version || '—' }}</strong>
          <small>登记依据：{{ view.registration_basis || '无可用版本' }}</small>
        </article>
        <article class="publish-card">
          <span class="stat-label">主键字段</span>
          <strong class="stat-value">{{ view.primary_key.field || '—' }}</strong>
          <small>类型：{{ view.primary_key.type || '未声明' }}</small>
        </article>
        <article class="publish-card">
          <span class="stat-label">待切换快照</span>
          <strong class="stat-value" data-testid="candidate-version">{{ view.candidate_version || '—' }}</strong>
          <small>{{ view.candidate_status || '无待切换版本' }}</small>
        </article>
        <article class="publish-card">
          <span class="stat-label">属性登记</span>
          <strong class="stat-value">{{ view.can_register ? '可用' : '不可用' }}</strong>
          <small>{{ view.can_register ? '必填字段齐全' : '等待快照生效' }}</small>
        </article>
      </div>

      <p v-if="view.candidate_missing_fields.length" class="publish-warning" data-testid="candidate-missing">
        待切换快照缺少：{{ view.candidate_missing_fields.join('、') }}
      </p>

      <div class="publish-columns">
        <div class="publish-block">
          <h4>已可用字段</h4>
          <p v-if="!view.available_fields.length" class="empty-state">切换生效后才会对外开放</p>
          <ul class="publish-chip-list">
            <li v-for="name in view.available_fields" :key="name" class="publish-chip">{{ name }}</li>
          </ul>
        </div>
        <div class="publish-block">
          <h4>待确认字段</h4>
          <p v-if="!view.pending_fields.length" class="empty-state">没有待确认字段</p>
          <ul class="publish-chip-list">
            <li v-for="name in view.pending_fields" :key="name" class="publish-chip pending">{{ name }}</li>
          </ul>
        </div>
      </div>

      <div v-if="view.incoming_fields.length" class="publish-block">
        <h4>切换后新增字段（新字段何时可见）</h4>
        <table class="data-table" data-testid="incoming-fields">
          <thead>
            <tr><th>属性名称</th><th>数据类型</th><th>可空</th><th>确认状态</th><th>版本</th><th>可见条件</th></tr>
          </thead>
          <tbody>
            <tr v-for="field in view.incoming_fields" :key="`${field.版本}-${field.属性名称}`">
              <td>{{ field.属性名称 }}</td>
              <td>{{ field.数据类型 }}</td>
              <td>{{ field.是否可空 }}</td>
              <td>{{ field.是否已确认 }}</td>
              <td>{{ field.版本 }}</td>
              <td>{{ field.可见条件 }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="publish-block">
        <h4>发布快照版本</h4>
        <table class="data-table" data-testid="snapshot-table">
          <thead>
            <tr>
              <th>版本</th><th>状态</th><th>供应查询</th><th>主键类型</th>
              <th>生效区间</th><th>字段数</th><th>缺少必填</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="snapshot in view.snapshots" :key="snapshot.snapshot_id">
              <td>{{ snapshot.version }}</td>
              <td>{{ snapshot.status }}</td>
              <td>{{ snapshot.serves_queries ? '是' : '否' }}</td>
              <td>{{ snapshot.primary_key_type || '—' }}</td>
              <td>{{ interval(snapshot) }}</td>
              <td>{{ snapshot.fields.length }}</td>
              <td>{{ snapshot.missing_required_fields.join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="publish-actions">
          <button
            class="btn primary"
            type="button"
            data-testid="switch-snapshot"
            :disabled="!view.candidate_version || switching"
            @click="switchSnapshot"
          >
            {{ switching ? '切换中…' : '切换发布快照' }}
          </button>
        </div>
      </div>

      <div class="publish-block">
        <h4>字段可用时间</h4>
        <table class="data-table" data-testid="timeline-table">
          <thead>
            <tr><th>属性名称</th><th>版本</th><th>状态</th><th>可用时间</th><th>计划可用时间</th><th>可空</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in view.field_timeline" :key="`${item.版本}-${item.属性名称}`">
              <td>{{ item.属性名称 }}</td>
              <td>{{ item.版本 }}</td>
              <td>{{ item.状态 }}</td>
              <td>{{ item.可用时间 || '—' }}</td>
              <td>{{ item.计划可用时间 || '—' }}</td>
              <td>{{ item.是否可空 }}</td>
            </tr>
            <tr v-if="!view.field_timeline.length">
              <td colspan="6" class="empty-state">该维度还没有登记字段</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="publish-block">
        <h4>属性登记使用的维度版本</h4>
        <p class="publish-desc">
          登记版本：{{ view.registration_versions.length ? view.registration_versions.join('、') : '暂无登记' }}
        </p>
        <table class="data-table" data-testid="registration-table">
          <thead>
            <tr><th>属性名称</th><th>数据类型</th><th>依据版本</th><th>登记时间</th><th>可用时间</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in view.registrations" :key="`${item.发布快照版本}-${item.属性名称}`">
              <td>{{ item.属性名称 }}</td>
              <td>{{ item.数据类型 }}</td>
              <td>{{ item.发布快照版本 }}</td>
              <td>{{ item.登记时间 || '—' }}</td>
              <td>{{ item.可用时间 || '—' }}</td>
              <td>{{ item.状态 }}</td>
            </tr>
            <tr v-if="!view.registrations.length">
              <td colspan="6" class="empty-state">暂无登记记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  loadPublishView,
  loadPublishViews,
  switchPublishSnapshot,
} from '@/api/backend-service'
import type { PublishSnapshot, PublishView } from '@/api/backend-service'

const views = ref<PublishView[]>([])
const view = ref<PublishView | null>(null)
const selected = ref('')
const errorMessage = ref('')
const switching = ref(false)

function interval(snapshot: PublishSnapshot): string {
  const start = snapshot.effective_from || '待生效'
  const end = snapshot.effective_to || '9999-12-31 23:59:59'
  return `${start} ~ ${end}`
}

async function reloadList() {
  const payload = await loadPublishViews()
  views.value = payload.items
  if (!selected.value && payload.items.length) {
    selected.value = payload.items[0].dim_code
  }
}

async function reloadView() {
  if (!selected.value) {
    view.value = null
    return
  }
  errorMessage.value = ''
  try {
    view.value = await loadPublishView(selected.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布视图读取失败'
  }
}

async function switchSnapshot() {
  if (!selected.value) return
  switching.value = true
  errorMessage.value = ''
  try {
    const result = await switchPublishSnapshot(selected.value)
    if (!result.ok) throw new Error(result.message)
    await reloadList()
    await reloadView()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换失败'
  } finally {
    switching.value = false
  }
}

onMounted(async () => {
  try {
    await reloadList()
    await reloadView()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布视图读取失败'
  }
})

defineExpose({ refresh: reloadView })
</script>

<style scoped>
.publish-panel {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: #fff;
}
.publish-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.publish-head h3 { margin: 0; font-size: 15px; }
.publish-desc { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
.publish-select { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.publish-select select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.publish-summary { display: flex; gap: 12px; margin: 12px 0; }
.publish-card { flex: 1; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.publish-card small { display: block; color: var(--muted); font-size: 11px; margin-top: 4px; }
.publish-warning { color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 8px 10px; font-size: 12px; }
.publish-columns { display: flex; gap: 12px; }
.publish-block { flex: 1; margin-top: 12px; }
.publish-block h4 { margin: 0 0 6px; font-size: 13px; }
.publish-chip-list { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; padding: 0; margin: 0; }
.publish-chip { background: #ecfdf3; color: #067647; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.publish-chip.pending { background: #fef3f2; color: #b42318; }
.publish-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
</style>
