/**
 * Typed API client for the FastAPI backend.
 * All calls go through `request()` so error handling is uniform: the backend's
 * {"success": false, "error": "..."} envelope is surfaced as ApiError.
 */
import type {
  AnalyzeResponse,
  AuditLogResponse,
  AuditVerifyResponse,
  DemoCasesResponse,
  DemoRunResponse,
  HistoryResponse,
  InvestigationCase,
  InvestigationFilters,
  InvestigationSearchResponse,
  InvestigationStats,
  PipelineInfo,
  ReviewDecisionResponse,
  ReviewPendingResponse,
  ReviewStateResponse,
  ScreeningDetail,
  ScreeningResult,
  UploadResponse,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** JWT access token storage (memory + sessionStorage; no localStorage). */
let accessToken: string | null = sessionStorage.getItem('screening.token')

export function setToken(token: string | null): void {
  accessToken = token
  if (token) sessionStorage.setItem('screening.token', token)
  else sessionStorage.removeItem('screening.token')
}

export function getToken(): string | null {
  return accessToken
}

export interface SessionUser {
  id: number
  username: string
  role: 'ADMIN' | 'SECURITY_OFFICER' | 'REVIEWER'
  is_active: boolean
}

export interface LoginResponse {
  success: boolean
  access_token: string
  token_type: string
  expires_in: number
  user: SessionUser
  permissions: string[]
  must_change_password: boolean
}

export class ApiError extends Error {
  status: number
  details?: unknown
  constructor(message: string, status: number, details?: unknown) {
    super(message)
    this.status = status
    this.details = details
  }
}

/** Invoked when the server rejects the token (expired/revoked mid-session). */
let onUnauthorized: (() => void) | null = null

/** Register a callback fired on any 401 while a token is present. */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.headers as Record<string, string>),
  }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, headers })
  } catch {
    throw new ApiError(
      API_BASE
        ? 'Cannot reach the backend at ' + API_BASE + '. Check VITE_API_BASE / API server.'
        : 'Cannot reach the API. Is the backend server running?',
      0,
    )
  }

  let body: Record<string, unknown> | null = null
  try {
    body = await res.json()
  } catch {
    /* non-JSON error body */
  }

  if (!res.ok || body?.success === false) {
    // Expired/revoked token mid-session (30-min TTL): drop it and let the
    // app return to the login screen instead of a stuck half-broken UI.
    if (res.status === 401 && accessToken && !path.startsWith('/api/auth/login')) {
      setToken(null)
      onUnauthorized?.()
    }
    throw new ApiError(
      (body?.error as string) ?? `Request failed (${res.status})`,
      res.status,
      body?.details,
    )
  }
  return body as T
}

function fileUrl(runId: string): string {
  return `${API_BASE}/api/screening/${runId}/file`
}

export const api = {
  health: () => request<{ status: string; mock_mode: boolean }>('/health'),

  login: (username: string, password: string) =>
    request<LoginResponse>('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),

  logout: () => request<{ logged_out: boolean }>('/api/auth/logout', { method: 'POST' }),

  me: () =>
    request<{
      user: SessionUser
      permissions: Record<string, boolean>
    }>('/api/auth/me'),

  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ changed: boolean }>('/api/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),

  pipelineInfo: () => request<PipelineInfo>('/pipeline/info'),

  upload: (file: File, docTypeHint: string, probeImage?: File | null) => {
    const form = new FormData()
    form.append('file', file)
    form.append('doc_type_hint', docTypeHint)
    if (probeImage) form.append('probe_image', probeImage)
    return request<UploadResponse>('/api/screening/upload', { method: 'POST', body: form })
  },

  probeFileUrl: (runId: string): string => `${API_BASE}/api/screening/${runId}/probe-file`,

  analyze: (runId: string) =>
    request<AnalyzeResponse>('/api/screening/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId }),
    }),

  detail: (runId: string) => request<ScreeningDetail>(`/api/screening/${runId}`),

  /** Step-9 consolidated result (screening_id, per-module payloads, audit). */
  result: (runId: string) => request<ScreeningResult>(`/api/screening/${runId}/result`),

  /** Step-9 audit trail + chain anchor status for one run. */
  audit: (runId: string) =>
    request<{
      run_id: string
      events: {
        event_type: string
        payload_sha256: string
        anchor_status: string
        chain: string
        tx_hash: string | null
        block_number: number | null
        created_at: string | null
      }[]
      chain: { implemented: boolean; engine: string; note: string | null }
      disclaimer: string
    }>(`/api/screening/${runId}/audit`),

  /** Step-10: log the canonical audit record on the permissioned ledger. */
  auditLog: (runId: string) =>
    request<AuditLogResponse>('/api/audit/log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId }),
    }),

  /** Step-10: recompute the result hash and check it against the ledger. */
  auditVerify: (screeningId: string) =>
    request<AuditVerifyResponse>(`/api/audit/verify/${screeningId}`),

  history: (limit = 20, offset = 0) =>
    request<HistoryResponse>(`/api/screening/history?limit=${limit}&offset=${offset}`),

  /** Step-12: filtered investigation search (PII-minimal rows). */
  investigationSearch: (filters: InvestigationFilters, limit = 20, offset = 0) => {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(filters)) {
      if (v === undefined || v === null || v === '') continue
      params.set(k, String(v))
    }
    params.set('limit', String(limit))
    params.set('offset', String(offset))
    return request<InvestigationSearchResponse>(`/api/investigation/search?${params.toString()}`)
  },

  /** Step-12: aggregate chart statistics over the same filtered corpus. */
  investigationStats: (filters: InvestigationFilters = {}) => {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(filters)) {
      if (v === undefined || v === null || v === '') continue
      params.set(k, String(v))
    }
    return request<InvestigationStats>(`/api/investigation/stats?${params.toString()}`)
  },

  /** Step-12: inspect one case (whitelisted projection + audit verdict). */
  investigationCase: (runId: string) =>
    request<InvestigationCase>(`/api/investigation/cases/${runId}`),

  /** Step-14: the scripted SIH demonstration cases (synthetic fixtures). */
  demoCases: () => request<DemoCasesResponse>('/api/demo/cases'),

  /** Step-14: run one scripted demo case through the REAL pipeline. */
  demoRun: (caseId: string) =>
    request<DemoRunResponse>(`/api/demo/run/${caseId}`, { method: 'POST' }),

  /** Step-15: the human-review queue (screenings awaiting a decision). */
  reviewPending: (limit = 50) =>
    request<ReviewPendingResponse>(`/api/review/pending?limit=${limit}`),

  /** Step-15: decision state for one screening. */
  reviewGet: (runId: string) =>
    request<ReviewStateResponse>(`/api/review/${runId}`),

  /** Step-15: record the human decision (REVIEWER and above). */
  reviewDecide: (runId: string, decision: string, notes?: string) =>
    request<ReviewDecisionResponse>(`/api/review/${runId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, notes: notes || null }),
    }),

  fileUrl,
}
