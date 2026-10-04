// Credential handling for the browser client.
//
// The API key used to be kept in `localStorage`, which means it survived every
// browser restart and stayed readable to any script running on this origin, for
// ever. It now lives in memory for the lifetime of the tab, and is written to
// `sessionStorage` only when the operator explicitly asks to remember it for
// this tab. The tenant id is not a secret and stays in `localStorage` so the
// workspace is remembered.
//
// Values loaded here are only ever read back by this module: `requestHeaders()`
// in api.ts calls `getApiKey()`, so there is a single place that decides what
// goes on the wire.

export const TENANT_STORAGE_KEY = 'evalrag_tenant'

const SESSION_API_KEY = 'evalrag_api_key_session'
const LEGACY_API_KEY = 'evalrag_api_key'

let apiKey = ''

function removeQuietly(storage: Storage, key: string): void {
  try {
    storage.removeItem(key)
  } catch {
    // Storage can be unavailable (private mode, blocked cookies); the key then
    // simply lives in memory only, which is the safe direction to fail.
  }
}

/** Read the tenant id used to address the API. Never throws. */
export function loadTenantId(): string {
  try {
    return localStorage.getItem(TENANT_STORAGE_KEY) || ''
  } catch {
    return ''
  }
}

export function saveTenantId(tenantId: string): void {
  try {
    localStorage.setItem(TENANT_STORAGE_KEY, tenantId)
  } catch {
    // Ignore: the workspace still works for this session.
  }
}

export function clearTenantId(): void {
  removeQuietly(localStorage, TENANT_STORAGE_KEY)
}

/**
 * Load the API key held by this tab.
 *
 * A key left behind by an older build in `localStorage` is *deleted* rather than
 * promoted: promoting it would keep a long-lived credential in the at-rest
 * store this change exists to get rid of, and the operator can paste it again.
 */
export function loadApiKey(): string {
  removeQuietly(localStorage, LEGACY_API_KEY)
  try {
    apiKey = sessionStorage.getItem(SESSION_API_KEY) || ''
  } catch {
    apiKey = ''
  }
  return apiKey
}

export function getApiKey(): string {
  return apiKey
}

export function setApiKey(value: string, options: { remember: boolean }): void {
  apiKey = value.trim()
  if (options.remember && apiKey) {
    try {
      sessionStorage.setItem(SESSION_API_KEY, apiKey)
    } catch {
      // Not remembering is the safe fallback.
    }
  } else {
    removeQuietly(sessionStorage, SESSION_API_KEY)
  }
  removeQuietly(localStorage, LEGACY_API_KEY)
}

export function clearApiKey(): void {
  apiKey = ''
  removeQuietly(sessionStorage, SESSION_API_KEY)
  removeQuietly(localStorage, LEGACY_API_KEY)
}
