/**
 * StudyOwl typed API client.
 * All backend calls go through here — never use fetch() directly in components.
 */

const BASE = import.meta.env.VITE_API_URL ?? "";

// ── Types ────────────────────────────────────────────────────────────────────

export interface SignUpRequest {
  name: string;
  email: string;
  password: string;
  grade_level: string;
  role?: "student" | "teacher";
}

export interface LoginRequest {
  email: string;
  password: string;
  role: "student" | "teacher";
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  role: "student" | "teacher";
  name: string;
}

export interface StartSessionRequest {
  question: string;
  photo_b64?: string;
}

export interface StartSessionResponse {
  session_id: string;
  hint: string;
  hint_level: 1 | 2 | 3;
  subject: string;
}

// ── PR 9: streaming event shapes ──────────────────────────────────────────────

export interface StartSessionCreatedEvent {
  type: "session_created";
  session_id: string;
  subject: string;
  hint_level: 1 | 2 | 3;
}

export interface ChunkEvent {
  type: "chunk";
  text: string;
}

export interface DoneEvent {
  type: "done";
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface VerdictEvent {
  type: "verdict";
  status: "correct" | "wrong";
  hint_level: 1 | 2 | 3;
  message: string | null;
  review_mode: boolean;
  final_answer: string | null;
  learning_resources: LearningResource[];
  /** Set on review_mode=true paths where there's a canned hint but no streaming. */
  static_hint?: string;
}

export type StartSessionEvent = StartSessionCreatedEvent | ChunkEvent | DoneEvent | ErrorEvent;
export type AttemptEvent = VerdictEvent | ChunkEvent | DoneEvent | ErrorEvent;

export interface AttemptRequest {
  attempt_text: string;
}

export interface ClarifyRequest {
  message: string;
}

export interface ClarifyResponse {
  clarification: string;
  /** Clarifications left at the current hint level. */
  remaining: number;
}

export interface LearningResource {
  title: string;
  url?: string;
  summary?: string;
}

export interface AttemptResponse {
  status: "correct" | "wrong";
  hint?: string;
  hint_level?: 1 | 2 | 3;
  message?: string;
  final_answer?: string;
  review_mode?: boolean;
  review_url?: string;
  learning_resources?: LearningResource[];
}

export interface SubjectProgress {
  name: string;
  sessions: number;
  success_rate: number;
}

export interface RecentSession {
  id: string;
  question: string;
  subject: string;
  resolved: boolean;
  started_at: string;
}

export interface StudentProgress {
  subjects: SubjectProgress[];
  recent_sessions: RecentSession[];
}

export type AlertSeverity = "low" | "medium" | "high";
export type AlertReasonKind = "distress" | "repeated_failure" | "inactivity" | "legacy";
export type NotificationStatus = "pending" | "sent" | "failed";

export interface TeacherAlert {
  id: string;
  session_id: string;
  student_name: string;
  question: string;
  hint_level: number;
  fails_at_level: number;
  severity: AlertSeverity;
  reason_kind: AlertReasonKind;
  reason_text: string;
  notification_status: NotificationStatus;
  created_at: string;
  acknowledged_at: string | null;
  acknowledged_by_name: string | null;
}

export interface TeacherAlertsResponse {
  pending_alerts: TeacherAlert[];
}

export interface TeacherMetricsResponse {
  total_students: number;
  sessions_today: number;
  average_success_rate: number;
  pending_alerts: number;
}

export interface HistorySession {
  id: string;
  question: string;
  subject: string;
  resolved: boolean;
  started_at: string;
  resolved_at: string | null;
}

export interface StudentSessionHistoryResponse {
  sessions: HistorySession[];
  total: number;
  limit: number;
  offset: number;
}

export type ConceptStatus = "mastered" | "partial" | "struggling";

export interface ConceptMemoryItem {
  concept: string;
  label: string;
  subject: string;
  status: ConceptStatus;
  confidence: number;
  decayed_confidence: number;
  last_seen: string;
  attempts: number;
  correct: number;
}

export interface StudentMemoryResponse {
  concepts: ConceptMemoryItem[];
  generated_at: string;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem("studyowl_token");
  const isAuthRequest = path.startsWith("/api/") && !path.startsWith("/api/auth");

  if (isAuthRequest && !token) {
    throw new Error("Authentication token missing. Please log in again.");
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...((init?.headers as Record<string, string>) ?? {}),
  };

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    headers,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "API error");
  }
  return res.json() as Promise<T>;
}

/**
 * Stream NDJSON lines from a fetch Response. Lines are decoded incrementally
 * and parsed as JSON. Tolerates split chunks (buffers until the next newline).
 * Yields each parsed object.
 *
 * Caller is responsible for aborting via the AbortController passed to fetch.
 */
