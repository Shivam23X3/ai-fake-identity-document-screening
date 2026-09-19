/**
 * Shared types mirroring the backend API contract (app/schemas + responses).
 * Keep in sync with the FastAPI endpoints — OpenAPI codegen is a later step.
 */

export type StageStatus = 'ok' | 'not_implemented' | 'error' | 'skipped'
export type RiskBand = 'low' | 'medium' | 'high' | 'critical' | null
export type DocType =
  | 'passport'
  | 'visa'
  | 'national_id'
  | 'driving_license'
  | 'permit'
  | 'unknown'

export interface StageResult {
  stage: string
  status: StageStatus
  confidence: number | null
  data: Record<string, unknown>
  error: string | null
  human_review_required: boolean
  duration_ms: number | null
}

export interface HistoryItem {
  run_id: string
  doc_type_hint: string | null
  doc_type_detected: string | null
  status: string
  risk_score: number | null
  risk_band: RiskBand
  human_review_required: boolean
  created_at: string | null
}

export interface HistoryResponse {
  success: boolean
  items: HistoryItem[]
  limit: number
  offset: number
  count: number
}

export interface AnalyzeResponse {
  success: boolean
  run_id: string
  status: string
  doc_type_hint: string | null
  doc_type_detected: string | null
  risk_score: number | null
  risk_band: RiskBand
  human_review_required: boolean
  stages: StageResult[]
  disclaimer: string
}

export interface ScreeningDetail extends AnalyzeResponse {
  file_sha256: string
  created_at: string | null
  completed_at: string | null
  report: { stages?: StageResult[]; input?: Record<string, unknown>; disclaimer?: string }
}

export interface UploadResponse {
  success: boolean
  run_id: string
  status: string
  doc_type_hint: string
  file: { filename: string; size_bytes: number; sha256: string; detected_type: string }
  disclaimer: string
}

export interface OcrField {
  name: string
  value: string
  confidence: number
  flagged?: boolean
  /** "mrz" | "visual_zone" | "header" — where the value came from. */
  source?: string
  /** null/undefined = no check digit for this field. */
  check_digit_ok?: boolean | null
  notes?: string[]
  note?: string
}

export interface ProviderStatus {
  configured: string
  registered: string[]
  active: string
  is_placeholder: boolean
  interface: string
}

export interface PipelineInfo {
  pipeline: {
    name: string
    stages: { name: string; requires: string[]; provides: string[] }[]
    providers: Record<string, ProviderStatus>
  }
}

/** Step-9 consolidated screening result (GET /api/screening/{run_id}/result). */
export interface ScreeningResult {
  screening_id: string
  document: {
    run_id: string
    original_path?: string
    file_sha256?: string
    doc_type_hint?: string | null
    doc_type_detected?: string | null
  }
  ocr: { fields?: unknown[]; mrz_raw?: string | null; doc_type_detected?: string | null; overall_confidence?: number | null }
  validation: Record<string, unknown>
  tampering: Record<string, unknown>
  face_verification: Record<string, unknown>
  risk_assessment: {
    risk_score?: number | null
    risk_level?: string | null
    reasons?: string[]
    contributions?: { signal: string; points: number; details?: string }[]
    confidence?: number | null
    human_review_required?: boolean
  }
  final_status: string
  human_review_required: boolean
  processing_time_ms: number
  confidence_summary?: Record<string, number | null>
  stages?: StageResult[]
  audit?: {
    event_id?: number | null
    payload_sha256?: string | null
    anchor_status?: string | null
    tx_hash?: string | null
    block_number?: number | null
    on_chain?: { verified?: boolean; reason?: string }
    error?: string
  }
  disclaimer: string
}

/** Step-10 canonical audit record (hash-only on the ledger — no PII). */
export interface AuditRecord {
  screening_id: string
  timestamp: string
  result_hash: string
  risk_score: number | null
  validation_status: string
  tampering_status: string
  face_match_status: string
  system_id: string
}

/** ----------------------------------------------------------------------
 *  Step 14 — SIH demonstration mode (scripted cases over the real pipeline)
 *  ---------------------------------------------------------------------- */
export interface DemoFixtureDoc {
  filename: string
  doc_type_hint: string
  note: string
}

export interface DemoCase {
  title: string | null
  storyline: string | null
  expected: string[]
  document: DemoFixtureDoc | null
  probe: DemoFixtureDoc | null
  probe_alternate?: DemoFixtureDoc | null
}

export interface DemoCasesResponse {
  success: boolean
  notice: string
  cases: Record<string, DemoCase>
}

/** One honest expected-vs-actual checkpoint (computed from the REAL result). */
export interface DemoCheck {
  criterion: string
  met: boolean
  detail: string
  informational?: boolean
}

