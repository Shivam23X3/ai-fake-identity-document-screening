/**
 * Step 12 — Investigation & Intelligence view.
 *
 * Authorized users can:
 *  - search screening records (text = run id / file hash / doc type only —
 *    PII fields are deliberately NOT searchable),
 *  - filter by risk band, date range, document type, human-review flag,
 *  - view suspicious cases (human-review queue) via one click,
 *  - inspect risk factors, validation failures, tampering indicators and
 *    the face-verification result in the case inspector,
 *  - verify blockchain audit integrity per case and corpus-wide.
 *
 * Visualizations are dependency-free SVG charts (bar / donut / hbar) fed by
 * the aggregate-only /api/investigation/stats endpoint — no per-case data
 * is used for any chart.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import type {
  InvestigationCase,
  InvestigationFilters,
  InvestigationRow,
  InvestigationStats,
} from '../types'
import { DOC_TYPE_LABELS, RISK_BANDS, formatDateTime, shortRunId } from '../helpers'
import { ErrorBanner, Panel, RiskBadge, Spinner } from './ui'

// ---------------------------------------------------------------------------
// Tiny dependency-free chart primitives (SVG)
// ---------------------------------------------------------------------------
function BarChart({
  data,
  color = 'var(--accent)',
  height = 120,
}: {
  data: { label: string; value: number }[]
  color?: string
  height?: number
}) {
  const max = Math.max(1, ...data.map((d) => d.value))
  return (
    <div className="barchart" role="img" aria-label="bar chart">
      {data.map((d) => (
        <div key={d.label} className="barchart-col" title={`${d.label}: ${d.value}`}>
          <span className="barchart-value">{d.value || ''}</span>
          <div
            className="barchart-bar"
            style={{ height: `${Math.max(2, (d.value / max) * (height - 30))}px`, background: color }}
          />
          <span className="barchart-label">{d.label}</span>
        </div>
      ))}
    </div>
  )
}

function HBarChart({ data }: { data: { label: string; value: number; className?: string }[] }) {
  const max = Math.max(1, ...data.map((d) => d.value))
  return (
    <div className="hbar" role="img" aria-label="distribution chart">
      {data.map((d) => (
        <div key={d.label} className="hbar-row" title={`${d.label}: ${d.value}`}>
          <span className="hbar-label">{d.label}</span>
          <div className="hbar-track">
            <div className={`hbar-fill ${d.className ?? ''}`} style={{ width: `${(d.value / max) * 100}%` }} />
          </div>
          <span className="hbar-value">{d.value}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Filter bar
// ---------------------------------------------------------------------------
const EMPTY_FILTERS: InvestigationFilters = { q: '', risk_band: '', doc_type: '', date_from: '', date_to: '', status: '' }

function FilterBar({
  filters,
  onChange,
  onSearch,
  busy,
}: {
  filters: InvestigationFilters
  onChange: (f: InvestigationFilters) => void
  onSearch: () => void
  busy: boolean
}) {
  const set = (patch: Partial<InvestigationFilters>) => onChange({ ...filters, ...patch })
  return (
    <Panel title="Search & Filters" subtitle="Metadata search only — names and document numbers are not searchable by design">
      <form
        className="inv-filters"
        onSubmit={(e) => {
          e.preventDefault()
          onSearch()
        }}
      >
        <label className="inv-field inv-field-wide">
          <span>Search (run id / file hash / doc type)</span>
          <input
            value={filters.q ?? ''}
            onChange={(e) => set({ q: e.target.value })}
            placeholder="e.g. paste a run id or sha256 fragment"
          />
        </label>
        <label className="inv-field">
          <span>Risk band</span>
          <select value={filters.risk_band ?? ''} onChange={(e) => set({ risk_band: e.target.value })}>
            <option value="">Any</option>
            {Object.entries(RISK_BANDS).map(([k, v]) => (
              <option key={k} value={k}>{v.label}</option>
            ))}
          </select>
        </label>
        <label className="inv-field">
          <span>Document type</span>
          <select value={filters.doc_type ?? ''} onChange={(e) => set({ doc_type: e.target.value })}>
            <option value="">Any</option>
            {Object.entries(DOC_TYPE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </label>
        <label className="inv-field">
          <span>From (UTC)</span>
          <input type="date" value={filters.date_from ?? ''} onChange={(e) => set({ date_from: e.target.value })} />
        </label>
        <label className="inv-field">
          <span>To (UTC)</span>
          <input type="date" value={filters.date_to ?? ''} onChange={(e) => set({ date_to: e.target.value })} />
        </label>
        <label className="inv-field">
          <span>Pipeline status</span>
          <select value={filters.status ?? ''} onChange={(e) => set({ status: e.target.value })}>
            <option value="">Any</option>
            <option value="pending_review">pending_review</option>
            <option value="processing">processing</option>
            <option value="error">error</option>
          </select>
        </label>
        <label className="inv-field inv-field-check">
          <input
            type="checkbox"
            checked={filters.review_required === true}
            onChange={(e) => set({ review_required: e.target.checked ? true : null })}
          />
          <span>Suspicious only (human review required)</span>
        </label>
        <div className="inv-actions">
          <button className="btn btn-primary" type="submit" disabled={busy}>Search</button>
          <button
            className="btn btn-ghost"
            type="button"
            onClick={() => onChange({ ...EMPTY_FILTERS })}
          >
            Reset
          </button>
        </div>
      </form>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// Intelligence charts
// ---------------------------------------------------------------------------
function IntelligencePanels({ stats }: { stats: InvestigationStats }) {
  const dayLabel = (iso: string) => iso.slice(5) // MM-DD
  const riskData = Object.entries(stats.risk_distribution).map(([k, v]) => ({
    label: RISK_BANDS[k as keyof typeof RISK_BANDS]?.label ?? k.replace('_', ' ').toUpperCase(),
    value: v,
    className: k === 'low' ? 'band-low' : k === 'medium' ? 'band-medium' : k === 'high' ? 'band-high' : k === 'critical' ? 'band-critical' : '',
  }))
  const docData = Object.entries(stats.doc_type_distribution).map(([k, v]) => ({
    label: DOC_TYPE_LABELS[k] ?? k,
    value: v,
  }))
  const valData = Object.entries(stats.validation).map(([k, v]) => ({
    label: k.toUpperCase(),
    value: v,
    className: k === 'valid' ? 'band-low' : k === 'invalid' ? 'band-critical' : '',
  }))
  const tamperData = Object.entries(stats.tampering).map(([k, v]) => ({
    label: k.replace(/_/g, ' '),
    value: v,
    className: k === 'no_obvious_manipulation' ? 'band-low' : k === 'unknown' ? '' : 'band-high',
  }))
  const faceData = Object.entries(stats.face).map(([k, v]) => ({
    label: k.replace(/_/g, ' ').toUpperCase(),
    value: v,
    className: k === 'match' ? 'band-low' : k === 'no_match' ? 'band-critical' : '',
  }))
  const review = stats.human_review
  const anchored = stats.integrity

  return (
    <div className="grid-charts">
      <Panel title="Screenings per Day" subtitle={`Last ${stats.screenings_per_day.window_days} days${stats.screenings_per_day.older_than_window ? ` · ${stats.screenings_per_day.older_than_window} older` : ''}`}>
        <BarChart data={stats.screenings_per_day.days.map((d) => ({ label: dayLabel(d.day), value: d.count }))} />
      </Panel>
      <Panel title="Risk Distribution" subtitle={`${stats.total} screening(s) in the current filter window`}>
        <HBarChart data={riskData} />
      </Panel>
      <Panel title="Document Type Distribution">
        <HBarChart data={docData} />
      </Panel>
      <Panel title="Validation Outcomes">
        <HBarChart data={valData} />
      </Panel>
      <Panel title="Tampering Detection Statistics">
        <HBarChart data={tamperData} />
      </Panel>
      <Panel title="Face Verification Outcomes">
        <HBarChart data={faceData} />
      </Panel>
      <Panel title="Human-Review Cases">
        <div className="big-stats">
          <div className="big-stat stat-warn">
            <span className="big-stat-num">{review.true ?? 0}</span>
            <span className="big-stat-label">flagged for review</span>
          </div>
          <div className="big-stat">
            <span className="big-stat-num">{review.false ?? 0}</span>
            <span className="big-stat-label">no review flag</span>
          </div>
        </div>
      </Panel>
      <Panel title="Blockchain Audit Integrity" subtitle={anchored.note}>
        <div className="big-stats">
          <div className="big-stat">
            <span className="big-stat-num">{anchored.total_screenings}</span>
            <span className="big-stat-label">screenings</span>
          </div>
          <div className="big-stat stat-ok">
            <span className="big-stat-num">{anchored.anchored_on_chain}</span>
            <span className="big-stat-label">anchored on chain</span>
          </div>
          <div className="big-stat">
            <span className="big-stat-num">{anchored.audit_logged}</span>
            <span className="big-stat-label">audit-logged</span>
          </div>
          <div className="big-stat stat-warn">
            <span className="big-stat-num">{anchored.pending}</span>
            <span className="big-stat-label">pending anchor</span>
          </div>
        </div>
      </Panel>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Case inspector
// ---------------------------------------------------------------------------
function CheckRow({ status, field, message }: { status: string | null; field: string | null; message: string | null }) {
  const cls = status === 'PASS' ? 'check-pass' : status === 'FAIL' ? 'check-fail' : 'check-warn'
  return (
    <li className={`check ${cls}`}>
      <strong>{field ?? '—'}</strong>
      <span className={`badge ${status === 'PASS' ? 'band-low' : status === 'FAIL' ? 'band-critical' : 'band-medium'}`}>{status}</span>
      <p>{message}</p>
    </li>
  )
}

function CaseInspector({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [kase, setKase] = useState<InvestigationCase | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setBusy(true)
    setError(null)
    api.investigationCase(runId)
      .then((res) => { if (alive) setKase(res) })
      .catch((err) => { if (alive) setError(err instanceof ApiError ? err.message : 'Could not load case') })
      .finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [runId])

  if (busy) return <Spinner label="Loading case…" />
  if (error) return <ErrorBanner message={error} />
  if (!kase) return null

  const verify = kase.audit.verify
  const verifyClass = verify?.verified ? 'band-low' : verify?.record_changed ? 'band-critical' : 'band-medium'

  return (
    <div className="grid-charts">
      <Panel
        title={`Case ${shortRunId(kase.run_id)}`}
        subtitle={`Completed ${formatDateTime(kase.completed_at)} · pipeline status: ${kase.status}`}
        actions={<button className="btn btn-ghost" onClick={onBack}>← Back to search</button>}
      >
        <div className="case-head">
          <RiskBadge band={(kase.risk.risk_level?.toLowerCase() as InvestigationRow['risk_band']) ?? null} score={kase.risk.risk_score} />
          {kase.human_review_required && <span className="badge band-high">⚠ HUMAN REVIEW REQUIRED</span>}
          <span className="badge badge-mock mono">{DOC_TYPE_LABELS[kase.doc_type_detected ?? kase.doc_type_hint ?? 'unknown'] ?? 'Unknown doc'}</span>
          {kase.final_status && <span className="badge band-unknown">{kase.final_status}</span>}
        </div>
        {kase.pipeline_error && <ErrorBanner message={`Pipeline error: ${kase.pipeline_error}`} />}
        <p className="hint mono break">sha256: {kase.file_sha256}</p>
      </Panel>

      <Panel title="Risk Factors" subtitle="Every point is reconstructible from the contribution table">
        <ul className="contrib-list">
          {(kase.risk.contributions ?? []).map((c, i) => (
            <li key={i} className="contrib-row">
              <span className="contrib-points">+{c.points}</span>
              <div>
                <strong>{c.signal}</strong>
                {c.details && <p className="hint">{c.details}</p>}
              </div>
            </li>
          ))}
          {(kase.risk.contributions ?? []).length === 0 && <li className="hint">No adverse contributions recorded.</li>}
        </ul>
        {kase.risk.reasons?.length > 0 && (
          <>
            <h3 className="mini-heading">Explainability</h3>
            <ul className="mini-list">
              {kase.risk.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </>
        )}
      </Panel>

      <Panel title="Validation Failures" subtitle={kase.validation.ruleset ? `ruleset: ${kase.validation.ruleset}` : undefined}>
        <div className="validity-row">
          <span className={`badge ${kase.validation.is_valid ? 'band-low' : kase.validation.is_valid === false ? 'band-critical' : 'band-unknown'}`}>
            {kase.validation.is_valid === true ? 'VALID' : kase.validation.is_valid === false ? 'INVALID' : 'UNKNOWN'}
          </span>
          {typeof kase.validation.confidence === 'number' && (
            <span className="hint">confidence {Math.round(kase.validation.confidence * 100)}%</span>
          )}
        </div>
        {kase.validation.failures?.length > 0 && (
          <ul className="mini-list">
            {kase.validation.failures.map((f, i) => <li key={i} className="flag-text">✗ {f}</li>)}
          </ul>
        )}
        {kase.validation.warnings?.length > 0 && (
          <ul className="mini-list">
            {kase.validation.warnings.map((w, i) => <li key={i} className="warn-text">⚠ {w}</li>)}
          </ul>
        )}
        <ul className="check-list">
          {(kase.validation.checks ?? []).map((c, i) => <CheckRow key={i} status={c.status} field={c.field} message={c.message} />)}
        </ul>
      </Panel>

      <Panel title="Tampering Indicators" subtitle="Heuristic forensic signals — never proof of forgery">
        <div className="validity-row">
          <span className={`badge ${kase.tampering.verdict === 'no_obvious_manipulation' ? 'band-low' : kase.tampering.verdict === 'unknown' ? 'band-unknown' : 'band-high'}`}>
            {(kase.tampering.verdict ?? 'unknown').replace(/_/g, ' ').toUpperCase()}
          </span>
          {typeof kase.tampering.risk_score === 'number' && (
            <span className="hint">forensic risk {kase.tampering.risk_score}/100</span>
          )}
        </div>
        {kase.tampering.explanation && <p className="hint">{kase.tampering.explanation}</p>}
        <ul className="check-list">
          {(kase.tampering.indicators ?? []).map((ind, i) => (
            <li key={i} className="check">
              <strong>{(ind.type ?? 'indicator').replace(/_/g, ' ')}</strong>
              <span className={`badge ${ind.severity === 'high' ? 'band-critical' : ind.severity === 'medium' ? 'band-high' : 'band-medium'}`}>{ind.severity ?? '—'}</span>
              {typeof ind.confidence === 'number' && <span className="hint">{Math.round(ind.confidence * 100)}%</span>}
              <p>{ind.note}</p>
            </li>
          ))}
          {(kase.tampering.indicators ?? []).length === 0 && <li className="hint">No forensic indicators fired.</li>}
        </ul>
      </Panel>

      <Panel title="Face Verification" subtitle="One-to-one verification only — never population search">
        <div className="validity-row">
          <span className={`badge ${kase.face.match_status === 'MATCH' ? 'band-low' : kase.face.match_status === 'NO_MATCH' ? 'band-critical' : 'band-medium'}`}>
            {kase.face.match_status ?? 'NOT PERFORMED'}
          </span>
          {typeof kase.face.similarity_score === 'number' && (
            <span className="hint">similarity {kase.face.similarity_score.toFixed(4)}</span>
          )}
          {typeof kase.face.confidence === 'number' && (
            <span className="hint">confidence {Math.round(kase.face.confidence * 100)}%</span>
          )}
        </div>
        {(kase.face.warnings ?? []).length > 0 && (
          <ul className="mini-list">
            {kase.face.warnings!.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        )}
      </Panel>

      <Panel title="Blockchain Audit Integrity" subtitle="Recomputed hash vs the ledger commitment">
        {verify ? (
          <div className="validity-row">
            <span className={`badge ${verifyClass}`}>{verify.conclusion}</span>
            <span className="hint">backend: {verify.ledger_backend}</span>
          </div>
        ) : (
          <p className="hint">{kase.audit.note ?? 'Integrity check unavailable.'}</p>
        )}
        <ul className="mini-list">
          {kase.audit.events.map((e, i) => (
            <li key={i} className="mono">
              {e.event_type} · {e.anchor_status}
              {e.tx_hash ? ` · tx ${e.tx_hash.slice(0, 14)}…` : ''} · {formatDateTime(e.created_at)}
            </li>
          ))}
          {kase.audit.events.length === 0 && <li>No audit events recorded yet.</li>}
        </ul>
      </Panel>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------
export function Investigation() {
  const [filters, setFilters] = useState<InvestigationFilters>({ ...EMPTY_FILTERS })
  const [rows, setRows] = useState<InvestigationRow[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [stats, setStats] = useState<InvestigationStats | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const PAGE = 10

  const load = useCallback(async (f: InvestigationFilters, off: number) => {
    setBusy(true)
    setError(null)
    try {
      const clean: InvestigationFilters = { ...f }
      if (!clean.review_required) delete clean.review_required
      const [search, st] = await Promise.all([
        api.investigationSearch(clean, PAGE, off),
        api.investigationStats(clean),
      ])
      setRows(search.items)
      setTotal(search.total)
      setStats(st)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Search failed')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { load({ ...EMPTY_FILTERS }, 0) }, [load])

  const suspiciousFirst = useMemo(
    () => [...rows].sort((a, b) => Number(b.human_review_required) - Number(a.human_review_required)),
    [rows],
  )

  if (selected) return <CaseInspector runId={selected} onBack={() => setSelected(null)} />

  return (
    <div className="inv-view">
      <FilterBar
        filters={filters}
        onChange={setFilters}
        onSearch={() => { setOffset(0); load(filters, 0) }}
        busy={busy}
      />
      {error && <ErrorBanner message={error} />}

      {stats && <IntelligencePanels stats={stats} />}

      <Panel
        title="Case Search Results"
        subtitle={`${total} matching screening(s)${total > 0 ? ' · click a row to inspect' : ''}`}
        actions={busy ? <Spinner label="Searching…" /> : undefined}
      >
        {suspiciousFirst.length === 0 && !busy ? (
          <p className="hint">No screenings match the current filters.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Document</th>
                  <th>Status</th>
                  <th>Risk</th>
                  <th>Review</th>
                  <th>Created (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {suspiciousFirst.map((r) => (
                  <tr key={r.run_id} className="row-click" onClick={() => setSelected(r.run_id)}>
                    <td><code>{shortRunId(r.run_id)}</code></td>
                    <td>{DOC_TYPE_LABELS[r.doc_type_detected ?? r.doc_type_hint ?? 'unknown'] ?? 'Unknown'}</td>
                    <td><span className={`status-text status-text-${r.status}`}>{r.status}</span></td>
                    <td><RiskBadge band={r.risk_band} score={r.risk_score} /></td>
                    <td>{r.human_review_required ? <span className="flag-text">⚠ Yes</span> : '—'}</td>
                    <td>{formatDateTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="pager">
          <button
            className="btn btn-ghost"
            disabled={offset === 0 || busy}
            onClick={() => { const off = Math.max(0, offset - PAGE); setOffset(off); load(filters, off) }}
          >
            ← Previous
          </button>
          <span className="hint">{total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <button
            className="btn btn-ghost"
            disabled={offset + PAGE >= total || busy}
            onClick={() => { const off = offset + PAGE; setOffset(off); load(filters, off) }}
          >
            Next →
          </button>
        </div>
      </Panel>

      <p className="hint inv-privacy">
        Privacy: search operates on screening metadata only (run id, file hash,
        document type). Names, document numbers and images are never exposed in
        this view; charts show aggregate counts only.
      </p>
    </div>
  )
}
