import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { DocumentRecord } from '../api'
import DocumentPanel from './DocumentPanel.vue'

const ready: DocumentRecord = {
  id: 'doc-1',
  knowledge_base_id: 'kb-1',
  filename: '07_成员资格与出资规定.md',
  version: 'v2',
  status: 'ready',
  chunks: 4,
  progress: 100,
}

function mountPanel(overrides: Record<string, unknown> = {}) {
  return mount(DocumentPanel, {
    props: {
      version: 'latest',
      documents: [ready],
      knowledgeBaseName: '村社章程',
      selectedKb: 'kb-1',
      busy: false,
      ...overrides,
    },
  })
}

describe('DocumentPanel', () => {
  it('lists documents with version, status, chunk count and progress', () => {
    const row = mountPanel().find('tbody tr')
    expect(row.text()).toContain('07_成员资格与出资规定.md')
    expect(row.text()).toContain('v2')
    expect(row.find('.status').text()).toBe('ready')
    expect(row.find('progress').attributes('value')).toBe('100')
  })

  it('emits the picked file and clears the input so the same file can be re-picked', async () => {
    const panel = mountPanel()
    const input = panel.find('input[type="file"]')
    const element = input.element as HTMLInputElement
    const file = new File(['# 规定'], '07_成员资格与出资规定.md', { type: 'text/markdown' })
    Object.defineProperty(element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    expect(panel.emitted('upload')).toEqual([[file]])
    expect(element.value).toBe('')
  })

  it('disables uploading without a knowledge base and asks before deleting', async () => {
    const blocked = mountPanel({ selectedKb: '' })
    expect(blocked.find('input[type="file"]').attributes('disabled')).toBeDefined()

    const panel = mountPanel()
    await panel.find('.danger-link').trigger('click')
    expect(panel.emitted('remove')).toEqual([[ready]])
  })
})
