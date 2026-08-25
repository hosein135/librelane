import Link from "next/link";
import { ReactNode } from "react";

export function AppShell({
  title,
  username,
  children,
}: {
  title?: string;
  username?: string;
  children: ReactNode;
}) {
  return (
    <>
      <header className="site-header">
        <Link href="/" className="brand">
          LibreLane Web
        </Link>
        <span className="tagline">
          {title || "Colab notebook flow — sky130 SPM design"}
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: "0.75rem", alignItems: "center" }}>
          {username ? <span className="meta">{username}</span> : null}
          {username ? (
            <a href="/logout" className="btn">
              Sign out
            </a>
          ) : (
            <>
              <Link href="/login" className="btn">
                Sign in
              </Link>
              <Link href="/signup" className="btn primary">
                Sign up
              </Link>
            </>
          )}
        </span>
      </header>
      <main className="container">{children}</main>
    </>
  );
}
