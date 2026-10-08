<script setup lang="ts">
import type { EvaluationDataset, EvaluationJob, SearchPayload } from '../api'

const selectedDataset = defineModel<string>('selectedDataset', { required: true })
const retrievalMode = defineModel<SearchPayload['retrieval_mode']>('retrievalMode', {
  required: true,
})
const topK = defineModel<number>('topK', { required: true })
const queryVersion = defineModel<string>('queryVersion', { required: true })
const baselineEvaluationId = defineModel<string>('baselineEvaluationId', { required: true })
const experimentName = defineModel<string>('experimentName', { required: true })
const rerank = defineModel<boolean>('rerank', { required: true })
const queryRewrite = defineModel<boolean>('queryRewrite', { required: true })
const answerEvaluation = defineModel<boolean>('answerEvaluation', { required: true })

defineProps<{
  datasets: EvaluationDataset[]
  completedEvaluations: EvaluationJob[]
  busy: boolean
}>()

const emit = defineEmits<{
  run: []
}>()
</script>

<template>
  <section class="panel experiment-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">实验配置</p>
        <h2>运行检索评测</h2>
      </div>
    </div>
    <div class="experiment-form">
      <label>
        <span>数据集</span>
        <select v-model="selectedDataset">
          <option value="">请选择</option>
          <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.name">
            {{ dataset.name }}
          </option>
        </select>
      </label>
      <label>
        <span>检索模式</span>
        <select v-model="retrievalMode">
          <option value="hybrid">混合检索</option>
          <option value="dense">Dense</option>
          <option value="sparse">Sparse</option>
        </select>
      </label>
      <label>
        <span>文档版本</span>
        <input v-model="queryVersion" placeholder="留空 = 全部版本" />
      </label>
      <label>
        <span>Top K</span>
        <input v-model.number="topK" type="number" min="1" max="20" />
      </label>
      <label>
        <span>基线实验</span>
        <select v-model="baselineEvaluationId">
          <option value="">不比较</option>
          <option
            v-for="evaluation in completedEvaluations"
            :key="evaluation.id"
            :value="evaluation.id"
          >
            {{ evaluation.dataset_name }} · {{ evaluation.id.slice(0, 8) }}
          </option>
        </select>
      </label>
      <label>
        <span>实验名称</span>
        <input v-model="experimentName" placeholder="例如 rerank-v2" />
      </label>
      <div class="experiment-toggles">
        <label class="toggle">
          <input v-model="rerank" type="checkbox" />
          启用 Reranker
        </label>
        <label class="toggle">
          <input v-model="queryRewrite" type="checkbox" />
          启用 Query Rewrite
        </label>
        <label class="toggle">
          <input v-model="answerEvaluation" type="checkbox" />
          启用答案级评测
        </label>
      </div>
      <button class="primary" :disabled="busy || !selectedDataset" @click="emit('run')">
        {{ busy ? '实验运行中' : '运行实验' }}
      </button>
    </div>
  </section>
</template>
