/**
 * Analysis panels: Document Preview, OCR, Validation, Tampering, Face,
 * Risk, Final Result. Every panel reads the REAL StageResult payloads the
 * backend produced; when a module is a placeholder the panel shows the
 * "module pending" state — no fake AI results anywhere.
 */
import type { AnalyzeResponse, ScreeningResult, StageResult } from '../types'
import { DOC_TYPE_LABELS, bandInfo, formatConfidence } from '../helpers'
import {
  ConfidenceBar, HumanReviewNotice, InfoBanner, MockTag,
  ModulePendingNote, Panel, RiskBadge,
} from './ui'
import { StageCard } from './StageCard'

/** Interpret a stage payload honestly: implemented vs module-pending. */
function stagePayload(stage: StageResult | undefined) {
  if (!stage) return { pending: true as const, note: 'No result from backend.' }
  const data = (stage.data ?? {}) as Record<string, unknown>
  const implemented = data.implemented === true || stage.status === 'ok'
  return {
    pending: !implemented,
    data,
    note: (data.note as string) ?? null,
    engine: (data.engine as string) ?? null,
  }
}

// ---------------------------------------------------------------------------
export function DocumentPreviewPanel({
  runId, filename, sizeBytes, sha256, docTypeHint, docTypeDetected,
}: {
  runId: string
  filename: string
  sizeBytes: number
  sha256: string
  docTypeHint: string
  docTypeDetected: string | null
}) {
  return (
    <Panel
      title="Document Preview"
      subtitle={`run_id: ${runId}`}
      actions={<MockTag>SOURCE: LIVE UPLOAD</MockTag>}
    >
      <div className="detail-preview">
        <img src={apiFileUrl(runId)} alt="Stored document" className="detail-img" />
        <dl className="kv">
          <div><dt>Filename</dt><dd>{filename}</dd></div>
          <div><dt>Size</dt><dd>{formatBytesLocal(sizeBytes)}</dd></div>
          <div><dt>SHA-256</dt><dd className="mono break">{sha256}</dd></div>
          <div>
            <dt>Declared type</dt>
            <dd>{DOC_TYPE_LABELS[docTypeHint] ?? docTypeHint}</dd>
          </div>
          <div>
            <dt>AI-detected type</dt>
            <dd>{docTypeDetected ? DOC_TYPE_LABELS[docTypeDetected] ?? docTypeDetected : '— (OCR pending)'}</dd>
          </div>
        </dl>
      </div>
    </Panel>
  )
}
function apiFileUrl(runId: string): string {
  return `/api/screening/${runId}/file`
}
function formatBytesLocal(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

// ---------------------------------------------------------------------------
const OCR_FIELD_LABELS: Record<string, string> = {
  full_name: 'Full Name',
  passport_number: 'Passport Number',
  nationality: 'Nationality',
  date_of_birth: 'Date of Birth',
  expiry_date: 'Expiry Date',
  gender: 'Gender',
  visa_number: 'Visa Number',
  visa_type: 'Visa Type',
  entry_validity: 'Entry Validity',
  stay_duration: 'Stay Duration',
  document_number: 'Document Number',
  surname: 'Surname',
  given_names: 'Given Names',
  issuing_country: 'Issuing Country',
  personal_number: 'Personal Number',
  document_code: 'Document Code',
}
const SOURCE_LABELS: Record<string, string> = {
  mrz: 'MRZ',
  visual_zone: 'Visual zone',
  header: 'Header',
}

export function OcrPanel({ stage }: { stage: StageResult | undefined }) {
  const { pending, data, note } = stagePayload(stage)
  const fields = (data?.ocr_fields as OcrFieldRow[]) ?? []
  const mrzRaw = (data?.mrz_raw as string | null) ?? null
  const mrzFormat = (data?.mrz_format as string | null) ?? null
  const rawText = (data?.raw_text as string | null) ?? null
  const overall = (data?.overall_confidence as number | null) ?? null
  const flaggedCount = fields.filter((f) => f.flagged).length

  return (
    <Panel
      title="OCR Information"
      subtitle={`Extracted fields with per-field confidence${overall !== null ? ` · overall ${Math.round(overall * 100)}%` : ''}`}
    >
      {pending ? (
        <ModulePendingNote
          capability="OCR (RapidOCR + MRZ parser)"
          note={note ?? 'OCR engine unavailable in this deployment.'}
        />
      ) : (
        <>
          {fields.length === 0 && <InfoBanner>No fields extracted — verify readability.</InfoBanner>}
          {fields.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>Field</th><th>Value</th><th>Source</th><th>Confidence</th></tr>
                </thead>
                <tbody>
                  {fields.map((f) => (
                    <tr key={f.name} className={f.flagged ? 'row-flagged' : ''}>
                      <td>{OCR_FIELD_LABELS[f.name] ?? f.name}</td>
                      <td className="mono">{f.value || '—'}</td>
                      <td className="hint">{SOURCE_LABELS[f.source ?? ''] ?? '—'}</td>
                      <td className={f.confidence < 0.6 ? 'conf-text-low' : 'conf-text-ok'}>
                        {Math.round(f.confidence * 100)}%
                        {f.check_digit_ok === true && <span title="MRZ check digit verified"> ✓</span>}
                        {f.check_digit_ok === false && <span title="MRZ check digit mismatch"> ⚠</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {mrzRaw && (
            <div className="mrz-block">
              <h4>MRZ (machine-readable zone){mrzFormat ? ` · ${mrzFormat}` : ''}</h4>
              <pre className="mono">{mrzRaw}</pre>
            </div>
          )}
          {flaggedCount > 0 && (
            <WarningBannerLocal>
              {flaggedCount} field{flaggedCount > 1 ? 's' : ''} flagged (low confidence,
              {' '}check-digit mismatch or missing) — verify against the image.
            </WarningBannerLocal>
          )}
          {rawText && rawText.trim() && (
            <details className="raw-text-details">
              <summary>Raw OCR text</summary>
              <pre className="mono">{rawText}</pre>
            </details>
          )}
        </>
      )}
    </Panel>
  )
}
interface OcrFieldRow {
  name: string
  value: string
  confidence: number
  flagged?: boolean
  source?: string
  check_digit_ok?: boolean | null
  notes?: string[]
}
function WarningBannerLocal({ children }: { children: React.ReactNode }) {
  return <div className="banner banner-warning">{children}</div>
}

// ---------------------------------------------------------------------------
export function ValidationPanel({ stage }: { stage: StageResult | undefined }) {
  const { pending, data, note } = stagePayload(stage)
  const validation = data?.validation as ValidationBlock | undefined
  const checks = (data?.checks as ValidationCheckRow[] | undefined) ?? validation?.checks ?? []
  const isValid = (data?.is_valid as boolean | undefined) ?? validation?.is_valid
  const confidence =
    (data?.confidence as number | null | undefined) ?? validation?.confidence ?? null
  const failures = (data?.failures as string[] | undefined) ?? validation?.failures ?? []
  const warnings = validation?.warnings ?? []
  const registry = (data?.registry as RegistryHit | null | undefined) ?? validation?.registry ?? null
  const notes = validation?.notes ?? []
  const ruleset = data?.ruleset as string | undefined

  const passCount = checks.filter((c) => c.status === 'PASS').length
  const warnCount = checks.filter((c) => c.status === 'WARNING').length
  const failCount = checks.filter((c) => c.status === 'FAIL').length

  return (
    <Panel
      title="Document Validation"
      subtitle={`Configurable rule engine${ruleset ? ` · ruleset: ${ruleset}` : ''} · registry lookups are MOCK`}
    >
      {pending ? (
        <ModulePendingNote
          capability="document validation"
          note={note ?? 'Validation engine unavailable in this deployment.'}
        />
      ) : (
        <>
          {typeof isValid === 'boolean' && (
            <div className="validity-row">
              <span className={`badge ${isValid ? 'status-ok' : 'status-error'}`}>
                {isValid ? 'VALID (no failed checks)' : 'INVALID (failed checks)'}
              </span>
              <span className="hint">
                {passCount} passed · {warnCount} warning{warnCount === 1 ? '' : 's'} · {failCount} failed
              </span>
            </div>
          )}
          <ConfidenceBar value={confidence ?? null} label="Validation confidence (mean OCR confidence of checked fields)" />

          {checks.length === 0 && (
            <InfoBanner>No validation checks recorded.</InfoBanner>
          )}
          {checks.length > 0 && (
            <ul className="check-list">
              {checks.map((c, i) => (
                <li
                  key={`${c.field}-${i}`}
                  className={`check check-${c.status === 'PASS' ? 'pass' : c.status === 'FAIL' ? 'fail' : 'warn'}`}
                >
                  <span className="check-icon">
                    {c.status === 'PASS' ? '✓' : c.status === 'FAIL' ? '✗' : '⚠'}
                  </span>
                  <div>
                    <strong>{OCR_FIELD_LABELS[c.field] ?? c.field}</strong>
                    <p>{c.message}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {failures.length > 0 && (
            <div className="banner banner-error">
              <strong>Validation failures:</strong>
              <ul>{failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
            </div>
          )}
          {warnings.length > 0 && failures.length === 0 && (
            <div className="banner banner-warning">
              <strong>Warnings — human verification advised:</strong>
              <ul>{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
            </div>
          )}

          {registry && (
            <p className="hint">
              Registry hit: <MockTag>MOCK REGISTRY</MockTag>{' '}
              <code>{registry.doc_type} · {registry.doc_number} · {registry.status}</code>
              {registry.full_name ? ` · ${registry.full_name}` : ''}
            </p>
          )}

          {notes.length > 0 && (
            <details className="raw-text-details">
              <summary>Rule engine notes</summary>
              <ul className="mini-list">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </details>
          )}
          <p className="hint">
            Generic, simplified rules — not any country's official document rules.
            Registry data is clearly-marked demo data, never a government database.
          </p>
        </>
      )}
    </Panel>
  )
}
interface ValidationCheckRow { field: string; status: 'PASS' | 'FAIL' | 'WARNING'; message: string; rule?: string }
interface ValidationBlock {
  is_valid?: boolean
  checks?: ValidationCheckRow[]
  failures?: string[]
  warnings?: string[]
  confidence?: number | null
  registry?: RegistryHit | null
  notes?: string[]
}
interface RegistryHit {
  mock?: boolean
  doc_type?: string
  doc_number?: string
  full_name?: string
  status?: string
}

// ---------------------------------------------------------------------------
interface TamperingPayload {
  tampering_detected?: boolean
  risk_score?: number | null // 0-100
  confidence?: number | null // 0-1 confidence in the verdict
  verdict?: string
  indicators?: TamperingIndicator[]
  explanation?: string
  disclaimer?: string
  coverage?: number | null
  detector_failures?: string[]
  warnings?: string[]
  model?: { name?: string; available?: boolean }
}
interface TamperingIndicator {
  type: string
  severity: string
  confidence: number
  note?: string
}

const VERDICT_META: Record<string, { label: string; className: string; hint: string }> = {
  no_obvious_manipulation: {
    label: 'NO OBVIOUS MANIPULATION DETECTED',
    className: 'band-low',
    hint: 'Detectors ran and found nothing worth flagging. This is NOT a certificate of authenticity.',
  },
  suspicious: {
    label: 'SUSPICIOUS',
    className: 'band-medium',
    hint: 'Some signals fired — each is explainable by scanning/printing artifacts. Manual inspection recommended.',
  },
  likely_manipulated: {
    label: 'LIKELY MANIPULATED',
    className: 'band-high',
    hint: 'Multiple independent signals agree. Escalate to expert inspection — still not forensic proof.',
  },
  inconclusive: {
    label: 'INCONCLUSIVE',
    className: 'band-critical',
    hint: 'Image quality/coverage too poor to conclude anything either way. Inspect the original.',
  },
}

export function TamperingPanel({ stage }: { stage: StageResult | undefined }) {
  const { pending, data, note } = stagePayload(stage)
  const legacy = (data?.tampering as { signals?: TamperingSignal[] })?.signals ?? []
  const t: TamperingPayload = (data?.tampering as TamperingPayload) ?? {}
  const verdict = t.verdict ? VERDICT_META[t.verdict] : undefined
  const indicators = t.indicators ?? []

  return (
    <Panel title="Tampering Detection" subtitle="Probabilistic forensic signals — not proof">
      {pending ? (
        <ModulePendingNote
          capability="tampering detection"
          note={note ?? 'Tampering engine unavailable.'}
        />
      ) : (
        <>
          <div className="tamper-summary">
            <div>
              <span className="kv-label">Tampering risk</span>
              <ConfidenceBar
                value={t.risk_score != null ? t.risk_score / 100 : null}
                label="Risk (0-100)"
              />
            </div>
            <div>
              <span className="kv-label">Verdict confidence</span>
              <ConfidenceBar
                value={t.confidence ?? null}
                label="Verdict confidence"
              />
              <p className="hint">{verdict?.hint ?? 'A high risk score is a reason for inspection, not evidence of fraud.'}</p>
            </div>
          </div>
          {verdict && (
            <p className="validity-row">
              <span className={`badge ${verdict.className}`}>{verdict.label}</span>
              {t.tampering_detected != null && (
                <span className="hint">
                  tampering_detected: {t.tampering_detected ? 'true (indicator-level flag)' : 'false'}
                </span>
              )}
            </p>
          )}
          {t.explanation && <p className="hint">{t.explanation}</p>}
          {indicators.length > 0 && (
            <ul className="check-list">
              {indicators.map((ind, i) => (
                <li
                  key={i}
                  className={`check ${ind.severity === 'info' ? 'check-pass' : 'check-fail'}`}
                >
                  <span className="check-icon">{ind.severity === 'info' ? 'ℹ' : '⚑'}</span>
                  <div>
                    <strong>
                      {ind.type} · {ind.severity.toUpperCase()} · {Math.round(ind.confidence * 100)}%
                    </strong>
                    <p>{ind.note}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {indicators.length === 0 && legacy.length > 0 && (
            <ul className="check-list">
              {legacy.map((s, i) => (
                <li key={i} className={`check check-${s.suspicious ? 'fail' : 'pass'}`}>
                  <span className="check-icon">{s.suspicious ? '⚑' : '✓'}</span>
                  <div>
                    <strong>{s.name}</strong>
                    <p>{s.details}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {(t.warnings?.length || t.detector_failures?.length) && (
            <ul className="mini-list">
              {t.warnings?.map((wn, i) => <li key={`w${i}`}>{wn}</li>)}
              {t.detector_failures?.map((f, i) => <li key={`f${i}`}>Detector failure: {f}</li>)}
            </ul>
          )}
          {t.disclaimer && <p className="hint">{t.disclaimer}</p>}
        </>
      )}
    </Panel>
  )
}
interface TamperingSignal { name: string; suspicious: boolean; details?: string }

// ---------------------------------------------------------------------------
interface FaceVerificationPayload {
  face_detected_document?: boolean | null
  face_detected_presented_person?: boolean | null
  similarity_score?: number | null
  match_status?: 'MATCH' | 'NO_MATCH' | 'INCONCLUSIVE' | null
  confidence?: number | null
  similarity_match_threshold?: number
  similarity_no_match_max?: number
  reasons?: { code?: string; note?: string }[]
  document_face?: { box?: { x: number; y: number; w: number; h: number }; score?: number } | null
  presented_face?: { box?: { x: number; y: number; w: number; h: number }; score?: number } | null
  embedding_backend?: string
  one_to_one_only?: boolean
  no_population_search?: boolean
  disclaimer?: string
}

const MATCH_STATUS_META: Record<
  string,
  { label: string; className: string; hint: string }
> = {
  MATCH: {
    label: 'CONSISTENT WITH SAME PERSON',
    className: 'band-low',
    hint: 'Embeddings are consistent with the same person. Advisory evidence — not proof of identity.',
  },
  NO_MATCH: {
    label: 'NOT CONSISTENT',
    className: 'band-high',
    hint: 'Embeddings are NOT consistent with the same person. Escalate to manual inspection.',
  },
  INCONCLUSIVE: {
    label: 'INCONCLUSIVE',
    className: 'band-critical',
    hint: 'Verification could not complete (no face, poor quality, or missing input). Human inspection required.',
  },
}

export function FacePanel({ stage }: { stage: StageResult | undefined }) {
  const { pending, data, note } = stagePayload(stage)
  const fv = ((data?.face_verification as FaceVerificationPayload) ?? {}) as FaceVerificationPayload
  const statusMeta = fv.match_status ? MATCH_STATUS_META[fv.match_status] : undefined
  const sim = typeof fv.similarity_score === 'number' ? fv.similarity_score : null
  const matchMin = fv.similarity_match_threshold ?? 0.363
  const noMatchMax = fv.similarity_no_match_max ?? 0.25

  return (
    <Panel
      title="Face Verification"
      subtitle="One-to-one only · document portrait vs presented person · never population search"
    >
      {pending ? (
        <ModulePendingNote
          capability="face verification (YuNet + SFace embeddings)"
          note={note ?? 'Face-verification engine unavailable (models missing?).'}
        />
      ) : (
        <>
          <div className="face-grid">
            <div>
              <span className="kv-label">Similarity (cosine)</span>
              <ConfidenceBar
                value={sim != null ? Math.max(0, Math.min(1, sim)) : null}
                label="Cosine similarity"
              />
              <p className="hint">
                Match ≥ {matchMin} · no-match ≤ {noMatchMax} · gray zone ⇒ INCONCLUSIVE.
                {sim != null && (
                  <> Score: <strong>{sim.toFixed(3)}</strong></>
                )}
              </p>
            </div>
            <div>
              <span className="kv-label">Faces detected</span>
              <p className="hint">
                Document portrait: {fv.face_detected_document ? '✓ detected' : '✗ none'}
                {' · '}
                Presented person: {fv.face_detected_presented_person ? '✓ detected' : '✗ none'}
              </p>
              {typeof fv.confidence === 'number' && (
                <>
                  <span className="kv-label">Verdict confidence</span>
                  <ConfidenceBar value={fv.confidence} label="Verdict confidence" />
                </>
              )}
            </div>
          </div>

          {statusMeta && (
            <p className="validity-row">
              <span className={`badge ${statusMeta.className}`}>{statusMeta.label}</span>
            </p>
          )}
          <p className="hint">{statusMeta?.hint}</p>

          {fv.reasons && fv.reasons.length > 0 && (
            <div className="banner banner-warning">
              <strong>Safeguards triggered — verify manually:</strong>
              <ul>
                {fv.reasons.map((r, i) => <li key={i}>{r.note ?? r.code}</li>)}
              </ul>
            </div>
          )}

          {(fv.document_face || fv.presented_face) && (
            <details className="raw-text-details">
              <summary>Detection details</summary>
              <ul className="mini-list">
                {fv.document_face?.box && (
                  <li>
                    Document face box: {fv.document_face.box.w}×{fv.document_face.box.h}px
                    {typeof fv.document_face.score === 'number' &&
                      ` (detector confidence ${Math.round(fv.document_face.score * 100)}%)`}
                  </li>
                )}
                {fv.presented_face?.box && (
                  <li>
                    Presented face box: {fv.presented_face.box.w}×{fv.presented_face.box.h}px
                    {typeof fv.presented_face.score === 'number' &&
                      ` (detector confidence ${Math.round(fv.presented_face.score * 100)}%)`}
                  </li>
                )}
                {fv.embedding_backend && <li>Embedding backend: {fv.embedding_backend}</li>}
              </ul>
            </details>
          )}

          {fv.one_to_one_only && (
            <p className="hint">
              ⚠ 1:1 verification only — this module never performs biometric
              identification against a population database.
            </p>
          )}
          {fv.disclaimer && <p className="hint">{fv.disclaimer}</p>}
        </>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
export function RiskPanel({ stage, riskScore, riskBand }: {
  stage: StageResult | undefined
  riskScore: number | null
  riskBand: AnalyzeResponse['risk_band']
}) {
  const { pending, data, note } = stagePayload(stage)
  const riskBlock = data?.risk as Record<string, unknown> | undefined
  const contributions = (riskBlock?.contributions ?? data?.contributions ?? []) as RiskContribution[]
  const reasons = (riskBlock?.reasons ?? []) as string[]

  return (
    <Panel title="Risk Assessment" subtitle="Weighted fusion of all AI signals">
      {pending ? (
        <ModulePendingNote
          capability="risk scoring"
          note={note ?? 'Planned in Step 7 of the build plan.'}
        />
      ) : (
        <>
          <div className="risk-score-row">
            <RiskBadge band={riskBand} score={riskScore} />
            <ConfidenceBar
              value={riskScore != null ? (riskScore <= 1 ? riskScore : riskScore / 100) : null}
              label="Risk score"
            />
          </div>
          {contributions.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Signal</th><th>Points</th><th>Detail</th></tr></thead>
                <tbody>
                  {contributions.map((c, i) => {
                    const pts = c.points ?? (c.impact != null ? c.impact * 100 : 0)
                    return (
                      <tr key={i}>
                        <td>{c.signal}</td>
                        <td className={pts > 0 ? 'conf-text-low' : 'conf-text-ok'}>
                          {pts > 0 ? '+' : ''}{pts.toFixed(1)}
                        </td>
                        <td>{c.details}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          {reasons.length > 0 && (
            <ul className="hint" style={{ margin: '8px 0 0', paddingLeft: '18px' }}>
              {reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
        </>
      )}
    </Panel>
  )
}
interface RiskContribution { signal: string; points?: number; impact?: number; details?: string }

// ---------------------------------------------------------------------------
export function FinalResultPanel({ analysis }: { analysis: AnalyzeResponse }) {
  const band = bandInfo(analysis.risk_band)
  const highRisk =
    analysis.risk_band === 'high' || analysis.risk_band === 'critical'

  return (
    <Panel title="Final Screening Result" subtitle="Advisory output — human decision required">
      <div className={`final-result ${highRisk ? 'final-high' : ''}`}>
        <div className="final-main">
          <div className="final-status">
            {analysis.status === 'pending_review' ? 'PENDING HUMAN REVIEW' : analysis.status.toUpperCase()}
          </div>
          <div className="final-meta">
            <RiskBadge band={analysis.risk_band} score={analysis.risk_score} />
            {band && (
              <span className="hint">
                AI risk model · not a legal determination
              </span>
            )}
          </div>
          {analysis.human_review_required && <HumanReviewNotice />}
          <p className="disclaimer-text">{analysis.disclaimer}</p>
        </div>
        <aside className="final-pipeline">
          <h4>Pipeline stages</h4>
          {analysis.stages.map((s) => (
            <div key={s.stage} className={`pipe-row pipe-${s.status}`}>
              <span>{s.stage}</span>
              <StatusChipInline status={s.status} />
            </div>
          ))}
        </aside>
      </div>
    </Panel>
  )
}
function StatusChipInline({ status }: { status: StageResult['status'] }) {
  const labels: Record<string, string> = {
    ok: '✓', not_implemented: '○', error: '✗', skipped: '—',
  }
  return <span className={`pipe-chip pipe-chip-${status}`}>{labels[status] ?? '?'}</span>
}

// ---------------------------------------------------------------------------
export function PipelineOverviewPanel({ stages }: { stages: StageResult[] }) {
  return (
    <Panel title="Pipeline Execution" subtitle="Per-stage status, engine and timing">
      <div className="stage-grid">
        {stages.map((s) => <StageCard key={s.stage} result={s} />)}
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// Step-9 consolidated result (orchestrator payload + audit trail)
// ---------------------------------------------------------------------------
export function ConsolidatedResultPanel({ result }: { result: ScreeningResult }) {
  const ra = result.risk_assessment
  const audit = result.audit ?? {}
  const confEntries = Object.entries(result.confidence_summary ?? {})

  return (
    <Panel
      title="Screening Result (Consolidated)"
      subtitle={`Orchestrator output · ${result.processing_time_ms} ms`}
    >
      <div className="final-result">
        <div className="final-main">
          <div className="final-status">{result.final_status.toUpperCase().replace(/_/g, ' ')}</div>
          <div className="final-meta">
            <code className="hint">screening_id: {result.screening_id}</code>
          </div>
          {ra?.risk_score != null && (
            <div className="risk-score-row">
              <RiskBadge
                band={(ra.risk_level ?? '').toLowerCase() as AnalyzeResponse['risk_band']}
                score={ra.risk_score}
              />
              {ra.confidence != null && (
                <ConfidenceBar value={ra.confidence} label="Fusion confidence" />
              )}
            </div>
          )}
          {result.human_review_required && <HumanReviewNotice />}
          <p className="disclaimer-text">{result.disclaimer}</p>
        </div>
        <aside className="final-pipeline">
          <h4>Module confidence</h4>
          {confEntries.map(([stage, conf]) => (
            <div key={stage} className="pipe-row">
              <span>{stage}</span>
              <span className="hint">{formatConfidence(conf)}</span>
            </div>
          ))}
          <h4>Audit trail</h4>
          <div className="pipe-row">
            <span>report hash</span>
            <span className="hint" title={audit.payload_sha256 ?? ''}>
              {audit.payload_sha256 ? `${audit.payload_sha256.slice(0, 10)}…` : '—'}
            </span>
          </div>
          <div className="pipe-row">
            <span>chain anchor</span>
            <StatusChipInline
              status={audit.anchor_status === 'anchored' ? 'ok'
                : audit.anchor_status === 'failed' ? 'error' : 'not_implemented'}
            />
          </div>
          {audit.tx_hash && (
            <div className="pipe-row">
              <span>tx</span>
              <span className="hint" title={audit.tx_hash}>{audit.tx_hash.slice(0, 10)}…</span>
            </div>
          )}
          {audit.on_chain?.verified && <span className="badge status-ok">ON-CHAIN VERIFIED</span>}
        </aside>
      </div>
    </Panel>
  )
}
