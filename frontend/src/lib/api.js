const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL;
export const API_BASE = configuredBaseUrl ?? (import.meta.env.DEV ? "http://localhost:8000" : "");

let accessToken = "";

export function setAccessToken(token) {
  accessToken = token || "";
}

function authHeaders(headers = {}) {
  return accessToken ? { ...headers, Authorization: `Bearer ${accessToken}` } : headers;
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.detail || "Request failed";
    throw new Error(message);
  }
  return payload;
}

export async function signup({ name, email, password }) {
  const response = await fetch(`${API_BASE}/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, email, password })
  });
  return parseResponse(response);
}

export async function login({ email, password }) {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password })
  });
  return parseResponse(response);
}

export async function getCurrentUser() {
  const response = await fetch(`${API_BASE}/auth/me`, { headers: authHeaders() });
  return parseResponse(response);
}

export async function uploadDataset(file, sessionId) {
  const form = new FormData();
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  const response = await fetch(`${API_BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  return parseResponse(response);
}

export async function askQuestion({ sessionId, fileId, question }) {
  const response = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ session_id: sessionId, file_id: fileId, question })
  });
  return parseResponse(response);
}

export function chartUrl(relativeUrl) {
  return relativeUrl.startsWith("http") ? relativeUrl : `${API_BASE}${relativeUrl}`;
}

export async function fetchChartBlob(relativeUrl) {
  const response = await fetch(chartUrl(relativeUrl), { headers: authHeaders() });
  if (!response.ok) {
    await parseResponse(response);
  }
  return response.blob();
}
