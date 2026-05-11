import { useEffect, useMemo, useState } from "react";
import { AlertCircle, LogOut, ShieldCheck, UserCircle } from "lucide-react";
import AuthPanel from "./components/AuthPanel.jsx";
import ChatPanel from "./components/ChatPanel.jsx";
import ProfilePanel from "./components/ProfilePanel.jsx";
import UploadPanel from "./components/UploadPanel.jsx";
import { askQuestion, getCurrentUser, login, setAccessToken, signup, uploadDataset } from "./lib/api.js";

const AUTH_STORAGE_KEY = "statbot.auth";

function readStoredAuth() {
  try {
    const stored = localStorage.getItem(AUTH_STORAGE_KEY);
    return stored ? JSON.parse(stored) : null;
  } catch {
    return null;
  }
}

export default function App() {
  const [auth, setAuth] = useState(() => {
    const stored = readStoredAuth();
    setAccessToken(stored?.accessToken || "");
    return stored;
  });
  const [sessionId, setSessionId] = useState(null);
  const [fileId, setFileId] = useState(null);
  const [fileName, setFileName] = useState("");
  const [profile, setProfile] = useState(null);
  const [messages, setMessages] = useState([]);
  const [authLoading, setAuthLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");

  const disabled = useMemo(() => !sessionId || !fileId, [sessionId, fileId]);

  useEffect(() => {
    if (!auth?.accessToken) return;
    let cancelled = false;
    getCurrentUser()
      .then((user) => {
        if (!cancelled) {
          const nextAuth = { accessToken: auth.accessToken, user };
          setAuth(nextAuth);
          localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(nextAuth));
        }
      })
      .catch(() => {
        if (!cancelled) handleLogout("Your session expired. Please log in again.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function applyAuth(response) {
    const nextAuth = { accessToken: response.access_token, user: response.user };
    setAccessToken(response.access_token);
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(nextAuth));
    setAuth(nextAuth);
    setError("");
  }

  async function handleLogin(credentials) {
    setAuthLoading(true);
    setError("");
    try {
      applyAuth(await login(credentials));
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthLoading(false);
    }
  }

  async function handleSignup(credentials) {
    setAuthLoading(true);
    setError("");
    try {
      applyAuth(await signup(credentials));
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthLoading(false);
    }
  }

  function clearWorkspace() {
    setSessionId(null);
    setFileId(null);
    setFileName("");
    setProfile(null);
    setMessages([]);
    setUploading(false);
    setAsking(false);
  }

  function handleLogout(message = "") {
    setAccessToken("");
    localStorage.removeItem(AUTH_STORAGE_KEY);
    setAuth(null);
    clearWorkspace();
    setError(message);
  }

  async function handleUpload(file) {
    setUploading(true);
    setError("");
    try {
      const response = await uploadDataset(file, sessionId);
      setSessionId(response.session_id);
      setFileId(response.file_id);
      setFileName(file.name);
      setProfile(response.profile);
      setMessages([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  }

  async function handleAsk(question) {
    if (!sessionId || !fileId) return;
    const userMessage = { id: crypto.randomUUID(), role: "user", content: question, tables: [], charts: [] };
    setMessages((current) => [...current, userMessage]);
    setAsking(true);
    setError("");
    try {
      const response = await askQuestion({ sessionId, fileId, question });
      setMessages((current) => [
        ...current,
        {
          id: response.message_id,
          role: "assistant",
          content: response.answer,
          tables: response.tables,
          charts: response.charts
        }
      ]);
    } catch (err) {
      setError(err.message);
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "assistant", content: `Analysis failed: ${err.message}`, tables: [], charts: [] }
      ]);
    } finally {
      setAsking(false);
    }
  }

  if (!auth) {
    return (
      <>
        {error ? (
          <div className="auth-alert">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        ) : null}
        <AuthPanel onLogin={handleLogin} onSignup={handleSignup} loading={authLoading} />
      </>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>StatBot Pro</h1>
          <p>Autonomous CSV Data Analyst Agent</p>
        </div>
        <div className="topbar-actions">
          <div className="account-note">
            <UserCircle size={17} />
            <span>{auth.user.name}</span>
          </div>
          <div className="security-note">
            <ShieldCheck size={17} />
            <span>Docker sandboxed execution</span>
          </div>
          <button type="button" className="logout-button" onClick={() => handleLogout()} title="Logout">
            <LogOut size={17} />
          </button>
        </div>
      </header>

      {error ? (
        <div className="alert">
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="workspace">
        <aside className="sidebar">
          <UploadPanel fileName={fileName} onUpload={handleUpload} loading={uploading} error={null} />
          <ProfilePanel profile={profile} />
        </aside>
        <ChatPanel messages={messages} onAsk={handleAsk} disabled={disabled} loading={asking} />
      </div>
    </main>
  );
}
