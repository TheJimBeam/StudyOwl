import React, { useMemo, useState } from 'react'
import { api } from '../api/studyowl'
import type {
  ConceptMemoryItem,
  ConceptStatus,
  GeneratedProblem,
  PracticeAttemptResponse,
  PracticeDifficulty,
} from '../api/studyowl'

interface PracticeAgentProps {
  /** All memory rows for the student. Sorted weakest-first inside; [0] is the recommendation. */
  concepts: ConceptMemoryItem[]
  /** Subject filter used when memory is empty (cold start). */
  defaultSubject?: string
  /** Bumped to tell the parent it should refetch memory after a verified attempt. */
  onAttemptResolved?: () => void
}

const DIFFICULTY_BADGE: Record<PracticeDifficulty, string> = {
  easy: 'bg-green-100 text-green-800',
  medium: 'bg-amber-100 text-amber-800',
  hard: 'bg-red-100 text-red-800',
}

const SUBJECT_LABEL: Record<string, string> = {
  math: 'Math',
  science: 'Science',
  english: 'English',
  history: 'History',
}
const SUBJECT_OPTIONS = Object.keys(SUBJECT_LABEL)

const STATUS_META: Record<ConceptStatus, { label: string; dot: string; text: string }> = {
  mastered: { label: 'Mastered', dot: 'bg-green-500', text: 'text-green-700' },
  partial: { label: 'Partial', dot: 'bg-amber-500', text: 'text-amber-700' },
  struggling: { label: 'Struggling', dot: 'bg-red-500', text: 'text-red-700' },
}

function subjectLabel(s: string): string {
  return SUBJECT_LABEL[s] ?? s
}

export const PracticeAgent: React.FC<PracticeAgentProps> = ({
  concepts,
  defaultSubject = 'math',
  onAttemptResolved,
}) => {
  const sortedConcepts = useMemo(
    () => [...concepts].sort((a, b) => a.decayed_confidence - b.decayed_confidence),
    [concepts],
  )
  const weakestConcept = sortedConcepts[0] ?? null

  const [problem, setProblem] = useState<GeneratedProblem | null>(null)
  const [answer, setAnswer] = useState('')
  const [attemptResult, setAttemptResult] = useState<PracticeAttemptResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [subject, setSubject] = useState<string>(
    weakestConcept?.subject ?? defaultSubject,
  )
  // Picker visibility is lifted here so the resolved view can re-open it
  // when the student wants to switch concepts after submitting.
  const [showPicker, setShowPicker] = useState(!weakestConcept)
  const [selectedConcept, setSelectedConcept] = useState<string>('')

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

  const handlePickAnother = () => {
    reset()
    setShowPicker(true)
  }

  return (
    <div className="bg-white rounded-lg shadow-lg p-4 sm:p-6">
      <div className="flex items-center justify-between gap-3 mb-4">
        <h2 className="text-2xl font-bold text-gray-800">Adaptive Practice</h2>
        {problem && (
          <button
            type="button"
            onClick={reset}
            className="text-sm font-semibold text-indigo-700 hover:text-indigo-900"
          >
            Start over
          </button>
        )}
      </div>

      {error && (
        <p className="mb-4 rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {!problem && (
        <IdleState
          loading={loading}
          weakestConcept={weakestConcept}
          concepts={sortedConcepts}
          subject={subject}
          onSubjectChange={(s) => {
            setSubject(s)
            setSelectedConcept('')
          }}
          onGenerate={handleGenerate}
          showPicker={showPicker}
          onTogglePicker={() => setShowPicker((v) => !v)}
          selectedConcept={selectedConcept}
          onSelectedConceptChange={setSelectedConcept}
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
          onPickAnother={handlePickAnother}
          loading={loading}
        />
      )}
    </div>
  )
}

// ── Idle ─────────────────────────────────────────────────────────────────────

interface IdleStateProps {
  loading: boolean
  weakestConcept: ConceptMemoryItem | null
  concepts: ConceptMemoryItem[]
  subject: string
  onSubjectChange: (s: string) => void
  onGenerate: (opts?: { concept?: string; subject?: string }) => void
  showPicker: boolean
  onTogglePicker: () => void
  selectedConcept: string
  onSelectedConceptChange: (concept: string) => void
}

