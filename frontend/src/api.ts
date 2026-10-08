import { getApiKey, getSessionToken } from './auth'
import { SSE_DONE, SseDecoder } from './sse'

export type KnowledgeBase = {
  id: string
  tenant_id: string
  name: string
  description: string
}

export type DocumentRecord = {
  id: string
  filename: string
  knowledge_base_id: string
  chunks: number
  status: string
  progress: number
  error_message?: string
  version: string
}

export type Citation = {
  document_id: string
  page: number
  text: string
  version: string
  score?: number
}

export type RetrievalDiagnostics = {
  cache_hit: boolean
  rewritten_queries: string[]
  candidate_count: number
  reranked: boolean
  document_version: string | null
}

// null means "every version": the backend treats a null/blank version as no filter.
export type SearchPayload = {
  tenant_id: string
  knowledge_base_id: string
  question: string
  top_k: number
  retrieval_mode: 'dense' | 'sparse' | 'hybrid'
  document_version: string | null
  rerank: boolean
  query_rewrite: boolean
}

export type SearchResponse = {
  answer: string
  citations: Citation[]
  trace_id: string
  retrieval: RetrievalDiagnostics
}

export type EvaluationExample = {
  id: string
  dataset_id: string
  question: string
  expected_answer?: string
  expected_document_id: string
  expected_page?: number
  category: string
}

export type EvaluationDataset = {
  id: string
  tenant_id: string
  knowledge_base_id: string
  name: string
  description: string
  examples: EvaluationExample[]
  created_at?: string
}

export type EvaluationJob = {
  id: string
  tenant_id: string
  knowledge_base_id: string
  dataset_id?: string
  dataset_name: string
  retrieval_mode: string
  top_k: number
  document_version?: string
  experiment_name?: string
  baseline_evaluation_id?: string
  status: string
  results?: {
    metrics?: Record<string, number>
    examples?: Array<Record<string, unknown>>
    baseline_diff?: Record<string, number>
    langsmith?: Record<string, unknown>
  }
  error_message?: string
  created_at?: string
  completed_at?: string
}

/** The short-lived bearer token handed out by `POST /api/v1/auth/session`. */
export type SessionToken = {
  token: string
  token_type: string
  tenant_id: string
  expires_at: string
  expires_in: number
  /** False when the deployment runs with AUTH_ENABLED off: a session, but unproven. */
  authenticated: boolean
}

/** A session as an operator sees it: lifecycle and identity, never the secret. */
export type SessionInfo = {
  id: string
  tenant_id: string
  key_fingerprint: string
  created_at: string | null
  expires_at: string
  last_used_at: string | null
  revoked_at: string | null
  current: boolean
}

export type StreamHandlers = {
  onText: (text: string) => void
  onCitations: (items: Citation[]) => void
  onRetrieval?: (diagnostics: RetrievalDiagnostics) => void
  onTrace?: (traceId: string) => void
  /** Terminal `event: error` frame (refused answer, generation failure). */
  onError?: (message: string) => void
}

function requestHeaders(json = false): HeadersInit {
  const headers: Record<string, string> = {}
  const token = getSessionToken()
  if (token) {
    // Everything after the exchange authenticates with the session token, so a leaked
    // request log never contains the long-lived key.
    headers['Authorization'] = `Bearer ${token}`
  } else {
    const apiKey = getApiKey()
    if (apiKey) headers['X-API-Key'] = apiKey
  }
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.detail || `请求失败：${response.status}`)
  }
  return response
}

