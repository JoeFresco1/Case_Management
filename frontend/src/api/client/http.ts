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
  readonly code?: string;
  readonly details?: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    correlationId?: string,
    details?: Record<string, unknown>,
    code?: string,
  ) {
    super(message);
    this.status = status;
    this.correlationId = correlationId;
    this.details = details;
    this.code = code;
  }
}

type ErrorPayload = {
  code?: string;
  message?: string;
  details?: Record<string, unknown> | null;
  correlation_id?: string;
};

function mutationMethod(method: string): boolean {
  return ["POST", "PATCH", "PUT", "DELETE"].includes(method.toUpperCase());
}

function shouldRequireIdempotencyKey(path: string, method: string): boolean {
  if (!mutationMethod(method)) {
    return false;
  }
  if (method.toUpperCase() !== "POST") {
    return true;
  }

  // These POSTs are queries or bearer-token authentication, not actor mutations.
  const routePath = path
    .split("?", 1)[0]
    .replace(/^\/api\/v1/, "")
    .replace(/\/$/, "");
  return !(
    routePath === "/search" ||
    routePath === "/search/semantic" ||
    /^\/search\/saved\/[^/]+\/run$/.test(routePath) ||
    /^\/admin\/workflows\/[^/]+\/versions\/[^/]+\/simulate$/.test(routePath) ||
    routePath === "/bundles/links/redeem"
  );
}

function requestHeaders(path: string, init: RequestInit): Headers {
  const isMultipart = init.body instanceof FormData;
  const method = init.method ?? "GET";
  const headers = new Headers({
    ...(isMultipart ? {} : { "Content-Type": "application/json" }),
    "X-Correlation-Id": crypto.randomUUID(),
    ...(shouldRequireIdempotencyKey(path, method)
      ? { "Idempotency-Key": crypto.randomUUID() }
      : {}),
  });
  new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  return headers;
}

async function readErrorPayload(response: Response): Promise<ErrorPayload> {
  try {
    return (await response.json()) as ErrorPayload;
  } catch {
    return {};
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: requestHeaders(path, init),
  });
  if (!response.ok) {
    const body = await readErrorPayload(response);
    throw new ApiClientError(
      body.message ?? `request failed: ${response.status}`,
      response.status,
      body.correlation_id,
      body.details ?? undefined,
      body.code,
    );
  }
  if (response.status === 204) {
    return undefined as T;
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
async function requestVoid(
  path: string,
  init: RequestInit = {},
): Promise<void> {
  await request<unknown>(path, init);
}

/**
 * Returns true for authorization/visibility denials that must render the
 * metadata-free forbidden state (never leak file names or reasons).
 */
export function isForbiddenLike(error: unknown): boolean {
  return (
    error instanceof ApiClientError &&
    (error.status === 403 || error.status === 404)
  );
}

export { request, requestVoid };
