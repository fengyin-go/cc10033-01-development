<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "维度定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "维度属性", "created": 0, "pending": 0, "abnormal": 0}, {"name": "事实设计", "created": 0, "pending": 0, "abnormal": 0}, {"name": "度量配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "SCD处理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "代理键", "created": 0, "pending": 0, "abnormal": 0}, {"name": "层级构建", "created": 0, "pending": 0, "abnormal": 0}, {"name": "星型查询", "created": 0, "pending": 0, "abnormal": 0}, {"name": "汇总构建", "created": 0, "pending": 0, "abnormal": 0}, {"name": "桥接关联", "created": 0, "pending": 0, "abnormal": 0}, {"name": "ETL映射", "created": 0, "pending": 0, "abnormal": 0}, {"name": "快照计划", "created": 0, "pending": 0, "abnormal": 0}, {"name": "模型血缘", "created": 0, "pending": 0, "abnormal": 0}, {"name": "一致性维度", "created": 0, "pending": 0, "abnormal": 0}, {"name": "DataVault", "created": 0, "pending": 0, "abnormal": 0}, {"name": "多维立方", "created": 0, "pending": 0, "abnormal": 0}, {"name": "变更日志", "created": 0, "pending": 0, "abnormal": 0}, {"name": "日期维度", "created": 0, "pending": 0, "abnormal": 0}, {"name": "模型校验", "created": 0, "pending": 0, "abnormal": 0}, {"name": "部署流水线", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
