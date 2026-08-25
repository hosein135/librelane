"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { AppShell } from "@/components/layout/AppShell";

export function SignupForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/backend/signup", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          username,
          password,
          email,
          first_name: firstName.trim(),
          last_name: lastName.trim(),
        }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setError(data.error || "Signup failed.");
        return;
      }
      window.location.assign("/login?reason=signup_ok");
    } catch {
      setError("Could not reach the server. Check that the backend is running.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AppShell title="Create account">
      <section className="panel auth-card">
        <h1>Create account</h1>
        <form onSubmit={onSubmit}>
          <div className="form-grid">
            <label>
              First name
              <input value={firstName} onChange={(ev) => setFirstName(ev.target.value)} />
            </label>
            <label>
              Last name
              <input value={lastName} onChange={(ev) => setLastName(ev.target.value)} />
            </label>
            <label>
              Username
              <input
                value={username}
                onChange={(ev) => setUsername(ev.target.value)}
                required
                autoComplete="username"
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={password}
                onChange={(ev) => setPassword(ev.target.value)}
                required
                autoComplete="new-password"
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(ev) => setEmail(ev.target.value)}
                required
                autoComplete="email"
              />
            </label>
          </div>
          {error ? <pre className="error">{error}</pre> : null}
          <button type="submit" className="btn primary" disabled={submitting}>
            {submitting ? "Creating…" : "Sign up"}
          </button>
        </form>
        <p className="meta" style={{ marginTop: "1rem" }}>
          <Link href="/login">Already have an account? Sign in</Link>
        </p>
      </section>
    </AppShell>
  );
}
