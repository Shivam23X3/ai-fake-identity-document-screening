/** Screening history table. */
import type { HistoryItem } from '../types'
import { DOC_TYPE_LABELS, formatDateTime, shortRunId } from '../helpers'
import { EmptyState, RiskBadge } from './ui'

export function HistoryTable({
  items,
  onSelect,
}: {
  items: HistoryItem[]
  onSelect: (runId: string) => void
}) {
  if (items.length === 0) {
    return <EmptyState>No screenings yet. Upload a document to create one.</EmptyState>
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Run</th>
            <th>Document</th>
            <th>Status</th>
            <th>Risk</th>
            <th>Review</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.run_id} onClick={() => onSelect(item.run_id)} className="row-click">
              <td><code>{shortRunId(item.run_id)}</code></td>
              <td>
                {DOC_TYPE_LABELS[item.doc_type_detected ?? item.doc_type_hint ?? 'unknown'] ??
                  (item.doc_type_hint ?? 'Unknown')}
              </td>
              <td><span className={`status-text status-text-${item.status}`}>{item.status}</span></td>
              <td><RiskBadge band={item.risk_band} score={item.risk_score} /></td>
              <td>{item.human_review_required ? <span className="flag-text">⚠ Yes</span> : '—'}</td>
              <td>{formatDateTime(item.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
