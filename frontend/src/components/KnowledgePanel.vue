<script setup lang="ts">
import type { KnowledgeBase } from '../api'

const name = defineModel<string>('name', { required: true })
const description = defineModel<string>('description', { required: true })

defineProps<{
  knowledgeBases: KnowledgeBase[]
  selectedKb: string
  busy: boolean
}>()

const emit = defineEmits<{
  create: []
  select: [id: string]
}>()
</script>

<template>
  <aside class="panel knowledge-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">知识范围</p>
        <h2>知识库</h2>
      </div>
      <span class="count">{{ knowledgeBases.length }}</span>
    </div>
    <div class="form-stack">
      <input v-model="name" placeholder="知识库名称" />
      <textarea v-model="description" class="compact" placeholder="用途或数据范围说明" />
      <button class="primary" :disabled="busy" @click="emit('create')">创建知识库</button>
    </div>
    <div class="list-divider" />
    <button
      v-for="knowledgeBase in knowledgeBases"
      :key="knowledgeBase.id"
      class="list-item"
      :class="{ active: selectedKb === knowledgeBase.id }"
      @click="emit('select', knowledgeBase.id)"
    >
      <span>{{ knowledgeBase.name }}</span>
      <small>{{ knowledgeBase.description || '暂无说明' }}</small>
    </button>
    <p v-if="!knowledgeBases.length" class="empty">尚未创建知识库</p>
  </aside>
</template>
