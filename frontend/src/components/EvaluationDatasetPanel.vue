<script setup lang="ts">
import type { EvaluationDataset } from '../api'

const selectedDataset = defineModel<string>('selectedDataset', { required: true })

defineProps<{
  datasets: EvaluationDataset[]
}>()
</script>

<template>
  <aside class="panel dataset-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">黄金样例</p>
        <h2>评测数据集</h2>
      </div>
      <span class="count">{{ datasets.length }}</span>
    </div>
    <button
      v-for="dataset in datasets"
      :key="dataset.id"
      class="list-item"
      :class="{ active: selectedDataset === dataset.name }"
      @click="selectedDataset = dataset.name"
    >
      <span>{{ dataset.name }}</span>
      <small>{{ dataset.examples.length }} 条样例 · {{ dataset.knowledge_base_id }}</small>
    </button>
    <p v-if="!datasets.length" class="empty">尚未创建评测数据集</p>
  </aside>
</template>
