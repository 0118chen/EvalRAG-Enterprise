import { describe, expect, it } from 'vitest'
import type { EvaluationJob } from './api'
import { formatMetric, metricEntries, metricLabel } from './metrics'

function jobWith(metrics: Record<string, unknown>): EvaluationJob {
  return { id: 'job-1', results: { metrics } } as unknown as EvaluationJob
}

describe('metric presentation', () => {
  it('keeps numeric metrics in API order and drops the rest', () => {
    const job = jobWith({ recall_at_5: 0.5, note: 'n/a', page_hit: 1, example_count: 4 })
    expect(metricEntries(job).map(([key]) => key)).toEqual([
      'recall_at_5',
      'page_hit',
      'example_count',
    ])
  })

  it('returns nothing for an unfinished job', () => {
    expect(metricEntries(null)).toEqual([])
    expect(metricEntries({ id: 'job-2' } as unknown as EvaluationJob)).toEqual([])
  })

  it('labels known metrics and falls back to the raw key', () => {
    expect(metricLabel('recall_at_5')).toBe('Recall@5')
    expect(metricLabel('page_hit')).toBe('页码命中')
    expect(metricLabel('brand_new_metric')).toBe('brand_new_metric')
  })

  it('formats counts as integers, latency in ms and scores to 3 decimals', () => {
    expect(formatMetric('example_count', 4.4)).toBe('4')
    expect(formatMetric('completed_example_count', 4.6)).toBe('5')
    expect(formatMetric('latency_ms', 12.3456)).toBe('12.3')
    expect(formatMetric('recall_at_5', 0.5)).toBe('0.500')
  })
})
