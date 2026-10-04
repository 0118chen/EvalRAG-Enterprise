import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChatPanel from './ChatPanel.vue'

function mountPanel(overrides: Record<string, unknown> = {}) {
  return mount(ChatPanel, {
    props: {
      question: '',
      retrievalMode: 'hybrid',
      topK: 5,
      queryVersion: '',
      rerank: true,
      queryRewrite: false,
      feedbackComment: '',
      busy: false,
      selectedKb: 'kb-1',
      answer: '',
      citations: [],
      retrieval: null,
      traceId: '',
      feedbackSent: false,
      ...overrides,
    },
  })
}

describe('ChatPanel', () => {
  it('asks with the current question and blocks asking without a knowledge base', async () => {
    const panel = mountPanel()
    await panel.find('.ask-button').trigger('click')
    expect(panel.emitted('ask')).toHaveLength(1)

    const blocked = mountPanel({ selectedKb: '' })
    expect(blocked.find('.ask-button').attributes('disabled')).toBeDefined()
  })

  it('writes the question back through the model prop', async () => {
    const panel = mountPanel()
    await panel.find('.composer textarea').setValue('出资额是多少')
    expect(panel.emitted('update:question')?.at(-1)).toEqual(['出资额是多少'])
  })

  it('hides the answer region until there is an answer or a citation', () => {
    expect(mountPanel().find('.answer-region').exists()).toBe(false)
    expect(mountPanel({ answer: '五十元' }).find('.answer-text').text()).toBe('五十元')
  })

  it('renders citations with their page, version and score', () => {
    const panel = mountPanel({
      answer: '见规定',
      citations: [
        {
          document_id: '07_law.docx',
          page: 3,
          version: 'v2',
          text: '证据文本',
          score: 0.5,
        },
      ],
    })
    const summary = panel.find('.citation-list summary').text()
    expect(summary).toContain('07_law.docx · 第 3 页')
    expect(summary).toContain('v2')
    expect(summary).toContain('0.5000')
    expect(panel.find('.citation-list details p').text()).toBe('证据文本')
  })

  it('sends one of the three feedback verdicts and hides the form afterwards', async () => {
    const panel = mountPanel({ answer: '答', traceId: 'trace-1' })
    const buttons = panel.findAll('.feedback-row button')
    expect(buttons.map((button) => button.text())).toEqual(['回答正确', '引用有误', '回答错误'])
    await buttons[1].trigger('click')
    expect(panel.emitted('feedback')).toEqual([['citation_error']])

    const recorded = mountPanel({ answer: '答', traceId: 'trace-1', feedbackSent: true })
    expect(recorded.find('.feedback-row button').exists()).toBe(false)
    expect(recorded.find('.success-text').text()).toBe('反馈已记录')
  })

  it('shows retrieval diagnostics when the backend reports them', () => {
    const panel = mountPanel({
      answer: '候选 12 的来源',
      retrieval: {
        candidate_count: 12,
        cache_hit: true,
        reranked: true,
        document_version: 'v2',
        rewritten_queries: ['a', 'b'],
      },
    })
    const diagnostics = panel.find('.diagnostics').text()
    expect(diagnostics).toContain('候选 12')
    expect(diagnostics).toContain('缓存命中')
    expect(diagnostics).toContain('Query 2 路')
  })
})
