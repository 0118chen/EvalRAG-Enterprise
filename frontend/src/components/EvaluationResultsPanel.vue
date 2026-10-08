<script setup lang="ts">
import type { EvaluationJob } from '../api'
import { formatMetric, metricEntries, metricLabel } from '../metrics'

defineProps<{
  evaluation: EvaluationJob | null
  comparison: Record<string, number>
}>()
</script>

<template>
  <section class="panel result-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">实验结果</p>
        <h2>{{ evaluation?.experiment_name || '选择实验查看结果' }}</h2>
      </div>
      <span v-if="evaluation?.completed_at" class="muted">
        {{ new Date(evaluation.completed_at).toLocaleString() }}
      </span>
    </div>

    <p v-if="evaluation?.error_message" class="alert error">
      {{ evaluation.error_message }}
    </p>

    <div v-if="evaluation?.results?.metrics" class="metric-grid">
      <div v-for="[key, value] in metricEntries(evaluation)" :key="key" class="metric">
        <span>{{ metricLabel(key) }}</span>
        <strong>{{ formatMetric(key, value) }}</strong>
        <small v-if="key in comparison" :class="comparison[key] >= 0 ? 'positive' : 'negative'">
          {{ comparison[key] >= 0 ? '+' : '' }}{{ comparison[key].toFixed(4) }} vs 基线
        </small>
      </div>
    </div>

    <div v-if="evaluation?.results?.examples?.length" class="example-results">
      <details
        v-for="(example, index) in evaluation.results.examples"
        :key="String(example.example_id || index)"
      >
        <summary>
          <span>{{ index + 1 }}. {{ example.question }}</span>
          <span class="tag">{{ example.category }}</span>
        </summary>
        <div class="result-detail">
          <p>
            期望文档 {{ example.expected_document_id }}
            <span v-if="example.expected_page"> · 第 {{ example.expected_page }} 页</span>
          </p>
          <p v-if="example.expected_answer">标准答案：{{ example.expected_answer }}</p>
          <p v-if="example.generated_answer">模型答案：{{ example.generated_answer }}</p>
          <div class="retrieved-list">
            <span
              v-for="(item, itemIndex) in example.retrieved as Array<Record<string, unknown>>"
              :key="itemIndex"
              class="retrieved-item"
            >
              {{ item.document_id }} / p{{ item.page }} / {{ Number(item.score).toFixed(4) }}
            </span>
          </div>
          <p class="metrics-inline">
            <span v-for="(value, key) in example.metrics as Record<string, number>" :key="key">
              {{ key }} {{ Number(value).toFixed(3) }}
            </span>
          </p>
        </div>
      </details>
    </div>
    <p v-else-if="!evaluation" class="empty">从历史任务中选择一次实验</p>
  </section>
</template>