export interface DemoRunResponse {
  success: boolean
  run_id: string
  case_id: string
  case_title: string | null
  demo_notice: string
  disclaimer: string
  result: ScreeningResult & {
    demo?: {
      is_demo: boolean
      notice: string
      case_id: string
      case_title?: string | null
      fixtures?: { document?: string; probe?: string }
      checks?: DemoCheck[]
    }
  }
}

/** GET /api/audit/verify/{screening_id} — recomputed hash vs ledger. */
export interface AuditVerifyResponse {
  success: boolean
  screening_id: string
  recomputed_result_hash: string
  db_committed_hash: string | null
  db_hash_match: boolean | null
  ledger_backend: string
  ledger_verify: Record<string, unknown>
  verified: boolean
  on_ledger: boolean
  record_changed: boolean
  conclusion: string
  disclaimer: string
}

/** Step-12 investigation search row (PII-minimal metadata only). */
export interface InvestigationRow {
  run_id: string
  doc_type_hint: string | null
  doc_type_detected: string | null
  status: string
  risk_score: number | null
  risk_band: RiskBand
  human_review_required: boolean
  created_at: string | null
  completed_at: string | null
}

export interface InvestigationSearchResponse {
  success: boolean
  items: InvestigationRow[]
  total: number
  limit: number
  offset: number
  count: number
  filters_applied: Record<string, unknown>
  disclaimer: string
}

export interface InvestigationFilters {
  q?: string
  risk_band?: string
  doc_type?: string
  date_from?: string
  date_to?: string
  review_required?: boolean | null
  status?: string
}

/** Step-12 aggregate statistics (label/count pairs only — no PII). */
export interface InvestigationStats {
  success: boolean
  window: Record<string, unknown>
  total: number
  human_review: { true: number; false: number }
  screenings_per_day: { days: { day: string; count: number }[]; window_days: number; older_than_window: number }
  risk_distribution: Record<string, number>
  doc_type_distribution: Record<string, number>
  validation: Record<string, number>
  tampering: Record<string, number>
  face: Record<string, number>
  status: Record<string, number>
  integrity: {
    total_screenings: number
    anchored_on_chain: number
    audit_logged: number
    pending: number
    note: string
  }
  privacy_note: string
  disclaimer: string
}

/** Step-12 case inspection (whitelisted module projection + audit verdict). */
export interface InvestigationCase {
  success: boolean
  run_id: string
  created_at: string | null
  completed_at: string | null
  status: string
  doc_type_hint: string | null
  doc_type_detected: string | null
  file_sha256: string
  human_review_required: boolean
  final_status: string | null
  processing_time_ms: number | null
  confidence_summary: Record<string, number | null> | null
  pipeline_error: string | null
  stages: { stage: string; status: string; confidence: number | null; human_review_required: boolean; duration_ms: number | null }[]
  risk: {
    risk_score: number | null
    risk_level: string | null
    reasons: string[]
    contributions: { signal: string; points: number; details?: string }[]
    confidence: number | null
    routing_reasons: string[]
  }
  validation: {
    is_valid?: boolean | null
    confidence?: number | null
    doc_type?: string
    ruleset?: string | null
    failures: string[]
    warnings: string[]
    notes: string[]
    checks: { field: string | null; status: string | null; message: string | null }[]
  }
  tampering: {
    verdict?: string | null
    risk_score?: number | null
    confidence?: number | null
    tampering_detected?: boolean
    human_review_required?: boolean
    explanation?: string | null
    indicators: { type: string | null; severity: string | null; confidence: number | null; note: string }[]
  }
  face: {
    match_status?: string | null
    verdict?: string | null
    similarity_score?: number | null
    confidence?: number | null
    face_detected_document?: boolean
    face_detected_presented_person?: boolean
    warnings?: string[]
    one_to_one_only?: boolean
    no_population_search?: boolean
  }
  audit: {
    events: {
      event_type: string
      payload_sha256: string
      anchor_status: string
      chain: string
      tx_hash: string | null
      block_number: number | null
      created_at: string | null
    }[]
    chain: Record<string, unknown> | null
    verify: { verified: boolean | null; on_ledger: boolean | null; record_changed: boolean | null; conclusion: string; ledger_backend: string } | null
    note: string | null
  }
  raw_report?: Record<string, unknown>
  disclaimer: string
}

/** POST /api/audit/log — build + log the canonical record on the ledger. */
export interface AuditLogResponse {
  success: boolean
  logged: boolean
  screening_id: string
  audit_record: AuditRecord
  ledger_backend: string
  ledger: {
    ledger_status?: string
    tx_hash?: string | null
    block_number?: number | null
    note?: string | null
    error?: string | null
  }
  event_id: number
  note: string
  disclaimer: string
}
