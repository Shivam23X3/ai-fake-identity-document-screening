/**
 * Human Review panel — the closing half of the human-in-the-loop design.
 * Lists screenings awaiting a decision and lets REVIEWER/ADMIN record
 * cleared / flagged / escalated / inconclusive. Every decision is
 * attributed server-side and mirrored into the audit trail.
 *
 * UX notes:
 * - A row expands in place to show the case's module verdicts (validation,
 *   tampering, face, risk) so the reviewer can decide WITHOUT leaving the
 *   queue; "Open full report" stays available for deep inspection.
 * - Flagged/escalated decisions open a confirmation (irreversible-ish
 *   actions get friction; cleared does not).
 * - Filters (risk band + run-id text) keep a long queue navigable.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import type {
  ReviewDecision,
  ReviewPendingItem,
  ReviewStateResponse,
  ScreeningResult,
} from '../types'
import { ErrorBanner, Panel, Spinner } from './ui'

const DECISION_OPTIONS: {
  value: ReviewDecision
  label: string
  hint: string
  className: string
}[] = [
  { value: 'cleared', label: 'Cleared', hint: 'Examined — no problem found', className: 'dec-cleared' },
  { value: 'flagged', label: 'Flagged', hint: 'Problem confirmed (fraud/quality)', className: 'dec-flagged' },
  { value: 'escalated', label: 'Escalate', hint: 'Route to a higher authority', className: 'dec-escalated' },
  { value: 'inconclusive', label: 'Inconcl.', hint: 'More evidence needed', className: 'dec-inconclusive' },
]

const BANDS: string[] = ['all', 'low', 'medium', 'high', 'critical']

function bandClass(band: string | null): string {
  if (band === 'low') return 'band-low'
  if (band === 'medium') return 'band-medium'
  if (band === 'high') return 'band-high'
  if (band === 'critical') return 'band-critical'
  return 'band-unknown'
}

function ageOf(createdIso: string | null): string {
  if (!createdIso) return '—'
  const then = new Date(createdIso).getTime()
  if (Number.isNaN(then)) return '—'
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

/** Compact module verdicts fetched from the consolidated result. */
function CaseSummary({ runId }: { runId: string }) {
  const [data, setData] = useState<ScreeningResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setData(null)
    setError(null)
    api
      .result(runId)
      .then((res) => {
        if (alive) setData(res)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof ApiError ? e.message : 'Could not load the case summary')
      })
    return () => {
      alive = false
    }
  }, [runId])

  if (error) return <p className="hint">Summary unavailable: {error}</p>
  if (!data) return <Spinner label="Loading case summary…" />

  const face = (data.face_verification ?? {}) as Record<string, unknown>
  const tampering = (data.tampering ?? {}) as Record<string, unknown>
  const validation = data.validation as { is_valid?: boolean } | undefined
  const risk = data.risk_assessment

  return (
    <div className="review-summary">
      <div className="review-summary-cell">
        <span className="kv-label">Validation</span>
        <span className={`badge ${validation?.is_valid === false ? 'status-error' : validation?.is_valid ? 'status-ok' : 'band-unknown'}`}>
          {validation?.is_valid === false ? 'INVALID' : validation?.is_valid ? 'VALID' : 'UNKNOWN'}
        </span>
      </div>
      <div className="review-summary-cell">
        <span className="kv-label">Tampering</span>
        <span className={`badge ${String(tampering.verdict ?? '') === 'no_obvious_manipulation' ? 'band-low' : String(tampering.verdict ?? '') === 'inconclusive' ? 'band-critical' : 'band-medium'}`}>
          {String(tampering.verdict ?? '—').replaceAll('_', ' ').toUpperCase()}
        </span>
      </div>
      <div className="review-summary-cell">
        <span className="kv-label">Face 1:1</span>
        <span className={`badge ${String(face.match_status ?? '') === 'MATCH' ? 'band-low' : String(face.match_status ?? '') === 'NO_MATCH' ? 'band-high' : 'band-unknown'}`}>
          {String(face.match_status ?? '—').replaceAll('_', ' ')}
          {typeof face.similarity_score === 'number' ? ` · ${face.similarity_score.toFixed(2)}` : ''}
        </span>
      </div>
      <div className="review-summary-cell">
        <span className="kv-label">Risk</span>
        <span className={`badge ${bandClass((risk?.risk_level ?? '').toLowerCase() || null)}`}>
          {risk?.risk_level ?? '—'}{risk?.risk_score != null ? ` · ${risk.risk_score}` : ''}
        </span>
      </div>
      <div className="review-summary-cell">
        <span className="kv-label">Processing</span>
        <span className="hint" style={{ margin: 0 }}>{data.processing_time_ms} ms</span>
      </div>
      <div className="review-summary-cell review-summary-disclaimer" title={data.disclaimer}>
        {data.human_review_required ? 'Advisory AI — this decision is yours.' : ''}
      </div>
    </div>
  )
}

