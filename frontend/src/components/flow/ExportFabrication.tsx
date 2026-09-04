"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { apiFetch, type FabJobStatus, type FabTarget } from "@/lib/api";

function statusPath(runId: number, targetId: string): string {
  return `/api/runs/${runId}/fabrication/${targetId}/status`;
}

function buildPath(runId: number, targetId: string): string {
  return `/api/runs/${runId}/fabrication/${targetId}/build`;
}

function stopPath(runId: number, targetId: string): string {
  return `/api/runs/${runId}/fabrication/${targetId}/stop`;
}

export function ExportFabricationButton({
  runId,
  enabled,
  pdk,
  targets,
}: {
  runId: number;
  enabled: boolean;
  pdk?: string;
  targets: FabTarget[];
}) {
  const [open, setOpen] = useState(false);
  const [jobs, setJobs] = useState<Record<string, FabJobStatus>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [copiedLog, setCopiedLog] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState<string>("");
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const copyTimers = useRef<Record<string, number>>({});

  useEffect(() => {
    const node = dialogRef.current;
    if (!node) return;
    if (open) {
      if (!node.open) node.showModal();
    } else if (node.open) {
      node.close();
    }
  }, [open]);

  const refreshAll = useCallback(async () => {
    const ids = targets.map((t) => t.id).filter(Boolean);
    if (!ids.length) return;
    const entries = await Promise.all(
      ids.map(async (id) => {
        const res = await apiFetch<FabJobStatus>(statusPath(runId, id));
        return [id, res.ok ? res.data : { status: "idle", error: res.error }] as const;
      }),
    );
    setJobs(Object.fromEntries(entries));
  }, [runId, targets]);

  useEffect(() => {
    // Always hydrate statuses so a background build is visible after reopen/login.
    void refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    if (!open) return;
    void refreshAll();
  }, [open, refreshAll]);

  const anyRunning = Object.values(jobs).some(
    (job) => job?.status === "running" || job?.running,
  );

  useEffect(() => {
    // Keep polling even if the dialog is closed — the server job continues
    // independently of the browser session.
    if (!anyRunning) return;
    const timer = window.setInterval(() => {
      void refreshAll();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [anyRunning, refreshAll]);

  function progressPct(job?: FabJobStatus): number {
    if (!job) return 0;
    if (job.status === "done" || job.ready) return 100;
    const raw = Number(job.progress_pct);
    if (Number.isFinite(raw)) return Math.max(0, Math.min(100, Math.round(raw)));
    return job.running || job.status === "running" ? 2 : 0;
  }

  async function startBuild(target: FabTarget, force = false) {
    if (!target.id) return;
    setActionError("");
    setPending((prev) => ({ ...prev, [target.id]: true }));
    const res = await apiFetch<FabJobStatus>(buildPath(runId, target.id), {
      method: "POST",
      body: JSON.stringify({ force }),
    });
    setPending((prev) => ({ ...prev, [target.id]: false }));
    if (!res.ok) {
      setActionError(res.error || "Could not start the shuttle GDS build.");
      return;
    }
    setJobs((prev) => ({
      ...prev,
      [target.id]: {
        ...res.data,
        progress_pct: res.data.progress_pct ?? 2,
        progress_indeterminate: res.data.progress_indeterminate ?? true,
        progress_label: res.data.progress_label || "Starting…",
      },
    }));
  }

  async function stopBuild(target: FabTarget) {
    if (!target.id) return;
    setActionError("");
    setPending((prev) => ({ ...prev, [target.id]: true }));
    const res = await apiFetch<FabJobStatus>(stopPath(runId, target.id), {
      method: "POST",
      body: JSON.stringify({}),
    });
    setPending((prev) => ({ ...prev, [target.id]: false }));
    if (!res.ok) {
      setActionError(res.error || "Could not stop the shuttle GDS build.");
      void refreshAll();
      return;
    }
    setJobs((prev) => ({ ...prev, [target.id]: res.data }));
  }

  async function downloadZip(target: FabTarget) {
    if (!target.id) return;
    setActionError("");
    setPending((prev) => ({ ...prev, [target.id]: true }));
    try {
      const res = await fetch(`/runs/${runId}/fabrication/${target.id}.zip`, {
        credentials: "same-origin",
      });
      if (!res.ok) {
        let message = `Download failed (HTTP ${res.status}).`;
        try {
          const body = (await res.json()) as { error?: string };
          if (body?.error) message = body.error;
        } catch {
          /* not JSON */
        }
        setActionError(message);
        void refreshAll();
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const disp = res.headers.get("Content-Disposition") || "";
      const match = /filename="?([^"]+)"?/i.exec(disp);
      link.href = url;
      link.download = match?.[1] || `${target.id}-fabrication.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } finally {
      setPending((prev) => ({ ...prev, [target.id]: false }));
    }
  }

  async function copyLog(targetId: string, log: string) {
    if (!log) return;
    setActionError("");
    try {
      await navigator.clipboard.writeText(log);
      setCopiedLog((prev) => ({ ...prev, [targetId]: true }));
      if (copyTimers.current[targetId]) {
        window.clearTimeout(copyTimers.current[targetId]);
      }
      copyTimers.current[targetId] = window.setTimeout(() => {
        setCopiedLog((prev) => ({ ...prev, [targetId]: false }));
      }, 2000);
    } catch {
      setActionError("Could not copy the build log to the clipboard.");
    }
  }

  useEffect(() => {
    return () => {
      for (const timer of Object.values(copyTimers.current)) {
        window.clearTimeout(timer);
      }
    };
  }, []);

  const list = targets.length
    ? targets
    : [
        {
          id: "",
          label: "No shuttle for this PDK",
          vendor: "",
          description: `No fabrication targets are configured for PDK ${pdk || "(unknown)"}.`,
          pdks: [],
          submit_url: "",
          docs_url: "",
        },
      ];

  return (
    <>
      <button
        type="button"
        className="btn"
        disabled={!enabled}
        title={
          enabled
            ? anyRunning
              ? "Shuttle GDS build running in the background — open for progress or Stop"
              : "Build the shuttle top GDS, then download a foundry zip"
            : "Available when the run completes"
        }
        onClick={() => enabled && setOpen(true)}
      >
        {anyRunning ? "Export to Fabrication (building…)" : "Export to Fabrication"}
      </button>
      <dialog
        ref={dialogRef}
        className="fab-dialog"
        aria-labelledby={titleId}
        onClose={() => setOpen(false)}
        onClick={(event) => {
          if (event.target === dialogRef.current) setOpen(false);
        }}
      >
        <div className="fab-dialog-panel">
          <header className="fab-dialog-head">
            <div>
              <p className="fab-dialog-kicker">Foundry package</p>
              <h2 id={titleId}>Export to Fabrication</h2>
              <p className="meta">
                Choose a shuttle for PDK <code>{pdk || "—"}</code>. First build the
                shuttle top GDS (LibreLane chip-level flow — usually 30–90 minutes;
                IHP is a short rename), then download the zip. Builds keep running
                if you close this dialog, leave the page, or log out — use Stop to
                cancel and delete generated files.
              </p>
            </div>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              Close
            </button>
          </header>
          {anyRunning ? (
            <p className="fab-bg-note">
              A shuttle GDS build is running in the background on the server.
            </p>
          ) : null}
          {actionError ? <pre className="error">{actionError}</pre> : null}
          <ul className="fab-target-list">
            {list.map((target) => {
              const job = target.id ? jobs[target.id] : undefined;
              const status = job?.status || "idle";
              const running = status === "running" || Boolean(job?.running);
              const ready = status === "done" || Boolean(job?.ready);
              const failed = status === "failed" || status === "stopped";
              const busy = Boolean(pending[target.id]) || running;
              return (
                <li key={target.id || target.label} className="fab-target-card">
                  <div className="fab-target-main">
                    <div className="fab-target-copy">
                      <p className="fab-target-vendor">{target.vendor || "Shuttle"}</p>
                      <h3>{target.label}</h3>
                      <p>{target.description}</p>
                      {target.eta ? (
                        <p className="fab-target-eta">Build time: {target.eta}</p>
                      ) : null}
                      {running ? (
                        <p className="fab-target-status">
                          {job?.message || job?.progress_label || "Building shuttle top GDS…"}
                        </p>
                      ) : null}
                      {ready ? (
                        <p className="fab-target-status ok">Shuttle top GDS is ready.</p>
                      ) : null}
                      {failed ? (
                        <p className="fab-target-status err">
                          {job?.error || job?.message || "Shuttle GDS build failed."}
                        </p>
                      ) : null}
                      {running || ready || (failed && progressPct(job) > 0) ? (
                        <div
                          className={
                            "fab-progress" +
                            (running ? " is-running" : "") +
                            (failed ? " is-failed" : "") +
                            (ready ? " is-done" : "")
                          }
                          role="progressbar"
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={
                            job?.progress_indeterminate && running
                              ? undefined
                              : progressPct(job)
                          }
                          aria-valuetext={
                            job?.progress_label ||
                            job?.message ||
                            (ready ? "Complete" : running ? "Building" : undefined)
                          }
                          aria-label="Shuttle GDS build progress"
                        >
                          <div className="fab-progress-track">
                            <div
                              className={
                                "fab-progress-fill" +
                                (job?.progress_indeterminate && running
                                  ? " is-indeterminate"
                                  : "")
                              }
                              style={
                                job?.progress_indeterminate && running
                                  ? undefined
                                  : { width: `${progressPct(job)}%` }
                              }
                            />
                          </div>
                          <div className="fab-progress-meta">
                            <span>
                              {ready
                                ? "Complete"
                                : job?.progress_step != null && job?.progress_total
                                  ? `Step ${job.progress_step}/${job.progress_total}`
                                  : running
                                    ? "In progress"
                                    : status === "stopped"
                                      ? "Stopped"
                                      : failed
                                        ? "Failed"
                                        : ""}
                            </span>
                            <span>{progressPct(job)}%</span>
                          </div>
                        </div>
                      ) : null}
                      {target.docs_url ? (
                        <a href={target.docs_url} target="_blank" rel="noreferrer">
                          Submission docs
                        </a>
                      ) : null}
                    </div>
                    <div className="fab-target-actions">
                      {!target.id ? (
                        <span className="btn" aria-disabled>
                          Unavailable
                        </span>
                      ) : running ? (
                        <>
                          <button type="button" className="btn primary" disabled>
                            Building…
                          </button>
                          <button
                            type="button"
                            className="btn fab-stop-btn"
                            disabled={Boolean(pending[target.id])}
                            onClick={() => void stopBuild(target)}
                          >
                            {pending[target.id] ? "Stopping…" : "Stop"}
                          </button>
                        </>
                      ) : ready ? (
                        <>
                          <button
                            type="button"
                            className="btn primary"
                            disabled={busy}
                            onClick={() => void downloadZip(target)}
                          >
                            Download zip
                          </button>
                          <button
                            type="button"
                            className="btn"
                            disabled={busy}
                            onClick={() => void startBuild(target, true)}
                          >
                            Rebuild GDS
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          className="btn primary"
                          disabled={busy}
                          onClick={() => void startBuild(target, failed)}
                        >
                          {failed ? "Retry GDS build" : "Build shuttle GDS"}
                        </button>
                      )}
                    </div>
                  </div>
                  {job?.errors && job.errors.length ? (
                    <pre className="error fab-job-errors">
                      {job.errors.join("\n")}
                    </pre>
                  ) : null}
                  {job?.log ? (
                    <details className="fab-job-log" open={failed || running}>
                      <summary className="fab-job-log-head">
                        <span>
                          {failed
                            ? "Build log"
                            : running
                              ? "Build log (live)"
                              : "Build log"}
                        </span>
                        <button
                          type="button"
                          className="btn fab-copy-log"
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            void copyLog(target.id, job.log || "");
                          }}
                        >
                          {copiedLog[target.id] ? "Copied" : "Copy log"}
                        </button>
                      </summary>
                      <pre
                        className="log"
                        ref={(node) => {
                          if (node && (running || failed)) {
                            node.scrollTop = node.scrollHeight;
                          }
                        }}
                      >
                        {job.log}
                      </pre>
                    </details>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      </dialog>
    </>
  );
}
