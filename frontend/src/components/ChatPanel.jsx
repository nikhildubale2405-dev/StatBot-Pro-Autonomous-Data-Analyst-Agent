import { Bot, Loader2, Send, User } from "lucide-react";
import { ChartResult, TableResult } from "./Results.jsx";

export default function ChatPanel({ messages, onAsk, disabled, loading }) {
  const examples = ["Show top 10 products by revenue", "Plot monthly sales trend", "Summarize missing values and numeric columns"];

  function handleSubmit(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const question = String(form.get("question") || "").trim();
    if (!question) return;
    onAsk(question);
    event.currentTarget.reset();
  }

  return (
    <section className="panel chat-panel">
      <div className="panel-title">
        <Bot size={18} />
        <span>Analysis Chat</span>
      </div>
      <div className="messages">
        {messages.length === 0 ? (
          <div className="empty-chat">
            <Bot size={28} />
            <p>Ask a question after uploading a dataset.</p>
            <div className="prompt-row">
              {examples.map((example) => (
                <button type="button" key={example} disabled={disabled || loading} onClick={() => onAsk(example)}>
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="avatar">{message.role === "user" ? <User size={16} /> : <Bot size={16} />}</div>
              <div className="bubble">
                <p>{message.content}</p>
                {(message.tables || []).map((table, index) => <TableResult table={table} key={`${message.id}-table-${index}`} />)}
                {(message.charts || []).map((chart) => <ChartResult chart={chart} key={chart.id || chart.url} />)}
              </div>
            </article>
          ))
        )}
        {loading ? (
          <article className="message assistant">
            <div className="avatar"><Bot size={16} /></div>
            <div className="bubble loading-bubble"><Loader2 className="spin" size={16} /> Running sandboxed analysis...</div>
          </article>
        ) : null}
      </div>
      <form className="chat-form" onSubmit={handleSubmit}>
        <input name="question" disabled={disabled || loading} placeholder={disabled ? "Upload a dataset first" : "Ask a question about your data"} />
        <button type="submit" disabled={disabled || loading} title="Send question">
          {loading ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
        </button>
      </form>
    </section>
  );
}