const IdleState: React.FC<IdleStateProps> = ({
  loading,
  weakestConcept,
  concepts,
  subject,
  onSubjectChange,
  onGenerate,
  showPicker,
  onTogglePicker,
  selectedConcept,
  onSelectedConceptChange,
}) => {
  const conceptsInSubject = concepts.filter((c) => c.subject === subject)

  return (
    <div className="space-y-4">
      {weakestConcept ? (
        <RecommendationTile
          concept={weakestConcept}
          loading={loading}
          onPractice={() =>
            onGenerate({ concept: weakestConcept.concept, subject: weakestConcept.subject })
          }
          onAnyInSubject={() => onGenerate({ subject: weakestConcept.subject })}
        />
      ) : (
        <p className="text-gray-600">
          No concept memory yet. Pick a subject (and optionally a concept) below
          to get a fresh practice problem. The agent will adapt to your weak
          concepts once you've completed a few sessions.
        </p>
      )}

      {weakestConcept && (
        <button
          type="button"
          onClick={onTogglePicker}
          className="text-sm font-semibold text-indigo-700 hover:text-indigo-900"
          aria-expanded={showPicker}
        >
          {showPicker ? 'Hide options' : 'Practice something else'}
        </button>
      )}

      {(showPicker || !weakestConcept) && (
        <div className="rounded-lg border border-gray-200 p-4 space-y-4">
          <div>
            <p className="text-sm font-semibold text-gray-700 mb-2">Subject</p>
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Subject">
              {SUBJECT_OPTIONS.map((s) => (
                <Chip
                  key={s}
                  active={s === subject}
                  onClick={() => onSubjectChange(s)}
                  label={SUBJECT_LABEL[s]}
                />
              ))}
            </div>
          </div>

          <div>
            <div className="flex items-baseline justify-between mb-2">
              <p className="text-sm font-semibold text-gray-700">Concept</p>
              {conceptsInSubject.length > 0 && (
                <p className="text-xs text-gray-500">
                  {conceptsInSubject.length} from your memory
                </p>
              )}
            </div>
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Concept">
              <Chip
                active={selectedConcept === ''}
                onClick={() => onSelectedConceptChange('')}
                label={`Any ${subjectLabel(subject).toLowerCase()} concept`}
              />
              {conceptsInSubject.map((c) => (
                <Chip
                  key={c.concept}
                  active={selectedConcept === c.concept}
                  onClick={() => onSelectedConceptChange(c.concept)}
                  label={c.label}
                  statusDot={STATUS_META[c.status].dot}
                />
              ))}
            </div>
            {conceptsInSubject.length === 0 && (
              <p className="mt-2 text-xs text-gray-500">
                No memory for {subjectLabel(subject)} yet — will generate a generic problem.
              </p>
            )}
          </div>

          <button
            type="button"
            onClick={() => onGenerate({ subject, concept: selectedConcept || undefined })}
            disabled={loading}
            className="w-full bg-indigo-600 text-white py-2 rounded-lg font-semibold hover:bg-indigo-700 disabled:opacity-50 transition"
          >
            {loading ? 'Generating...' : 'Generate problem'}
          </button>
        </div>
      )}
    </div>
  )
}

// ── Recommendation tile ──────────────────────────────────────────────────────

const RecommendationTile: React.FC<{
  concept: ConceptMemoryItem
  loading: boolean
  onPractice: () => void
  onAnyInSubject: () => void
}> = ({ concept, loading, onPractice, onAnyInSubject }) => {
  const status = STATUS_META[concept.status]
  const pct = Math.round(concept.decayed_confidence * 100)
  return (
    <div className="rounded-lg border border-gray-200 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
        Recommended for you
      </p>
      <p className="text-lg font-semibold text-gray-900 break-words">{concept.label}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="text-gray-600">{subjectLabel(concept.subject)}</span>
        <span className="flex items-center gap-1.5">
          <span aria-hidden="true" className={`inline-block w-2 h-2 rounded-full ${status.dot}`} />
          <span className={status.text}>{status.label}</span>
        </span>
        <span className="text-gray-500">{pct}% confidence</span>
      </div>
      <div className="flex flex-wrap items-center gap-2 mt-4">
        <button
          type="button"
          onClick={onPractice}
          disabled={loading}
          className="bg-indigo-600 text-white py-2 px-4 rounded-lg font-semibold hover:bg-indigo-700 disabled:opacity-50 transition"
        >
          {loading ? 'Generating...' : 'Practice this concept'}
        </button>
        <button
          type="button"
          onClick={onAnyInSubject}
          disabled={loading}
          className="bg-white text-indigo-700 border border-indigo-200 py-2 px-4 rounded-lg font-semibold hover:bg-indigo-50 disabled:opacity-50 transition"
        >
          Any {subjectLabel(concept.subject).toLowerCase()} problem
        </button>
      </div>
    </div>
  )
}

