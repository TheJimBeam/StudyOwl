import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api/studyowl'
import type {
  AttemptEvent,
  ChunkEvent,
  ErrorEvent,
  StartSessionCreatedEvent,
  StartSessionEvent,
  StudentProgress,
  VerdictEvent,
} from '../api/studyowl'
import HintBubble from '../components/HintBubble'
import { useAuth } from '../auth/AuthContext'
import PhotoUpload from '../components/PhotoUpload'
import ProgressChart from '../components/ProgressChart'
import QuestionHistoryPanel from '../components/QuestionHistoryPanel'
import { PracticeAgent } from '../components/PracticeAgent'
import type { ConceptMemoryItem } from '../api/studyowl'

interface LearningResource {
  title: string
  url?: string
  summary?: string
}

interface ClarifyEntry {
  question: string
  response: string
}

// Mirrors the backend default (settings.clarifications_per_level_limit).
// If backend tightens the cap mid-deploy, the API still rejects past this; the
// constant just controls the optimistic UX before the round-trip.
const CLARIFICATIONS_PER_LEVEL = 3

export const StudentChat: React.FC = () => {
  const { user } = useAuth()
  const [question, setQuestion] = useState('')
  const [photoB64, setPhotoB64] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [hintLevel, setHintLevel] = useState<1 | 2 | 3>(1)
  const [attempt, setAttempt] = useState('')
  const [status, setStatus] = useState<'idle' | 'loading' | 'answered'>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reviewMode, setReviewMode] = useState(false)
  const [learningResources, setLearningResources] = useState<LearningResource[]>([])
  const [sessionStage, setSessionStage] = useState<'start' | 'inProgress' | 'complete'>('start')
  const [finalAnswer, setFinalAnswer] = useState<string | null>(null)
  const [progress, setProgress] = useState<StudentProgress | null>(null)
  const [progressError, setProgressError] = useState<string | null>(null)
  // Full concept memory list — feeds the Practice Agent. The agent derives the
  // weakest concept internally and also exposes the full list in its picker so
  // students can choose a different concept/subject than the recommendation.
  // Refetched whenever historyVersion bumps OR a practice attempt resolves
  // (memoryVersion below).
  const [conceptMemory, setConceptMemory] = useState<ConceptMemoryItem[]>([])
  const [memoryVersion, setMemoryVersion] = useState(0)
  const bumpMemory = () => setMemoryVersion((v) => v + 1)
  // Bumped after every session-state change that the history panel cares about
  // (created / resolved / review / final-answer revealed). The panel watches
  // this and re-fetches page 1 when it changes.
  const [historyVersion, setHistoryVersion] = useState(0)
  const bumpHistory = () => setHistoryVersion((v) => v + 1)
  const analyticsDialogRef = useRef<HTMLDialogElement>(null)

  const openAnalyticsDetails = () => analyticsDialogRef.current?.showModal()
  const closeAnalyticsDetails = () => analyticsDialogRef.current?.close()
  const handleAnalyticsBackdrop = (e: React.MouseEvent<HTMLDialogElement>) => {
    if (e.target === analyticsDialogRef.current) closeAnalyticsDetails()
  }

  // Clarifications (PR 8) — state for "Ask a question" UX.
  const [clarifyHistory, setClarifyHistory] = useState<ClarifyEntry[]>([])
  const [clarifyRemaining, setClarifyRemaining] = useState<number>(CLARIFICATIONS_PER_LEVEL)
  const [clarifyMode, setClarifyMode] = useState<boolean>(false)
  const [clarifyText, setClarifyText] = useState<string>('')
  const [clarifyLoading, setClarifyLoading] = useState<boolean>(false)
  const [clarifyError, setClarifyError] = useState<string | null>(null)

  // Streaming (PR 9) — one in-flight AbortController at a time. Cancelled on
  // "Next Problem", on a new request, and on unmount.
  const streamAbortRef = useRef<AbortController | null>(null)

  const startNewAbort = (): AbortController => {
    streamAbortRef.current?.abort()
    const ctrl = new AbortController()
    streamAbortRef.current = ctrl
    return ctrl
  }

  // Cancel any in-flight stream when the component unmounts.
  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort()
    }
  }, [])

  const handleStartSession = async () => {
    if (!question.trim() && !photoB64) {
      setError('Please enter a homework question or attach a photo')
      return
    }

    const ctrl = startNewAbort()
    setStatus('loading')
    setError(null)
    setReviewMode(false)
    setLearningResources([])
    setHint('')
    setMessage(null)

    try {
      const stream = await api.startSessionStream(
        { question, photo_b64: photoB64 ?? undefined },
        ctrl.signal,
      )
      for await (const event of stream as AsyncIterable<StartSessionEvent>) {
        if (event.type === 'session_created') {
          const ev = event as StartSessionCreatedEvent
          setSessionId(ev.session_id)
          setHintLevel(ev.hint_level)
          setSessionStage('inProgress')
          setAttempt('')
          // New row exists server-side — surface it in the history panel as "open".
          bumpHistory()
        } else if (event.type === 'chunk') {
          const ev = event as ChunkEvent
          setHint((curr) => (curr ?? '') + ev.text)
        } else if (event.type === 'error') {
          const ev = event as ErrorEvent
          setError(ev.message)
          break
        } else if (event.type === 'done') {
          // Terminal — fall out of the loop.
          break
        }
      }
      // Photo (if any) has been consumed by the server; clear local state.
      setPhotoB64(null)
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError((err as Error).message)
      }
    } finally {
      setStatus('idle')
      if (streamAbortRef.current === ctrl) {
        streamAbortRef.current = null
      }
    }
  }

  const handleSubmitAttempt = async () => {
    if (!sessionId || !attempt.trim()) return

    const ctrl = startNewAbort()
    setStatus('loading')
    setError(null)
    // We don't pre-clear `hint` because the verdict may keep us in a
    // non-streaming branch (correct / review). The verdict + first chunk
    // arrive before any UI change matters.

    let willStreamHint = false

    try {
      const stream = await api.submitAttemptStream(
        sessionId,
        { attempt_text: attempt },
        ctrl.signal,
      )
      for await (const event of stream as AsyncIterable<AttemptEvent>) {
        if (event.type === 'verdict') {
          const ev = event as VerdictEvent
          if (ev.status === 'correct') {
            setMessage('You got it! Great work!')
            setReviewMode(false)
            setLearningResources([])
            setFinalAnswer(null)
            setSessionStage('complete')
            setAttempt('')
            // Session resolved — flip the row from "open" to "resolved".
            bumpHistory()
            continue
          }
          // Wrong path.
          const newLevel = ev.hint_level ?? hintLevel
          const levelChanged = newLevel !== hintLevel
          setHintLevel(newLevel)
          setMessage(ev.message ?? null)
          setReviewMode(ev.review_mode ?? false)
          setLearningResources(ev.learning_resources ?? [])
          setFinalAnswer(ev.final_answer ?? null)
          setAttempt('')
          if (levelChanged) {
            setClarifyHistory([])
            setClarifyRemaining(CLARIFICATIONS_PER_LEVEL)
            setClarifyMode(false)
            setClarifyText('')
            setClarifyError(null)
          }
          // Review-mode + final-answer paths carry a canned hint or no hint
          // at all and never stream. Otherwise we reset and prepare to append.
          if (ev.review_mode || ev.final_answer != null) {
            setHint(ev.static_hint ?? null)
            // Terminal wrong path — refresh history so status reflects server-side.
            bumpHistory()
          } else {
            setHint('')
            willStreamHint = true
          }
        } else if (event.type === 'chunk') {
          if (!willStreamHint) continue
          const ev = event as ChunkEvent
          setHint((curr) => (curr ?? '') + ev.text)
        } else if (event.type === 'error') {
          const ev = event as ErrorEvent
          setError(ev.message)
          break
        } else if (event.type === 'done') {
          break
        }
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError((err as Error).message)
      }
    } finally {
      setStatus('idle')
      if (streamAbortRef.current === ctrl) {
        streamAbortRef.current = null
      }
    }
  }


  const handleNextProblem = () => {
    setSessionId(null)
    setSessionStage('start')
    setQuestion('')
    setPhotoB64(null)
    setHint(null)
    setHintLevel(1)
    setAttempt('')
    setMessage(null)
    setError(null)
    setReviewMode(false)
    setFinalAnswer(null)
    setLearningResources([])
    setClarifyHistory([])
    setClarifyRemaining(CLARIFICATIONS_PER_LEVEL)
    setClarifyMode(false)
    setClarifyText('')
    setClarifyError(null)
  }

  const handleSubmitClarification = async () => {
    if (!sessionId || !clarifyText.trim() || clarifyLoading) return
    setClarifyLoading(true)
    setClarifyError(null)
    const question = clarifyText.trim()
    try {
      const result = await api.clarify(sessionId, { message: question })
      setClarifyHistory((curr) => [...curr, { question, response: result.clarification }])
      setClarifyRemaining(result.remaining)
      setClarifyText('')
      if (result.remaining === 0) {
        setClarifyMode(false)
      }
    } catch (err) {
      setClarifyError((err as Error).message)
    } finally {
      setClarifyLoading(false)
    }
  }

  const handleHistorySelect = (text: string) => {
    if (sessionStage !== 'start') handleNextProblem()
    setQuestion(text)
    setPhotoB64(null)
    setError(null)
    document
      .getElementById('question-textarea')
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  useEffect(() => {
    // ProtectedRoute guarantees user is non-null here, but guard anyway.
    if (!user) {
      setProgressError('Unable to load your progress.')
      return
    }

    api.getProgress(user.id)
      .then((data) => setProgress(data))
      .catch((err) => setProgressError(err.message))
    // historyVersion is bumped on session created / correct / review / final-answer —
    // exactly the events that change progress, so refresh the analytics card in
    // lockstep with the QuestionHistoryPanel.
  }, [user, historyVersion])

  // Knowledge-graph memory drives the Practice Agent CTA. Refetch whenever a
  // homework session resolves (historyVersion) or a practice attempt closes
  // the loop (memoryVersion). Failure is non-fatal — the agent falls back to
  // a subject picker.
  useEffect(() => {
    if (!user) return
    api.getStudentMemory(user.id)
      .then((data) => setConceptMemory(data.concepts))
      .catch(() => setConceptMemory([]))
  }, [user, historyVersion, memoryVersion])

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 p-3 sm:p-4">
      <div className="max-w-2xl mx-auto">
        <header className="text-center mb-6 sm:mb-8 pt-4 sm:pt-8">
          <h1 className="text-3xl sm:text-4xl font-bold text-indigo-900 mb-2">
            🦉 StudyOwl
          </h1>
          <p className="text-gray-600">Your AI Homework Assistant</p>
        </header>

        <div className="grid gap-4 sm:gap-6 mb-6 lg:grid-cols-3 items-start">
          <div className="lg:col-span-2 rounded-3xl bg-white/90 shadow-lg border border-indigo-100 overflow-hidden">
            <div className="flex items-center justify-between gap-3 px-4 sm:px-6 pt-4 sm:pt-6 pb-3">
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-xl" aria-hidden="true">📊</span>
                <h2 className="text-xl font-semibold text-indigo-900 truncate">Homework Analytics</h2>
              </div>
              {progress && progress.subjects.length > 0 && (
                <button
                  type="button"
                  onClick={openAnalyticsDetails}
                  className="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs sm:text-sm font-semibold text-indigo-700 hover:bg-indigo-100 transition whitespace-nowrap"
                >
                  View details
                  <span aria-hidden="true">→</span>
                </button>
              )}
            </div>

            <div className="px-4 sm:px-6 pb-4 sm:pb-6">
              {progressError ? (
                <p className="text-sm text-red-600">{progressError}</p>
              ) : progress ? (
                <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4">
                  <div className="rounded-2xl bg-indigo-50 p-4">
                    <p className="text-xs uppercase tracking-wide text-indigo-600">Subjects Tracked</p>
                    <p className="mt-2 text-3xl font-bold text-indigo-900">{progress.subjects.length}</p>
                  </div>
                  <div className="rounded-2xl bg-sky-50 p-4">
                    <p className="text-xs uppercase tracking-wide text-sky-600">Success Rate</p>
                    <p className="mt-2 text-3xl font-bold text-sky-900">{Math.round(progress.subjects.reduce((sum, item) => sum + item.success_rate, 0) / (progress.subjects.length || 1) * 100) / 100}%</p>
                  </div>
                  <div className="rounded-2xl bg-emerald-50 p-4">
                    <p className="text-xs uppercase tracking-wide text-emerald-600">Recent Sessions</p>
                    <p className="mt-2 text-3xl font-bold text-emerald-900">{progress.recent_sessions.length}</p>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-slate-500">Loading your learning progress...</p>
              )}
            </div>
          </div>

          <div className="rounded-3xl bg-white/90 p-4 sm:p-6 shadow-lg border border-indigo-100">
            <h2 className="text-xl font-semibold text-indigo-900 mb-3">Quick Tip</h2>
            <p className="text-sm text-slate-600">
              Track your progress on each subject, then use the hints below to build confidence before the final step.
            </p>
          </div>
        </div>

        <dialog
          ref={analyticsDialogRef}
          onClick={handleAnalyticsBackdrop}
          aria-labelledby="analytics-dialog-title"
          className="m-auto rounded-3xl bg-white shadow-2xl p-0 backdrop:bg-slate-900/50 w-[min(640px,calc(100vw-1.5rem))] max-h-[calc(100vh-2rem)]"
        >
          {progress && (
            <div className="flex flex-col max-h-[inherit]">
              <header className="flex items-center justify-between gap-3 border-b border-slate-100 px-5 sm:px-6 pt-5 sm:pt-6 pb-3">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-xl" aria-hidden="true">📊</span>
                  <h3 id="analytics-dialog-title" className="text-lg sm:text-xl font-semibold text-indigo-900 truncate">
                    Analytics — Details
                  </h3>
                </div>
                <button
                  type="button"
                  onClick={closeAnalyticsDetails}
                  aria-label="Close details"
                  className="rounded-full p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 transition"
                >
                  <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                    <path
                      fillRule="evenodd"
                      d="M4.293 4.293a1 1 0 0 1 1.414 0L10 8.586l4.293-4.293a1 1 0 1 1 1.414 1.414L11.414 10l4.293 4.293a1 1 0 0 1-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L8.586 10 4.293 5.707a1 1 0 0 1 0-1.414z"
                      clipRule="evenodd"
                    />
                  </svg>
                </button>
              </header>

              <div className="overflow-y-auto px-5 sm:px-6 py-4 sm:py-5 space-y-4">
                {progress.subjects.length > 0 && (
                  <ProgressChart
                    data={progress.subjects.map((s) => ({
                      subject: s.name,
                      sessions: s.sessions,
                      success_rate: Math.round(s.success_rate * 100),
                    }))}
                  />
                )}
                <div className="space-y-3">
                  {progress.subjects.map((subject) => (
                    <div key={subject.name} className="rounded-2xl bg-slate-50 p-4 border border-slate-200">
                      <div className="flex items-center justify-between gap-4">
                        <p className="font-semibold text-slate-900">{subject.name}</p>
                        <p className="text-sm text-slate-500">{subject.sessions} sessions</p>
                      </div>
                      <div className="mt-3 h-2 rounded-full bg-white">
                        <div className="h-full rounded-full bg-indigo-500" style={{ width: `${subject.success_rate * 100}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </dialog>

        {user && (
          <div className="mb-6">
            <QuestionHistoryPanel
              studentId={user.id}
              onSelect={handleHistorySelect}
              version={historyVersion}
            />
          </div>
        )}

        <div className="mb-6">
          <PracticeAgent
            concepts={conceptMemory}
            defaultSubject="math"
            onAttemptResolved={bumpMemory}
          />
        </div>

        <div className="bg-white rounded-lg shadow-lg p-4 sm:p-6 mb-6">
          {sessionStage === 'start' ? (
            <>
              <h2 className="text-2xl font-bold text-gray-800 mb-4">
                What are you working on?
              </h2>
              <textarea
                id="question-textarea"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Type or paste your homework question here..."
                className="w-full h-32 p-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 mb-4"
              />
              <div className="mb-4">
                <PhotoUpload onPhotoCapture={setPhotoB64} loading={status === 'loading'} />
                {photoB64 && (
                  <div className="mt-3 flex items-center justify-between rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-2 text-sm">
                    <span className="text-indigo-900">📷 Photo attached — we'll read the problem from it.</span>
                    <button
                      type="button"
                      onClick={() => setPhotoB64(null)}
                      className="text-indigo-700 underline hover:text-indigo-900"
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>
              <button
                onClick={handleStartSession}
                disabled={status === 'loading'}
                className="w-full bg-indigo-600 text-white py-2 rounded-lg font-semibold hover:bg-indigo-700 disabled:opacity-50 transition"
              >
                {status === 'loading' ? 'Loading...' : 'Get First Hint'}
              </button>
            </>
          ) : sessionStage === 'inProgress' ? (
            <>
             {message && (
                <div className="bg-green-50 border-l-4 border-green-500 p-4 mb-4 rounded">
                  <p className="text-gray-800">{message}</p>
                </div>
              )}
              <div className="mb-6">
                <p className="text-gray-600 mb-2">Question:</p>
                <p className="text-gray-900 bg-gray-50 p-3 rounded-lg whitespace-pre-wrap break-words">
                  {question}
                </p>
              </div>

              {hint && <HintBubble hint={hint} level={hintLevel} />}

              {hint && !reviewMode && !finalAnswer && (
                <div className="mt-3 mb-4 rounded-2xl border border-indigo-100 bg-indigo-50/40 p-4">
                  {clarifyHistory.length > 0 && (
                    <div className="space-y-3 mb-3">
                      {clarifyHistory.map((entry, idx) => (
                        <div key={idx} className="text-sm">
                          <p className="text-indigo-900">
                            <span className="font-semibold">You asked:</span> {entry.question}
                          </p>
                          <p className="text-gray-800 mt-1 pl-3 border-l-2 border-indigo-300">
                            {entry.response}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}

                  {clarifyMode ? (
                    <div>
                      <textarea
                        value={clarifyText}
                        onChange={(e) => setClarifyText(e.target.value)}
                        placeholder="Ask a question about this hint…"
                        className="w-full h-20 p-2 text-sm border border-indigo-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white"
                        disabled={clarifyLoading}
                      />
                      <div className="flex items-center justify-end gap-2 mt-2">
                        <button
                          onClick={() => {
                            setClarifyMode(false)
                            setClarifyText('')
                            setClarifyError(null)
                          }}
                          disabled={clarifyLoading}
                          className="text-xs text-gray-600 hover:text-gray-900 px-2"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={handleSubmitClarification}
                          disabled={clarifyLoading || !clarifyText.trim()}
                          className="text-xs px-3 py-1 rounded-full bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 transition"
                        >
                          {clarifyLoading ? 'Thinking…' : 'Ask'}
                        </button>
                      </div>
                      {clarifyError && (
                        <p className="text-xs text-red-700 mt-1">{clarifyError}</p>
                      )}
                    </div>
                  ) : (
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-xs text-gray-600">
                        {clarifyRemaining > 0
                          ? `${clarifyRemaining} question${clarifyRemaining === 1 ? '' : 's'} left at this hint`
                          : "You've used your clarifications for this hint — try submitting an answer."}
                      </p>
                      <button
                        onClick={() => setClarifyMode(true)}
                        disabled={clarifyRemaining === 0}
                        className="text-xs px-3 py-1 rounded-full bg-white border border-indigo-300 text-indigo-700 hover:bg-indigo-100 disabled:opacity-50 disabled:cursor-not-allowed transition"
                      >
                        Ask a question
                      </button>
                    </div>
                  )}
                </div>
              )}

              {reviewMode && (
                <div className="relative overflow-hidden rounded-lg p-6 bg-gradient-to-br from-orange-50 via-white to-amber-50 border border-orange-200 mb-4">
                  <div className="celebration-wrapper pointer-events-none">
                    {Array.from({ length: 10 }).map((_, index) => (
                      <span
                        key={index}
                        className="confetti-piece"
                        style={{
                          left: `${8 + index * 8}%`,
                          animationDelay: `${index * 0.08}s`,
                          backgroundColor: ['#f59e0b', '#ea580c', '#d97706', '#f97316'][index % 4],
                        }}
                      />
                    ))}
                  </div>
                  <div className="relative">
                    <h2 className="text-3xl font-bold text-orange-900 mb-3 text-center">
                      😞 Review Needed
                    </h2>
                    <p className="text-orange-800 mb-6 text-lg text-center">
                      You've used all 3 attempts. It's time to learn more before trying again.
                    </p>

                    {learningResources && learningResources.length > 0 ? (
                      <>
                        <p className="text-orange-900 font-semibold mb-4 text-center">
                          Here are some resources to help you understand this topic better:
                        </p>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4 mb-6">
                          {learningResources.map((resource, index) => (
                            <div
                              key={index}
                              className="bg-white rounded-lg p-4 border border-orange-200 hover:shadow-md transition-shadow"
                            >
                              <h3 className="font-semibold text-orange-900 mb-2 line-clamp-2">
                                {resource.title || 'Learning Resource'}
                              </h3>
                              {resource.summary && (
                                <p className="text-sm text-gray-700 mb-3 line-clamp-3">
                                  {resource.summary}
                                </p>
                              )}
                              {resource.url ? (
                                <a
                                  href={resource.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="inline-flex items-center text-sm font-semibold text-indigo-700 hover:text-indigo-900 underline"
                                >
                                  Open resource →
                                </a>
                              ) : (
                                <p className="text-sm text-gray-500">No direct link available</p>
                              )}
                            </div>
                          ))}
                        </div>
                      </>
                    ) : (
                      <p className="text-sm text-orange-800 mb-6 text-center">
                        Review a related website article or textbook example about the topic, then return and try a new question.
                      </p>
                    )}

                    <div className="text-center">
                      <button
                        onClick={handleNextProblem}
                        className="px-6 py-3 bg-indigo-600 text-white rounded-lg font-semibold hover:bg-indigo-700 transition"
                      >
                        Back to new question
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {/* {message && (
                <div className="bg-green-50 border-l-4 border-green-500 p-4 mb-4 rounded">
                  <p className="text-gray-800">{message}</p>
                </div>
              )} */}

              {finalAnswer && (
                <div className="bg-blue-50 border-l-4 border-blue-500 p-4 mb-4 rounded">
                  <p className="text-blue-900 font-semibold mb-2">Final answer revealed</p>
                  <p className="text-gray-800 whitespace-pre-wrap">{finalAnswer}</p>
                  <div className="mt-4 text-right">
                    <button
                      onClick={handleNextProblem}
                      className="inline-flex items-center rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 transition"
                    >
                      Try a new question
                    </button>
                  </div>
                </div>
              )}

              {!reviewMode && sessionId && !finalAnswer && (
                <>
                  <input
                    type="text"
                    value={attempt}
                    onChange={(e) => setAttempt(e.target.value)}
                    placeholder="Your answer..."
                    className="w-full mb-3 p-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    onKeyPress={(e) => e.key === 'Enter' && handleSubmitAttempt()}
                  />
                  <button
                    onClick={handleSubmitAttempt}
                    disabled={status === 'loading' || !attempt.trim()}
                    className="w-full bg-green-600 text-white py-2 rounded-lg font-semibold hover:bg-green-700 disabled:opacity-50 transition"
                  >
                    {status === 'loading' ? 'Checking...' : 'Submit Answer'}
                  </button>
                </>
              )}
            </>
          ) : (
            <div className="relative overflow-hidden rounded-lg p-6 bg-gradient-to-br from-emerald-50 via-white to-lime-50 border border-emerald-200 mb-4">
              <div className="celebration-wrapper pointer-events-none">
                {Array.from({ length: 12 }).map((_, index) => (
                  <span
                    key={index}
                    className="confetti-piece"
                    style={{
                      left: `${8 + index * 7}%`,
                      animationDelay: `${index * 0.1}s`,
                      backgroundColor: ['#84cc16', '#22c55e', '#14b8a6', '#0ea5e9'][index % 4],
                    }}
                  />
                ))}
              </div>
              <div className="relative text-center">
                <h2 className="text-3xl font-bold text-emerald-900 mb-3">
                  🎉 Congratulations!
                </h2>
                <p className="text-gray-700 mb-6 text-lg">
                  {message ?? 'That was the right answer! Great job.'}
                </p>
                <button
                  onClick={handleNextProblem}
                  className="px-6 py-3 bg-indigo-600 text-white rounded-lg font-semibold hover:bg-indigo-700 transition"
                >
                  Next problem
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700">
              {error}
            </div>
          )}
        </div>

        <div className="bg-indigo-50 rounded-lg p-4 text-center text-sm text-gray-600">
          <p>
            📚 StudyOwl uses Socratic questioning to help you learn, not spoil the answers!
          </p>
        </div>
      </div>
    </div>
  )
}

export default StudentChat
