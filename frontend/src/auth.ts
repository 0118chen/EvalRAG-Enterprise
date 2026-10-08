// Credential handling for the browser client.
//
// The browser used to hold the long-lived API key itself: kept in `localStorage` it
// survived every restart, and kept in `sessionStorage` it was still a secret with no
// expiry and no way to revoke it short of rotating the key for every client. Neither
// store holds a key any more. Login trades the key once for a server-side session token
// (`POST /api/v1/auth/session`) and only that token is kept: short-lived, revocable,
// tab-scoped, and gone when the tab closes. The key itself lives in memory for the
// length of the page load and is never written down.
//
// Credentials left behind by an older build (`evalrag_api_key`,
// `evalrag_api_key_session`) are deleted rather than promoted: promoting them would
// re-create the at-rest secret this module exists to remove, and the operator can paste
// the key again.
//
// Values loaded here are only ever read back by this module and by `requestHeaders()` in
// api.ts, so there is a single place that decides what goes on the wire.

export const TENANT_STORAGE_KEY = 'evalrag_tenant'

/** Tab-scoped storage for the short-lived session token: never `localStorage`. */
const SESSION_TOKEN_KEY = 'evalrag_session_token'

/** Keys older builds wrote long-lived API keys to. Deleted on sight, never read. */
const LEGACY_API_KEY_KEYS = ['evalrag_api_key', 'evalrag_api_key_session'] as const

let sessionToken = ''
let apiKey = ''

type StorageKind = 'local' | 'session'

/**
 * Resolve a web storage object, tolerating browsers that refuse even the lookup.
 *
 * Reading `sessionStorage` itself can throw (`SecurityError` in Safari's private mode and
 * in sandboxed iframes), and that throw happens before any `try` around `getItem`, so the
 * lookup is the thing that has to be guarded — hence this indirection instead of passing a
 * `Storage` instance around.
 */
function storeOf(kind: StorageKind): Storage | null {
  try {
    return kind === 'local' ? localStorage : sessionStorage
  } catch {
    return null
  }
}

function readQuietly(kind: StorageKind, key: string): string {
  const storage = storeOf(kind)
  if (!storage) return ''
  try {
    return storage.getItem(key) || ''
  } catch {
    return ''
  }
}

function removeQuietly(kind: StorageKind, key: string): void {
  const storage = storeOf(kind)
  if (!storage) return
  try {
    storage.removeItem(key)
  } catch {
    // Storage can be unavailable (private mode, blocked cookies); credentials then
    // simply live in memory only, which is the safe direction to fail.
  }
}

function writeQuietly(kind: StorageKind, key: string, value: string): void {
  const storage = storeOf(kind)
  if (!storage) return
  try {
    storage.setItem(key, value)
  } catch {
    // Not remembering is the safe fallback.
  }
}

/** Delete API keys left behind by older builds, wherever they are. */
function dropLegacyApiKeys(): void {
  for (const key of LEGACY_API_KEY_KEYS) {
    removeQuietly('local', key)
    removeQuietly('session', key)
  }
}

/** Read the tenant id used to address the API. Never throws. */
export function loadTenantId(): string {
  return readQuietly('local', TENANT_STORAGE_KEY)
}

export function saveTenantId(tenantId: string): void {
  writeQuietly('local', TENANT_STORAGE_KEY, tenantId)
}

export function clearTenantId(): void {
  removeQuietly('local', TENANT_STORAGE_KEY)
}

/**
 * Load the session token held by this tab.
 *
 * The token is short-lived and tab-scoped, so there is nothing to migrate: a key left in
 * either store by an older build is deleted, and the tab starts logged out.
 */
export function loadSessionToken(): string {
  dropLegacyApiKeys()
  sessionToken = readQuietly('session', SESSION_TOKEN_KEY)
  return sessionToken
}

export function getSessionToken(): string {
  return sessionToken
}

/**
 * Remember the token minted by `POST /api/v1/auth/session`.
 *
 * With `remember` the token goes to `sessionStorage`: it dies with the tab and is never
 * readable after a restart. Without it the token only lives in memory, and a reload asks
 * for the API key again.
 */
export function saveSessionToken(token: string, options: { remember?: boolean } = {}): void {
  sessionToken = token.trim()
  if (options.remember && sessionToken) {
    writeQuietly('session', SESSION_TOKEN_KEY, sessionToken)
  } else {
    removeQuietly('session', SESSION_TOKEN_KEY)
  }
  dropLegacyApiKeys()
}

export function clearSessionToken(): void {
  sessionToken = ''
  removeQuietly('session', SESSION_TOKEN_KEY)
  dropLegacyApiKeys()
}

/**
 * The long-lived API key used by this page load, in memory only.
 *
 * `requestHeaders()` sends it as `X-API-Key` only when no session token is available,
 * which keeps CLI and debugging callers working; the browser login path exchanges the key
 * for a token and stores neither.
 */
export function getApiKey(): string {
  return apiKey
}

export function setApiKey(value: string): void {
  apiKey = value.trim()
}

export function clearApiKey(): void {
  apiKey = ''
}