export const api = {
  /**
   * Trade the long-lived API key for a short-lived session token.
   *
   * This is the one call that carries `X-API-Key`; every protected call after it uses
   * `Authorization: Bearer <token>`. The tenant is optional because with AUTH_ENABLED on
   * the server reads it from the key and rejects a mismatch with a 403.
   */
  async createSession(payload: { apiKey?: string; tenantId?: string } = {}): Promise<SessionToken> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const apiKey = (payload.apiKey || getApiKey()).trim()
    if (apiKey) headers['X-API-Key'] = apiKey
    const response = await fetch('/api/v1/auth/session', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload.tenantId ? { tenant_id: payload.tenantId } : {}),
    })
    return (await checked(response)).json()
  },

  /** Describe the session behind the stored token; 401 once it expired or was revoked. */
  async getSession(): Promise<SessionInfo> {
    const response = await fetch('/api/v1/auth/session', { headers: requestHeaders() })
    return (await checked(response)).json()
  },

  /** Revoke the session behind the stored token: logging out ends the credential. */
  async revokeSession(): Promise<void> {
    const response = await fetch('/api/v1/auth/session', {
      method: 'DELETE',
      headers: requestHeaders(),
    })
    await checked(response)
  },

  async listKnowledgeBases(tenantId: string): Promise<KnowledgeBase[]> {
    const response = await fetch(
      `/api/v1/knowledge-bases?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async createKnowledgeBase(
    tenantId: string,
    name: string,
    description = '',
  ): Promise<KnowledgeBase> {
    const response = await fetch(
      `/api/v1/knowledge-bases?tenant_id=${encodeURIComponent(tenantId)}`,
      {
        method: 'POST',
        headers: requestHeaders(true),
        body: JSON.stringify({ name, description }),
      },
    )
    return (await checked(response)).json()
  },

  async listDocuments(tenantId: string, knowledgeBaseId: string): Promise<DocumentRecord[]> {
    const response = await fetch(
      `/api/v1/knowledge-bases/${knowledgeBaseId}/documents?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async upload(
    tenantId: string,
    knowledgeBaseId: string,
    file: File,
    version: string,
  ): Promise<DocumentRecord> {
    const body = new FormData()
    body.append('tenant_id', tenantId)
    body.append('knowledge_base_id', knowledgeBaseId)
    body.append('version', version)
    body.append('file', file)
    const response = await fetch('/api/v1/documents', {
      method: 'POST',
      headers: requestHeaders(),
      body,
    })
    return (await checked(response)).json()
  },

  async deleteDocument(tenantId: string, id: string): Promise<void> {
    const response = await fetch(
      `/api/v1/documents/${id}?tenant_id=${encodeURIComponent(tenantId)}`,
      { method: 'DELETE', headers: requestHeaders() },
    )
    await checked(response)
  },

  async document(tenantId: string, id: string): Promise<DocumentRecord> {
    const response = await fetch(
      `/api/v1/documents/${id}?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async search(payload: SearchPayload): Promise<SearchResponse> {
    const response = await fetch('/api/v1/retrieval/search', {
      method: 'POST',
      headers: requestHeaders(true),
      body: JSON.stringify(payload),
    })
    return (await checked(response)).json()
  },

  async streamChat(payload: SearchPayload, handlers: StreamHandlers): Promise<void> {
    const response = await fetch('/api/v1/chat/stream', {
      method: 'POST',
      headers: requestHeaders(true),
      body: JSON.stringify(payload),
    })
    const reader = (await checked(response)).body?.getReader()
    if (!reader) throw new Error('浏览器不支持流式响应')
    const decoder = new TextDecoder()
    const sse = new SseDecoder()
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      for (const event of sse.push(decoder.decode(value, { stream: true }))) {
        if (event.data === SSE_DONE) continue
        if (event.event === 'citations') handlers.onCitations(JSON.parse(event.data))
        if (event.event === 'retrieval') handlers.onRetrieval?.(JSON.parse(event.data))
        if (event.event === 'trace') handlers.onTrace?.(JSON.parse(event.data).trace_id)
        if (event.event === 'token') handlers.onText(JSON.parse(event.data))
        if (event.event === 'error') {
          const payload = JSON.parse(event.data) as { message?: string }
          handlers.onError?.(payload.message || '回答流式传输失败')
        }
      }
    }
  },

  async feedback(tenantId: string, traceId: string, feedback: string, comment = ''): Promise<void> {
    const response = await fetch('/api/v1/feedback', {
      method: 'POST',
      headers: requestHeaders(true),
      body: JSON.stringify({ tenant_id: tenantId, trace_id: traceId, feedback, comment }),
    })
    await checked(response)
  },

  async listDatasets(tenantId: string): Promise<EvaluationDataset[]> {
    const response = await fetch(
      `/api/v1/evaluation-datasets?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async createDataset(payload: {
    tenant_id: string
    knowledge_base_id: string
    name: string
    description: string
    examples: Array<{
      question: string
      expected_answer?: string
      expected_document_id: string
      expected_page?: number
      category: string
    }>
  }): Promise<EvaluationDataset> {
    const response = await fetch('/api/v1/evaluation-datasets', {
      method: 'POST',
      headers: requestHeaders(true),
      body: JSON.stringify(payload),
    })
    return (await checked(response)).json()
  },

  async listEvaluations(tenantId: string): Promise<EvaluationJob[]> {
    const response = await fetch(`/api/v1/evaluations?tenant_id=${encodeURIComponent(tenantId)}`, {
      headers: requestHeaders(),
    })
    return (await checked(response)).json()
  },

  async createEvaluation(payload: {
    tenant_id: string
    knowledge_base_id: string
    dataset_name: string
    retrieval_mode: SearchPayload['retrieval_mode']
    top_k: number
    document_version: string | null
    rerank: boolean
    query_rewrite: boolean
    answer_evaluation?: boolean
    baseline_evaluation_id?: string
    experiment_name?: string
  }): Promise<EvaluationJob> {
    const response = await fetch('/api/v1/evaluations', {
      method: 'POST',
      headers: requestHeaders(true),
      body: JSON.stringify(payload),
    })
    return (await checked(response)).json()
  },

  async evaluation(id: string, tenantId: string): Promise<EvaluationJob> {
    const response = await fetch(
      `/api/v1/evaluations/${id}?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async compareEvaluation(
    id: string,
    tenantId: string,
    baselineId?: string,
  ): Promise<Record<string, unknown>> {
    const query = new URLSearchParams({ tenant_id: tenantId })
    if (baselineId) query.set('baseline_id', baselineId)
    const response = await fetch(`/api/v1/evaluations/${id}/compare?${query}`, {
      headers: requestHeaders(),
    })
    return (await checked(response)).json()
  },
}
