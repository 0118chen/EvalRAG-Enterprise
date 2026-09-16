export type KnowledgeBase = { id: string; tenant_id: string; name: string; description: string }
export type DocumentRecord = { id: string; filename: string; knowledge_base_id: string; chunks: number; status: string; progress: number; error_message?: string }
export type Citation = { document_id: string; page: number; text: string; score?: number }

async function checked(response: Response) {
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `请求失败：${response.status}`)
  return response
}

export const api = {
  async listKnowledgeBases(tenantId: string): Promise<KnowledgeBase[]> {
    return (await checked(await fetch(`/api/v1/knowledge-bases?tenant_id=${encodeURIComponent(tenantId)}`))).json()
  },
  async createKnowledgeBase(tenantId: string, name: string): Promise<KnowledgeBase> {
    return (await checked(await fetch(`/api/v1/knowledge-bases?tenant_id=${encodeURIComponent(tenantId)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }))).json()
  },
  async listDocuments(tenantId: string, kbId: string): Promise<DocumentRecord[]> {
    return (await checked(await fetch(`/api/v1/knowledge-bases/${kbId}/documents?tenant_id=${encodeURIComponent(tenantId)}`))).json()
  },
  async upload(tenantId: string, kbId: string, file: File): Promise<DocumentRecord> {
    const body = new FormData(); body.append('tenant_id', tenantId); body.append('knowledge_base_id', kbId); body.append('file', file)
    return (await checked(await fetch('/api/v1/documents', { method: 'POST', body }))).json()
  },
  async document(tenantId: string, id: string): Promise<DocumentRecord> {
    return (await checked(await fetch(`/api/v1/documents/${id}?tenant_id=${encodeURIComponent(tenantId)}`))).json()
  },
  async feedback(traceId: string, feedback: string) {
    await checked(await fetch('/api/v1/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ trace_id: traceId, feedback, comment: '' }) }))
  },
  async search(payload: object): Promise<{ citations: Citation[]; trace_id: string }> {
    return (await checked(await fetch('/api/v1/retrieval/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }))).json()
  },
  async streamChat(payload: object, onText: (text: string) => void, onCitations: (items: Citation[]) => void) {
    const response = await checked(await fetch('/api/v1/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }))
    const reader = response.body!.getReader(); const decoder = new TextDecoder(); let buffer = ''; let event = ''
    while (true) {
      const { value, done } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n'); buffer = blocks.pop() || ''
      for (const block of blocks) {
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          if (line.startsWith('data:')) {
            const data = line.slice(5).trimStart(); if (data === '[DONE]') continue
            if (event === 'citations') onCitations(JSON.parse(data)); else onText(data)
          }
        }
        event = ''
      }
    }
  },
}
