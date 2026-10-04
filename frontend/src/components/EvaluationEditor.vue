<script setup lang="ts">
import type { DocumentRecord } from '../api'
import type { ExampleDraft } from '../types'

const datasetName = defineModel<string>('datasetName', { required: true })
const datasetDescription = defineModel<string>('datasetDescription', { required: true })
const exampleDrafts = defineModel<ExampleDraft[]>('exampleDrafts', { required: true })

defineProps<{
  documents: DocumentRecord[]
  busy: boolean
  selectedKb: string
}>()

const emit = defineEmits<{
  addExample: []
  removeExample: [index: number]
  save: []
}>()
</script>

<template>
  <section class="panel dataset-editor">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">数据准备</p>
        <h2>新建数据集</h2>
      </div>
      <button class="secondary" @click="emit('addExample')">添加样例</button>
    </div>
    <div class="dataset-meta">
      <input v-model="datasetName" placeholder="数据集名称" />
      <input v-model="datasetDescription" placeholder="评测目标说明" />
    </div>
    <div class="example-editor">
      <div v-for="(example, index) in exampleDrafts" :key="index" class="example-row">
        <span class="row-number">{{ index + 1 }}</span>
        <input v-model="example.question" placeholder="问题" />
        <select v-model="example.expected_document_id">
          <option value="">目标文档</option>
          <option v-for="document in documents" :key="document.id" :value="document.id">
            {{ document.filename }}
          </option>
        </select>
        <input v-model.number="example.expected_page" type="number" min="1" placeholder="页码" />
        <input v-model="example.category" placeholder="类别" />
        <button class="danger-link" @click="emit('removeExample', index)">移除</button>
        <textarea
          v-model="example.expected_answer"
          class="expected-answer"
          placeholder="标准答案（可选，用于答案级评测）"
        />
      </div>
    </div>
    <div class="editor-actions">
      <span class="muted">使用目标文档和页码计算 Recall、Precision、MRR、nDCG 与页码命中率。</span>
      <button class="primary" :disabled="busy || !selectedKb" @click="emit('save')">
        保存数据集
      </button>
    </div>
  </section>
</template>
