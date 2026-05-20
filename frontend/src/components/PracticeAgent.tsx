import React, { useState } from 'react'
import { api } from '../api/studyowl'
import type {
  ConceptMemoryItem,
  GeneratedProblem,
  PracticeAttemptResponse,
  PracticeDifficulty,
} from '../api/studyowl'

interface PracticeAgentProps {
  /** Memory rows are fetched once in StudentChat; we surface the weakest as a CTA. */
  weakestConcept: ConceptMemoryItem | null
  /** Subject filter used when memory is empty (cold start). */
  defaultSubject?: string
  /** Bumped to tell the parent it should refetch memory after a verified attempt. */
  onAttemptResolved?: () => void
}

const DIFFICULTY_BADGE: Record<PracticeDifficulty, string> = {
  easy: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  medium: 'bg-amber-100 text-amber-800 border-amber-300',
  hard: 'bg-rose-100 text-rose-800 border-rose-300',
}

const SUBJECT_OPTIONS = ['math', 'science', 'english', 'history']

export const PracticeAgent: React.FC<PracticeAgentProps> = ({
  weakestConcept,
  defaultSubject = 'math',
  onAttemptResolved,
}) => {
  const [problem, setProblem] = useState<GeneratedProblem | null>(null)
  const [answer, setAnswer] = useState('')
  const [attemptResult, setAttemptResult] = useState<PracticeAttemptResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [subject, setSubject] = useState<string>(
    weakestConcept?.subject ?? defaultSubject,
  )

  const reset = () => {
    setProblem(null)
    setAnswer('')
    setAttemptResult(null)
    setError(null)
  }

  const handleGenerate = async (opts?: { concept?: string; subject?: string }) => {
    reset()
    setLoading(true)
    try {
      const generated = await api.generatePracticeProblem({
        subject: opts?.subject ?? subject,
        concept: opts?.concept,
      })
      setProblem(generated)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = async () => {
    if (!problem || !answer.trim()) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.submitPracticeAttempt(problem.id, answer.trim())
      setAttemptResult(result)
      onAttemptResolved?.()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="rounded-3xl bg-white/90 p-4 sm:p-6 shadow-lg border border-indigo-100">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xl" aria-hidden="true">🎯</span>
          <h2 className="text-xl font-semibold text-indigo-900 truncate">
            Adaptive Practice
          </h2>
        </div>
        {problem && (
          <button
            type="button"
            onClick={reset}
            className="text-xs sm:text-sm font-semibold text-indigo-700 hover:text-indigo-900"
          >
            Start over
          </button>
        )}
      </div>

      {error && (
        <p className="mb-3 rounded-lg bg-rose-50 border border-rose-200 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {!problem && (
        <IdleState
          loading={loading}
          weakestConcept={weakestConcept}
          subject={subject}
          onSubjectChange={setSubject}
          onGenerate={handleGenerate}
        />
      )}

      {problem && !attemptResult && (
        <ActiveState
          problem={problem}
          answer={answer}
          loading={loading}
          onAnswerChange={setAnswer}
          onSubmit={handleSubmit}
        />
      )}

      {problem && attemptResult && (
        <ResolvedState
          problem={problem}
          result={attemptResult}
          onTryAnother={() => handleGenerate({ concept: problem.concept ?? undefined })}
          loading={loading}
        />
      )}
    </div>
  )
}

// ── Substates ────────────────────────────────────────────────────────────────

interface IdleStateProps {
  loading: boolean
  weakestConcept: ConceptMemoryItem | null
  subject: string
  onSubjectChange: (s: string) => void
  onGenerate: (opts?: { concept?: string; subject?: string }) => void
}

const IdleState: React.FC<IdleStateProps> = ({
  loading,
  weakestConcept,
  subject,
  onSubjectChange,
  onGenerate,
}) => {
  if (weakestConcept) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-slate-700">
          You've been working on{' '}
          <span className="font-semibold text-indigo-900">
            {weakestConcept.label}
          </span>{' '}
          <span className="text-slate-500">
            ({weakestConcept.subject}, {weakestConcept.status})
          </span>
          . Want a fresh problem targeting this concept?
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => onGenerate({ concept: weakestConcept.concept, subject: weakestConcept.subject })}
            disabled={loading}
            className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow hover:bg-indigo-700 disabled:opacity-50"
          >
            {loading ? 'Generating…' : 'Practice this concept'}
          </button>
          <button
            type="button"
            onClick={() => onGenerate({ subject: weakestConcept.subject })}
            disabled={loading}
            className="rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2 text-sm font-semibold text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
          >
            Any {weakestConcept.subject} problem
          </button>
        </div>
      </div>
    )
  }

  // Cold start: no memory rows yet → subject picker.
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-700">
        Pick a subject to get a fresh practice problem. The agent will adapt to
        your weak concepts once you've completed a few sessions.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs uppercase tracking-wide text-indigo-700">
          Subject
        </label>
        <select
          value={subject}
          onChange={(e) => onSubjectChange(e.target.value)}
          className="rounded-lg border border-indigo-200 bg-white px-2 py-1 text-sm text-indigo-900"
        >
          {SUBJECT_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => onGenerate({ subject })}
          disabled={loading}
          className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow hover:bg-indigo-700 disabled:opacity-50"
        >
          {loading ? 'Generating…' : 'Generate a problem'}
        </button>
      </div>
    </div>
  )
}

