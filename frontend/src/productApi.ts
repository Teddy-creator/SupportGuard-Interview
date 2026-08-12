import { bearerHeaders } from "./auth";
import type { Role, SessionContext } from "./productTypes";

export class ProductApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

const PUBLIC_PROBLEM_CODES = new Set([
  "authentication_required",
  "forbidden",
  "internal_error",
  "invalid_request",
  "rate_limited",
  "request_rejected",
  "resource_not_found",
  "service_unavailable",
  "state_conflict",
]);

function productProblem(payload: Record<string, unknown>) {
  const code = payload.public_code;
  const message = payload.message;
  const retryable = payload.retryable;
  const requestId = payload.request_id;
  if (
    typeof code !== "string" ||
    !PUBLIC_PROBLEM_CODES.has(code) ||
    typeof message !== "string" ||
    !message.trim() ||
    message.length > 500 ||
    typeof retryable !== "boolean" ||
    typeof requestId !== "string" ||
    !/^[A-Za-z0-9_.:-]{1,128}$/.test(requestId)
  )
    return null;
  return { code, message, retryable, requestId };
}

function fallbackError(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录。";
  if (status === 403) return "当前身份无权执行此操作。";
  if (status === 404) return "未找到该资源，或你没有访问权限。";
  if (status === 409) return "状态已经发生变化，请刷新后重试。";
  if (status === 422) return "提交内容不符合要求，请检查后重试。";
  if (status === 429) return "请求过于频繁，请稍后再试。";
  if ([502, 503, 504].includes(status)) return "服务暂时不可用，请稍后重试。";
  return "请求未能完成，请稍后重试。";
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
  csrf?: string,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...bearerHeaders(),
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let payload: Record<string, unknown> = {};
    if (response.headers.get("content-type")?.includes("application/json")) {
      try {
        payload = (await response.json()) as Record<string, unknown>;
      } catch {
        payload = {};
      }
    }
    const problem = productProblem(payload);
    const headerRequestId = response.headers.get("X-Request-ID");
    const requestId =
      headerRequestId && /^[A-Za-z0-9_.:-]{1,128}$/.test(headerRequestId)
        ? headerRequestId
        : problem?.requestId;
    const message = problem?.message ?? fallbackError(response.status);
    throw new ProductApiError(
      requestId ? `${message}（请求编号：${requestId}）` : message,
      response.status,
      problem?.code ?? `http_${response.status}`,
      problem?.retryable === true ||
        [429, 502, 503, 504].includes(response.status),
    );
  }
  return response.json() as Promise<T>;
}

export function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "请求失败，请稍后重试。";
}

export function sessionBootstrapErrorMessage(value: unknown): string {
  if (value instanceof ProductApiError && value.status === 401)
    return "登录状态已失效，请重新登录。";
  return "无法建立安全会话，暂时无法确认当前身份和租户。请重新连接；如问题持续，请重新登录。";
}

export function isAbortError(value: unknown): boolean {
  return (
    (value instanceof DOMException && value.name === "AbortError") ||
    (value instanceof Error && value.name === "AbortError")
  );
}

export async function createDemoSession(
  role: Role,
  options: {
    tenantId?: string;
    subjectId?: string;
    customerId?: string | null;
  } = {},
): Promise<string> {
  const custom = options.tenantId && options.subjectId;
  const response = await api<{ csrf_token: string }>("/demo-sessions", {
    method: "POST",
    body: JSON.stringify({
      role,
      customer_id:
        options.customerId ?? (role === "customer" && !custom ? "cust_demo" : null),
      tenant_id: custom ? options.tenantId : null,
      external_subject: custom ? options.subjectId : null,
    }),
  });
  return response.csrf_token;
}

export async function bootstrapSession(
  preferredRole: Role = "customer",
): Promise<{ csrf: string; context: SessionContext }> {
  const health = await api<{ auth_mode: string }>("/health");
  try {
    const context = await api<SessionContext>("/session");
    return { csrf: context.csrf_token ?? "", context };
  } catch (value) {
    if (
      health.auth_mode !== "development" ||
      !(value instanceof ProductApiError) ||
      ![401, 403].includes(value.status)
    )
      throw value;
  }
  const csrf = await createDemoSession(preferredRole);
  return { csrf, context: await api<SessionContext>("/session") };
}
