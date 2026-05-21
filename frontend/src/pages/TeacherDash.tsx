import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/studyowl'
import type {
  CriticDecisionsResponse,
  HistorySession,
  StudentMemoryResponse,
  TeacherAlert,
  TeacherAlertsResponse,
  TeacherMetricsResponse,
} from '../api/studyowl'
import { useAuth } from '../auth/AuthContext'
import { usePolling } from '../hooks/usePolling'
import { ConceptMastery } from '../components/ConceptMastery'
import { CoPilotDigest } from '../components/CoPilotDigest'
import { CriticDecisionsPanel } from '../components/CriticDecisionsPanel'

const TABS = [
  { key: 'subjects', label: 'Subjects' },
  { key: 'sessions', label: 'Recent Sessions' },
  { key: 'concepts', label: 'Concept Mastery' },
  { key: 'critic', label: 'Recent Critics' },
] as const
type TabKey = (typeof TABS)[number]['key']

// Infinite-scroll batch sizes. The alerts panel and each analytics tab start
// with a small visible window and grow as the user scrolls.
const SESSIONS_PAGE_SIZE = 2
const ALERTS_INITIAL = 2
const ALERTS_STEP = 5
const TAB_INITIAL = 2
const TAB_STEP = 5
// Trigger "load more" when the user is within this many pixels of the bottom
// of a scroll container.
const SCROLL_THRESHOLD_PX = 80

const SEVERITY_BADGE: Record<TeacherAlert['severity'], { label: string; classes: string }> = {
  high: { label: '🔴 HIGH', classes: 'bg-red-100 text-red-800 border-red-300' },
  medium: { label: '🟡 MED', classes: 'bg-amber-100 text-amber-800 border-amber-300' },
  low: { label: '🔵 LOW', classes: 'bg-sky-100 text-sky-800 border-sky-300' },
}

const REASON_LABEL: Record<TeacherAlert['reason_kind'], string> = {
  distress: 'Distress',
  repeated_failure: 'Stuck',
  inactivity: 'Inactive',
  legacy: 'Legacy',
}

interface StudentSummary {
  id: string
  name: string
  grade_level: string
}

interface StudentProgress {
  subjects: Array<{ name: string; sessions: number; success_rate: number }>
  recent_sessions: Array<{ id: string; question: string; subject: string; resolved: boolean; started_at: string }>
}

const POLL_INTERVAL_MS = 20_000

