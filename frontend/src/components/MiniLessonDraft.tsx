import React, { useState } from 'react'
import type { CopilotPattern, CopilotSignalKind } from '../api/studyowl'

interface MiniLessonDraftProps {
  pattern: CopilotPattern
  onRetryRegenerate?: () => void
}

const SIGNAL_BADGE: Record<CopilotSignalKind, { label: string; classes: string }> = {
  level3_stuck: { label: 'Level 3 stuck', classes: 'bg-rose-100 text-rose-700' },
  repeated_failure: { label: 'Repeated failure', classes: 'bg-amber-100 text-amber-700' },
  concept_struggle: { label: 'Concept struggle', classes: 'bg-sky-100 text-sky-700' },
}

// Trivial inline markdown renderer — enough for headings, bold, and bullets.
// We deliberately don't pull in a markdown lib for one component; the draft
// content is auto-generated and the structure is fixed by the prompt.
function renderMarkdown(md: string): React.ReactNode {
  const lines = md.split('\n')
  const out: React.ReactNode[] = []
  let buffer: string[] = []

  const flushList = () => {
    if (buffer.length === 0) return
    out.push(
      <ul key={`ul-${out.length}`} className="list-disc list-inside space-y-1 my-2 text-sm text-slate-700">
        {buffer.map((item, i) => (
          <li key={i}>{renderInline(item.replace(/^[-*]\s+/, ''))}</li>
        ))}
      </ul>,
    )
    buffer = []
  }

  lines.forEach((line, i) => {
    const trimmed = line.trim()
    if (/^[-*]\s+/.test(trimmed)) {
      buffer.push(trimmed)
      return
    }
    flushList()
    if (trimmed.startsWith('# ')) {
      out.push(
        <h3 key={i} className="text-base font-semibold text-slate-900 mt-3 mb-2">
          {renderInline(trimmed.slice(2))}
        </h3>,
      )
    } else if (trimmed.startsWith('## ')) {
      out.push(
        <h4 key={i} className="text-sm font-semibold text-slate-800 mt-3 mb-1">
          {renderInline(trimmed.slice(3))}
        </h4>,
      )
    } else if (trimmed === '') {
      // skip — paragraph breaks come naturally from block structure.
    } else {
      out.push(
        <p key={i} className="my-2 text-sm text-slate-700 leading-relaxed">
          {renderInline(trimmed)}
        </p>,
      )
    }
  })
  flushList()
  return out
}

function renderInline(text: string): React.ReactNode {
  // Bold (**…**) only — kept minimal on purpose.
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return (
        <strong key={i} className="font-semibold text-slate-900">
          {part.slice(2, -2)}
        </strong>
      )
    }
    return <React.Fragment key={i}>{part}</React.Fragment>
  })
}

export const MiniLessonDraft: React.FC<MiniLessonDraftProps> = ({ pattern, onRetryRegenerate }) => {
  const [expanded, setExpanded] = useState(false)
  const [reviewed, setReviewed] = useState(false)
  const [copied, setCopied] = useState(false)

  const badge = SIGNAL_BADGE[pattern.signal_kind]
  const ratioPct = Math.round(pattern.affected_ratio * 100)
  const draftAvailable = pattern.mini_lesson_status === 'ok' && pattern.mini_lesson_md

  const handleCopy = async () => {
    if (!pattern.mini_lesson_md) return
    try {
      await navigator.clipboard.writeText(pattern.mini_lesson_md)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard denied — keep the button responsive but silent */
    }
  }

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-semibold text-slate-900 break-words">
            {pattern.headline}
          </p>
          <p className="text-xs text-slate-500 mt-0.5">
            {pattern.subject.charAt(0).toUpperCase() + pattern.subject.slice(1)}
            {pattern.concept_label && ` • ${pattern.concept_label}`}
            {` • ${ratioPct}% of cohort`}
          </p>
        </div>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${badge.classes}`}
          title={`Signal: ${pattern.signal_kind}`}
        >
          {badge.label}
        </span>
      </div>

      {draftAvailable && (
        <div className="mt-3 rounded-xl bg-slate-50 border border-slate-200 p-3">
          <p className="text-xs italic text-slate-600 mb-2">
            This is a suggestion, not a plan. The Co-Pilot looked at last week's
            struggles and drafted a starting point — edit freely, or ignore.
          </p>

          <div className="flex flex-wrap items-center gap-2 mb-1">
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="inline-flex items-center gap-1 text-xs font-semibold px-3 py-1.5 rounded-lg border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50 transition"
            >
              {expanded ? 'Hide draft' : 'Open draft'}
            </button>
            <button
              type="button"
              onClick={handleCopy}
              disabled={!expanded}
              className="inline-flex items-center gap-1 text-xs font-semibold px-3 py-1.5 rounded-lg border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50 transition"
              title="Copy markdown source"
            >
              {copied ? 'Copied!' : 'Copy markdown'}
            </button>
            <button
              type="button"
              onClick={() => setReviewed((v) => !v)}
              className={`inline-flex items-center gap-1 text-xs font-semibold px-3 py-1.5 rounded-lg border transition ${
                reviewed
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
              }`}
            >
              {reviewed ? '✓ Reviewed' : 'Mark as reviewed'}
            </button>
          </div>

          {expanded && (
            <div className="mt-2 prose-sm max-w-none">
              {renderMarkdown(pattern.mini_lesson_md as string)}
            </div>
          )}
        </div>
      )}

      {pattern.mini_lesson_status === 'failed' && (
        <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-amber-800">
            Rollup ready, but the draft mini-lesson couldn't be generated.
          </p>
          {onRetryRegenerate && (
            <button
              type="button"
              onClick={onRetryRegenerate}
              className="text-xs font-semibold px-3 py-1.5 rounded-lg border border-amber-300 bg-white text-amber-800 hover:bg-amber-100 transition"
            >
              Try regenerating
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export default MiniLessonDraft
