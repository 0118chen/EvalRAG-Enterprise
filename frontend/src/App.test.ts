import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  Citation,
  DocumentRecord,
  EvaluationDataset,
  EvaluationJob,
  KnowledgeBase,
  RetrievalDiagnostics,
  SearchResponse,
  SessionInfo,
  SessionToken,
} from './api'
import { TENANT_STORAGE_KEY, getSessionToken } from './auth'

// App.vue is the only place that knows the difference between "this tab has a tenant" and
// "this tab can talk to the API". That difference is what these tests pin: the workspace
// opens on a server-minted session token (not on a stored tenant, not on a stored key), the
// key never reaches storage, and a session the server no longer honours falls back to the
// login form instead of a shell full of 401s.

const mockApi = vi.hoisted(() => ({
  createSession: vi.fn(),
  getSession: vi.fn(),
  revokeSession: vi.fn(),
  listKnowledgeBases: vi.fn(),
  createKnowledgeBase: vi.fn(),
  listDocuments: vi.fn(),
  upload: vi.fn(),
  deleteDocument: vi.fn(),
  document: vi.fn(),
  search: vi.fn(),
  streamChat: vi.fn(),
  feedback: vi.fn(),
  listDatasets: vi.fn(),
  createDataset: vi.fn(),
  listEvaluations: vi.fn(),
  createEvaluation: vi.fn(),
  evaluation: vi.fn(),
  compareEvaluation: vi.fn(),
}))

vi.mock('./api', () => ({ api: mockApi }))

import App from './App.vue'

const TENANT = 'demo-enterprise'
const TOKEN = 'ers_5f3c1d'
const LEGACY_API_KEY = 'evalrag_api_key'
const LEGACY_SESSION_API_KEY = 'evalrag_api_key_session'
const SESSION_TOKEN_KEY = 'evalrag_session_token'

const SESSION: SessionToken = {
  token: TOKEN,
  token_type: 'Bearer',
  tenant_id: TENANT,
  expires_at: '2026-10-08T00:00:00',
  expires_in: 3600,
  authenticated: true,
}

const SESSION_INFO: SessionInfo = {
  id: 'sess-1',
  tenant_id: TENANT,
  key_fingerprint: 'ab12cd',
  created_at: '2026-10-07T23:00:00',
  expires_at: '2026-10-08T00:00:00',
  last_used_at: null,
  revoked_at: null,
  current: true,
}

const KB: KnowledgeBase = {
  id: 'kb-1',
  tenant_id: TENANT,
  name: '合规知识库',
  description: '制度与合同',
}

const DOCUMENT: DocumentRecord = {
  id: 'doc-1',
  filename: '07_law.docx',
  knowledge_base_id: 'kb-1',
  chunks: 12,
  status: 'ready',
  progress: 100,
  version: 'v2',
}

const DATASET: EvaluationDataset = {
  id: 'ds-1',
  tenant_id: TENANT,
  knowledge_base_id: 'kb-1',
  name: '回归集',
  description: '',
  examples: [],
}

const EVALUATION: EvaluationJob = {
  id: 'eval-1',
  tenant_id: TENANT,
  knowledge_base_id: 'kb-1',
  dataset_name: '回归集',
  retrieval_mode: 'hybrid',
  top_k: 5,
  status: 'completed',
}

const RETRIEVAL: RetrievalDiagnostics = {
  cache_hit: false,
  rewritten_queries: ['注册资本是多少'],
  candidate_count: 4,
  reranked: true,
  document_version: 'v2',
}

const CITATION: Citation = {
  document_id: '07_law.docx',
  page: 3,
  text: '注册资本为五十元',
  version: 'v2',
  score: 0.87,
}

const SEARCH: SearchResponse = {
  answer: '',
  citations: [CITATION],
  trace_id: 'trace-1',
  retrieval: RETRIEVAL,
}

async function settle() {
  await flushPromises()
  await flushPromises()
}

function loginInputs(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.login-panel input')
}

async function fillLogin(
  wrapper: ReturnType<typeof mount>,
  options: { apiKey?: string; remember?: boolean } = {},
) {
  const inputs = loginInputs(wrapper)
  await inputs[0].setValue(TENANT)
  await inputs[1].setValue(options.apiKey ?? 'secret-key')
  await inputs[2].setValue(options.remember ?? true)
  await wrapper.find('.login-panel button.primary').trigger('click')
  await settle()
}

