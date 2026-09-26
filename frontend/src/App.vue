<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  api,
  type Citation,
  type DocumentRecord,
  type EvaluationDataset,
  type EvaluationJob,
  type KnowledgeBase,
  type RetrievalDiagnostics,
  type SearchPayload,
} from './api'

type View = 'workspace' | 'evaluation'
type ExampleDraft = {
  question: string
  expected_answer: string
  expected_document_id: string
  expected_page?: number
  category: string
}

const activeView = ref<View>('workspace')
const tenantId = ref(localStorage.getItem('evalrag_tenant') || '')
const apiKey = ref(localStorage.getItem('evalrag_api_key') || '')
const loginName = ref(tenantId.value || 'demo-enterprise')
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const notice = ref('')

const knowledgeBases = ref<KnowledgeBase[]>([])
const selectedKb = ref('')
const documents = ref<DocumentRecord[]>([])
const kbName = ref('')
const kbDescription = ref('')
const uploadVersion = ref('latest')

const question = ref('')
const answer = ref('')
const citations = ref<Citation[]>([])
const retrieval = ref<RetrievalDiagnostics | null>(null)
const traceId = ref('')
const feedbackSent = ref(false)
const feedbackComment = ref('')

const retrievalMode = ref<SearchPayload['retrieval_mode']>('hybrid')
const topK = ref(5)
const queryVersion = ref('latest')
const rerank = ref(true)
const queryRewrite = ref(true)
const answerEvaluation = ref(false)

const datasets = ref<EvaluationDataset[]>([])
const selectedDataset = ref('')
const datasetName = ref('')
const datasetDescription = ref('')
const exampleDrafts = ref<ExampleDraft[]>([
  {
    question: '',
    expected_answer: '',
    expected_document_id: '',
    expected_page: undefined,
    category: 'general',
  },
])
const evaluations = ref<EvaluationJob[]>([])
const selectedEvaluation = ref<EvaluationJob | null>(null)
const baselineEvaluationId = ref('')
const experimentName = ref('')
const comparison = ref<Record<string, number>>({})

const loggedIn = computed(() => Boolean(tenantId.value))
const activeKnowledgeBase = computed(
  () => knowledgeBases.value.find((item) => item.id === selectedKb.value) || null,
)
const completedEvaluations = computed(() =>
  evaluations.value.filter((item) => item.status === 'completed'),
)

function setError(value: unknown) {
  error.value = value instanceof Error ? value.message : String(value)
  notice.value = ''
}

function setNotice(value: string) {
  notice.value = value
  error.value = ''
}

function logout() {
  tenantId.value = ''
  localStorage.removeItem('evalrag_tenant')
  localStorage.removeItem('evalrag_api_key')
}

async function login() {
  const tenant = loginName.value.trim()
  if (!tenant) return
  tenantId.value = tenant
  localStorage.setItem('evalrag_tenant', tenant)
  if (apiKey.value.trim()) localStorage.setItem('evalrag_api_key', apiKey.value.trim())
  else localStorage.removeItem('evalrag_api_key')
  await loadAll()
}

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    await loadKnowledgeBases()
    await Promise.all([loadDatasets(), loadEvaluations()])
  } catch (reason) {
    setError(reason)
  } finally {
    loading.value = false
  }
}

async function loadKnowledgeBases() {
  knowledgeBases.value = await api.listKnowledgeBases(tenantId.value)
  if (!selectedKb.value && knowledgeBases.value[0]) selectedKb.value = knowledgeBases.value[0].id
  await loadDocuments()
}

async function selectKnowledgeBase(id: string) {
  selectedKb.value = id
  queryVersion.value = 'latest'
  await loadDocuments()
}

async function loadDocuments() {
  documents.value = selectedKb.value
    ? await api.listDocuments(tenantId.value, selectedKb.value)
    : []
}

