<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
import {
  clearApiKey,
  clearSessionToken,
  clearTenantId,
  getSessionToken,
  loadSessionToken,
  loadTenantId,
  saveSessionToken,
  saveTenantId,
  setApiKey,
} from './auth'
import ChatPanel from './components/ChatPanel.vue'
import DocumentPanel from './components/DocumentPanel.vue'
import EvaluationDatasetPanel from './components/EvaluationDatasetPanel.vue'
import EvaluationEditor from './components/EvaluationEditor.vue'
import EvaluationExperimentPanel from './components/EvaluationExperimentPanel.vue'
import EvaluationHistoryPanel from './components/EvaluationHistoryPanel.vue'
import EvaluationResultsPanel from './components/EvaluationResultsPanel.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import { emptyExampleDraft, type ExampleDraft } from './types'

type View = 'workspace' | 'evaluation'

const activeView = ref<View>('workspace')
const tenantId = ref(loadTenantId())
// The token, not the key, is what keeps this tab signed in; the key only exists in the
// input until the exchange succeeds.
const sessionToken = ref(loadSessionToken())
const apiKey = ref('')
const rememberSession = ref(true)
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
const queryVersion = ref('')
const rerank = ref(true)
const queryRewrite = ref(true)
const answerEvaluation = ref(false)

const datasets = ref<EvaluationDataset[]>([])
const selectedDataset = ref('')
const datasetName = ref('')
const datasetDescription = ref('')
const exampleDrafts = ref<ExampleDraft[]>([emptyExampleDraft()])
const evaluations = ref<EvaluationJob[]>([])
const selectedEvaluation = ref<EvaluationJob | null>(null)
const baselineEvaluationId = ref('')
const experimentName = ref('')
const comparison = ref<Record<string, number>>({})

// A remembered tenant is not a session: the workspace only opens once this tab holds a
// token the server has actually minted (and, on mount, confirmed is still alive).
const loggedIn = computed(() => Boolean(tenantId.value && sessionToken.value))
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
  // Best effort: the token is revoked server-side so logging out ends the credential
  // rather than just forgetting it, but an unreachable server must not trap the operator
  // inside the workspace.
  api.revokeSession().catch(() => undefined)
  forgetCredentials()
  tenantId.value = ''
  clearTenantId()
  clearWorkspace()
}

/** Drop every local trace of the session: memory, tab storage and the in-memory key. */
function forgetCredentials() {
  sessionToken.value = ''
  clearSessionToken()
  apiKey.value = ''
  clearApiKey()
}

function clearWorkspace() {
  knowledgeBases.value = []
  selectedKb.value = ''
  documents.value = []
  datasets.value = []
  selectedDataset.value = ''
  evaluations.value = []
  selectedEvaluation.value = null
  comparison.value = {}
  answer.value = ''
  citations.value = []
  retrieval.value = null
  traceId.value = ''
}

async function login() {
  const tenant = loginName.value.trim()
  if (!tenant) return
  busy.value = true
  error.value = ''
  try {
    const session = await api.createSession({ apiKey: apiKey.value.trim(), tenantId: tenant })
    setApiKey(apiKey.value)
    apiKey.value = ''
    saveSessionToken(session.token, { remember: rememberSession.value })
    sessionToken.value = getSessionToken()
    tenantId.value = session.tenant_id || tenant
    saveTenantId(tenantId.value)
    await loadAll()
  } catch (reason) {
    // Nothing is stored on a refused exchange: no token, and no tenant either, so a
    // typo cannot leave the shell pointing at a tenant this tab cannot authenticate as.
    setError(reason)
  } finally {
    busy.value = false
  }
}

/**
 * Confirm the token loaded from this tab is still live before opening the workspace.
 *
 * A token can be expired or revoked while the tab is closed, and every request behind the
 * shell would then fail with 401; checking once turns that into the login form.
 */