/** A tab that already holds a token for this tenant, as if it had logged in earlier. */
function signIn(tenant = TENANT, token = TOKEN) {
  localStorage.setItem(TENANT_STORAGE_KEY, tenant)
  sessionStorage.setItem(SESSION_TOKEN_KEY, token)
}

describe('App shell', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    mockApi.listKnowledgeBases.mockResolvedValue([])
    mockApi.listDocuments.mockResolvedValue([])
    mockApi.listDatasets.mockResolvedValue([])
    mockApi.listEvaluations.mockResolvedValue([])
    mockApi.search.mockResolvedValue(SEARCH)
    mockApi.streamChat.mockResolvedValue(undefined)
    mockApi.createSession.mockResolvedValue(SESSION)
    mockApi.getSession.mockResolvedValue(SESSION_INFO)
    mockApi.revokeSession.mockResolvedValue(undefined)
  })

  it('shows the login form while the tab holds no session', async () => {
    const wrapper = mount(App)
    await settle()
    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(wrapper.find('.app-shell').exists()).toBe(false)
    expect(mockApi.getSession).not.toHaveBeenCalled()
    expect(mockApi.listKnowledgeBases).not.toHaveBeenCalled()
  })

  it('exchanges the API key for a token and stores only the token', async () => {
    localStorage.setItem(LEGACY_API_KEY, 'legacy-key')
    localStorage.setItem(LEGACY_SESSION_API_KEY, 'legacy-key')
    const wrapper = mount(App)
    await fillLogin(wrapper, { apiKey: 'secret-key' })

    expect(mockApi.createSession).toHaveBeenCalledWith({
      apiKey: 'secret-key',
      tenantId: TENANT,
    })
    expect(wrapper.find('.login-page').exists()).toBe(false)
    expect(wrapper.find('.app-shell').exists()).toBe(true)
    expect(wrapper.find('.tenant-badge').text()).toBe(TENANT)
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBe(TOKEN)
    expect(getSessionToken()).toBe(TOKEN)
    expect(localStorage.getItem(LEGACY_API_KEY)).toBeNull()
    expect(localStorage.getItem(LEGACY_SESSION_API_KEY)).toBeNull()
    expect(Object.keys(sessionStorage)).toEqual([SESSION_TOKEN_KEY])
    expect(Object.keys(localStorage)).toEqual([TENANT_STORAGE_KEY])
  })

  it('keeps the token in memory only when tab storage is declined', async () => {
    const wrapper = mount(App)
    await fillLogin(wrapper, { apiKey: 'secret-key', remember: false })

    expect(wrapper.find('.app-shell').exists()).toBe(true)
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull()
    expect(getSessionToken()).toBe(TOKEN)
    expect(mockApi.listKnowledgeBases).toHaveBeenCalledWith(TENANT)
  })

  it('stays on the login form when the exchange is refused', async () => {
    mockApi.createSession.mockRejectedValue(
      new Error('valid X-API-Key is required to open a session'),
    )
    const wrapper = mount(App)
    await fillLogin(wrapper, { apiKey: 'wrong-key' })

    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(wrapper.find('.app-shell').exists()).toBe(false)
    expect(wrapper.find('.login-panel .alert.error').text()).toBe(
      'valid X-API-Key is required to open a session',
    )
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(TENANT_STORAGE_KEY)).toBeNull()
    expect(mockApi.listKnowledgeBases).not.toHaveBeenCalled()
  })

  it('validates the stored session and loads the workspace on mount', async () => {
    signIn()
    mockApi.listKnowledgeBases.mockResolvedValue([KB])
    mockApi.listDocuments.mockResolvedValue([DOCUMENT])
    mockApi.listDatasets.mockResolvedValue([DATASET])
    mockApi.listEvaluations.mockResolvedValue([EVALUATION])
    const wrapper = mount(App)
    await settle()

    expect(mockApi.getSession).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.app-shell').exists()).toBe(true)
    expect(wrapper.find('.login-page').exists()).toBe(false)
    expect(mockApi.listKnowledgeBases).toHaveBeenCalledWith(TENANT)
    expect(wrapper.find('.knowledge-panel .list-item').text()).toContain('合规知识库')
    expect(mockApi.listDocuments).toHaveBeenCalledWith(TENANT, 'kb-1')
    expect(wrapper.find('.document-panel tbody tr strong').text()).toBe('07_law.docx')
    expect(mockApi.listDatasets).toHaveBeenCalledWith(TENANT)
    expect(mockApi.listEvaluations).toHaveBeenCalledWith(TENANT)
  })

  it('falls back to the login form when the stored session is refused', async () => {
    signIn()
    mockApi.getSession.mockRejectedValue(new Error('session token has expired'))
    const wrapper = mount(App)
    await settle()

    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(wrapper.find('.app-shell').exists()).toBe(false)
    expect(wrapper.find('.login-panel .alert.error').text()).toBe('登录状态已失效，请重新登录')
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(TENANT_STORAGE_KEY)).toBeNull()
    expect(mockApi.listKnowledgeBases).not.toHaveBeenCalled()
  })

  it('revokes the session and forgets it on logout', async () => {
    signIn()
    mockApi.listKnowledgeBases.mockResolvedValue([KB])
    const wrapper = mount(App)
    await settle()
    await wrapper.find('.topbar button.ghost').trigger('click')
    await settle()

    expect(mockApi.revokeSession).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(wrapper.find('.app-shell').exists()).toBe(false)
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(TENANT_STORAGE_KEY)).toBeNull()
  })

  it('logs out locally even when the revocation call fails', async () => {
    signIn()
    mockApi.revokeSession.mockRejectedValue(new Error('网络不可用'))
    const wrapper = mount(App)
    await settle()
    await wrapper.find('.topbar button.ghost').trigger('click')
    await settle()

    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(sessionStorage.getItem(SESSION_TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(TENANT_STORAGE_KEY)).toBeNull()
  })

  it('renders the answer and citations delivered as SSE frames', async () => {
    signIn()
    mockApi.listKnowledgeBases.mockResolvedValue([KB])
    mockApi.streamChat.mockImplementation(
      async (
        _payload: unknown,
        handlers: { onCitations?: (items: Citation[]) => void; onText: (text: string) => void },
      ) => {
        handlers.onCitations?.([CITATION])
        handlers.onText('注册资本为')
        handlers.onText('五十元')
      },
    )
    const wrapper = mount(App)
    await settle()
    await wrapper.find('.composer textarea').setValue('注册资本是多少？')
    await wrapper.find('.ask-button').trigger('click')
    await settle()

    expect(wrapper.find('.answer-region').exists()).toBe(true)
    expect(wrapper.find('.answer-text').text()).toBe('注册资本为五十元')
    expect(wrapper.find('.citation-list summary').text()).toContain('07_law.docx')
  })

  it('shows the message of a terminal SSE error frame', async () => {
    signIn()
    mockApi.listKnowledgeBases.mockResolvedValue([KB])
    mockApi.search.mockResolvedValue({ ...SEARCH, citations: [] })
    mockApi.streamChat.mockImplementation(
      async (_payload: unknown, handlers: { onError?: (message: string) => void }) => {
        handlers.onError?.('回答生成失败')
      },
    )
    const wrapper = mount(App)
    await settle()
    await wrapper.find('.composer textarea').setValue('注册资本是多少？')
    await wrapper.find('.ask-button').trigger('click')
    await settle()

    expect(wrapper.find('.alert.error').text()).toBe('回答生成失败')
    expect(wrapper.find('.answer-region').exists()).toBe(false)
  })

  it('surfaces a rejected ask call through the error banner', async () => {
    signIn()
    mockApi.listKnowledgeBases.mockResolvedValue([KB])
    mockApi.search.mockRejectedValue(new Error('请求失败：500'))
    const wrapper = mount(App)
    await settle()
    await wrapper.find('.composer textarea').setValue('注册资本是多少？')
    await wrapper.find('.ask-button').trigger('click')
    await settle()

    expect(wrapper.find('.alert.error').text()).toBe('请求失败：500')
  })

  it('drops keys left behind by an older build on mount', async () => {
    localStorage.setItem(LEGACY_API_KEY, 'legacy-key')
    localStorage.setItem(LEGACY_SESSION_API_KEY, 'legacy-key')
    mount(App)
    await settle()

    expect(localStorage.getItem(LEGACY_API_KEY)).toBeNull()
    expect(localStorage.getItem(LEGACY_SESSION_API_KEY)).toBeNull()
  })
})
