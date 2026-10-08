import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearApiKey,
  clearSessionToken,
  clearTenantId,
  getApiKey,
  getSessionToken,
  loadSessionToken,
  loadTenantId,
  saveSessionToken,
  saveTenantId,
  setApiKey,
} from './auth'

// The long-lived API key used to live in localStorage forever, and even after it moved to
// sessionStorage it was still a secret with no expiry and no way to revoke it. These tests
// pin the rule that replaced it: the tab holds a server-side session token, never the key,
// `localStorage` only ever carries the (non-secret) tenant id, and keys left behind by an
// older build are deleted on sight rather than migrated forward.

const TOKEN_KEY = 'evalrag_session_token'
const LEGACY_KEY = 'evalrag_api_key'
const LEGACY_SESSION_KEY = 'evalrag_api_key_session'

describe('auth storage', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearSessionToken()
    clearApiKey()
  })

  it('keeps the tenant id in localStorage across reloads', () => {
    saveTenantId('demo-enterprise')
    expect(loadTenantId()).toBe('demo-enterprise')
    clearTenantId()
    expect(loadTenantId()).toBe('')
  })

  it('stores a remembered token in sessionStorage and nothing in localStorage', () => {
    saveSessionToken('ers_abc', { remember: true })
    expect(getSessionToken()).toBe('ers_abc')
    expect(loadSessionToken()).toBe('ers_abc')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('ers_abc')
    expect(Object.keys(localStorage)).toEqual([])
  })

  it('keeps an unremembered token in memory only', () => {
    saveSessionToken('  ers_abc  ', { remember: false })
    expect(getSessionToken()).toBe('ers_abc')
    expect(loadSessionToken()).toBe('')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(Object.keys(sessionStorage)).toEqual([])
  })

  it('deletes the legacy API keys instead of adopting them', () => {
    localStorage.setItem(LEGACY_KEY, 'legacy-key')
    sessionStorage.setItem(LEGACY_SESSION_KEY, 'legacy-key')
    expect(loadSessionToken()).toBe('')
    expect(getSessionToken()).toBe('')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(sessionStorage.getItem(LEGACY_SESSION_KEY)).toBeNull()
  })

  it('drops the legacy keys even when a live token is present', () => {
    saveSessionToken('ers_abc', { remember: true })
    localStorage.setItem(LEGACY_KEY, 'legacy-key')
    sessionStorage.setItem(LEGACY_SESSION_KEY, 'legacy-key')
    expect(loadSessionToken()).toBe('ers_abc')
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(sessionStorage.getItem(LEGACY_SESSION_KEY)).toBeNull()
    expect(Object.keys(localStorage)).toEqual([])
    expect(Object.keys(sessionStorage)).toEqual([TOKEN_KEY])
  })

  it('clears storage and memory together', () => {
    saveSessionToken('ers_abc', { remember: true })
    clearSessionToken()
    expect(getSessionToken()).toBe('')
    expect(loadSessionToken()).toBe('')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull()
  })

  it('treats a blank token as no token', () => {
    saveSessionToken('   ', { remember: true })
    expect(getSessionToken()).toBe('')
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull()
  })

  it('keeps the API key in memory without persisting it anywhere', () => {
    setApiKey('  secret  ')
    expect(getApiKey()).toBe('secret')
    expect(Object.keys(localStorage)).toEqual([])
    expect(Object.keys(sessionStorage)).toEqual([])
    clearApiKey()
    expect(getApiKey()).toBe('')
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
      expect(loadSessionToken()).toBe('')
      expect(() => saveSessionToken('ers_abc', { remember: true })).not.toThrow()
      expect(getSessionToken()).toBe('ers_abc')
      expect(() => clearSessionToken()).not.toThrow()
    } finally {
      Object.defineProperty(window, 'sessionStorage', { configurable: true, value: original })
    }
  })
})