async function createKnowledgeBase() {
  if (!kbName.value.trim()) return
  busy.value = true
  try {
    const created = await api.createKnowledgeBase(
      tenantId.value,
      kbName.value.trim(),
      kbDescription.value.trim(),
    )
    kbName.value = ''
    kbDescription.value = ''
    await loadKnowledgeBases()
    await selectKnowledgeBase(created.id)
    setNotice('知识库已创建')
  } catch (reason) {
    setError(reason)
  } finally {
    busy.value = false
  }
}

async function uploadDocument(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !selectedKb.value) return
  busy.value = true
  try {
    const document = await api.upload(
      tenantId.value,
      selectedKb.value,
      file,
      uploadVersion.value.trim() || 'latest',
    )
    documents.value.unshift(document)
    await pollDocument(document.id)
    setNotice('文档处理完成')
  } catch (reason) {
    setError(reason)
  } finally {
    busy.value = false
  }
}

async function pollDocument(id: string) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const document = await api.document(tenantId.value, id)
    const index = documents.value.findIndex((item) => item.id === id)
    if (index >= 0) documents.value[index] = document
    if (document.status === 'ready' || document.status === 'failed') return
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
}

async function removeDocument(document: DocumentRecord) {
  if (!window.confirm(`删除文档“${document.filename}”？`)) return
  try {
    await api.deleteDocument(tenantId.value, document.id)
    documents.value = documents.value.filter((item) => item.id !== document.id)
    setNotice('文档已删除')
  } catch (reason) {
    setError(reason)
  }
}

function searchPayload(): SearchPayload {
  return {
    tenant_id: tenantId.value,
    knowledge_base_id: selectedKb.value,
    question: question.value.trim(),
    top_k: topK.value,
    retrieval_mode: retrievalMode.value,
    document_version: queryVersion.value.trim() || 'latest',
    rerank: rerank.value,
    query_rewrite: queryRewrite.value,
  }
}

async function ask() {
  if (!question.value.trim() || !selectedKb.value) return
  busy.value = true
  answer.value = ''
  citations.value = []
  retrieval.value = null
  traceId.value = ''
  feedbackSent.value = false
  feedbackComment.value = ''
  try {
    const payload = searchPayload()
    const search = await api.search(payload)
    citations.value = search.citations
    retrieval.value = search.retrieval
    traceId.value = search.trace_id
    await api.streamChat(payload, {
      onText: (text) => {
        answer.value += text
      },
      onCitations: (items) => {
        if (!citations.value.length) citations.value = items
      },
      onRetrieval: (value) => {
        retrieval.value = value
      },
      onTrace: (value) => {
        traceId.value = value
      },
    })
  } catch (reason) {
    setError(reason)
  } finally {
    busy.value = false
  }
}

async function sendFeedback(value: string) {
  if (!traceId.value) return
  try {
    await api.feedback(
      tenantId.value,
      traceId.value,
      value,
      feedbackComment.value.trim(),
    )
    feedbackSent.value = true
    setNotice('反馈已记录')
  } catch (reason) {
    setError(reason)
  }
}

function addExample() {
  exampleDrafts.value.push({
    question: '',
    expected_answer: '',
    expected_document_id: '',
    expected_page: undefined,
    category: 'general',
  })
}

function removeExample(index: number) {
  if (exampleDrafts.value.length > 1) exampleDrafts.value.splice(index, 1)
}

async function loadDatasets() {
  datasets.value = await api.listDatasets(tenantId.value)
  if (!selectedDataset.value && datasets.value[0]) selectedDataset.value = datasets.value[0].name
}