async function* streamNDJSON<T>(res: Response): AsyncIterable<T> {
  if (!res.body) throw new Error("Response has no body to stream");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const nl = buf.indexOf("\n");
        if (nl < 0) break;
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          yield JSON.parse(line) as T;
        } catch {
          // Skip malformed lines rather than tearing down the whole stream.
          continue;
        }
      }
    }
    // Flush any trailing line (no newline at EOF).
    const trailing = (buf + decoder.decode()).trim();
    if (trailing) {
      try { yield JSON.parse(trailing) as T; } catch { /* ignore */ }
    }
  } finally {
    try { reader.releaseLock(); } catch { /* ignore */ }
  }
}

/**
 * Open a streaming POST request and return an AsyncIterable of parsed events.
 * The caller passes an AbortSignal to cancel mid-stream.
 */
async function streamingPost<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<AsyncIterable<T>> {
  const token = localStorage.getItem("studyowl_token");
  if (!token) throw new Error("Authentication token missing. Please log in again.");

  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      Accept: "application/x-ndjson",
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    // Errors from the auth/validation layer are still JSON, not NDJSON.
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "API error");
  }
  return streamNDJSON<T>(res);
}

// ── API calls ────────────────────────────────────────────────────────────────

export const api = {
  /**
   * Sign up a new account. The caller (AuthContext) is responsible for
   * persisting the token; this function intentionally does not touch
   * localStorage.
   */
  signup: (body: SignUpRequest): Promise<TokenResponse> =>
    apiFetch<TokenResponse>("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Log in. The caller (AuthContext) is responsible for persisting the token;
   * this function intentionally does not touch localStorage.
   */
  login: (body: LoginRequest): Promise<TokenResponse> =>
    apiFetch<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Fetch students list for teachers. */
  getStudentList: () =>
    apiFetch<{ students: Array<{ id: string; name: string; grade_level: string }> }>(
      "/api/student/list"
    ),

  /** Fetch a specific student's progress. */
  getStudentProgress: (studentId: string) =>
    apiFetch<StudentProgress>(`/api/student/${studentId}/progress`),

  /** Fetch a student's knowledge-graph memory (concepts + decayed confidence). */
  getStudentMemory: (studentId: string, subject?: string) =>
    apiFetch<StudentMemoryResponse>(
      `/api/student/${studentId}/memory${subject ? `?subject=${encodeURIComponent(subject)}` : ""}`,
    ),

  /** Log out by removing token. */
  logout: () => {
    localStorage.removeItem("studyowl_token");
  },

  /**
   * Start a new homework session — returns an NDJSON stream of events.
   * See StartSessionEvent for the event shapes.
   */
  startSessionStream: (body: StartSessionRequest, signal?: AbortSignal) =>
    streamingPost<StartSessionEvent>("/api/session/start", body, signal),

  /**
   * Non-streaming `startSession` retained for callers that haven't migrated
   * to the streaming API yet. Hits the same endpoint; backend negotiates by
   * the Accept header.
   */
  startSession: (body: StartSessionRequest) =>
    apiFetch<StartSessionResponse>("/api/session/start", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Submit a student's answer attempt — returns an NDJSON stream of events.
   * See AttemptEvent for the event shapes.
   */
  submitAttemptStream: (sessionId: string, body: AttemptRequest, signal?: AbortSignal) =>
    streamingPost<AttemptEvent>(`/api/session/${sessionId}/attempt`, body, signal),

  /** Ask a clarifying question about the current hint. Does NOT advance the hint level. */
  clarify: (sessionId: string, body: ClarifyRequest) =>
    apiFetch<ClarifyResponse>(`/api/session/${sessionId}/clarify`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Fetch a student's progress data for the dashboard. */
  getProgress: (studentId: string) =>
    apiFetch<StudentProgress>(`/api/student/${studentId}/progress`),

  /** Fetch a paginated history of a student's past sessions (full untruncated questions). */
  getStudentSessions: (
    studentId: string,
    opts?: { limit?: number; offset?: number; signal?: AbortSignal },
  ) => {
    const params = new URLSearchParams();
    if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
    if (opts?.offset !== undefined) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return apiFetch<StudentSessionHistoryResponse>(
      `/api/student/${studentId}/sessions${qs ? `?${qs}` : ""}`,
      { signal: opts?.signal },
    );
  },

  /** Fetch teacher classroom analytics and alert metrics. */
  getTeacherMetrics: (opts?: { signal?: AbortSignal }) =>
    apiFetch<TeacherMetricsResponse>("/api/alert/metrics", { signal: opts?.signal }),

  /** Fetch current unresolved alerts for teachers. */
  getAlerts: (opts?: { signal?: AbortSignal }) =>
    apiFetch<TeacherAlertsResponse>("/api/alert", { signal: opts?.signal }),

  /** Mark an alert as acknowledged by the current teacher. Idempotent. */
  acknowledgeAlert: (alertId: string) =>
    apiFetch<TeacherAlert>(`/api/alert/${alertId}/acknowledge`, {
      method: "POST",
    }),

  /** Mark an alert resolved (and implicitly acknowledged if not already). */
  resolveAlert: (alertId: string) =>
    apiFetch<TeacherAlert>(`/api/alert/${alertId}/resolve`, {
      method: "POST",
    }),
};

