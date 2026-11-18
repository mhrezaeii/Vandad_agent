/**
 * Thin fetch wrapper for the Vandad Agent API.
 *
 * - Sends the application bearer token and optional company scope.
 * - Normalizes the backend error envelope into a typed error.
 * - Never logs or stores provider credentials; only the user token is kept.
 */

export type ApiErrorDetail = {
  field?: string;
  message?: string;
  loc?: string[];
  [key: string]: unknown;
};

type ApiErrorBody = {
  error?: { code?: string; message?: string; details?: ApiErrorDetail[] };
  detail?: string | { msg?: string }[] | ApiErrorDetail[];
};

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ApiErrorDetail[];

  constructor(
    message: string,
    status: number,
    code = "request_failed",
    details: ApiErrorDetail[] = [],
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const TOKEN_KEY = "vandad.token";
export const USER_KEY = "vandad.user";
export const LOCALE_KEY = "vandad.locale";
export const COMPANY_KEY = "vandad.companyId";

const apiBase = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1"
).replace(/\/$/, "");

export const apiUrl = apiBase;

export function readToken(): string | null {
  if (typeof window === "undefined") return null;
  const token = window.localStorage.getItem(TOKEN_KEY);
  return token && token.trim() ? token : null;
}

export function readCompanyId(): string | null {
  if (typeof window === "undefined") {
    return process.env.NEXT_PUBLIC_COMPANY_ID || null;
  }
  return (
    window.localStorage.getItem(COMPANY_KEY) ||
    process.env.NEXT_PUBLIC_COMPANY_ID ||
    null
  );
}

function buildHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const token = readToken();
  if (token) {
    // Authenticated sessions resolve the company from the account on the
    // server; sending an explicit tenant header would be rejected as
    // cross-tenant access.
    headers.set("Authorization", `Bearer ${token}`);
  } else {
    const companyId = readCompanyId();
    if (companyId) headers.set("X-Company-ID", companyId);
  }
  return headers;
}

async function toApiError(response: Response): Promise<ApiRequestError> {
  let code = `http_${response.status}`;
  let message = `Request failed (${response.status}).`;
  let details: ApiErrorDetail[] = [];
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body?.error) {
      code = body.error.code || code;
      message = body.error.message || message;
      details = Array.isArray(body.error.details) ? body.error.details : [];
    } else if (typeof body?.detail === "string") {
      message = body.detail;
    } else if (Array.isArray(body?.detail)) {
      details = body.detail as ApiErrorDetail[];
      message = details[0]?.message || message;
    }
  } catch {
    // Non-JSON error body: keep the status-derived message.
  }
  return new ApiRequestError(message, response.status, code, details);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(
    `${apiBase}${path.startsWith("/") ? path : `/${path}`}`,
    {
      ...init,
      headers: buildHeaders(init),
      cache: "no-store",
      credentials: "include",
    },
  );
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Fetch a binary response (authorized video stream) as a Blob. */
export async function apiBlob(path: string): Promise<Blob> {
  const response = await fetch(
    `${apiBase}${path.startsWith("/") ? path : `/${path}`}`,
    {
      headers: buildHeaders({}),
      cache: "no-store",
      credentials: "include",
    },
  );
  if (!response.ok) throw await toApiError(response);
  return response.blob();
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) return error.message;
  if (error instanceof Error) return error.message;
  return "Unexpected error.";
}