async function createDataset() {
  const examples = exampleDrafts.value
    .filter((item) => item.question.trim() && item.expected_document_id.trim())
    .map((item) => ({
      question: item.question.trim(),
      expected_answer: item.expected_answer.trim() || undefined,
      expected_document_id: item.expected_document_id.trim(),
      expected_page: item.expected_page || undefined,
      category: item.category.trim() || 'general',
    }))
  if (!selectedKb.value || !datasetName.value.trim() || !examples.length) {
    setError('请选择知识库，填写数据集名称并至少添加一条有效样例')
    return
  }
  busy.value = true
  try {
    const dataset = await api.createDataset({
      tenant_id: tenantId.value,
      knowledge_base_id: selectedKb.value,
      name: datasetName.value.trim(),
      description: datasetDescription.value.trim(),
      examples,
    })
    selectedDataset.value = dataset.name
    datasetName.value = ''
    datasetDescription.value = ''
    exampleDrafts.value = [
      {
        question: '',
        expected_answer: '',
        expected_document_id: '',
        expected_page: undefined,
        category: 'general',
      },
    ]
    await loadDatasets()
    setNotice('评测数据集已创建')
  } catch (reason) {
    setError(reason)
  } finally {
    busy.value = false
  }
}

async function loadEvaluations() {
  evaluations.value = await api.listEvaluations(tenantId.value)
  if (selectedEvaluation.value) {
    const current = evaluations.value.find((item) => item.id === selectedEvaluation.value?.id)
    if (current) selectedEvaluation.value = current
  }
}

async function runEvaluation() {
  const dataset = datasets.value.find((item) => item.name === selectedDataset.value)
  if (!dataset || !dataset.knowledge_base_id) {
    setError('请先选择评测数据集')
    return
  }
  busy.value = true
  comparison.value = {}
  try {
    const created = await api.createEvaluation({
      tenant_id: tenantId.value,
      knowledge_base_id: dataset.knowledge_base_id,
      dataset_name: dataset.name,
      retrieval_mode: retrievalMode.value,
      top_k: topK.value,
      document_version: queryVersion.value.trim() || 'latest',
      rerank: rerank.value,
      query_rewrite: queryRewrite.value,
      answer_evaluation: answerEvaluation.value,
      baseline_evaluation_id: baselineEvaluationId.value || undefined,
      experiment_name: experimentName.value.trim() || undefined,
    })
    selectedEvaluation.value = created
    await pollEvaluation(created.id)
    setNotice('评测实验已完成')
  } catch (reason) {
    setError(reason)
  } finally {
    busy.value = false
  }
}

async function pollEvaluation(id: string) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const evaluation = await api.evaluation(id, tenantId.value)
    selectedEvaluation.value = evaluation
    const index = evaluations.value.findIndex((item) => item.id === id)
    if (index >= 0) evaluations.value[index] = evaluation
    else evaluations.value.unshift(evaluation)
    if (evaluation.status === 'completed' || evaluation.status === 'failed') {
      if (evaluation.status === 'completed') await loadComparison(id)
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
}

async function selectEvaluation(evaluation: EvaluationJob) {
  selectedEvaluation.value = evaluation
  comparison.value = {}
  if (evaluation.status === 'completed') await loadComparison(evaluation.id)
}

async function loadComparison(id: string) {
  const result = await api.compareEvaluation(
    id,
    tenantId.value,
    baselineEvaluationId.value || undefined,
  )
  comparison.value = (result.delta || {}) as Record<string, number>
}

function metricEntries(job: EvaluationJob | null): Array<[string, number]> {
  const metrics = job?.results?.metrics || {}
  return Object.entries(metrics).filter(([, value]) => typeof value === 'number')
}

function metricLabel(key: string): string {
  const labels: Record<string, string> = {
    recall_at_3: 'Recall@3',
    recall_at_5: 'Recall@5',
    precision_at_3: 'Precision@3',
    precision_at_5: 'Precision@5',
    ndcg_at_3: 'nDCG@3',
    ndcg_at_5: 'nDCG@5',
    mrr: 'MRR',
    page_hit: '页码命中',
    answer_correctness: '答案正确性',
    answer_faithfulness: '答案忠实度',
    answer_completeness: '答案完整性',
    latency_ms: '平均耗时(ms)',
    example_count: '样例数',
  }
  return labels[key] || key
}

function formatMetric(key: string, value: number): string {
  if (key === 'latency_ms') return value.toFixed(1)
  if (key === 'example_count') return String(Math.round(value))
  return value.toFixed(3)
}

onMounted(() => {
  if (loggedIn.value) loadAll()
})
</script>

