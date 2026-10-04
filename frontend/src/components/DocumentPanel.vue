<script setup lang="ts">
import type { DocumentRecord } from '../api'

const version = defineModel<string>('version', { required: true })

defineProps<{
  documents: DocumentRecord[]
  knowledgeBaseName: string
  selectedKb: string
  busy: boolean
}>()

const emit = defineEmits<{
  upload: [file: File]
  remove: [document: DocumentRecord]
}>()

function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) emit('upload', file)
}
</script>

<template>
  <section class="panel document-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">数据资产</p>
        <h2>{{ knowledgeBaseName || '文档' }}</h2>
      </div>
      <div class="inline-controls">
        <input v-model="version" class="version-input" placeholder="文档版本" />
        <label class="button upload-button" :class="{ disabled: busy || !selectedKb }">
          {{ busy ? '处理中' : '上传文档' }}
          <input
            type="file"
            accept=".pdf,.docx,.html,.htm,.xlsx,.txt,.md"
            :disabled="busy || !selectedKb"
            @change="onFile"
          />
        </label>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>文档</th>
            <th>版本</th>
            <th>状态</th>
            <th>分块</th>
            <th>进度</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="document in documents" :key="document.id">
            <td>
              <strong>{{ document.filename }}</strong>
              <small v-if="document.error_message" class="danger-text">
                {{ document.error_message }}
              </small>
            </td>
            <td><span class="tag">{{ document.version }}</span></td>
            <td><span class="status" :class="document.status">{{ document.status }}</span></td>
            <td>{{ document.chunks }}</td>
            <td>
              <progress :value="document.progress" max="100" />
            </td>
            <td class="row-actions">
              <button class="danger-link" @click="emit('remove', document)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!documents.length" class="empty">当前知识库暂无文档</p>
  </section>
</template>
