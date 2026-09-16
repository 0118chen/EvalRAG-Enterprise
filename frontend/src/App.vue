<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type Citation, type DocumentRecord, type KnowledgeBase } from './api'

const tenantId = ref(localStorage.getItem('evalrag_tenant') || '')
const loginName = ref(tenantId.value || 'demo-enterprise')
const knowledgeBases = ref<KnowledgeBase[]>([]); const selectedKb = ref(''); const documents = ref<DocumentRecord[]>([])
const kbName = ref(''); const question = ref(''); const answer = ref(''); const citations = ref<Citation[]>([])
const busy = ref(false); const error = ref(''); const feedbackSent = ref(false)
const traceId = ref('')
const loggedIn = computed(() => Boolean(tenantId.value))

async function loadKnowledgeBases() { knowledgeBases.value = await api.listKnowledgeBases(tenantId.value); if (!selectedKb.value && knowledgeBases.value[0]) selectedKb.value = knowledgeBases.value[0].id; await loadDocuments() }
async function loadDocuments() { documents.value = selectedKb.value ? await api.listDocuments(tenantId.value, selectedKb.value) : [] }
async function login() { tenantId.value = loginName.value.trim(); localStorage.setItem('evalrag_tenant', tenantId.value); await loadKnowledgeBases() }
async function createKb() { if (!kbName.value.trim()) return; const kb = await api.createKnowledgeBase(tenantId.value, kbName.value); kbName.value = ''; await loadKnowledgeBases(); selectedKb.value = kb.id; await loadDocuments() }
async function upload(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file || !selectedKb.value) return; busy.value = true; try { const doc = await api.upload(tenantId.value, selectedKb.value, file); documents.value.unshift(doc); await poll(doc.id) } catch (e) { error.value = String(e) } finally { busy.value = false } }
async function poll(id: string) { for (let i = 0; i < 60; i++) { const doc = await api.document(tenantId.value, id); const index = documents.value.findIndex(item => item.id === id); if (index >= 0) documents.value[index] = doc; if (doc.status === 'ready' || doc.status === 'failed') return; await new Promise(resolve => setTimeout(resolve, 1000)) } }
async function ask() { if (!question.value.trim() || !selectedKb.value) return; busy.value = true; error.value = ''; answer.value = ''; citations.value = []; feedbackSent.value = false; const payload = { tenant_id: tenantId.value, knowledge_base_id: selectedKb.value, question: question.value, top_k: 5, retrieval_mode: 'hybrid' }; try { const search = await api.search(payload); citations.value = search.citations; traceId.value = search.trace_id; await api.streamChat(payload, text => answer.value += text, items => { if (!citations.value.length) citations.value = items }) } catch (e) { error.value = String(e) } finally { busy.value = false } }
async function sendFeedback(value: string) { await api.feedback(traceId.value, value); feedbackSent.value = true }
onMounted(() => { if (loggedIn.value) loadKnowledgeBases().catch(e => error.value = String(e)) })
</script>

<template>
  <main v-if="!loggedIn" class="login"><section class="card"><p class="eyebrow">EvalRAG Enterprise</p><h1>企业知识，从可追溯的答案开始。</h1><p>使用演示租户进入本地工作台。</p><input v-model="loginName" @keyup.enter="login" placeholder="租户标识" /><button @click="login">进入工作台</button></section></main>
  <main v-else class="shell">
    <header><div><p class="eyebrow">EvalRAG Enterprise</p><h1>知识检索工作台</h1></div><span class="tenant">{{ tenantId }}</span></header>
    <p v-if="error" class="error">{{ error }}</p>
    <section class="grid">
      <aside class="card"><h2>知识库</h2><div class="inline"><input v-model="kbName" placeholder="新知识库名称" /><button @click="createKb">创建</button></div><button v-for="kb in knowledgeBases" :key="kb.id" class="kb" :class="{ active: selectedKb === kb.id }" @click="selectedKb = kb.id; loadDocuments()">{{ kb.name }}</button></aside>
      <section class="card"><div class="section-head"><div><h2>文档</h2><p>PDF、DOCX、TXT、Markdown</p></div><label class="upload">{{ busy ? '处理中…' : '上传文档' }}<input type="file" accept=".pdf,.docx,.txt,.md" @change="upload" /></label></div><div v-if="!documents.length" class="empty">暂无文档</div><div v-for="doc in documents" :key="doc.id" class="doc"><div><strong>{{ doc.filename }}</strong><small>{{ doc.status }} · {{ doc.chunks }} chunks</small></div><progress :value="doc.progress" max="100" /></div></section>
          <section class="card chat"><h2>问答</h2><div class="composer"><textarea v-model="question" placeholder="输入关于知识库的问题…" @keydown.ctrl.enter="ask" /><button @click="ask" :disabled="busy">提问</button></div><article v-if="answer" class="answer"><h3>回答</h3><p>{{ answer }}</p><div v-if="citations.length" class="sources"><h3>引用来源</h3><details v-for="source in citations" :key="source.document_id + source.page"><summary>{{ source.document_id }} · 第 {{ source.page }} 页 · score {{ source.score?.toFixed(3) ?? 'n/a' }}</summary><p>{{ source.text }}</p></details></div><small v-if="traceId">Trace: {{ traceId }}</small><div class="feedback" v-if="!feedbackSent"><span>这个回答有帮助吗？</span><button @click="sendFeedback('correct')">正确</button><button @click="sendFeedback('incorrect')">错误</button></div><p v-else class="success">反馈已记录</p></article></section>
    </section>
  </main>
</template>
