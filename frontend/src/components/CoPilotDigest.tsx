import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/studyowl'
import type { CopilotReport } from '../api/studyowl'
import { MiniLessonDraft } from './MiniLessonDraft'

const DEBOUNCE_TOAST_MS = 4000

function formatWeekRange(startIso: string, endIso: string): string {
  const start = new Date(startIso)
  const end = new Date(endIso)
  // end is exclusive (next Monday 00:00). Show "May 11 – May 17, 2026".
  const inclusiveEnd = new Date(end.getTime() - 86_400_000)
  const fmt = (d: Date) =>
    d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const year = inclusiveEnd.getFullYear()
  return `${fmt(start)} – ${fmt(inclusiveEnd)}, ${year}`
}

function secondsUntil(iso: string | null, windowSeconds: number): number {
  if (!iso) return 0
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return 0
  const elapsed = (Date.now() - ts) / 1000
  return Math.max(0, Math.ceil(windowSeconds - elapsed))
}

// Mirror of backend `copilot_regenerate_min_interval_seconds`. Used only for
// the client-side button countdown; the server is the authoritative debounce.
const REGENERATE_COUNTDOWN_SECONDS = 300

export const CoPilotDigest: React.FC = () => {
  const [report, setReport] = useState<CopilotReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  // Re-renders the debounce countdown.
  const [, setNowTick] = useState(0)

  const fetchLatest = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.getLatestCopilotReport({ signal })
      setReport(data)
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchLatest(controller.signal)
    return () => controller.abort()
  }, [fetchLatest])

  // Tick once per second so the debounce countdown updates in the button label.
  useEffect(() => {
    const id = window.setInterval(() => setNowTick((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const debounceRemaining = useMemo(
    () => secondsUntil(report?.regenerated_at ?? null, REGENERATE_COUNTDOWN_SECONDS),
    // We want this to recompute on each tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [report?.regenerated_at, regenerating],
  )

  const handleRegenerate = async () => {
    if (regenerating) return
    setRegenerating(true)
    setError(null)
    try {
      const res = await api.regenerateCopilotReport()
      setReport(res.report)
      if (res.debounced) {
        setToast('Just regenerated — try again in a few minutes.')
      } else {
        setToast('Digest regenerated.')
      }
      window.setTimeout(() => setToast(null), DEBOUNCE_TOAST_MS)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setRegenerating(false)
    }
  }

  const patterns = report?.patterns ?? []
  const hasPatterns = patterns.length > 0
  const isFailed = report?.status === 'failed'

  return (
    <div>
      <p className="text-sm text-slate-500 mb-3">
        {report
          ? `Weekly Digest — ${formatWeekRange(report.week_start_at, report.week_end_at)}`
          : "Weekly Digest — auto-generated drafts based on last week's class activity. Review and edit before using."}
      </p>

      <div>
          {loading && !report && (
            <p className="text-sm text-slate-600">Loading digest…</p>
          )}

          {!loading && !report && !error && (
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <p className="text-sm text-slate-700">
                No digest yet. The Co-Pilot needs about a week of class
                activity to find patterns. Check back Monday — or click
                <strong> Generate now </strong>
                to see what it can find so far.
              </p>
              <div className="mt-3">
                <button
                  type="button"
                  onClick={handleRegenerate}
                  disabled={regenerating}
                  className="inline-flex items-center gap-1 text-sm font-semibold px-4 py-2 rounded-lg bg-indigo-600 text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50 transition"
                >
                  {regenerating ? 'Generating…' : 'Generate now'}
                </button>
              </div>
            </div>
          )}

          {report && (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4">
                <div className="rounded-2xl bg-slate-50 p-3 text-center">
                  <p className="text-xl font-bold text-indigo-600">{report.student_count}</p>
                  <p className="text-xs text-slate-600">Students</p>
                </div>
                <div className="rounded-2xl bg-slate-50 p-3 text-center">
                  <p className="text-xl font-bold text-green-600">{report.session_count}</p>
                  <p className="text-xs text-slate-600">Sessions last week</p>
                </div>
                <div className="rounded-2xl bg-slate-50 p-3 text-center">
                  <p className="text-xl font-bold text-blue-600">{report.resolved_count}</p>
                  <p className="text-xs text-slate-600">Resolved</p>
                </div>
              </div>

              {isFailed && (
                <div className="rounded-2xl border border-rose-200 bg-rose-50 p-4 mb-4">
                  <p className="text-sm font-semibold text-rose-800">
                    This week's digest couldn't be generated.
                  </p>
                  <p className="text-xs text-rose-700 mt-1">
                    Aggregation errored before any drafts were drafted. Try
                    regenerating below — if it keeps failing, ping the dev team.
                  </p>
                </div>
              )}

              {!isFailed && report.narrative_status === 'ok' && report.narrative && (
                <p className="text-sm text-slate-700 leading-relaxed mb-4">
                  {report.narrative}
                </p>
              )}

              {!isFailed && !hasPatterns && (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 mb-4">
                  <p className="text-sm text-slate-700">
                    Not enough class activity this week to surface patterns.
                    The Co-Pilot needs a few more sessions before it can
                    suggest anything useful.
                  </p>
                </div>
              )}

              {hasPatterns && (
                <div className="space-y-3">
                  {patterns.map((p) => (
                    <MiniLessonDraft
                      key={p.id}
                      pattern={p}
                      onRetryRegenerate={handleRegenerate}
                    />
                  ))}
                </div>
              )}

              <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={handleRegenerate}
                  disabled={regenerating || debounceRemaining > 0}
                  className="inline-flex items-center gap-1 text-sm font-semibold px-4 py-2 rounded-lg border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 transition"
                  title={
                    debounceRemaining > 0
                      ? `Just regenerated — try again in ${debounceRemaining}s`
                      : 'Replace the current draft with a fresh run'
                  }
                >
                  {regenerating
                    ? 'Regenerating…'
                    : debounceRemaining > 0
                      ? `Just regenerated — wait ${debounceRemaining}s`
                      : 'Regenerate digest'}
                </button>
              </div>
            </>
          )}

          {error && (
            <p className="mt-3 text-sm text-red-700">{error}</p>
          )}

          {toast && (
            <div
              aria-live="polite"
              className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-800"
            >
              {toast}
            </div>
          )}
        </div>
    </div>
  )
}

export default CoPilotDigest
