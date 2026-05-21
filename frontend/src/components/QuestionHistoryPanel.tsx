import { useEffect, useRef, useState } from 'react'
import { api, type HistorySession } from '../api/studyowl'

interface Props {
  studentId: string
  onSelect: (questionText: string) => void
  /**
   * Bump this value (any monotonic counter) from the parent whenever the
   * history should be re-fetched — e.g. after a session is created or resolved.
   * Refetches page 1 and resets pagination.
   */
  version?: number
}

const PAGE_SIZE = 10
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diff = then - Date.now()
  const abs = Math.abs(diff)
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (abs < hour) return rtf.format(Math.round(diff / minute), 'minute')
  if (abs < day) return rtf.format(Math.round(diff / hour), 'hour')
  return rtf.format(Math.round(diff / day), 'day')
}

export function QuestionHistoryPanel({ studentId, onSelect, version = 0 }: Props) {
  const [sessions, setSessions] = useState<HistorySession[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setLoading(true)
    setError(null)
    api
      .getStudentSessions(studentId, { limit: PAGE_SIZE, offset: 0, signal: controller.signal })
      .then((res) => {
        setSessions(res.sessions)
        setTotal(res.total)
        setLoading(false)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError((err as Error).message)
        setLoading(false)
      })

    return () => controller.abort()
  }, [studentId, version])

  const handleLoadMore = async () => {
    setLoadingMore(true)
    setError(null)
    try {
      const res = await api.getStudentSessions(studentId, {
        limit: PAGE_SIZE,
        offset: sessions.length,
      })
      setSessions((prev) => [...prev, ...res.sessions])
      setTotal(res.total)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoadingMore(false)
    }
  }

  const hasMore = sessions.length < total

  const panelId = 'question-history-panel'

  return (
    <div className="rounded-3xl bg-white/90 shadow-lg border border-indigo-100 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        className="w-full flex items-center justify-between gap-3 p-4 sm:p-6 text-left hover:bg-indigo-50/40 transition"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-xl" aria-hidden="true">📜</span>
          <h2 className="text-xl font-semibold text-indigo-900 truncate">Question History</h2>
          {!loading && !error && total > 0 && (
            <span className="ml-1 rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-700">
              {total}
            </span>
          )}
        </span>
        <svg
          className={`h-5 w-5 text-indigo-700 transition-transform ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06z"
            clipRule="evenodd"
          />
        </svg>
      </button>

      {open && (
        <div id={panelId} className="px-4 sm:px-6 pb-4 sm:pb-6">
          {loading ? (
            <p className="text-sm text-slate-500">Loading your past questions…</p>
          ) : error ? (
            <p className="text-sm text-red-600">{error}</p>
          ) : sessions.length === 0 ? (
            <p className="text-sm text-slate-500">No past questions yet — ask your first one!</p>
          ) : (
            <>
              <ul className="grid grid-cols-1 sm:grid-cols-2 gap-2 sm:gap-3 list-none p-0 m-0">
                {sessions.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(s.question)}
                      className="w-full text-left rounded-2xl border border-slate-200 bg-white p-3 hover:border-indigo-300 hover:bg-indigo-50 transition"
                      title="Click to pre-fill the question form"
                    >
                      <p className="text-sm text-slate-900 line-clamp-2 break-words">{s.question}</p>
                      <div className="mt-2 flex items-center justify-between gap-2 text-xs">
                        <span className="inline-flex items-center gap-1">
                          <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">
                            {s.subject}
                          </span>
                          <span
                            className={`rounded-full px-2 py-0.5 font-medium ${
                              s.resolved
                                ? 'bg-emerald-50 text-emerald-700'
                                : 'bg-amber-50 text-amber-700'
                            }`}
                          >
                            {s.resolved ? 'resolved' : 'open'}
                          </span>
                        </span>
                        <span className="text-slate-500 whitespace-nowrap">
                          {relativeTime(s.started_at)}
                        </span>
                      </div>
                      {s.teacher_comment && (
                        <div className="mt-3 pt-3 border-t border-slate-100">
                          <p className="text-xs font-semibold text-indigo-700 mb-1">
                            Teacher comment
                          </p>
                          <p className="text-sm text-slate-800 whitespace-pre-wrap break-words">
                            {s.teacher_comment.body}
                          </p>
                          <p className="text-xs text-slate-500 mt-1">
                            — {s.teacher_comment.teacher_name} ·{' '}
                            {relativeTime(s.teacher_comment.updated_at)}
                          </p>
                        </div>
                      )}
                    </button>
                  </li>
                ))}
              </ul>

              {hasMore && (
                <button
                  type="button"
                  onClick={handleLoadMore}
                  disabled={loadingMore}
                  className="mt-4 w-full rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-semibold text-indigo-800 hover:bg-indigo-100 disabled:opacity-60 transition"
                >
                  {loadingMore ? 'Loading…' : 'Load more'}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default QuestionHistoryPanel
