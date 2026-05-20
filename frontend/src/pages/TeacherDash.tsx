import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/studyowl'
import type {
  StudentMemoryResponse,
  TeacherAlert,
  TeacherAlertsResponse,
  TeacherMetricsResponse,
} from '../api/studyowl'
import { usePolling } from '../hooks/usePolling'
import { ConceptMastery } from '../components/ConceptMastery'

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

  const [students, setStudents] = useState<StudentSummary[]>([])
  const [studentsLoading, setStudentsLoading] = useState(true)
  const [studentsError, setStudentsError] = useState<string | null>(null)
  const [selectedStudentProgress, setSelectedStudentProgress] = useState<StudentProgress | null>(null)
  const [selectedStudentMemory, setSelectedStudentMemory] = useState<StudentMemoryResponse | null>(null)
  const [loadingStudent, setLoadingStudent] = useState(false)
  const [studentDetailError, setStudentDetailError] = useState<string | null>(null)
  // In-flight ack/resolve to prevent double-clicks. Keyed by alert ID.
  const [actionInFlight, setActionInFlight] = useState<Record<string, boolean>>({})
  // Errors from ack/resolve actions (separate from polling errors).
  const [actionError, setActionError] = useState<string | null>(null)

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
    if (!selectedStudentId) {
      setSelectedStudentProgress(null)
      setSelectedStudentMemory(null)
      return
    }

    const loadStudentProgress = async () => {
      setLoadingStudent(true)
      setStudentDetailError(null)
      try {
        const [progress, memory] = await Promise.all([
          api.getStudentProgress(selectedStudentId),
          api.getStudentMemory(selectedStudentId).catch(() => null),
        ])
        setSelectedStudentProgress(progress)
        setSelectedStudentMemory(memory)
      } catch (err) {
        setStudentDetailError((err as Error).message)
      } finally {
        setLoadingStudent(false)
      }
    }

    loadStudentProgress()
  }, [selectedStudentId])

  const error = studentsError ?? studentDetailError ?? actionError

  return (
    <div className="min-h-screen bg-gray-100 p-3 sm:p-4">
      <div className="max-w-6xl mx-auto">
        <header className="mb-6 sm:mb-8 flex items-start justify-between gap-3 sm:gap-4 flex-wrap">
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

        <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
          <div className="space-y-6">
            <div className="bg-white rounded-lg shadow p-4 sm:p-6">
              <h2 className="text-xl font-bold text-gray-800 mb-4">📚 Student Roster</h2>
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

            <div className="bg-white rounded-lg shadow p-4 sm:p-6">
              <h2 className="text-xl font-bold text-gray-800 mb-4">⚠️ Alerts</h2>
              {alertsPoll.isLoading ? (
                <p className="text-gray-600">Loading alerts...</p>
              ) : alerts.length === 0 ? (
                <p className="text-gray-600">No active alerts right now.</p>
              ) : (
                <div className="space-y-4">
                  {alerts.map((alert) => {
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
              )}
            </div>
          </div>

          <div className="space-y-6">
            <div className="bg-white rounded-lg shadow p-4 sm:p-6">
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

            <div className="bg-white rounded-lg shadow p-4 sm:p-6">
              <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
                <div className="min-w-0">
                  <h2 className="text-xl font-bold text-gray-800">Student Analytics</h2>
                  <p className="text-sm text-gray-500">View details for the selected student.</p>
                </div>
                <span className="rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold text-indigo-700 whitespace-nowrap">
                  {selectedStudentId ? 'Student selected' : 'Pick a student'}
                </span>
              </div>

              {loadingStudent ? (
                <p className="text-gray-600">Loading student progress...</p>
              ) : selectedStudentProgress ? (
                <div className="space-y-5">
                  <div>
                    <h3 className="text-sm uppercase tracking-wide text-slate-500">Subjects</h3>
                    <div className="mt-3 space-y-3">
                      {selectedStudentProgress.subjects.map((subject) => (
                        <div key={subject.name} className="rounded-2xl bg-slate-50 p-4">
                          <div className="flex items-center justify-between gap-4">
                            <p className="font-semibold text-slate-900">{subject.name}</p>
                            <p className="text-sm text-slate-600">{subject.sessions} sessions</p>
                          </div>
                          <div className="mt-2 h-2 rounded-full bg-white">
                            <div className="h-full rounded-full bg-indigo-600" style={{ width: `${subject.success_rate * 100}%` }} />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <h3 className="text-sm uppercase tracking-wide text-slate-500">Recent Sessions</h3>
                    <div className="mt-3 space-y-3">
                      {selectedStudentProgress.recent_sessions.map((session) => (
                        <div key={session.id} className="rounded-2xl border border-slate-200 bg-white p-4">
                          <p className="font-semibold text-slate-900 break-words">{session.question}</p>
                          <p className="text-sm text-slate-500">{session.subject} • {session.resolved ? 'Resolved' : 'Open'}</p>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <h3 className="text-sm uppercase tracking-wide text-slate-500">Concept Mastery</h3>
                    <div className="mt-3">
                      <ConceptMastery
                        concepts={selectedStudentMemory?.concepts ?? []}
                        loading={loadingStudent}
                      />
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-gray-600">Select a student to see their analytics.</p>
              )}
            </div>
          </div>
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">
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
