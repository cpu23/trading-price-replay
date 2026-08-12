const jsonHeaders = { "Content-Type": "application/json" };

export function apiErrorDetail(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (!body || typeof body !== "object") return fallback;

  const detail = "detail" in body ? body.detail : body;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item) {
          const location = "loc" in item && Array.isArray(item.loc)
            ? `${item.loc.filter((part: unknown) => part !== "body").join(" → ")}: `
            : "";
          return `${location}${String(item.msg)}`;
        }
        return "";
      })
      .filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  try {
    return JSON.stringify(detail) ?? fallback;
  } catch {
    return fallback;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text();
  let body: unknown;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    throw new Error(apiErrorDetail(body, response.statusText || `Request failed (${response.status})`));
  }
  return body as T;
}

function bodyInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: body === undefined ? undefined : jsonHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, bodyInit("POST", body)),
  patch: <T>(path: string, body: unknown) => request<T>(path, bodyInit("PATCH", body)),
  put: <T>(path: string, body: unknown) => request<T>(path, bodyInit("PUT", body)),
  delete: <T = void>(path: string) => request<T>(path, { method: "DELETE" }),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