<template>
  <main v-if="!loggedIn" class="login-page">
    <section class="login-panel">
      <p class="eyebrow">EvalRAG Enterprise</p>
      <h1>企业知识检索与评测工作台</h1>
      <p class="muted">使用租户标识和可选 API Key 进入工作区。</p>
      <label>
        <span>租户标识</span>
        <input v-model="loginName" placeholder="demo-enterprise" @keyup.enter="login" />
      </label>
      <label>
        <span>API Key</span>
        <input v-model="apiKey" type="password" placeholder="生产环境必填" @keyup.enter="login" />
      </label>
      <p v-if="error" class="alert error">{{ error }}</p>
      <button class="primary wide" @click="login">进入工作台</button>
    </section>
  </main>

  <main v-else class="app-shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">EvalRAG Enterprise</p>
        <h1>知识检索与评测控制台</h1>
      </div>
      <div class="topbar-actions">
        <nav class="segmented" aria-label="主导航">
          <button :class="{ active: activeView === 'workspace' }" @click="activeView = 'workspace'">
            知识工作台
          </button>
          <button :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">
            评测实验
          </button>
        </nav>
        <span class="tenant-badge">{{ tenantId }}</span>
        <button class="ghost" @click="logout">退出</button>
      </div>
    </header>

    <p v-if="error" class="alert error">{{ error }}</p>
    <p v-if="notice" class="alert success">{{ notice }}</p>

    <section v-if="activeView === 'workspace'" class="workspace-layout">
      <aside class="panel knowledge-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">知识范围</p>
            <h2>知识库</h2>
          </div>
          <span class="count">{{ knowledgeBases.length }}</span>
        </div>
        <div class="form-stack">
          <input v-model="kbName" placeholder="知识库名称" />
          <textarea v-model="kbDescription" class="compact" placeholder="用途或数据范围说明" />
          <button class="primary" :disabled="busy" @click="createKnowledgeBase">创建知识库</button>
        </div>
        <div class="list-divider" />
        <button
          v-for="knowledgeBase in knowledgeBases"
          :key="knowledgeBase.id"
          class="list-item"
          :class="{ active: selectedKb === knowledgeBase.id }"
          @click="selectKnowledgeBase(knowledgeBase.id)"
        >
          <span>{{ knowledgeBase.name }}</span>
          <small>{{ knowledgeBase.description || '暂无说明' }}</small>
        </button>
        <p v-if="!knowledgeBases.length" class="empty">尚未创建知识库</p>
      </aside>

      <section class="panel document-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">数据资产</p>
            <h2>{{ activeKnowledgeBase?.name || '文档' }}</h2>
          </div>
          <div class="inline-controls">
            <input v-model="uploadVersion" class="version-input" placeholder="文档版本" />
            <label class="button upload-button" :class="{ disabled: busy || !selectedKb }">
              {{ busy ? '处理中' : '上传文档' }}
              <input
                type="file"
                accept=".pdf,.docx,.txt,.md"
                :disabled="busy || !selectedKb"
                @change="uploadDocument"
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
                  <button class="danger-link" @click="removeDocument(document)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!documents.length" class="empty">当前知识库暂无文档</p>
      </section>

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
            @keydown.ctrl.enter="ask"
          />
          <button class="primary ask-button" :disabled="busy || !selectedKb" @click="ask">
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
            <details v-for="(source, index) in citations" :key="`${source.document_id}-${source.page}-${index}`">
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
              <button @click="sendFeedback('correct')">回答正确</button>
              <button @click="sendFeedback('citation_error')">引用有误</button>
              <button @click="sendFeedback('incorrect')">回答错误</button>
            </template>
            <p v-else class="success-text">反馈已记录</p>
          </section>
        </article>
      </section>
    </section>

    <section v-else class="evaluation-layout">
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

      <section class="panel dataset-editor">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">数据准备</p>
            <h2>新建数据集</h2>
          </div>
          <button class="secondary" @click="addExample">添加样例</button>
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
            <input
              v-model.number="example.expected_page"
              type="number"
              min="1"
              placeholder="页码"
            />
            <input v-model="example.category" placeholder="类别" />
            <button class="danger-link" @click="removeExample(index)">移除</button>
            <textarea
              v-model="example.expected_answer"
              class="expected-answer"
              placeholder="标准答案（可选，用于答案级评测）"
            />
          </div>
        </div>
        <div class="editor-actions">
          <span class="muted">使用目标文档和页码计算 Recall、Precision、MRR、nDCG 与页码命中率。</span>
          <button class="primary" :disabled="busy || !selectedKb" @click="createDataset">
            保存数据集
          </button>
        </div>
      </section>

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
            <input v-model="queryVersion" placeholder="latest" />
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
          <button class="primary" :disabled="busy || !selectedDataset" @click="runEvaluation">
            {{ busy ? '实验运行中' : '运行实验' }}
          </button>
        </div>
      </section>

      <section class="panel history-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">实验记录</p>
            <h2>历史任务</h2>
          </div>
          <button class="ghost" @click="loadEvaluations">刷新</button>
        </div>
        <div class="history-list">
          <button
            v-for="evaluation in evaluations"
            :key="evaluation.id"
            class="history-row"
            :class="{ active: selectedEvaluation?.id === evaluation.id }"
            @click="selectEvaluation(evaluation)"
          >
            <span>
              <strong>{{ evaluation.experiment_name || evaluation.dataset_name }}</strong>
              <small>{{ evaluation.retrieval_mode }} · Top {{ evaluation.top_k }} · {{ evaluation.id.slice(0, 8) }}</small>
            </span>
            <span class="status" :class="evaluation.status">{{ evaluation.status }}</span>
          </button>
          <p v-if="!evaluations.length" class="empty">暂无评测实验</p>
        </div>
      </section>

      <section class="panel result-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">实验结果</p>
            <h2>{{ selectedEvaluation?.experiment_name || '选择实验查看结果' }}</h2>
          </div>
          <span v-if="selectedEvaluation?.completed_at" class="muted">
            {{ new Date(selectedEvaluation.completed_at).toLocaleString() }}
          </span>
        </div>

        <p v-if="selectedEvaluation?.error_message" class="alert error">
          {{ selectedEvaluation.error_message }}
        </p>

        <div v-if="selectedEvaluation?.results?.metrics" class="metric-grid">
          <div
            v-for="[key, value] in metricEntries(selectedEvaluation)"
            :key="key"
            class="metric"
          >
            <span>{{ metricLabel(key) }}</span>
            <strong>{{ formatMetric(key, value) }}</strong>
            <small
              v-if="key in comparison"
              :class="comparison[key] >= 0 ? 'positive' : 'negative'"
            >
              {{ comparison[key] >= 0 ? '+' : '' }}{{ comparison[key].toFixed(4) }} vs 基线
            </small>
          </div>
        </div>

        <div
          v-if="selectedEvaluation?.results?.examples?.length"
          class="example-results"
        >
          <details
            v-for="(example, index) in selectedEvaluation.results.examples"
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
              <p v-if="example.expected_answer">
                标准答案：{{ example.expected_answer }}
              </p>
              <p v-if="example.generated_answer">
                模型答案：{{ example.generated_answer }}
              </p>
              <div class="retrieved-list">
                <span
                  v-for="(item, itemIndex) in (example.retrieved as Array<Record<string, unknown>>)"
                  :key="itemIndex"
                  class="retrieved-item"
                >
                  {{ item.document_id }} / p{{ item.page }} / {{ Number(item.score).toFixed(4) }}
                </span>
              </div>
              <p class="metrics-inline">
                <span v-for="(value, key) in (example.metrics as Record<string, number>)" :key="key">
                  {{ key }} {{ Number(value).toFixed(3) }}
                </span>
              </p>
            </div>
          </details>
        </div>
        <p v-else-if="!selectedEvaluation" class="empty">从历史任务中选择一次实验</p>
      </section>
    </section>
  </main>
</template>
