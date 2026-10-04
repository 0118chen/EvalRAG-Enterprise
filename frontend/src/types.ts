// Types shared by the shell and the evaluation editor components.

/** One row of the "新建数据集" editor, before it is trimmed and posted. */
export type ExampleDraft = {
  question: string
  expected_answer: string
  expected_document_id: string
  expected_page?: number
  category: string
}

export function emptyExampleDraft(): ExampleDraft {
  return {
    question: '',
    expected_answer: '',
    expected_document_id: '',
    expected_page: undefined,
    category: 'general',
  }
}