async function validateSession() {
  try {
    const session = await api.getSession()
    if (session?.tenant_id) {
      tenantId.value = session.tenant_id
      saveTenantId(session.tenant_id)
    }
    await loadAll()
  } catch {
    forgetCredentials()
    tenantId.value = ''
    clearTenantId()
    setError('登录状态已失效，请重新登录')
  }
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
  queryVersion.value = ''
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

async function uploadDocument(file: File) {
  if (!selectedKb.value) return
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
    if (
      document.status === 'ready' ||
      document.status === 'failed' ||
      document.status === 'needs_ocr'
    )
      return
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
    document_version: queryVersion.value.trim() || null,
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
      onError: (message) => {
        setError(message)
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
    await api.feedback(tenantId.value, traceId.value, value, feedbackComment.value.trim())
    feedbackSent.value = true
    setNotice('反馈已记录')
  } catch (reason) {
    setError(reason)
  }
}

function addExample() {
  exampleDrafts.value.push(emptyExampleDraft())
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
    exampleDrafts.value = [emptyExampleDraft()]
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
      document_version: queryVersion.value.trim() || null,
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

onMounted(() => {
  if (loggedIn.value) validateSession()
})
</script>

<template>
  <main v-if="!loggedIn" class="login-page">
    <section class="login-panel">
      <p class="eyebrow">EvalRAG Enterprise</p>
      <h1>企业知识检索与评测工作台</h1>
      <p class="muted">使用租户标识登录；API Key 只用于兑换本标签页的短期会话令牌。</p>
      <label>
        <span>租户标识</span>
        <input v-model="loginName" placeholder="demo-enterprise" @keyup.enter="login" />
      </label>
      <label>
        <span>API Key</span>
        <input v-model="apiKey" type="password" placeholder="生产环境必填" @keyup.enter="login" />
      </label>
      <label class="toggle">
        <input v-model="rememberSession" type="checkbox" />
        在本标签页内保持登录（仅存短期令牌）
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
          <button
            :class="{ active: activeView === 'evaluation' }"
            @click="activeView = 'evaluation'"
          >
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
      <KnowledgePanel
        v-model:name="kbName"
        v-model:description="kbDescription"
        :knowledge-bases="knowledgeBases"
        :selected-kb="selectedKb"
        :busy="busy"
        @create="createKnowledgeBase"
        @select="selectKnowledgeBase"
      />

      <DocumentPanel
        v-model:version="uploadVersion"
        :documents="documents"
        :knowledge-base-name="activeKnowledgeBase?.name || ''"
        :selected-kb="selectedKb"
        :busy="busy"
        @upload="uploadDocument"
        @remove="removeDocument"
      />

      <ChatPanel
        v-model:question="question"
        v-model:retrieval-mode="retrievalMode"
        v-model:top-k="topK"
        v-model:query-version="queryVersion"
        v-model:rerank="rerank"
        v-model:query-rewrite="queryRewrite"
        v-model:feedback-comment="feedbackComment"
        :busy="busy"
        :selected-kb="selectedKb"
        :answer="answer"
        :citations="citations"
        :retrieval="retrieval"
        :trace-id="traceId"
        :feedback-sent="feedbackSent"
        @ask="ask"
        @feedback="sendFeedback"
      />
    </section>

    <section v-else class="evaluation-layout">
      <EvaluationDatasetPanel v-model:selected-dataset="selectedDataset" :datasets="datasets" />

      <EvaluationEditor
        v-model:dataset-name="datasetName"
        v-model:dataset-description="datasetDescription"
        v-model:example-drafts="exampleDrafts"
        :documents="documents"
        :busy="busy"
        :selected-kb="selectedKb"
        @add-example="addExample"
        @remove-example="removeExample"
        @save="createDataset"
      />

      <EvaluationExperimentPanel
        v-model:selected-dataset="selectedDataset"
        v-model:retrieval-mode="retrievalMode"
        v-model:top-k="topK"
        v-model:query-version="queryVersion"
        v-model:baseline-evaluation-id="baselineEvaluationId"
        v-model:experiment-name="experimentName"
        v-model:rerank="rerank"
        v-model:query-rewrite="queryRewrite"
        v-model:answer-evaluation="answerEvaluation"
        :datasets="datasets"
        :completed-evaluations="completedEvaluations"
        :busy="busy"
        @run="runEvaluation"
      />

      <EvaluationHistoryPanel
        :evaluations="evaluations"
        :selected-evaluation="selectedEvaluation"
        @select="selectEvaluation"
        @refresh="loadEvaluations"
      />

      <EvaluationResultsPanel :evaluation="selectedEvaluation" :comparison="comparison" />
    </section>
  </main>
</template>
