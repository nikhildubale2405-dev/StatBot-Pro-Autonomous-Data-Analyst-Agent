import { useState } from "react";
import { Loader2, LogIn, UserPlus } from "lucide-react";

export default function AuthPanel({ onLogin, onSignup, loading }) {
  const [mode, setMode] = useState("login");
  const isSignup = mode === "signup";

  function handleSubmit(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      name: String(form.get("name") || "").trim(),
      email: String(form.get("email") || "").trim(),
      password: String(form.get("password") || "")
    };
    if (isSignup) {
      onSignup(payload);
    } else {
      onLogin({ email: payload.email, password: payload.password });
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel">
        <div className="auth-copy">
          <span className="auth-kicker">StatBot Pro</span>
          <h1>{isSignup ? "Create your workspace" : "Welcome back"}</h1>
          <p>Sign in to keep your datasets, analysis sessions, tables, and generated charts private to your account.</p>
        </div>

        <div className="auth-card">
          <div className="auth-tabs" role="tablist" aria-label="Authentication mode">
            <button type="button" className={!isSignup ? "active" : ""} onClick={() => setMode("login")}>
              <LogIn size={16} />
              <span>Login</span>
            </button>
            <button type="button" className={isSignup ? "active" : ""} onClick={() => setMode("signup")}>
              <UserPlus size={16} />
              <span>Sign up</span>
            </button>
          </div>

          <form className="auth-form" onSubmit={handleSubmit}>
            {isSignup ? (
              <label>
                <span>Name</span>
                <input name="name" type="text" autoComplete="name" required minLength={1} maxLength={120} />
              </label>
            ) : null}
            <label>
              <span>Email</span>
              <input name="email" type="email" autoComplete="email" required />
            </label>
            <label>
              <span>Password</span>
              <input name="password" type="password" autoComplete={isSignup ? "new-password" : "current-password"} required minLength={isSignup ? 8 : 1} />
            </label>
            <button type="submit" disabled={loading}>
              {loading ? <Loader2 className="spin" size={18} /> : isSignup ? <UserPlus size={18} /> : <LogIn size={18} />}
              <span>{isSignup ? "Create account" : "Login"}</span>
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}
