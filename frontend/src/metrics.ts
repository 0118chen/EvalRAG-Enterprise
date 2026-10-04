// Presentation of evaluation metrics returned by the API.
//
// Kept out of the components so the label/format rules are one testable place:
// the backend keeps adding metrics (answer quality, passage-level hits, timing),
// and an unlabelled key silently shows up as a raw identifier in the UI.

import type { EvaluationJob } from './api'

const METRIC_LABELS: Record<string, string> = {
  recall_at_1: 'Recall@1',
  recall_at_3: 'Recall@3',
  recall_at_5: 'Recall@5',
  precision_at_3: 'Precision@3',
  precision_at_5: 'Precision@5',
  ndcg_at_3: 'nDCG@3',
  ndcg_at_5: 'nDCG@5',
  mrr: 'MRR',
  page_hit: '页码命中',
  passage_hit: '段落命中',
  passage_at_1: '段落@1',
  quote_hit: '证据引用命中',
  all_targets_at_5: '全部目标@5',
  any_target_at_5: '任一目标@5',
  answer_correctness: '答案正确性',
  answer_faithfulness: '答案忠实度',
  answer_completeness: '答案完整性',
  latency_ms: '平均耗时(ms)',
  example_count: '样例数',
  completed_example_count: '已完成样例',
  timed_out_example_count: '超时样例',
}

const INTEGER_METRICS = new Set(['example_count', 'completed_example_count', 'timed_out_example_count'])
const MILLISECOND_METRICS = new Set(['latency_ms', 'latency_p50_ms', 'latency_p95_ms'])

/** Numeric metrics of a finished job, in the order the API returned them. */
export function metricEntries(job: EvaluationJob | null): Array<[string, number]> {
  const metrics = job?.results?.metrics || {}
  return Object.entries(metrics).filter(([, value]) => typeof value === 'number')
}

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] || key
}

export function formatMetric(key: string, value: number): string {
  if (INTEGER_METRICS.has(key)) return String(Math.round(value))
  if (MILLISECOND_METRICS.has(key)) return value.toFixed(1)
  return value.toFixed(3)
}
