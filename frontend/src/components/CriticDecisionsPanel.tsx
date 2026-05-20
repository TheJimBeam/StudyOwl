import React, { useMemo, useState } from 'react'
import type { CriticDecisionItem, CriticSeverity } from '../api/studyowl'

interface CriticDecisionsPanelProps {
  decisions: CriticDecisionItem[]
  loading?: boolean
}

const SEVERITY_BADGE: Record<CriticSeverity, string> = {
  high: 'bg-rose-100 text-rose-800 border-rose-300',
  medium: 'bg-amber-100 text-amber-800 border-amber-300',
  low: 'bg-sky-100 text-sky-800 border-sky-300',
}

function relativeAgo(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const ms = Date.now() - then
  const min = Math.floor(ms / 60_000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  const d = Math.floor(hr / 24)
  return `${d}d ago`
}

export const CriticDecisionsPanel: React.FC<CriticDecisionsPanelProps> = ({
  decisions,
  loading,
}) => {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const rejects = useMemo(
    () => decisions.filter(d => d.verdict === 'reject'),
    [decisions],
  )

  if (loading) {
    return <p className="text-gray-600">Loading critic decisions…</p>
  }

  if (rejects.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        No critic rejections — the hint engine has been delivering clean hints
        for this student.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      {rejects.map(d => {
        const isOpen = !!expanded[d.id]
        return (
          <div key={d.id} className="rounded-2xl border border-slate-200 bg-white">
            <button
              type="button"
              onClick={() => setExpanded(m => ({ ...m, [d.id]: !m[d.id] }))}
              className="w-full p-3 text-left flex items-start justify-between gap-3"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`rounded-full border px-2 py-0.5 text-xs font-semibold ${SEVERITY_BADGE[d.severity]}`}>
                    {d.severity.toUpperCase()}
                  </span>
                  <span className="text-xs text-slate-500">
                    Level {d.hint_level} • {relativeAgo(d.created_at)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-slate-800 truncate">
                  {d.reasons[0] ?? 'rejected by critic'}
                  {d.reasons.length > 1 && (
                    <span className="text-slate-500"> +{d.reasons.length - 1} more</span>
                  )}
                </p>
              </div>
              <span className="text-slate-400 text-xs shrink-0">
                {isOpen ? '▾' : '▸'}
              </span>
            </button>

            {isOpen && (
              <div className="border-t border-slate-100 p-3 space-y-3 text-sm">
                {d.reasons.length > 0 && (
                  <div>
                    <p className="text-xs uppercase tracking-wide text-slate-500 mb-1">
                      Why rejected
                    </p>
                    <ul className="list-disc list-inside text-slate-700 space-y-0.5">
                      {d.reasons.map((r, i) => <li key={i}>{r}</li>)}
                    </ul>
                  </div>
                )}
                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500 mb-1">
                    Rejected draft
                  </p>
                  <p className="rounded-lg bg-rose-50 border border-rose-100 p-2 text-rose-900 whitespace-pre-wrap">
                    {d.original_hint}
                  </p>
                </div>
                {d.regenerated_hint && (
                  <div>
                    <p className="text-xs uppercase tracking-wide text-slate-500 mb-1">
                      Delivered instead
                    </p>
                    <p className="rounded-lg bg-emerald-50 border border-emerald-100 p-2 text-emerald-900 whitespace-pre-wrap">
                      {d.regenerated_hint}
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default CriticDecisionsPanel
