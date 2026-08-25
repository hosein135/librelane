"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { LOGIN_REASON_MESSAGES } from "@/lib/session";

const LOGIN_REASON_TITLES: Record<string, string> = {
  session_invalidated: "Signed out — another sign-in",
  expired: "Session expired",
  logged_out: "Signed out",
  bad_creds: "Sign in failed",
  signup_ok: "Account created",
};

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    router.prefetch("/");
  }, [router]);

  useEffect(() => {
    const reason = searchParams.get("reason") || "";
    if (!reason || reason === "required") {
      if (reason === "required") router.replace("/login");
      return;
    }
    const msg = LOGIN_REASON_MESSAGES[reason];
    if (msg) setNotice(msg);
    router.replace("/login");
  }, [searchParams, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/backend/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setError(data.error || "Sign-in failed.");
        return;
      }
      window.location.assign("/");
    } catch {
      setError("Could not reach the server. Check that the backend is running.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AppShell title="Sign in">
      <section className="panel auth-card">
        <h1>Sign in</h1>
        {notice ? <p className="meta">{notice}</p> : null}
        <form onSubmit={onSubmit}>
          <div className="form-grid">
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
                autoComplete="current-password"
              />
            </label>
          </div>
          {error ? <pre className="error">{error}</pre> : null}
          <button type="submit" className="btn primary" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="meta" style={{ marginTop: "1rem" }}>
          <Link href="/signup">Create an account</Link>
        </p>
      </section>
    </AppShell>
  );
}
