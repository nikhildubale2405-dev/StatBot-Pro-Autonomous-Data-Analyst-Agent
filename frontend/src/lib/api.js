const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL;
export const API_BASE = configuredBaseUrl ?? (import.meta.env.DEV ? "http://localhost:8000" : "");

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.detail || "Request failed";
    throw new Error(message);
  }
  return payload;
}

export async function uploadDataset(file, sessionId) {
  const form = new FormData();
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  const response = await fetch(`${API_BASE}/upload`, { method: "POST", body: form });
  return parseResponse(response);
}

export async function askQuestion({ sessionId, fileId, question }) {
  const response = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, file_id: fileId, question })
  });
  return parseResponse(response);
}

export function chartUrl(relativeUrl) {
  return relativeUrl.startsWith("http") ? relativeUrl : `${API_BASE}${relativeUrl}`;
}
