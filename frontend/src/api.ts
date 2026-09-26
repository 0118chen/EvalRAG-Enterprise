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

export type SearchPayload = {
  tenant_id: string
  knowledge_base_id: string
  question: string
  top_k: number
  retrieval_mode: 'dense' | 'sparse' | 'hybrid'
  document_version: string
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

type StreamHandlers = {
  onText: (text: string) => void
  onCitations: (items: Citation[]) => void
  onRetrieval?: (diagnostics: RetrievalDiagnostics) => void
  onTrace?: (traceId: string) => void
}

function requestHeaders(json = false): HeadersInit {
  const headers: Record<string, string> = {}
  const apiKey = localStorage.getItem('evalrag_api_key')
  if (apiKey) headers['X-API-Key'] = apiKey
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

  async listDocuments(
    tenantId: string,
    knowledgeBaseId: string,
  ): Promise<DocumentRecord[]> {
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
    let buffer = ''
    let event = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          if (!line.startsWith('data:')) continue
          const data = line.slice(5).trimStart()
          if (data === '[DONE]') continue
          if (event === 'citations') handlers.onCitations(JSON.parse(data))
          if (event === 'retrieval') handlers.onRetrieval?.(JSON.parse(data))
          if (event === 'trace') handlers.onTrace?.(JSON.parse(data).trace_id)
          if (event === 'token') handlers.onText(JSON.parse(data))
        }
        event = ''
      }
    }
  },

  async feedback(
    tenantId: string,
    traceId: string,
    feedback: string,
    comment = '',
  ): Promise<void> {
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
    const response = await fetch(
      `/api/v1/evaluations?tenant_id=${encodeURIComponent(tenantId)}`,
      { headers: requestHeaders() },
    )
    return (await checked(response)).json()
  },

  async createEvaluation(payload: {
    tenant_id: string
    knowledge_base_id: string
    dataset_name: string
    retrieval_mode: SearchPayload['retrieval_mode']
    top_k: number
    document_version: string
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
