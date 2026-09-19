/** Formatting + domain mapping helpers. */
import type { RiskBand, StageStatus } from './types'

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

export function formatConfidence(conf: number | null | undefined): string {
  if (conf === null || conf === undefined) return 'n/a'
  return `${Math.round(conf * 100)}%`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

export const DOC_TYPE_LABELS: Record<string, string> = {
  passport: 'Passport',
  visa: 'Visa',
  national_id: 'National ID',
  driving_license: 'Driving License',
  permit: 'Permit',
  unknown: 'Unknown',
}

/** Risk band display config. CRITICAL is produced by the Step 7 risk engine. */
export const RISK_BANDS: Record<
  Exclude<RiskBand, null>,
  { label: string; className: string; priority: number }
> = {
  low: { label: 'LOW', className: 'band-low', priority: 0 },
  medium: { label: 'MEDIUM', className: 'band-medium', priority: 1 },
  high: { label: 'HIGH', className: 'band-high', priority: 2 },
  critical: { label: 'CRITICAL', className: 'band-critical', priority: 3 },
}

export function bandInfo(band: RiskBand) {
  return band ? RISK_BANDS[band] : null
}

export const STAGE_LABELS: Record<string, string> = {
  preprocess: '1 · Image Preprocessing',
  ocr: '2 · OCR Extraction',
  document_validation: '3 · Document Validation',
  tampering_detection: '4 · Tampering Detection',
  face_verification: '5 · Face Verification',
  risk_assessment: '6 · Risk Assessment',
}

export const STATUS_META: Record<StageStatus, { label: string; className: string }> = {
  ok: { label: 'COMPLETED', className: 'status-ok' },
  not_implemented: { label: 'MODULE PENDING', className: 'status-pending' },
  error: { label: 'ERROR', className: 'status-error' },
  skipped: { label: 'SKIPPED', className: 'status-skipped' },
}

export function shortRunId(runId: string): string {
  return runId.length > 12 ? `${runId.slice(0, 12)}…` : runId
}
