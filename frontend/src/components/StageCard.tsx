/** Card rendering one pipeline StageResult from the backend. */
import type { StageResult } from '../types'
import { STAGE_LABELS, formatConfidence } from '../helpers'
import { ConfidenceBar, StatusChip } from './ui'

export function StageCard({ result }: { result: StageResult }) {
  const engine = (result.data?.engine as string) ?? null
  const note = (result.data?.note as string) ?? null

  return (
    <article className={`stage-card stage-${result.status}`}>
      <header className="stage-card-header">
        <h3>{STAGE_LABELS[result.stage] ?? result.stage}</h3>
        <StatusChip status={result.status} />
      </header>

      <div className="stage-card-meta">
        <span>Engine: <code>{engine ?? '—'}</code></span>
        {result.duration_ms !== null && result.duration_ms !== undefined && (
          <span> · {result.duration_ms} ms</span>
        )}
        <span> · Confidence: <strong>{formatConfidence(result.confidence)}</strong></span>
      </div>

      <ConfidenceBar value={result.confidence} />

      {result.human_review_required && (
        <p className="stage-review-flag">⚠ Routed to human review</p>
      )}
      {note && <p className="stage-note">{note}</p>}
      {result.error && <p className="stage-error">Error: {result.error}</p>}
    </article>
  )
}
