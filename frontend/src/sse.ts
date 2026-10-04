// Server-sent-event decoding for the chat stream.
//
// This is the one piece of the browser client that has real parsing logic
// (`retrieval` / `citations` / `token` / `trace` / `error` frames, then
// `[DONE]`), and it used to be spread inline over the fetch loop in api.ts,
// where it could not be tested without a live backend. It is now a small
// incremental decoder: feed it decoded chunks, get whole events out.

export const SSE_DONE = '[DONE]'

export type SseEvent = {
  /** SSE `event:` field, or `message` when the frame does not name one. */
  event: string
  /** All `data:` lines of the frame, joined with newlines per the SSE spec. */
  data: string
}

/**
 * Parse one complete SSE frame (the text between two blank lines).
 *
 * Returns `null` for frames that carry no data (comments/keepalives such as
 * `: ping`), which is how the server keeps proxies from closing idle streams.
 */
export function parseSseBlock(block: string): SseEvent | null {
  let event = ''
  const data: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice('event:'.length).trim()
    else if (line.startsWith('data:')) data.push(line.slice('data:'.length).trimStart())
  }
  if (!data.length) return null
  return { event: event || 'message', data: data.join('\n') }
}

/**
 * Reassemble frames from arbitrarily split chunks.
 *
 * A chunk boundary can fall anywhere, including between the two newlines that
 * end a frame (and between the `\r` and `\n` of a CRLF pair), so the decoder
 * keeps the tail of an incomplete frame buffered until the rest arrives.
 */
export class SseDecoder {
  private buffer = ''

  push(chunk: string): SseEvent[] {
    this.buffer += chunk
    const events: SseEvent[] = []
    for (let boundary = this.nextBoundary(); boundary; boundary = this.nextBoundary()) {
      const frame = this.buffer.slice(0, boundary.index)
      this.buffer = this.buffer.slice(boundary.index + boundary.length)
      const parsed = parseSseBlock(frame)
      if (parsed) events.push(parsed)
    }
    return events
  }

  /** Text of a frame that has not been terminated yet (diagnostics/tests). */
  get pending(): string {
    return this.buffer
  }

  private nextBoundary(): { index: number; length: number } | null {
    const lf = this.buffer.indexOf('\n\n')
    const crlf = this.buffer.indexOf('\r\n\r\n')
    if (lf < 0 && crlf < 0) return null
    if (crlf >= 0 && (lf < 0 || crlf < lf)) return { index: crlf, length: 4 }
    return { index: lf, length: 2 }
  }
}
