<script setup lang="ts">
import type { Citation, RetrievalDiagnostics, SearchPayload } from '../api'

const question = defineModel<string>('question', { required: true })
const retrievalMode = defineModel<SearchPayload['retrieval_mode']>('retrievalMode', {
  required: true,
})
const topK = defineModel<number>('topK', { required: true })
const queryVersion = defineModel<string>('queryVersion', { required: true })
const rerank = defineModel<boolean>('rerank', { required: true })
const queryRewrite = defineModel<boolean>('queryRewrite', { required: true })
const feedbackComment = defineModel<string>('feedbackComment', { required: true })

defineProps<{
  busy: boolean
  selectedKb: string
  answer: string
  citations: Citation[]
  retrieval: RetrievalDiagnostics | null
  traceId: string
  feedbackSent: boolean
}>()

const emit = defineEmits<{
  ask: []
  feedback: [value: string]
}>()
</script>

<template>
  <section class="panel chat-panel">
    <div class="panel-heading">
      <div>
        <p class="section-kicker">可追溯问答</p>
        <h2>检索对话</h2>
      </div>
      <div class="retrieval-controls">
        <select v-model="retrievalMode">
          <option value="hybrid">混合检索</option>
          <option value="dense">Dense</option>
          <option value="sparse">Sparse</option>
        </select>
        <input v-model.number="topK" type="number" min="1" max="20" aria-label="Top K" />
        <input v-model="queryVersion" class="version-input" placeholder="检索版本" />
        <label class="toggle">
          <input v-model="rerank" type="checkbox" />
          Rerank
        </label>
        <label class="toggle">
          <input v-model="queryRewrite" type="checkbox" />
          Query Rewrite
        </label>
      </div>
    </div>
    <div class="composer">
      <textarea
        v-model="question"
        placeholder="输入问题，按 Ctrl+Enter 发起检索"
        @keydown.ctrl.enter="emit('ask')"
      />
      <button class="primary ask-button" :disabled="busy || !selectedKb" @click="emit('ask')">
        {{ busy ? '处理中' : '提问' }}
      </button>
    </div>

    <article v-if="answer || citations.length" class="answer-region">
      <div class="answer-heading">
        <div>
          <p class="section-kicker">回答</p>
          <h3>基于证据生成</h3>
        </div>
        <span v-if="traceId" class="trace-badge">Trace {{ traceId.slice(0, 12) }}</span>
      </div>
      <p class="answer-text">{{ answer }}</p>

      <div v-if="retrieval" class="diagnostics">
        <span>候选 {{ retrieval.candidate_count }}</span>
        <span>{{ retrieval.cache_hit ? '缓存命中' : '实时检索' }}</span>
        <span>{{ retrieval.reranked ? '已重排' : '未重排' }}</span>
        <span>版本 {{ retrieval.document_version || '全部' }}</span>
        <span v-if="retrieval.rewritten_queries.length">
          Query {{ retrieval.rewritten_queries.length }} 路
        </span>
      </div>

      <section v-if="citations.length" class="citation-list">
        <h3>引用来源</h3>
        <details
          v-for="(source, index) in citations"
          :key="`${source.document_id}-${source.page}-${index}`"
        >
          <summary>
            <span>{{ source.document_id }} · 第 {{ source.page }} 页</span>
            <span class="tag">{{ source.version }}</span>
            <span class="score">{{ source.score?.toFixed(4) ?? 'n/a' }}</span>
          </summary>
          <p>{{ source.text }}</p>
        </details>
      </section>

      <section class="feedback-row">
        <template v-if="!feedbackSent">
          <input v-model="feedbackComment" placeholder="可选反馈说明" />
          <button @click="emit('feedback', 'correct')">回答正确</button>
          <button @click="emit('feedback', 'citation_error')">引用有误</button>
          <button @click="emit('feedback', 'incorrect')">回答错误</button>
        </template>
        <p v-else class="success-text">反馈已记录</p>
      </section>
    </article>
  </section>
</template>
