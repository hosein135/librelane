import Link from "next/link";
import { ReactNode } from "react";

export function AppShell({
  title,
  username,
  wide,
  children,
}: {
  title?: string;
  username?: string;
  wide?: boolean;
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
        <span className="header-actions">
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
      <main className={wide ? "container container-wide" : "container"}>{children}</main>
    </>
  );
}
