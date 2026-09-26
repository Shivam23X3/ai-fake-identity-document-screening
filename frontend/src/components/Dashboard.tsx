/**
 * Main dashboard: upload → analyze → panels, plus history.
 * All data comes from the Step-2 backend; AI-module panels render honest
 * "module pending" states until real engines are plugged in.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { AnalyzeResponse, HistoryItem, ScreeningResult, UploadResponse } from '../types'
import { BandLegend, ErrorBanner, InfoBanner, Panel, Spinner } from './ui'
import { UploadArea } from './UploadArea'
import { HistoryTable } from './HistoryTable'
import {
  ConsolidatedResultPanel,
  DocumentPreviewPanel,
  FacePanel,
  FinalResultPanel,
  OcrPanel,
  PipelineOverviewPanel,
  RiskPanel,
  TamperingPanel,
  ValidationPanel,
} from './AnalysisPanels'
import { BlockchainPanel } from './BlockchainPanel'
import { Investigation } from './Investigation'
import { DemoMode } from './DemoMode'
import { ReviewPanel } from './ReviewPanel'

type View = 'new-screening' | 'detail' | 'investigation' | 'review' | 'demo'

const ROLE_LABELS: Record<string, string> = {
  ADMIN: 'Administrator',
  SECURITY_OFFICER: 'Security Officer',
  REVIEWER: 'Reviewer',
}

export function Dashboard({
  operator,
  role,
  mustChangePassword,
  onLogout,
}: {
  operator: string
  role: 'ADMIN' | 'SECURITY_OFFICER' | 'REVIEWER'
  mustChangePassword: boolean
  onLogout: () => void
}) {
  const [view, setView] = useState<View>('new-screening')
  const [lastUpload, setLastUpload] = useState<{ upload: UploadResponse; file: File } | null>(null)
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)
  const [consolidated, setConsolidated] = useState<ScreeningResult | null>(null)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [backendUp, setBackendUp] = useState<boolean | null>(null)

  const refreshHistory = useCallback(async () => {
    try {
      const res = await api.history(20, 0)
      setHistory(res.items)
    } catch {
      /* history is non-critical; detail view shows errors */
    }
  }, [])

  // Backend health pulse + initial history load.
  useEffect(() => {
    api.health()
      .then(() => setBackendUp(true))
      .catch(() => setBackendUp(false))
    refreshHistory()
  }, [refreshHistory])

  async function handleUploaded(upload: UploadResponse, file: File) {
    setLastUpload({ upload, file })
    setError(null)
    setBusy(true)
    setConsolidated(null)
    setView('new-screening')
    try {
      const result = await api.analyze(upload.run_id)
      setAnalysis(result)
      refreshHistory()
      // Step-9 consolidated payload (orchestrator + audit trail); advisory
      // only — the per-stage panels above stay the primary evidence view.
      try {
        setConsolidated(await api.result(upload.run_id))
      } catch {
        /* result endpoint is supplementary; per-stage panels suffice */
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Analysis failed')
    } finally {
      setBusy(false)
    }
  }

  async function openDetail(runId: string) {
    setBusy(true)
    setError(null)
    setConsolidated(null)
    try {
      const detail = await api.detail(runId)
      try {
        setConsolidated(await api.result(runId))
      } catch {
        /* legacy runs without a Step-9 report: per-stage panels suffice */
      }
      // Normalize: detail wraps stages inside report; build an AnalyzeResponse-like view.
      setAnalysis({
        success: true,
        run_id: detail.run_id,
        status: detail.status,
        doc_type_hint: detail.doc_type_hint,
        doc_type_detected: detail.doc_type_detected,
        risk_score: detail.risk_score,
        risk_band: detail.risk_band,
        human_review_required: detail.human_review_required,
        stages: detail.report.stages ?? [],
        disclaimer: detail.disclaimer,
      })
      setLastUpload(null)
      setView('detail')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load screening')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-emblem" aria-hidden>🛡</span>
          <div>
            <h1>Document Screening System</h1>
            <p className="topbar-sub">AI-Assisted · Decision Support · Human-in-the-Loop</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`conn-dot ${backendUp === false ? 'conn-down' : 'conn-up'}`} />
          <span className="conn-text">
            {backendUp === false ? 'Backend offline' : backendUp === true ? 'Backend online' : 'Connecting…'}
          </span>
          <span className="operator-chip" title={ROLE_LABELS[role] ?? role}>
            👤 {operator} · {ROLE_LABELS[role] ?? role}
          </span>
          <button className="btn btn-ghost" onClick={onLogout}>Sign out</button>
        </div>
      </header>

      <nav className="tabs">
        <button
          className={`tab ${view === 'new-screening' ? 'tab-active' : ''}`}
          onClick={() => setView('new-screening')}
        >
          New Screening
        </button>
        <button
          className={`tab ${view === 'detail' ? 'tab-active' : ''}`}
          onClick={() => setView('detail')}
          disabled={!analysis}
        >
          Screening Detail
        </button>
        <button
          className={`tab ${view === 'investigation' ? 'tab-active' : ''}`}
          onClick={() => setView('investigation')}
        >
          Investigation & Intelligence
        </button>
        <button
          className={`tab ${view === 'review' ? 'tab-active' : ''}`}
          onClick={() => setView('review')}
        >
          Human Review
        </button>
        <button
          className={`tab ${view === 'demo' ? 'tab-active' : ''}`}
          onClick={() => setView('demo')}
        >
          🎬 SIH Demo
        </button>
      </nav>

      <main className="main">
        {backendUp === false && (
          <ErrorBanner message="Cannot reach the backend. Start it with: .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000" />
        )}
        {error && <ErrorBanner message={error} />}
        {mustChangePassword && (
          <div className="banner banner-warning" role="alert">
            <strong>Rotate the bootstrap password now.</strong> Your account was
            seeded from environment credentials; change it via
            <code> POST /api/auth/change-password</code> (UI dialog in a later pass).
          </div>
        )}
        {busy && <Spinner label="Processing…" />}

        {view === 'new-screening' && (
          <div className="grid-new-screening">
            {role === 'REVIEWER' ? (
              <InfoBanner>
                Reviewer mode: inspect flagged cases from the history below.
                Running new screenings requires the SECURITY_OFFICER role.
              </InfoBanner>
            ) : (
              <UploadArea onUploaded={handleUploaded} />
            )}

            {analysis && (
              <>
                <FinalResultPanel analysis={analysis} />
                <PipelineOverviewPanel stages={analysis.stages} />
                <OcrPanel stage={analysis.stages.find((s) => s.stage === 'ocr')} />
                <ValidationPanel stage={analysis.stages.find((s) => s.stage === 'document_validation')} />
                <TamperingPanel stage={analysis.stages.find((s) => s.stage === 'tampering_detection')} />
                <FacePanel stage={analysis.stages.find((s) => s.stage === 'face_verification')} />
                <RiskPanel
                  stage={analysis.stages.find((s) => s.stage === 'risk_assessment')}
                  riskScore={analysis.risk_score}
                  riskBand={analysis.risk_band}
                />
                {consolidated && <ConsolidatedResultPanel result={consolidated} />}
                {consolidated && <BlockchainPanel result={consolidated} />}
              </>
            )}

            {!analysis && !busy && (
              <InfoBanner>
                Upload a document to run the screening pipeline. Preprocessing and OCR
                extract fields with per-field confidence; fields below threshold are
                flagged for human review. Modules still pending implementation are
                shown honestly as “module pending” — no simulated results are produced.
              </InfoBanner>
            )}
          </div>
        )}

        {view === 'investigation' && <Investigation />}

        {view === 'review' && <ReviewPanel onOpenDetail={openDetail} />}

        {view === 'demo' && <DemoMode onOpenDetail={openDetail} />}

        {view === 'detail' && analysis && (
          <div className="grid-detail">
            {lastUpload ? (
              <DocumentPreviewPanel
                runId={analysis.run_id}
                filename={lastUpload.file.name}
                sizeBytes={lastUpload.file.size}
                sha256={lastUpload.upload.file.sha256}
                docTypeHint={analysis.doc_type_hint ?? 'unknown'}
                docTypeDetected={analysis.doc_type_detected}
              />
            ) : (
              <Panel title="Document Preview">
                <p className="hint">
                  Detailed report for run <code>{analysis.run_id}</code>. Preview available
                  for fresh uploads in this session.
                </p>
              </Panel>
            )}
            <FinalResultPanel analysis={analysis} />
            <PipelineOverviewPanel stages={analysis.stages} />
            <OcrPanel stage={analysis.stages.find((s) => s.stage === 'ocr')} />
            <ValidationPanel stage={analysis.stages.find((s) => s.stage === 'document_validation')} />
            <TamperingPanel stage={analysis.stages.find((s) => s.stage === 'tampering_detection')} />
            <FacePanel stage={analysis.stages.find((s) => s.stage === 'face_verification')} />
            <RiskPanel
              stage={analysis.stages.find((s) => s.stage === 'risk_assessment')}
              riskScore={analysis.risk_score}
              riskBand={analysis.risk_band}
            />
            {consolidated && <ConsolidatedResultPanel result={consolidated} />}
            {consolidated && <BlockchainPanel result={consolidated} />}
          </div>
        )}

        <Panel
          title="Screening History"
          subtitle="All runs recorded in the local database"
          actions={<BandLegend />}
        >
          <HistoryTable items={history} onSelect={openDetail} />
        </Panel>
      </main>

      <footer className="footer">
        <span>
          SIH 2026 prototype · mock/demo data only · no government database connection ·
          AI output is advisory; final decisions rest with authorized personnel.
        </span>
      </footer>
    </div>
  )
}
