import { useMemo, useState } from "react";
import { AlertCircle, ShieldCheck } from "lucide-react";
import ChatPanel from "./components/ChatPanel.jsx";
import ProfilePanel from "./components/ProfilePanel.jsx";
import UploadPanel from "./components/UploadPanel.jsx";
import { askQuestion, uploadDataset } from "./lib/api.js";

export default function App() {
  const [sessionId, setSessionId] = useState(null);
  const [fileId, setFileId] = useState(null);
  const [fileName, setFileName] = useState("");
  const [profile, setProfile] = useState(null);
  const [messages, setMessages] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");

  const disabled = useMemo(() => !sessionId || !fileId, [sessionId, fileId]);

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

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>StatBot Pro</h1>
          <p>Autonomous CSV Data Analyst Agent</p>
        </div>
        <div className="security-note">
          <ShieldCheck size={17} />
          <span>Docker sandboxed execution</span>
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
