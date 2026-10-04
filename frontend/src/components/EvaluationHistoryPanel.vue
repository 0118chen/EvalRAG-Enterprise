<script setup lang="ts">
import type { EvaluationJob } from '../api'

defineProps<{
  evaluations: EvaluationJob[]
  selectedEvaluation: EvaluationJob | null
}>()

const emit = defineEmits<{
  select: [evaluation: EvaluationJob]
  refresh: []
}>()
</script>

<template>
  <section class="panel history-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">实验记录</p>
        <h2>历史任务</h2>
      </div>
      <button class="ghost" @click="emit('refresh')">刷新</button>
    </div>
    <div class="history-list">
      <button
        v-for="evaluation in evaluations"
        :key="evaluation.id"
        class="history-row"
        :class="{ active: selectedEvaluation?.id === evaluation.id }"
        @click="emit('select', evaluation)"
      >
        <span>
          <strong>{{ evaluation.experiment_name || evaluation.dataset_name }}</strong>
          <small>
            {{ evaluation.retrieval_mode }} · Top {{ evaluation.top_k }} ·
            {{ evaluation.id.slice(0, 8) }}
          </small>
        </span>
        <span class="status" :class="evaluation.status">{{ evaluation.status }}</span>
      </button>
      <p v-if="!evaluations.length" class="empty">暂无评测实验</p>
    </div>
  </section>
</template>
