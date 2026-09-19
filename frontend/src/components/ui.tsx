/** Shared UI primitives for the screening dashboard. */
import type { ReactNode } from 'react'
import type { RiskBand, StageStatus } from '../types'
import { RISK_BANDS, STATUS_META, bandInfo, formatConfidence } from '../helpers'

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = '',
}: {
  title?: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel-header">
          <div>
            {title && <h2 className="panel-title">{title}</h2>}
            {subtitle && <p className="panel-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className="panel-body">{children}</div>
    </section>
  )
}

/**
 * Display a risk score. The backend risk engine emits a 0-100 score while
 * the tampering stage's legacy seam carries 0-1; anything <= 1 is scaled up.
 */
export function RiskBadge({ band, score }: { band: RiskBand; score?: number | null }) {
  const info = bandInfo(band)
  if (!info) {
    return <span className="badge band-unknown">RISK: N/A</span>
  }
  const scaled =
    score === undefined || score === null ? null : score <= 1 ? Math.round(score * 100) : Math.round(score)
  return (
    <span className={`badge ${info.className}`}>
      RISK: {info.label}
      {scaled !== null ? ` · ${scaled}` : ''}
    </span>
  )
}

export function StatusChip({ status }: { status: StageStatus }) {
  const meta = STATUS_META[status] ?? { label: status.toUpperCase(), className: 'status-pending' }
  return <span className={`badge ${meta.className}`}>{meta.label}</span>
}

export function MockTag({ children = 'DEMO DATA' }: { children?: ReactNode }) {
  return <span className="badge badge-mock">{children}</span>
}

export function ConfidenceBar({
  value,
  label = 'Confidence',
}: {
  value: number | null
  label?: string
}) {
  const pct = value === null || value === undefined ? null : Math.round(value * 100)
  const level = pct === null ? 'unknown' : pct >= 80 ? 'high' : pct >= 50 ? 'mid' : 'low'
  return (
    <div className="confidence" title={`${label}: ${formatConfidence(value)}`}>
      <span className="confidence-label">
        {label}: <strong>{pct === null ? 'n/a' : `${pct}%`}</strong>
      </span>
      <div className={`confidence-track conf-${level}`}>
        <div className="confidence-fill" style={{ width: pct === null ? '0%' : `${pct}%` }} />
      </div>
    </div>
  )
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="banner banner-error" role="alert">
      <strong>Error:</strong> {message}
    </div>
  )
}

export function InfoBanner({ children }: { children: ReactNode }) {
  return <div className="banner banner-info">{children}</div>
}

export function WarningBanner({ children }: { children: ReactNode }) {
  return <div className="banner banner-warning">{children}</div>
}

export function HumanReviewNotice({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`review-notice ${compact ? 'review-notice-compact' : ''}`}>
      <span className="review-icon" aria-hidden>⚠</span>
      <div>
        <strong>HUMAN REVIEW REQUIRED</strong>
        {!compact && (
          <p>
            AI output is advisory only. Final screening decision must be made by
            authorized human personnel.
          </p>
        )}
      </div>
    </div>
  )
}

export function ModulePendingNote({
  capability,
  note,
}: {
  capability: string
  note?: string
}) {
  return (
    <div className="module-pending">
      <p>
        <strong>Module not yet active.</strong> The {capability} engine has not been
        plugged into the pipeline yet — no results are shown rather than fake ones.
      </p>
      {note && <p className="module-pending-note">{note}</p>}
    </div>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="spinner-row">
      <span className="spinner" aria-hidden />
      <span>{label}</span>
    </div>
  )
}

export function BandLegend() {
  return (
    <div className="band-legend">
      {Object.entries(RISK_BANDS).map(([key, meta]) => (
        <span key={key} className={`badge ${meta.className}`}>
          {meta.label}
        </span>
      ))}
    </div>
  )
}
