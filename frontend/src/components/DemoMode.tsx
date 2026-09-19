/**
 * Step 14 — SIH demonstration mode panel.
 *
 * Four scripted cases (valid / tampered / impostor / low-quality) run through
 * the REAL pipeline on synthetic SPECIMEN fixtures. Everything here is loudly
 * labeled DEMONSTRATION / SIMULATED; expected-vs-actual checkpoints are
 * computed from the real result and shown honestly (a failed checkpoint is
 * displayed as failed — never papered over).
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { DemoCasesResponse, DemoRunResponse } from '../types'
import { ErrorBanner, Panel, Spinner } from './ui'

const CASE_EMOJI: Record<string, string> = {
  case1_valid: '✅',
  case2_tampered: '🔍',
  case3_identity_mismatch: '🚫',
  case4_low_quality: '🌫',
}

function ChecksTable({ run }: { run: DemoRunResponse }) {
  const checks = run.result.demo?.checks ?? []
  if (checks.length === 0) return null
  return (
    <div className="demo-checks">
      <h3>Expected vs actual (measured on the real pipeline result)</h3>
      <ul>
        {checks.map((c) => (
          <li key={c.criterion} className={c.met ? 'check-met' : 'check-unmet'}>
            <span className="check-mark">{c.met ? '✓' : '✗'}</span>
            <div>
              <strong>{c.criterion}</strong>
              {c.informational && <span className="badge badge-mock">informational</span>}
              <p>{c.detail}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

function RunSummary({ run, onOpenDetail }: { run: DemoRunResponse; onOpenDetail: (runId: string) => void }) {
  const r = run.result
  const face = (r.face_verification ?? {}) as Record<string, unknown>
  const tampering = (r.tampering ?? {}) as Record<string, unknown>
  const risk = r.risk_assessment
  return (
    <Panel
      className="demo-run-panel"
      title={`${CASE_EMOJI[run.case_id] ?? '🎬'} ${run.case_title ?? run.case_id}`}
      subtitle={`run ${run.run_id} · ${r.processing_time_ms} ms`}
      actions={
        <button className="btn btn-ghost" onClick={() => onOpenDetail(run.run_id)}>
          Open full report →
        </button>
      }
    >
      <div className="demo-notice">
        ⚠️ {run.demo_notice}
      </div>
      <div className="demo-kpis">
        <div className="demo-kpi">
          <span className="demo-kpi-label">Risk</span>
          <strong>{risk?.risk_level ?? 'N/A'} · {risk?.risk_score ?? '—'}</strong>
        </div>
        <div className="demo-kpi">
          <span className="demo-kpi-label">Face</span>
          <strong>{String(face.match_status ?? 'N/A')}</strong>
          {typeof face.similarity_score === 'number' && (
            <span className="demo-kpi-sub">sim {face.similarity_score.toFixed(3)}</span>
          )}
        </div>
        <div className="demo-kpi">
          <span className="demo-kpi-label">Tampering</span>
          <strong>{String(tampering.verdict ?? 'N/A')}</strong>
        </div>
        <div className="demo-kpi">
          <span className="demo-kpi-label">Review</span>
          <strong>{r.human_review_required ? 'Required' : 'Not required'}</strong>
        </div>
      </div>
      <ChecksTable run={run} />
    </Panel>
  )
}

export function DemoMode({ onOpenDetail }: { onOpenDetail: (runId: string) => void }) {
  const [catalog, setCatalog] = useState<DemoCasesResponse | null>(null)
  const [running, setRunning] = useState<string | null>(null)
  const [lastRun, setLastRun] = useState<DemoRunResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadCatalog = useCallback(async () => {
    try {
      setCatalog(await api.demoCases())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the demo case catalog')
    }
  }, [])

  useEffect(() => {
    void loadCatalog()
  }, [loadCatalog])

  const runCase = useCallback(async (caseId: string) => {
    setRunning(caseId)
    setError(null)
    try {
      setLastRun(await api.demoRun(caseId))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The demo case failed to run')
    } finally {
      setRunning(null)
    }
  }, [])

  return (
    <div className="grid-demo">
      {error && <ErrorBanner message={error} />}
      <Panel
        title="🎬 SIH Demonstration Mode"
        subtitle="Four scripted scenarios executed by the real screening pipeline"
        actions={
          <button className="btn btn-ghost" onClick={() => void loadCatalog()}>
            Reload catalog
          </button>
        }
      >
        <div className="demo-notice demo-notice-top">
          ⚠️ {catalog?.notice ??
            'DEMONSTRATION / SIMULATED DATA ONLY. Synthetic specimen fixtures with fabricated identities. No real document, no government verification.'}
        </div>
        {!catalog && <Spinner label="Loading demo cases…" />}
        {catalog && (
          <div className="demo-cases">
            {Object.entries(catalog.cases).map(([caseId, c]) => (
              <article key={caseId} className="demo-case-card">
                <header>
                  <h3>
                    {CASE_EMOJI[caseId] ?? '🎬'} {c.title ?? caseId}
                  </h3>
                  <button
                    className="btn btn-primary"
                    disabled={running !== null}
                    onClick={() => void runCase(caseId)}
                  >
                    {running === caseId ? 'Running…' : 'Run case'}
                  </button>
                </header>
                <p className="demo-storyline">{c.storyline}</p>
                <p className="demo-expected">
                  <strong>Expected:</strong> {c.expected.join(' · ')}
                </p>
                <p className="demo-fixtures">
                  fixtures: <code>{c.document?.filename}</code> + <code>{c.probe?.filename}</code>
                </p>
              </article>
            ))}
          </div>
        )}
      </Panel>

      {running && <Spinner label={`Running ${running} through the full pipeline…`} />}
      {lastRun && <RunSummary run={lastRun} onOpenDetail={onOpenDetail} />}
    </div>
  )
}
