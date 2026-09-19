/**
 * Document upload area with preview + optional live-capture probe image.
 * The probe image enables 1:1 face verification (document portrait vs the
 * presented person). It is optional: without it the face stage honestly
 * reports that verification could not run.
 */
import { useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { DocType, UploadResponse } from '../types'
import { DOC_TYPE_LABELS, formatBytes } from '../helpers'
import { ErrorBanner, Panel } from './ui'

export function UploadArea({
  onUploaded,
}: {
  onUploaded: (upload: UploadResponse, file: File) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [probe, setProbe] = useState<File | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [probeUrl, setProbeUrl] = useState<string | null>(null)
  const [docType, setDocType] = useState<DocType>('passport')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const probeRef = useRef<HTMLInputElement>(null)

  function pickFile(f: File | null) {
    setFile(f)
    setError(null)
    if (previewUrl) URL.revokeObjectURL(previewUrl)
    setPreviewUrl(f && f.type.startsWith('image/') ? URL.createObjectURL(f) : null)
  }

  function pickProbe(f: File | null) {
    setProbe(f)
    if (probeUrl) URL.revokeObjectURL(probeUrl)
    setProbeUrl(f && f.type.startsWith('image/') ? URL.createObjectURL(f) : null)
  }

  async function submit() {
    if (!file) {
      setError('Choose a document image first.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const upload = await api.upload(file, docType, probe)
      onUploaded(upload, file)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Upload failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel title="Document Intake" subtitle="Upload → automatic AI screening pipeline">
      <div className="upload-grid">
        <div>
          <label className="field-label" htmlFor="doc-type">Document type</label>
          <select
            id="doc-type"
            className="input"
            value={docType}
            onChange={(e) => setDocType(e.target.value as DocType)}
          >
            {Object.entries(DOC_TYPE_LABELS)
              .filter(([k]) => k !== 'unknown')
              .map(([k, label]) => (
                <option key={k} value={k}>{label}</option>
              ))}
          </select>

          <label className="field-label" htmlFor="file-input">Document image / PDF</label>
          <input
            id="file-input"
            ref={inputRef}
            type="file"
            className="input input-file"
            accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff,.pdf"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
          {file && (
            <p className="file-meta">
              {file.name} · {formatBytes(file.size)}
            </p>
          )}

          <label className="field-label" htmlFor="probe-input">
            Presented person's photo <span className="hint">(optional — enables face verification)</span>
          </label>
          <input
            id="probe-input"
            ref={probeRef}
            type="file"
            className="input input-file"
            accept=".jpg,.jpeg,.png,.webp"
            onChange={(e) => pickProbe(e.target.files?.[0] ?? null)}
          />
          {probe && (
            <p className="file-meta">
              {probe.name} · {formatBytes(probe.size)}
            </p>
          )}

          <button className="btn btn-primary btn-block" onClick={submit} disabled={busy}>
            {busy ? 'Uploading…' : 'Upload & Create Screening'}
          </button>
          {error && <ErrorBanner message={error} />}
          <p className="hint">
            Allowed: JPG/PNG/WebP/BMP/TIFF/PDF, max 10 MB. Files are stored locally
            under <code>data/uploads/</code>. The presented-person photo is used
            for one-to-one face verification only — never population search.
          </p>
        </div>

        <div className="preview-box" aria-label="Document preview">
          {previewUrl ? (
            <img src={previewUrl} alt="Document preview" className="preview-img" />
          ) : (
            <div className="preview-empty">No document selected</div>
          )}
          {probeUrl && (
            <img src={probeUrl} alt="Presented person preview" className="preview-img" />
          )}
        </div>
      </div>
    </Panel>
  )
}
