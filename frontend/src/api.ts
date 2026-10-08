export type Row = Record<string, any>;
let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export async function api<T = any>(
  path: string,
  method = "GET",
  body?: unknown,
  key?: string,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
    headers["Idempotency-Key"] = key || crypto.randomUUID();
  }
  if (body && !(body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body:
      body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/login")
      window.dispatchEvent(new Event("session-expired"));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || data),
    );
  }
  return data;
}
export const num = (value: unknown, digits = 2) =>
  typeof value === "number"
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits })
    : "—";
export const labels: Record<string, string> = {
  LONG: "多",
  SHORT: "空",
  OPEN: "开仓",
  CLOSE: "平仓",
  CLOSETODAY: "平今",
  CLOSEYESTERDAY: "平昨",
  SUBMITTING: "提交中",
  NOTTRADED: "未成交",
  PARTTRADED: "部分成交",
  ALLTRADED: "全部成交",
  CANCELLED: "已撤单",
  REJECTED: "拒单",
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  pending: "待处理",
  submitted: "已提交",
  waiting_cancel: "等待撤单",
  unknown: "结果未知",
};
