/**
 * Shared fetch helpers, error type, and API base for the client.
 *
 * Every module under `./` routes through `request` / `requestVoid` so
 * correlation-id headers, JSON content-type handling, and error mapping
 * stay in one place.
 */
export const API_BASE = "/api/v1";

export class ApiClientError extends Error {
  readonly status: number;
  readonly correlationId?: string;
  readonly details?: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    correlationId?: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.status = status;
    this.correlationId = correlationId;
    this.details = details;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const correlationId = crypto.randomUUID();
  const isMultipart = init.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(isMultipart ? {} : { "Content-Type": "application/json" }),
      "X-Correlation-Id": correlationId,
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let body: {
      code?: string;
      message?: string;
      details?: Record<string, unknown>;
    } = {};
    try {
      body = await response.json();
    } catch {
      // ignore
    }
    throw new ApiClientError(
      body.message ?? `request failed: ${response.status}`,
      response.status,
      body.code,
      body.details,
    );
  }
  return (await response.json()) as T;
}

/**
 * Send a mutating request that may answer 204 No Content.
 *
 * The shared ``request`` helper always parses JSON, which throws on an
 * empty 204 body. Annotation DELETE (and several admin DELETEs) answer
 * 204, so this helper resolves void instead of parsing.
 */
async function requestVoid(path: string, init: RequestInit = {}): Promise<void> {
  const correlationId = crypto.randomUUID();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Correlation-Id": correlationId,
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let body: {
      code?: string;
      message?: string;
      details?: Record<string, unknown>;
    } = {};
    try {
      body = await response.json();
    } catch {
      // ignore
    }
    throw new ApiClientError(
      body.message ?? `request failed: ${response.status}`,
      response.status,
      body.code,
      body.details,
    );
  }
}

/**
 * Returns true for authorization/visibility denials that must render the
 * metadata-free forbidden state (never leak file names or reasons).
 */
export function isForbiddenLike(error: unknown): boolean {
  return (
    error instanceof ApiClientError && (error.status === 403 || error.status === 404)
  );
}

export { request, requestVoid };
