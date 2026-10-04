import { describe, expect, it } from 'vitest'
import { SSE_DONE, SseDecoder, parseSseBlock } from './sse'

describe('parseSseBlock', () => {
  it('reads the event name and the data line', () => {
    expect(parseSseBlock('event: token\ndata: {"text":"你"}')).toEqual({
      event: 'token',
      data: '{"text":"你"}',
    })
  })

  it('defaults to the message event when no name is given', () => {
    expect(parseSseBlock('data: hello')?.event).toBe('message')
  })

  it('joins multiple data lines with newlines per the SSE spec', () => {
    expect(parseSseBlock('event: token\ndata: one\ndata: two')?.data).toBe('one\ntwo')
  })

  it('ignores comment/keepalive frames', () => {
    expect(parseSseBlock(': ping')).toBeNull()
    expect(parseSseBlock('')).toBeNull()
  })

  it('passes the done sentinel through untouched', () => {
    expect(parseSseBlock(`data: ${SSE_DONE}`)).toEqual({ event: 'message', data: SSE_DONE })
  })
})

describe('SseDecoder', () => {
  it('emits whole events and buffers a partial frame', () => {
    const decoder = new SseDecoder()
    expect(decoder.push('event: token\ndata: par')).toEqual([])
    expect(decoder.pending).toBe('event: token\ndata: par')
    expect(decoder.push('tial\n\n')).toEqual([{ event: 'token', data: 'partial' }])
    expect(decoder.pending).toBe('')
  })

  it('decodes several events out of one chunk', () => {
    const decoder = new SseDecoder()
    const events = decoder.push('event: token\ndata: a\n\nevent: token\ndata: b\n\n')
    expect(events.map((event) => event.data)).toEqual(['a', 'b'])
  })

  it('handles CRLF frames even when the terminator is split across chunks', () => {
    const decoder = new SseDecoder()
    expect(decoder.push('event: token\r\ndata: hi\r')).toEqual([])
    expect(decoder.push('\n\r\n')).toEqual([{ event: 'token', data: 'hi' }])
  })

  it('keeps a trailing keepalive out of the event stream', () => {
    const decoder = new SseDecoder()
    expect(decoder.push(': ping\n\ndata: ok\n\n')).toEqual([{ event: 'message', data: 'ok' }])
  })

  it('handles a chunk that ends exactly on the CRLF boundary', () => {
    const decoder = new SseDecoder()
    expect(decoder.push('data: one\r\n\r\ndata: two\r\n')).toEqual([
      { event: 'message', data: 'one' },
    ])
    expect(decoder.push('\r\n')).toEqual([{ event: 'message', data: 'two' }])
  })
})
