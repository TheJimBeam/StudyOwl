import React, { useMemo } from 'react'
import type { ConceptMemoryItem, ConceptStatus } from '../api/studyowl'

interface ConceptMasteryProps {
  concepts: ConceptMemoryItem[]
  loading?: boolean
}

const STATUS_BADGE: Record<ConceptStatus, string> = {
  mastered: 'bg-emerald-100 text-emerald-700',
  partial: 'bg-amber-100 text-amber-700',
  struggling: 'bg-rose-100 text-rose-700',
}

const STATUS_BAR: Record<ConceptStatus, string> = {
  mastered: 'bg-emerald-500',
  partial: 'bg-amber-500',
  struggling: 'bg-rose-500',
}

const SUBJECT_LABEL: Record<string, string> = {
  math: 'Math',
  science: 'Science',
  english: 'English',
  history: 'History',
  other: 'Other',
}

function relativeDaysAgo(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const days = Math.max(0, Math.floor((Date.now() - then) / 86_400_000))
  if (days === 0) return 'today'
  if (days === 1) return '1d ago'
  return `${days}d ago`
}

export const ConceptMastery: React.FC<ConceptMasteryProps> = ({ concepts, loading }) => {
  const grouped = useMemo(() => {
    const m = new Map<string, ConceptMemoryItem[]>()
    for (const c of concepts) {
      const k = c.subject || 'other'
      if (!m.has(k)) m.set(k, [])
      m.get(k)!.push(c)
    }
    // Within each subject, weakest first.
    for (const arr of m.values()) {
      arr.sort((a, b) => a.decayed_confidence - b.decayed_confidence)
    }
    return Array.from(m.entries())
  }, [concepts])

  const reviewQueue = useMemo(
    () => concepts.filter(c => c.decayed_confidence < 0.4),
    [concepts],
  )

  if (loading) {
    return <p className="text-gray-600">Loading concept memory…</p>
  }

  if (concepts.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        No memory yet — this student needs to complete a few sessions before
        concepts can be tracked.
      </p>
    )
  }

  return (
    <div className="space-y-5">
      {reviewQueue.length > 0 && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm font-semibold text-amber-800">
            ⚠ Needs review ({reviewQueue.length})
          </p>
          <p className="text-xs text-amber-700 mt-1">
            {reviewQueue.map(c => c.label).join(' • ')}
          </p>
        </div>
      )}

      {grouped.map(([subject, items]) => (
        <div key={subject}>
          <h4 className="text-xs uppercase tracking-wide text-slate-500 mb-2">
            {SUBJECT_LABEL[subject] ?? subject}
          </h4>
          <div className="space-y-2">
            {items.map(c => {
              const pct = Math.round(c.decayed_confidence * 100)
              const decayed = c.decayed_confidence < 0.4
              return (
                <div key={c.concept} className="rounded-2xl bg-slate-50 p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium text-slate-900 truncate">
                        {decayed && <span title="confidence has decayed">⚠ </span>}
                        {c.label}
                      </p>
                      <p className="text-xs text-slate-500">
                        seen {relativeDaysAgo(c.last_seen)}
                        {c.attempts > 0 && ` • ${c.correct}/${c.attempts} correct`}
                      </p>
                    </div>
                    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ${STATUS_BADGE[c.status]}`}>
                      {c.status}
                    </span>
                  </div>
                  <div className="mt-2 h-2 rounded-full bg-white">
                    <div
                      className={`h-full rounded-full ${STATUS_BAR[c.status]}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

export default ConceptMastery