function formatTime(d: Date | null): string {
  if (!d) return '—'
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export const TeacherDash: React.FC = () => {
  const navigate = useNavigate()
  const { studentId: urlStudentId } = useParams<{ studentId?: string }>()
  const { user } = useAuth()

  const [students, setStudents] = useState<StudentSummary[]>([])
  const [studentsLoading, setStudentsLoading] = useState(true)
  const [studentsError, setStudentsError] = useState<string | null>(null)
  const [selectedStudentProgress, setSelectedStudentProgress] = useState<StudentProgress | null>(null)
  const [selectedStudentMemory, setSelectedStudentMemory] = useState<StudentMemoryResponse | null>(null)
  const [criticDecisions, setCriticDecisions] = useState<CriticDecisionsResponse | null>(null)
  const [loadingStudent, setLoadingStudent] = useState(false)
  const [studentDetailError, setStudentDetailError] = useState<string | null>(null)
  const [activeTopTab, setActiveTopTab] = useState<'analytics' | 'copilot'>('analytics')
  const [activeTab, setActiveTab] = useState<TabKey>('subjects')
  const [sessionHistory, setSessionHistory] = useState<HistorySession[]>([])
  const [sessionsTotal, setSessionsTotal] = useState(0)
  const [sessionsLoading, setSessionsLoading] = useState(false)
  const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false)
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  // Infinite-scroll windows — incremented by scroll handlers and the
  // auto-fill effect (which keeps loading until the scroll container actually
  // overflows; otherwise the user has no scrollbar to scroll with).
  const [visibleAlerts, setVisibleAlerts] = useState(ALERTS_INITIAL)
  const [visibleSubjects, setVisibleSubjects] = useState(TAB_INITIAL)
  const [visibleConcepts, setVisibleConcepts] = useState(TAB_INITIAL)
  const [visibleCritic, setVisibleCritic] = useState(TAB_INITIAL)
  const alertsScrollRef = useRef<HTMLDivElement | null>(null)
  const tabContentRef = useRef<HTMLDivElement | null>(null)
  // In-flight ack/resolve to prevent double-clicks. Keyed by alert ID for
  // alert mutations and `comment-${sessionId}` for session-comment mutations.
  const [actionInFlight, setActionInFlight] = useState<Record<string, boolean>>({})
  // Errors from ack/resolve actions (separate from polling errors).
  const [actionError, setActionError] = useState<string | null>(null)
  // Per-row draft text + edit toggle for the session-comment composer.
  const [commentDraft, setCommentDraft] = useState<Record<string, string>>({})
  const [editingComment, setEditingComment] = useState<Record<string, boolean>>({})

  const fetchAlerts = useCallback(
    (signal: AbortSignal) => api.getAlerts({ signal }),
    [],
  )
  const fetchMetrics = useCallback(
    (signal: AbortSignal) => api.getTeacherMetrics({ signal }),
    [],
  )

  const alertsPoll = usePolling<TeacherAlertsResponse>({ fetcher: fetchAlerts, intervalMs: POLL_INTERVAL_MS })
  const metricsPoll = usePolling<TeacherMetricsResponse>({ fetcher: fetchMetrics, intervalMs: POLL_INTERVAL_MS })

  const alerts = alertsPoll.data?.pending_alerts ?? []
  const metrics = metricsPoll.data

  // The most recent of the two polls — that's our "freshness" indicator.
  const lastUpdated =
    alertsPoll.lastUpdated && metricsPoll.lastUpdated
      ? new Date(Math.max(alertsPoll.lastUpdated.getTime(), metricsPoll.lastUpdated.getTime()))
      : alertsPoll.lastUpdated ?? metricsPoll.lastUpdated

  // Aggregate polling error — surfaces the most recent one without hiding stale data.
  const pollingError = alertsPoll.error ?? metricsPoll.error

  // URL is the source of truth for which student is selected. Fall back to the
  // first loaded student when the route is bare /teacher.
  const selectedStudentId = urlStudentId ?? students[0]?.id ?? null

  const selectedStudent = useMemo(
    () => students.find((s) => s.id === selectedStudentId) ?? null,
    [students, selectedStudentId],
  )

  const handleSelectStudent = (id: string) => {
    navigate(`/teacher/students/${id}`)
  }

  useEffect(() => {
    const loadStudents = async () => {
      try {
        const studentListResponse = await api.getStudentList()
        setStudents(studentListResponse.students)
      } catch (err) {
        setStudentsError((err as Error).message)
      } finally {
        setStudentsLoading(false)
      }
    }
    loadStudents()
  }, [])

  // Alert state is owned by usePolling — we trigger a refresh after server-side
  // mutation rather than optimistically rewriting local state. Snappy
  // optimistic UX from the perf branch was dropped during the main merge
  // because usePolling doesn't expose setData; revisit by extending the hook.
  const handleAcknowledge = async (alert: TeacherAlert) => {
    if (actionInFlight[alert.id]) return
    setActionInFlight((m) => ({ ...m, [alert.id]: true }))
    setActionError(null)
    try {
      await api.acknowledgeAlert(alert.id)
      alertsPoll.refresh()
    } catch (err) {
      setActionError((err as Error).message)
    } finally {
      setActionInFlight((m) => {
        const next = { ...m }
        delete next[alert.id]
        return next
      })
    }
  }

  const handleResolve = async (alert: TeacherAlert) => {
    if (actionInFlight[alert.id]) return
    setActionInFlight((m) => ({ ...m, [alert.id]: true }))
    setActionError(null)
    try {
      await api.resolveAlert(alert.id)
      alertsPoll.refresh()
      metricsPoll.refresh()
    } catch (err) {
      setActionError((err as Error).message)
    } finally {
      setActionInFlight((m) => {
        const next = { ...m }
        delete next[alert.id]
        return next
      })
    }
  }

  useEffect(() => {
    // Reset infinite-scroll windows when the selected student changes so the
    // next student starts fresh from the top of each tab.
    setVisibleSubjects(TAB_INITIAL)
    setVisibleConcepts(TAB_INITIAL)
    setVisibleCritic(TAB_INITIAL)

    if (!selectedStudentId) {
      setSelectedStudentProgress(null)
      setSelectedStudentMemory(null)
      setCriticDecisions(null)
      setSessionHistory([])
      setSessionsTotal(0)
      setSessionsError(null)
      return
    }

    const loadStudentProgress = async () => {
      setLoadingStudent(true)
      setStudentDetailError(null)
      try {
        const [progress, memory, critic] = await Promise.all([
          api.getStudentProgress(selectedStudentId),
          api.getStudentMemory(selectedStudentId).catch(() => null),
          api.getCriticDecisions(selectedStudentId, {
            limit: 20,
            onlyRejects: true,
          }).catch(() => null),
        ])
        setSelectedStudentProgress(progress)
        setSelectedStudentMemory(memory)
        setCriticDecisions(critic)
      } catch (err) {
        setStudentDetailError((err as Error).message)
      } finally {
        setLoadingStudent(false)
      }
    }

    loadStudentProgress()
  }, [selectedStudentId])

  // Load the first page of session history whenever the Recent Sessions tab is
  // opened for a student we haven't fetched yet. Subsequent pages are appended
  // by the infinite-scroll handler on the tab content container.
  useEffect(() => {
    if (!selectedStudentId || activeTab !== 'sessions') return
    if (sessionHistory.length > 0 || sessionsLoading) return

    const loadFirstPage = async () => {
      setSessionsLoading(true)
      setSessionsError(null)
      try {
        const res = await api.getStudentSessions(selectedStudentId, {
          limit: SESSIONS_PAGE_SIZE,
          offset: 0,
        })
        setSessionHistory(res.sessions)
        setSessionsTotal(res.total)
      } catch (err) {
        setSessionsError((err as Error).message)
      } finally {
        setSessionsLoading(false)
      }
    }
    loadFirstPage()
  }, [selectedStudentId, activeTab, sessionHistory.length, sessionsLoading])

  const handleLoadMoreSessions = async () => {
    if (!selectedStudentId || sessionsLoadingMore) return
    setSessionsLoadingMore(true)
    setSessionsError(null)
    try {
      const res = await api.getStudentSessions(selectedStudentId, {
        limit: SESSIONS_PAGE_SIZE,
        offset: sessionHistory.length,
      })
      setSessionHistory((prev) => [...prev, ...res.sessions])
      setSessionsTotal(res.total)
    } catch (err) {
      setSessionsError((err as Error).message)
    } finally {
      setSessionsLoadingMore(false)
    }
  }

  const reloadFirstSessionsPage = async () => {
    if (!selectedStudentId) return
    const res = await api.getStudentSessions(selectedStudentId, {
      limit: Math.max(sessionHistory.length, SESSIONS_PAGE_SIZE),
      offset: 0,
    })
    setSessionHistory(res.sessions)
    setSessionsTotal(res.total)
  }

  const handlePostComment = async (sessionId: string) => {
    const draft = (commentDraft[sessionId] ?? '').trim()
    if (!draft) return
    const key = `comment-${sessionId}`
    if (actionInFlight[key]) return
    setActionInFlight((m) => ({ ...m, [key]: true }))
    setActionError(null)
    try {
      await api.upsertSessionComment(sessionId, draft)
      setCommentDraft((d) => {
        const next = { ...d }
        delete next[sessionId]
        return next
      })
      setEditingComment((e) => {
        const next = { ...e }
        delete next[sessionId]
        return next
      })
      await reloadFirstSessionsPage()
    } catch (err) {
      setActionError((err as Error).message)
    } finally {
      setActionInFlight((m) => {
        const next = { ...m }
        delete next[key]
        return next
      })
    }
  }

  const handleDeleteComment = async (sessionId: string) => {
    const key = `comment-${sessionId}`
    if (actionInFlight[key]) return
    setActionInFlight((m) => ({ ...m, [key]: true }))
    setActionError(null)
    try {
      await api.deleteSessionComment(sessionId)
      await reloadFirstSessionsPage()
    } catch (err) {
      setActionError((err as Error).message)
    } finally {
      setActionInFlight((m) => {
        const next = { ...m }
        delete next[key]
        return next
      })
    }
  }

  const handleAlertsScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (visibleAlerts >= alerts.length) return
    const el = e.currentTarget
    if (el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD_PX) {
      setVisibleAlerts((c) => Math.min(c + ALERTS_STEP, alerts.length))
    }
  }

  const handleTabScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    if (el.scrollHeight - el.scrollTop - el.clientHeight > SCROLL_THRESHOLD_PX) return

    if (activeTab === 'subjects') {
      const total = selectedStudentProgress?.subjects.length ?? 0
      if (visibleSubjects < total) {
        setVisibleSubjects((c) => Math.min(c + TAB_STEP, total))
      }
    } else if (activeTab === 'sessions') {
      if (!sessionsLoading && !sessionsLoadingMore && sessionHistory.length < sessionsTotal) {
        handleLoadMoreSessions()
      }
    } else if (activeTab === 'concepts') {
      const total = selectedStudentMemory?.concepts.length ?? 0
      if (visibleConcepts < total) {
        setVisibleConcepts((c) => Math.min(c + TAB_STEP, total))
      }
    } else if (activeTab === 'critic') {
      const rejectsTotal = (criticDecisions?.decisions ?? []).filter((d) => d.verdict === 'reject').length
      if (visibleCritic < rejectsTotal) {
        setVisibleCritic((c) => Math.min(c + TAB_STEP, rejectsTotal))
      }
    }
  }

  // Sliced views feeding the rendered tab panels.
  const visibleAlertList = useMemo(
    () => alerts.slice(0, visibleAlerts),
    [alerts, visibleAlerts],
  )
  const visibleSubjectList = useMemo(
    () => (selectedStudentProgress?.subjects ?? []).slice(0, visibleSubjects),
    [selectedStudentProgress, visibleSubjects],
  )
  // Concepts are sorted globally weakest-first before slicing so the initial
  // window surfaces the student's biggest gaps. ConceptMastery still groups
  // them by subject for display.
  const visibleConceptList = useMemo(() => {
    const all = selectedStudentMemory?.concepts ?? []
    return [...all]
      .sort((a, b) => a.decayed_confidence - b.decayed_confidence)
      .slice(0, visibleConcepts)
  }, [selectedStudentMemory, visibleConcepts])
  const visibleCriticList = useMemo(() => {
    const rejects = (criticDecisions?.decisions ?? []).filter((d) => d.verdict === 'reject')
    return rejects.slice(0, visibleCritic)
  }, [criticDecisions, visibleCritic])

  const subjectsTotal = selectedStudentProgress?.subjects.length ?? 0
  const conceptsTotal = selectedStudentMemory?.concepts.length ?? 0
  const criticRejectsTotal = useMemo(
    () => (criticDecisions?.decisions ?? []).filter((d) => d.verdict === 'reject').length,
    [criticDecisions],
  )

  // While the alerts container doesn't overflow, keep loading the next batch
  // so scrolling becomes possible. Cascades through renders until the content
  // fills the container or we hit `alerts.length`.
  useEffect(() => {
    const el = alertsScrollRef.current
    if (!el) return
    if (visibleAlerts >= alerts.length) return
    if (el.scrollHeight > el.clientHeight + 4) return
    setVisibleAlerts((c) => Math.min(c + ALERTS_STEP, alerts.length))
  }, [visibleAlerts, alerts.length])

  // Same pattern for the active analytics tab. The dependency list intentionally
  // includes the totals/visible counts so the effect re-runs after each batch
  // and stops naturally once the container overflows.
  useEffect(() => {
    const el = tabContentRef.current
    if (!el || !selectedStudentProgress || loadingStudent) return
    if (activeTopTab !== 'analytics') return
    if (el.scrollHeight > el.clientHeight + 4) return

    if (activeTab === 'subjects') {
      if (visibleSubjects < subjectsTotal) {
        setVisibleSubjects((c) => Math.min(c + TAB_STEP, subjectsTotal))
      }
    } else if (activeTab === 'sessions') {
      if (!sessionsLoading && !sessionsLoadingMore && sessionHistory.length < sessionsTotal) {
        handleLoadMoreSessions()
      }
    } else if (activeTab === 'concepts') {
      if (visibleConcepts < conceptsTotal) {
        setVisibleConcepts((c) => Math.min(c + TAB_STEP, conceptsTotal))
      }
    } else if (activeTab === 'critic') {
      if (visibleCritic < criticRejectsTotal) {
        setVisibleCritic((c) => Math.min(c + TAB_STEP, criticRejectsTotal))
      }
    }
    // handleLoadMoreSessions is stable enough for this effect (it reads from
    // the latest state via its own closure check).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeTopTab,
    activeTab,
    selectedStudentProgress,
    loadingStudent,
    visibleSubjects,
    subjectsTotal,
    visibleConcepts,
    conceptsTotal,
    visibleCritic,
    criticRejectsTotal,
    sessionHistory.length,
    sessionsTotal,
    sessionsLoading,
    sessionsLoadingMore,
  ])

  const error = studentsError ?? studentDetailError ?? actionError

  return (
    <div className="min-h-screen bg-gray-100 lg:h-screen lg:overflow-hidden">
      <div className="max-w-6xl mx-auto lg:h-full flex flex-col p-3 sm:p-4">
        <header className="flex-shrink-0 mb-4 sm:mb-6 flex items-start justify-between gap-3 sm:gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 mb-1 sm:mb-2">
              🦉 Teacher Dashboard
            </h1>
            <p className="text-sm sm:text-base text-gray-600">Monitor student progress and help when needed</p>
          </div>
          <div className="text-left sm:text-right text-xs text-slate-500" aria-live="polite">
            <p>Auto-refreshing every {POLL_INTERVAL_MS / 1000}s</p>
            <p>Last updated: {formatTime(lastUpdated)}</p>
            {pollingError && (
              <p className="text-amber-700 mt-1">
                Live update failed — showing last known data
              </p>
            )}
          </div>
        </header>

        <div className="flex-1 min-h-0 grid gap-6 lg:grid-cols-[320px_1fr]">
          <div className="flex flex-col gap-6 min-h-0 lg:overflow-hidden">
            <div className="bg-white rounded-lg shadow flex flex-col min-h-0 lg:max-h-[45%]">
              <div className="px-4 sm:px-6 pt-4 sm:pt-6 pb-3 flex-shrink-0">
                <h2 className="text-xl font-bold text-gray-800">📚 Student Roster</h2>
              </div>
              <div className="px-4 sm:px-6 pb-4 sm:pb-6 overflow-y-auto flex-1 min-h-0">
                {studentsLoading ? (
                  <p className="text-gray-600">Loading students...</p>
                ) : students.length === 0 ? (
                  <div className="text-center py-8">
                    <p className="text-gray-600 text-lg">No students found.</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {students.map((student) => (
                      <button
                        key={student.id}
                        onClick={() => handleSelectStudent(student.id)}
                        className={`block w-full rounded-2xl border px-4 py-3 text-left transition ${selectedStudentId === student.id ? 'border-indigo-500 bg-indigo-50' : 'border-slate-200 bg-white hover:border-indigo-300 hover:bg-slate-50'}`}
                      >
                        <p className="font-semibold text-gray-900">{student.name}</p>
                        <p className="text-sm text-gray-500">{student.grade_level}</p>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="bg-white rounded-lg shadow flex flex-col min-h-0 lg:flex-1">
              <div className="px-4 sm:px-6 pt-4 sm:pt-6 pb-3 flex-shrink-0 flex items-center justify-between gap-2">
                <h2 className="text-xl font-bold text-gray-800">⚠️ Alerts</h2>
                {alerts.length > 0 && (
                  <span className="text-xs text-slate-500">
                    {Math.min(visibleAlerts, alerts.length)} / {alerts.length}
                  </span>
                )}
              </div>
              <div
                ref={alertsScrollRef}
                className="px-4 sm:px-6 pb-4 sm:pb-6 overflow-y-auto flex-1 min-h-0"
                onScroll={handleAlertsScroll}
              >
                {alertsPoll.isLoading ? (
                  <p className="text-gray-600">Loading alerts...</p>
                ) : alerts.length === 0 ? (
                  <p className="text-gray-600">No active alerts right now.</p>
                ) : (
                  <>
                    <div className="space-y-4">
                      {visibleAlertList.map((alert) => {
                        const badge = SEVERITY_BADGE[alert.severity]
                        const busy = !!actionInFlight[alert.id]
                        const stripeClass =
                          alert.severity === 'high' ? 'bg-red-500'
                          : alert.severity === 'medium' ? 'bg-amber-500'
                          : 'bg-sky-500'
                        return (
                          <div
                            key={alert.id}
                            className="group relative overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm hover:shadow-md transition-shadow"
                          >
                            <div aria-hidden="true" className={`absolute left-0 top-0 bottom-0 w-1 ${stripeClass}`} />
                            <div className="p-4 sm:p-5 pl-5 sm:pl-6 space-y-3">
                              <div className="flex items-start justify-between gap-3 flex-wrap">
                                <p className="font-semibold text-slate-900 break-words min-w-0 flex-1">
                                  {alert.student_name}
                                </p>
                                <span
                                  className={`text-[11px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full border ${badge.classes} whitespace-nowrap`}
                                  title={`${REASON_LABEL[alert.reason_kind]}: ${alert.reason_text}`}
                                >
                                  {badge.label}
                                </span>
                              </div>

                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                                  {REASON_LABEL[alert.reason_kind]}
                                </span>
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                                  Hint {alert.hint_level}/3
                                </span>
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                                  {alert.fails_at_level} {alert.fails_at_level === 1 ? 'fail' : 'fails'}
                                </span>
                                {alert.notification_status === 'failed' && (
                                  <span className="inline-flex items-center gap-1 rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-700">
                                    <span aria-hidden="true">⚠</span> Delivery failed
                                  </span>
                                )}
                              </div>

                              <blockquote
                                className="border-l-2 border-slate-200 pl-3 text-sm italic text-slate-600 line-clamp-2 break-words"
                                title={alert.question}
                              >
                                {alert.question}
                              </blockquote>

                              <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                                <span className="text-xs text-slate-500">
                                  {alert.acknowledged_at ? (
                                    <>
                                      Acknowledged by{' '}
                                      <span className="font-medium text-slate-700">
                                        {alert.acknowledged_by_name ?? 'a teacher'}
                                      </span>
                                    </>
                                  ) : (
                                    'Not yet acknowledged'
                                  )}
                                </span>
                                <div className="flex gap-2">
                                  {!alert.acknowledged_at && (
                                    <button
                                      disabled={busy}
                                      onClick={() => handleAcknowledge(alert)}
                                      className="inline-flex items-center gap-1 text-xs font-semibold px-3 py-1.5 rounded-lg border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 transition"
                                    >
                                      {busy ? '…' : 'Acknowledge'}
                                    </button>
                                  )}
                                  <button
                                    disabled={busy}
                                    onClick={() => handleResolve(alert)}
                                    className="inline-flex items-center gap-1 text-xs font-semibold px-3 py-1.5 rounded-lg bg-emerald-600 text-white shadow-sm hover:bg-emerald-700 disabled:opacity-50 transition"
                                  >
                                    {busy ? '…' : 'Resolve'}
                                  </button>
                                </div>
                              </div>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                    {visibleAlerts < alerts.length && (
                      <p className="mt-4 text-center text-xs text-slate-400">
                        Scroll for more ({alerts.length - visibleAlerts} remaining)
                      </p>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-6 min-h-0 lg:overflow-hidden">
            <div className="bg-white rounded-lg shadow p-4 sm:p-6 flex-shrink-0">
              <h2 className="text-xl font-bold text-gray-800 mb-4">📊 Class Overview</h2>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4">
                <div className="text-center rounded-2xl bg-slate-50 p-4">
                  <p className="text-2xl sm:text-3xl font-bold text-indigo-600">{metrics ? metrics.total_students : '—'}</p>
                  <p className="text-gray-600 text-sm">Total Students</p>
                </div>
                <div className="text-center rounded-2xl bg-slate-50 p-4">
                  <p className="text-2xl sm:text-3xl font-bold text-green-600">{metrics ? metrics.sessions_today : '—'}</p>
                  <p className="text-gray-600 text-sm">Sessions Today</p>
                </div>
                <div className="text-center rounded-2xl bg-slate-50 p-4">
                  <p className="text-2xl sm:text-3xl font-bold text-blue-600">{metrics ? `${metrics.average_success_rate}%` : '—'}</p>
                  <p className="text-gray-600 text-sm">Success Rate</p>
                </div>
              </div>
              <div className="mt-4 rounded-2xl bg-white p-4 border border-slate-200 text-center text-sm text-slate-600">
                Pending alerts: {metrics ? metrics.pending_alerts : '—'}
              </div>
            </div>

            <div className="bg-white rounded-lg shadow-lg flex flex-col min-h-0 lg:flex-1 overflow-hidden">
              <div className="px-4 sm:px-6 pt-4 sm:pt-5 flex-shrink-0">
                <div
                  role="tablist"
                  aria-label="Dashboard sections"
                  className="flex flex-wrap gap-2"
                >
                  <button
                    role="tab"
                    aria-selected={activeTopTab === 'analytics'}
                    onClick={() => setActiveTopTab('analytics')}
                    className={`inline-flex items-center px-3 py-1.5 rounded-lg text-sm font-medium border transition ${
                      activeTopTab === 'analytics'
                        ? 'bg-indigo-600 text-white border-indigo-600'
                        : 'bg-white text-gray-700 border-gray-300 hover:border-indigo-500 hover:text-indigo-700'
                    }`}
                  >
                    Student Analytics
                  </button>
                  <button
                    role="tab"
                    aria-selected={activeTopTab === 'copilot'}
                    onClick={() => setActiveTopTab('copilot')}
                    className={`inline-flex items-center px-3 py-1.5 rounded-lg text-sm font-medium border transition ${
                      activeTopTab === 'copilot'
                        ? 'bg-indigo-600 text-white border-indigo-600'
                        : 'bg-white text-gray-700 border-gray-300 hover:border-indigo-500 hover:text-indigo-700'
                    }`}
                  >
                    Teacher Co-Pilot
                  </button>
                </div>

                <div className="mt-4">
                  {activeTopTab === 'analytics' ? (
                    <>
                      <h2 className="text-2xl font-bold text-gray-800">
                        {selectedStudent ? selectedStudent.name : 'Select a student'}
                      </h2>
                      <p className="text-sm text-gray-500 mt-1">
                        {selectedStudent
                          ? selectedStudent.grade_level
                          : 'Pick a student from the roster on the left.'}
                      </p>
                    </>
                  ) : (
                    <>
                      <h2 className="text-2xl font-bold text-gray-800">Weekly Digest</h2>
                      <p className="text-sm text-gray-500 mt-1">Class-wide patterns</p>
                    </>
                  )}
                </div>

                {activeTopTab === 'analytics' && (
                  <div
                    role="tablist"
                    aria-label="Student analytics tabs"
                    className="flex flex-wrap gap-2 mt-4"
                  >
                    {TABS.map((tab) => {
                      const isActive = activeTab === tab.key
                      const disabled = !selectedStudentProgress || loadingStudent
                      const count =
                        tab.key === 'subjects' ? subjectsTotal
                        : tab.key === 'sessions' ? sessionsTotal
                        : tab.key === 'concepts' ? conceptsTotal
                        : criticRejectsTotal
                      return (
                        <button
                          key={tab.key}
                          role="tab"
                          aria-selected={isActive}
                          aria-controls={`tab-panel-${tab.key}`}
                          id={`tab-${tab.key}`}
                          onClick={() => setActiveTab(tab.key)}
                          disabled={disabled}
                          className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium border transition ${
                            isActive
                              ? 'bg-indigo-600 text-white border-indigo-600'
                              : disabled
                                ? 'bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed'
                                : 'bg-white text-gray-700 border-gray-300 hover:border-indigo-500 hover:text-indigo-700'
                          }`}
                        >
                          {tab.label}
                          {!disabled && count > 0 && (
                            <span
                              className={`text-[11px] font-semibold rounded-full px-1.5 py-0.5 min-w-[20px] text-center ${
                                isActive ? 'bg-white text-indigo-700' : 'bg-gray-100 text-gray-600'
                              }`}
                            >
                              {count}
                            </span>
                          )}
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>

              <div
                ref={tabContentRef}
                className="px-4 sm:px-6 py-4 sm:py-5 overflow-y-auto flex-1 min-h-0"
                onScroll={activeTopTab === 'analytics' ? handleTabScroll : undefined}
              >
                {activeTopTab === 'copilot' ? (
                  <CoPilotDigest />
                ) : loadingStudent ? (
                  <p className="text-gray-600">Loading student progress...</p>
                ) : !selectedStudentProgress ? (
                  <div className="py-8 text-center">
                    <p className="text-base font-semibold text-gray-800">
                      Pick a student to begin
                    </p>
                    <p className="text-sm text-gray-500 mt-1 max-w-sm mx-auto">
                      Choose a name from the roster on the left to see their subjects, sessions, concept mastery, and critic decisions.
                    </p>
                  </div>
                ) : (
                  <>
                    {activeTab === 'subjects' && (
                      <div
                        role="tabpanel"
                        id="tab-panel-subjects"
                        aria-labelledby="tab-subjects"
                      >
                        {subjectsTotal === 0 ? (
                          <p className="text-sm text-slate-500">
                            No subject activity yet for this student.
                          </p>
                        ) : (
                          <>
                            <div className="space-y-3">
                              {visibleSubjectList.map((subject) => (
                                <div
                                  key={subject.name}
                                  className="rounded-2xl bg-slate-50 p-4 border border-transparent hover:border-indigo-200 hover:bg-white hover:shadow-sm transition"
                                >
                                  <div className="flex items-center justify-between gap-4">
                                    <p className="font-semibold text-slate-900">{subject.name}</p>
                                    <div className="text-right">
                                      <p className="text-sm font-semibold text-indigo-700">
                                        {Math.round(subject.success_rate * 100)}%
                                      </p>
                                      <p className="text-xs text-slate-500">{subject.sessions} sessions</p>
                                    </div>
                                  </div>
                                  <div className="mt-2 h-2 rounded-full bg-white">
                                    <div className="h-full rounded-full bg-indigo-600 transition-all" style={{ width: `${subject.success_rate * 100}%` }} />
                                  </div>
                                </div>
                              ))}
                            </div>
                            {visibleSubjectList.length < subjectsTotal && (
                              <p className="mt-4 text-center text-xs text-slate-400">
                                Scroll for more ({subjectsTotal - visibleSubjectList.length} remaining)
                              </p>
                            )}
                          </>
                        )}
                      </div>
                    )}

                    {activeTab === 'sessions' && (
                      <div
                        role="tabpanel"
                        id="tab-panel-sessions"
                        aria-labelledby="tab-sessions"
                      >
                        {sessionsLoading && sessionHistory.length === 0 ? (
                          <p className="text-gray-600">Loading sessions…</p>
                        ) : sessionHistory.length === 0 ? (
                          <p className="text-sm text-slate-500">
                            No sessions recorded for this student yet.
                          </p>
                        ) : (
                          <>
                            <div className="space-y-3">
                              {sessionHistory.map((session) => {
                                const comment = session.teacher_comment
                                const isMine = !!(comment && user && comment.teacher_id === user.id)
                                const isEditing = editingComment[session.id] === true
                                const draft = commentDraft[session.id] ?? ''
                                const commentKey = `comment-${session.id}`
                                const busy = !!actionInFlight[commentKey]
                                return (
                                  <div
                                    key={session.id}
                                    className="rounded-2xl border border-slate-200 bg-white p-4 hover:border-indigo-300 hover:shadow-sm transition"
                                  >
                                    <p className="font-semibold text-slate-900 break-words">{session.question}</p>
                                    <p className="text-sm text-slate-500 mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                                        {session.subject}
                                      </span>
                                      <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                        session.resolved
                                          ? 'bg-emerald-100 text-emerald-700'
                                          : 'bg-amber-100 text-amber-700'
                                      }`}>
                                        {session.resolved ? 'Resolved' : 'Open'}
                                      </span>
                                      <span className="text-xs text-slate-500">
                                        {new Date(session.started_at).toLocaleString()}
                                      </span>
                                    </p>

                                    {!comment && (
                                      <div className="mt-3 pt-3 border-t border-gray-100">
                                        <textarea
                                          value={draft}
                                          onChange={(e) =>
                                            setCommentDraft((d) => ({ ...d, [session.id]: e.target.value }))
                                          }
                                          rows={2}
                                          maxLength={2000}
                                          placeholder="Add a comment for the student..."
                                          disabled={busy}
                                          className="w-full p-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                                        />
                                        <button
                                          type="button"
                                          onClick={() => handlePostComment(session.id)}
                                          disabled={busy || !draft.trim()}
                                          className="mt-2 bg-indigo-600 text-white text-xs font-semibold py-1.5 px-3 rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition"
                                        >
                                          {busy ? 'Posting...' : 'Post comment'}
                                        </button>
                                      </div>
                                    )}

                                    {comment && isMine && isEditing && (
                                      <div className="mt-3 pt-3 border-t border-gray-100">
                                        <textarea
                                          value={draft}
                                          onChange={(e) =>
                                            setCommentDraft((d) => ({ ...d, [session.id]: e.target.value }))
                                          }
                                          rows={2}
                                          maxLength={2000}
                                          disabled={busy}
                                          className="w-full p-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                                        />
                                        <div className="mt-2 flex gap-2">
                                          <button
                                            type="button"
                                            onClick={() => handlePostComment(session.id)}
                                            disabled={busy || !draft.trim()}
                                            className="bg-indigo-600 text-white text-xs font-semibold py-1.5 px-3 rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition"
                                          >
                                            {busy ? 'Saving...' : 'Save'}
                                          </button>
                                          <button
                                            type="button"
                                            onClick={() => {
                                              setEditingComment((e) => {
                                                const next = { ...e }
                                                delete next[session.id]
                                                return next
                                              })
                                              setCommentDraft((d) => {
                                                const next = { ...d }
                                                delete next[session.id]
                                                return next
                                              })
                                            }}
                                            disabled={busy}
                                            className="text-xs font-semibold py-1.5 px-3 rounded-lg text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition"
                                          >
                                            Cancel
                                          </button>
                                        </div>
                                      </div>
                                    )}

                                    {comment && !(isMine && isEditing) && (
                                      <div className="mt-3 pt-3 border-t border-gray-100">
                                        <p className="text-sm text-gray-700 whitespace-pre-wrap break-words">
                                          {comment.body}
                                        </p>
                                        <p className="text-xs text-gray-500 mt-1">
                                          — {comment.teacher_name} · {new Date(comment.updated_at).toLocaleString()}
                                        </p>
                                        {isMine && (
                                          <div className="mt-2 flex gap-3">
                                            <button
                                              type="button"
                                              onClick={() => {
                                                setEditingComment((e) => ({ ...e, [session.id]: true }))
                                                setCommentDraft((d) => ({ ...d, [session.id]: comment.body }))
                                              }}
                                              disabled={busy}
                                              className="text-xs font-semibold text-indigo-700 hover:text-indigo-900 disabled:opacity-50"
                                            >
                                              Edit
                                            </button>
                                            <button
                                              type="button"
                                              onClick={() => handleDeleteComment(session.id)}
                                              disabled={busy}
                                              className="text-xs font-semibold text-red-700 hover:text-red-900 disabled:opacity-50"
                                            >
                                              {busy ? 'Deleting...' : 'Delete'}
                                            </button>
                                          </div>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                )
                              })}
                            </div>
                            <div className="mt-4 text-center text-xs text-slate-400">
                              {sessionsLoadingMore
                                ? 'Loading more…'
                                : sessionHistory.length < sessionsTotal
                                  ? `Scroll for more (${sessionsTotal - sessionHistory.length} remaining)`
                                  : `Showing all ${sessionsTotal} sessions`}
                            </div>
                            {sessionsError && (
                              <p className="mt-2 text-center text-xs text-red-700">{sessionsError}</p>
                            )}
                          </>
                        )}
                      </div>
                    )}

                    {activeTab === 'concepts' && (
                      <div
                        role="tabpanel"
                        id="tab-panel-concepts"
                        aria-labelledby="tab-concepts"
                      >
                        <ConceptMastery
                          concepts={visibleConceptList}
                          loading={loadingStudent}
                        />
                        {visibleConceptList.length < conceptsTotal && (
                          <p className="mt-4 text-center text-xs text-slate-400">
                            Scroll for more ({conceptsTotal - visibleConceptList.length} remaining)
                          </p>
                        )}
                      </div>
                    )}

                    {activeTab === 'critic' && (
                      <div
                        role="tabpanel"
                        id="tab-panel-critic"
                        aria-labelledby="tab-critic"
                      >
                        <p className="text-xs text-slate-500 mb-3">
                          Hints the Socratic critic rejected and regenerated. Click a row to inspect.
                        </p>
                        <CriticDecisionsPanel
                          decisions={visibleCriticList}
                          loading={loadingStudent}
                        />
                        {visibleCriticList.length < criticRejectsTotal && (
                          <p className="mt-4 text-center text-xs text-slate-400">
                            Scroll for more ({criticRejectsTotal - visibleCriticList.length} remaining)
                          </p>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        </div>

        {error && (
          <div className="flex-shrink-0 mt-4 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">
            {error.includes("Authentication token missing")
              ? "Your session expired or the login token is missing. Please log out and sign in again."
              : error}
          </div>
        )}
      </div>
    </div>
  )
}

export default TeacherDash