// ── Chip ─────────────────────────────────────────────────────────────────────

const Chip: React.FC<{
  active: boolean
  onClick: () => void
  label: string
  statusDot?: string
}> = ({ active, onClick, label, statusDot }) => (
  <button
    type="button"
    role="radio"
    aria-checked={active}
    onClick={onClick}
    className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium border transition ${
      active
        ? 'bg-indigo-600 text-white border-indigo-600'
        : 'bg-white text-gray-700 border-gray-300 hover:border-indigo-500 hover:text-indigo-700'
    }`}
  >
    {statusDot && (
      <span aria-hidden="true" className={`inline-block w-1.5 h-1.5 rounded-full ${statusDot}`} />
    )}
    <span className="break-words">{label}</span>
  </button>
)

// ── Active ───────────────────────────────────────────────────────────────────

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
  <div className="space-y-4">
    <ProblemHeader problem={problem} />
    <div>
      <p className="text-gray-600 mb-2">Question:</p>
      <p className="text-gray-900 bg-gray-50 p-3 rounded-lg whitespace-pre-wrap break-words">
        {problem.prompt_text}
      </p>
    </div>
    <div>
      <label
        htmlFor="practice-answer"
        className="block text-sm font-semibold text-gray-700 mb-2"
      >
        Your answer
      </label>
      <textarea
        id="practice-answer"
        value={answer}
        onChange={(e) => onAnswerChange(e.target.value)}
        rows={3}
        placeholder="Type your answer..."
        className="w-full p-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
      />
    </div>
    <button
      type="button"
      onClick={onSubmit}
      disabled={loading || !answer.trim()}
      className="w-full bg-indigo-600 text-white py-2 rounded-lg font-semibold hover:bg-indigo-700 disabled:opacity-50 transition"
    >
      {loading ? 'Checking...' : 'Submit answer'}
    </button>
  </div>
)

// ── Resolved ─────────────────────────────────────────────────────────────────

interface ResolvedStateProps {
  problem: GeneratedProblem
  result: PracticeAttemptResponse
  loading: boolean
  onTryAnother: () => void
  onPickAnother: () => void
}

const ResolvedState: React.FC<ResolvedStateProps> = ({
  problem,
  result,
  loading,
  onTryAnother,
  onPickAnother,
}) => (
  <div className="space-y-4">
    <ProblemHeader problem={problem} />
    <div
      className={`p-4 rounded-lg border-l-4 ${
        result.correct
          ? 'bg-green-50 border-green-500'
          : 'bg-red-50 border-red-500'
      }`}
    >
      <p className={`font-semibold ${result.correct ? 'text-green-800' : 'text-red-800'}`}>
        {result.correct ? 'Correct! Nice work.' : 'Not quite.'}
      </p>
      <p className="mt-2 text-sm text-gray-700">
        <span className="font-semibold">Answer:</span>{' '}
        <span className="font-mono">{result.answer_key}</span>
      </p>
      {result.explanation && (
        <p className="mt-2 text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">
          {result.explanation}
        </p>
      )}
      {result.already_attempted && (
        <p className="mt-2 text-xs text-gray-500 italic">
          You've already attempted this problem — showing the stored verdict.
        </p>
      )}
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={onTryAnother}
        disabled={loading}
        className="flex-1 bg-indigo-600 text-white py-2 px-4 rounded-lg font-semibold hover:bg-indigo-700 disabled:opacity-50 transition"
      >
        {loading ? 'Generating...' : 'Try another like this'}
      </button>
      <button
        type="button"
        onClick={onPickAnother}
        disabled={loading}
        className="flex-1 bg-white text-indigo-700 border border-indigo-200 py-2 px-4 rounded-lg font-semibold hover:bg-indigo-50 disabled:opacity-50 transition"
      >
        Pick something else
      </button>
    </div>
  </div>
)

// ── Shared header ────────────────────────────────────────────────────────────

const ProblemHeader: React.FC<{ problem: GeneratedProblem }> = ({ problem }) => (
  <div className="flex flex-wrap items-center gap-2 text-xs">
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 font-semibold capitalize ${DIFFICULTY_BADGE[problem.difficulty]}`}>
      {problem.difficulty}
    </span>
    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-700 font-medium">
      {subjectLabel(problem.subject)}
    </span>
    {problem.concept_label && (
      <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-700">
        {problem.concept_label}
      </span>
    )}
    {problem.verifier_status === 'unverified' && (
      <span
        className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800"
        title="The verifier sub-agent couldn't double-check this problem. The answer key may be off."
      >
        ⚠ unverified
      </span>
    )}
  </div>
)