export function ReviewPanel({ onOpenDetail }: { onOpenDetail: (runId: string) => void }) {
  const [items, setItems] = useState<ReviewPendingItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deciding, setDeciding] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<{ runId: string; decision: ReviewDecision } | null>(null)
  const [noteOpen, setNoteOpen] = useState<Record<string, boolean>>({})
  const [noteText, setNoteText] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [bandFilter, setBandFilter] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [done, setDone] = useState<{ runId: string; decision: ReviewDecision } | null>(null)
  // Decision state for the CURRENT screening view (rendered inside Detail).
  const [state] = useState<ReviewStateResponse | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.reviewPending()
      setItems(res.items)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load the review queue')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter((it) => {
      if (bandFilter !== 'all' && (it.risk_band ?? '') !== bandFilter) return false
      if (q && !it.run_id.toLowerCase().includes(q)) return false
      return true
    })
  }, [items, bandFilter, query])

  const highCount = useMemo(
    () => items.filter((i) => i.risk_band === 'high' || i.risk_band === 'critical').length,
    [items],
  )

  async function decide(runId: string, decision: ReviewDecision) {
    setConfirming(null)
    setDeciding(runId)
    setError(null)
    try {
      const res = await api.reviewDecide(runId, decision, noteText[runId])
      setDone({ runId, decision })
      setItems((prev) => prev.filter((i) => i.run_id !== runId))
      setNoteOpen((prev) => ({ ...prev, [runId]: false }))
      void res
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record the decision')
    } finally {
      setDeciding(null)
    }
  }

  function handleDecisionClick(runId: string, decision: ReviewDecision) {
    if (decision === 'flagged' || decision === 'escalated') {
      setConfirming({ runId, decision })
    } else {
      void decide(runId, decision)
    }
  }

  return (
    <Panel
      title="Human Review Queue"
      subtitle="Cases awaiting a human decision — the AI never closes a case"
      actions={
        <button className="btn btn-ghost" onClick={() => void refresh()} disabled={loading}>
          Refresh
        </button>
      }
    >
      {loading && <Spinner label="Loading the review queue…" />}
      {error && <ErrorBanner message={error} />}
      {done && (
        <div className="banner banner-info" role="status">
          Decision recorded for <code>{done.runId.slice(0, 10)}…</code>:{' '}
          <strong>{done.decision}</strong> — review_decided audit event written.
        </div>
      )}

      {!loading && items.length > 0 && (
        <div className="review-toolbar">
          <div className="review-chips">
            <span className="big-stat">
              <span className="big-stat-num">{items.length}</span>
              <span className="big-stat-label">pending</span>
            </span>
            <span className={`big-stat ${highCount > 0 ? 'stat-warn' : ''}`}>
              <span className="big-stat-num">{highCount}</span>
              <span className="big-stat-label">high/critical</span>
            </span>
          </div>
          <div className="review-filters">
            <select
              className="review-filter-select"
              value={bandFilter}
              onChange={(e) => setBandFilter(e.target.value)}
              aria-label="Filter by risk band"
            >
              {BANDS.map((b) => (
                <option key={b} value={b}>
                  {b === 'all' ? 'All risk bands' : `Risk: ${b.toUpperCase()}`}
                </option>
              ))}
            </select>
            <input
              className="review-filter-input"
              placeholder="Filter by run id…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Filter by run id"
            />
          </div>
        </div>
      )}

      {!loading && visible.length === 0 && !error && (
        <div className="empty-state">
          {items.length === 0
            ? 'No screenings are awaiting review. Run a case from the SIH Demo tab to populate the queue.'
            : 'No cases match the current filters.'}
        </div>
      )}

      {visible.length > 0 && (
        <div className="table-wrap">
          <table className="table review-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Detected</th>
                <th>Risk</th>
                <th>Age</th>
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((it) => {
                const isOpen = !!expanded[it.run_id]
                return (
                  <FragmentRow
                    key={it.run_id}
                    item={it}
                    open={isOpen}
                    deciding={deciding === it.run_id}
                    anyDeciding={deciding !== null}
                    noteOpen={!!noteOpen[it.run_id]}
                    noteValue={noteText[it.run_id] ?? ''}
                    confirming={confirming?.runId === it.run_id ? confirming.decision : null}
                    onToggleOpen={() => setExpanded((p) => ({ ...p, [it.run_id]: !p[it.run_id] }))}
                    onToggleNote={() => setNoteOpen((p) => ({ ...p, [it.run_id]: !p[it.run_id] }))}
                    onNoteChange={(v) => setNoteText((p) => ({ ...p, [it.run_id]: v }))}
                    onDecide={(d) => handleDecisionClick(it.run_id, d)}
                    onConfirmYes={() => confirming && void decide(confirming.runId, confirming.decision)}
                    onConfirmNo={() => setConfirming(null)}
                    onOpenDetail={onOpenDetail}
                  />
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {state && !state.review.pending && (
        <p className="hint">
          Current decision: <strong>{state.review.decision}</strong> by reviewer #{state.review.reviewer_id}
        </p>
      )}
      <p className="hint">
        Decisions are irreversible in the queue (re-logging requires a new screening run) and every
        decision — including its notes — is attributed to your account and hashed into the audit trail.
      </p>
    </Panel>
  )
}

/** One queue row + its expandable summary row. */
function FragmentRow({
  item,
  open,
  deciding,
  anyDeciding,
  noteOpen,
  noteValue,
  confirming,
  onToggleOpen,
  onToggleNote,
  onNoteChange,
  onDecide,
  onConfirmYes,
  onConfirmNo,
  onOpenDetail,
}: {
  item: ReviewPendingItem
  open: boolean
  deciding: boolean
  anyDeciding: boolean
  noteOpen: boolean
  noteValue: string
  confirming: ReviewDecision | null
  onToggleOpen: () => void
  onToggleNote: () => void
  onNoteChange: (v: string) => void
  onDecide: (d: ReviewDecision) => void
  onConfirmYes: () => void
  onConfirmNo: () => void
  onOpenDetail: (runId: string) => void
}) {
  return (
    <>
      <tr className={open ? 'row-open' : 'row-click'} onClick={!open ? onToggleOpen : undefined}>
        <td>
          <button
            className="btn btn-ghost review-run-btn"
            title="Expand case summary"
            onClick={(e) => {
              e.stopPropagation()
              onToggleOpen()
            }}
          >
            {open ? '▾' : '▸'} {item.run_id.slice(0, 10)}…
          </button>
        </td>
        <td>{item.doc_type_detected ?? '—'}</td>
        <td>
          <span className={`badge ${bandClass(item.risk_band)}`}>
            {item.risk_band ?? 'N/A'}
            {item.risk_score != null ? ` · ${item.risk_score}` : ''}
          </span>
        </td>
        <td className="hint">{ageOf(item.created_at)}</td>
        <td onClick={(e) => e.stopPropagation()}>
          <div className="review-actions">
            {DECISION_OPTIONS.map((d) => (
              <button
                key={d.value}
                className={`btn btn-ghost review-dec-btn ${d.className}`}
                title={d.hint}
                disabled={anyDeciding}
                onClick={() => onDecide(d.value)}
              >
                {deciding ? '…' : d.label}
              </button>
            ))}
            <button
              className="btn btn-ghost review-note-btn"
              title="Add decision notes"
              onClick={onToggleNote}
            >
              {noteOpen ? '− note' : '+ note'}
            </button>
          </div>
          {noteOpen && (
            <input
              className="input review-note-input"
              placeholder="Decision notes (stored + audited)"
              value={noteValue}
              onChange={(e) => onNoteChange(e.target.value)}
              onClick={(e) => e.stopPropagation()}
            />
          )}
          {confirming && (
            <div className="review-confirm" role="alertdialog" aria-label="Confirm decision">
              <span>
                Confirm <strong>{confirming}</strong> for{' '}
                <code>{item.run_id.slice(0, 10)}…</code>?
              </span>
              <span className="review-confirm-actions">
                <button className="btn btn-ghost" onClick={onConfirmYes}>
                  Yes, record it
                </button>
                <button className="btn btn-ghost" onClick={onConfirmNo}>
                  Cancel
                </button>
              </span>
            </div>
          )}
        </td>
      </tr>
      {open && (
        <tr className="review-summary-row">
          <td colSpan={5}>
            <CaseSummary runId={item.run_id} />
            <div className="review-summary-links">
              <button className="btn btn-ghost" onClick={() => onOpenDetail(item.run_id)}>
                Open full report →
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
