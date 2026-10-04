import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearApiKey,
  clearTenantId,
  getApiKey,
  loadApiKey,
  loadTenantId,
  saveTenantId,
  setApiKey,
} from './auth'

// The API key used to live in localStorage forever, which means any XSS or any
// later user of the machine could read it back. These tests pin the new rule:
// localStorage is only ever read for the tenant id, and the legacy key is
// deleted on sight rather than being migrated forward.

const LEGACY_KEY = 'evalrag_api_key'

describe('auth storage', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearApiKey()
  })

  it('keeps the tenant id in localStorage across reloads', () => {
    saveTenantId('demo-enterprise')
    expect(loadTenantId()).toBe('demo-enterprise')
    clearTenantId()
    expect(loadTenantId()).toBe('')
  })

  it('deletes the legacy localStorage API key instead of adopting it', () => {
    localStorage.setItem(LEGACY_KEY, 'legacy-key')
    expect(loadApiKey()).toBe('')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(getApiKey()).toBe('')
  })

  it('never shadows a session key with the legacy localStorage key', () => {
    sessionStorage.setItem('evalrag_api_key_session', 'fresh-key')
    localStorage.setItem(LEGACY_KEY, 'legacy-key')
    expect(loadApiKey()).toBe('fresh-key')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('keeps a non-remembered key in memory only', () => {
    setApiKey('  secret  ', { remember: false })
    expect(getApiKey()).toBe('secret')
    expect(loadApiKey()).toBe('')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(sessionStorage.getItem('evalrag_api_key_session')).toBeNull()
  })

  it('stores a remembered key in sessionStorage only', () => {
    setApiKey('secret', { remember: true })
    expect(loadApiKey()).toBe('secret')
    expect(sessionStorage.getItem('evalrag_api_key_session')).toBe('secret')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(Object.keys(localStorage)).toEqual([])
  })

  it('clears storage and memory together', () => {
    setApiKey('secret', { remember: true })
    clearApiKey()
    expect(getApiKey()).toBe('')
    expect(loadApiKey()).toBe('')
    expect(sessionStorage.getItem('evalrag_api_key_session')).toBeNull()
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
  })

  it('clears a previously remembered key when logging in without remember', () => {
    setApiKey('old', { remember: true })
    setApiKey('new', { remember: false })
    expect(getApiKey()).toBe('new')
    expect(sessionStorage.getItem('evalrag_api_key_session')).toBeNull()
  })

  it('treats a blank key as no key', () => {
    setApiKey('   ', { remember: true })
    expect(getApiKey()).toBe('')
    expect(sessionStorage.getItem('evalrag_api_key_session')).toBeNull()
  })

  it('survives storage that throws (private mode)', () => {
    const original = window.sessionStorage
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      get() {
        throw new Error('SecurityError')
      },
    })
    try {
      expect(loadApiKey()).toBe('')
      expect(() => setApiKey('secret', { remember: true })).not.toThrow()
      expect(getApiKey()).toBe('secret')
    } finally {
      Object.defineProperty(window, 'sessionStorage', { configurable: true, value: original })
    }
  })
})