interface ActiveStateProps {
  problem: GeneratedProblem
  answer: string
  loading: boolean
  onAnswerChange: (s: string) => void
  onSubmit: () => void
}

const ActiveState: React.FC<ActiveStateProps> = ({
  problem,
  answer,
  loading,
  onAnswerChange,
  onSubmit,
}) => (
  <div className="space-y-3">
    <ProblemHeader problem={problem} />
    <div className="rounded-2xl bg-indigo-50 border border-indigo-200 p-4">
      <p className="text-sm text-indigo-900 whitespace-pre-wrap leading-relaxed">
        {problem.prompt_text}
      </p>
    </div>
    <label className="block text-xs uppercase tracking-wide text-indigo-700">
      Your answer
    </label>
    <textarea
      value={answer}
      onChange={(e) => onAnswerChange(e.target.value)}
      rows={2}
      placeholder="Type your answer…"
      className="w-full rounded-xl border border-indigo-200 bg-white px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-300"
    />
    <button
      type="button"
      onClick={onSubmit}
      disabled={loading || !answer.trim()}
      className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow hover:bg-indigo-700 disabled:opacity-50"
    >
      {loading ? 'Checking…' : 'Submit answer'}
    </button>
  </div>
)

interface ResolvedStateProps {
  problem: GeneratedProblem
  result: PracticeAttemptResponse
  loading: boolean
  onTryAnother: () => void
}

const ResolvedState: React.FC<ResolvedStateProps> = ({
  problem,
  result,
  loading,
  onTryAnother,
}) => (
  <div className="space-y-3">
    <ProblemHeader problem={problem} />
    <div
      className={`rounded-2xl border p-4 ${
        result.correct
          ? 'bg-emerald-50 border-emerald-200'
          : 'bg-rose-50 border-rose-200'
      }`}
    >
      <p className={`text-sm font-semibold ${result.correct ? 'text-emerald-800' : 'text-rose-800'}`}>
        {result.correct ? '✓ Correct! Nice work.' : '✗ Not quite.'}
      </p>
      <p className="mt-2 text-sm text-slate-700">
        <span className="font-semibold">Canonical answer:</span>{' '}
        <span className="font-mono">{result.answer_key}</span>
      </p>
      {result.explanation && (
        <p className="mt-2 text-sm text-slate-700 whitespace-pre-wrap">
          {result.explanation}
        </p>
      )}
      {result.already_attempted && (
        <p className="mt-2 text-xs text-slate-500">
          (You've already attempted this problem — showing the stored verdict.)
        </p>
      )}
    </div>
    <button
      type="button"
      onClick={onTryAnother}
      disabled={loading}
      className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow hover:bg-indigo-700 disabled:opacity-50"
    >
      {loading ? 'Generating…' : 'Try another'}
    </button>
  </div>
)

// ── Shared header ────────────────────────────────────────────────────────────

const ProblemHeader: React.FC<{ problem: GeneratedProblem }> = ({ problem }) => (
  <div className="flex flex-wrap items-center gap-2 text-xs">
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 font-semibold ${DIFFICULTY_BADGE[problem.difficulty]}`}>
      {problem.difficulty}
    </span>
    <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-indigo-700 font-medium">
      {problem.subject}
    </span>
    {problem.concept_label && (
      <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-700">
        {problem.concept_label}
      </span>
    )}
    {problem.verifier_status === 'unverified' && (
      <span
        className="rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-amber-800"
        title="The verifier sub-agent couldn't double-check this problem. The answer key may be off."
      >
        ⚠ unverified
      </span>
    )}
  </div>
)
