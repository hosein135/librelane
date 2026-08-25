"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { apiFetch, type FlowRun, type FlowStep } from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  done: "Done",
  failed: "Failed",
  skipped: "Skipped",
};

function formatBytes(bytes: unknown) {
  if (bytes == null || Number.isNaN(Number(bytes))) return "";
  const n = Number(bytes);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMetricName(key: string) {
  return key.replaceAll("__", " · ").replaceAll("_", " ");
}

function hasOutputContent(output: Record<string, unknown> | undefined) {
  if (!output) return false;
  if (output.error) return true;
  return (
    output.elapsed_s != null ||
    (Array.isArray(output.views_updated) && output.views_updated.length > 0) ||
    (output.metrics && Object.keys(output.metrics as object).length > 0) ||
    Boolean(output.preview_html) ||
    Boolean(output.preview_svg) ||
    (Array.isArray(output.artifacts) && output.artifacts.length > 0)
  );
}

function isCompleteStatus(status: string) {
  return status === "done" || status === "skipped";
}

function StepOutput({
  runId,
  step,
}: {
  runId: number;
  step: FlowStep;
}) {
  const output = step.output || {};
  if (!hasOutputContent(output) && !step.summary) return null;
  if (output.error) {
    return <pre className="error">{String(output.error)}</pre>;
  }
  const metrics = (output.metrics || {}) as Record<string, unknown>;
  const artifacts = (output.artifacts || []) as Array<{ path: string; size?: number }>;
  return (
    <div className="step-output">
      {output.elapsed_s != null ? (
        <p className="output-line">
          <strong>Time elapsed:</strong> {String(output.elapsed_s)}s
        </p>
      ) : null}
      {output.preview_svg ? (
        <div className="output-block output-preview">
          <strong>Layout preview</strong>
          <div className="preview-svg-wrap">
            <object
              type="image/svg+xml"
              data={`/runs/${runId}/steps/${step.order}/preview.svg`}
              className="layout-preview-svg"
              aria-label="Layout preview"
            />
          </div>
        </div>
      ) : null}
      {Object.keys(metrics).length ? (
        <div className="output-block">
          <strong>Metrics</strong>
          <table className="output-metrics">
            <thead>
              <tr>
                <th>Metric</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(metrics).map((key) => (
                <tr key={key}>
                  <td>{formatMetricName(key)}</td>
                  <td>
                    <code>{String(metrics[key])}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {artifacts.length ? (
        <div className="output-block">
          <strong>Output files</strong>
          <ul className="output-artifacts">
            {artifacts.map((a) => (
              <li key={a.path}>
                <code>{a.path}</code>{" "}
                <span className="meta">({formatBytes(a.size)})</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {!hasOutputContent(output) && step.summary ? (
        <pre className="summary step-summary-fallback">{step.summary}</pre>
      ) : null}
    </div>
  );
}

function StepMarker({ status, index }: { status: string; index: number }) {
  if (status === "done") {
    return (
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden>
        <path
          d="M3.5 8.5 6.5 11.5 12.5 4.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (status === "failed") {
    return (
      <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden>
        <path
          d="M4 4l8 8M12 4l-8 8"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  if (status === "running") {
    return <span className="progress-spinner" aria-hidden />;
  }
  return <span>{index + 1}</span>;
}

export function RunDetailClient({
  username,
  runId,
  watch,
}: {
  username: string;
  runId: number;
  watch: boolean;
}) {
  const [run, setRun] = useState<FlowRun | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const followRunning = useRef(true);
  const activeItemRef = useRef<HTMLButtonElement | null>(null);

  const load = useCallback(async () => {
    const res = await apiFetch<FlowRun>(`/api/runs/${runId}`);
    if (!res.ok) {
      setError(res.error || "Failed to load run.");
      return null;
    }
    setRun(res.data);
    setError(null);
    return res.data;
  }, [runId]);

  const pollOnce = useCallback(async () => {
    const res = await apiFetch<FlowRun>(`/api/runs/${runId}/status`);
    if (!res.ok) return;
    setRun(res.data);
    const stillBusy =
      res.data.is_running ||
      res.data.status === "setting_up" ||
      res.data.status === "running";
    if (stillBusy) {
      const delay = res.data.status === "setting_up" ? 2000 : 4000;
      pollTimer.current = setTimeout(() => void pollOnce(), delay);
    } else {
      busyRef.current = false;
      pollTimer.current = null;
    }
  }, [runId]);

  useEffect(() => {
    void load().then((data) => {
      if (watch || data?.is_running) {
        void pollOnce();
      }
    });
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [load, pollOnce, watch]);

  const steps = useMemo(() => run?.steps || [], [run]);

  useEffect(() => {
    if (!steps.length) return;
    const running = steps.find((s) => s.status === "running");
    if (running && followRunning.current) {
      setTab(`step-${running.order}`);
      return;
    }
    if (tab != null) return;
    const active =
      steps.find((s) => s.status === "running") ||
      steps.find((s) => s.status === "failed") ||
      steps.find((s) => s.status === "pending") ||
      steps[0];
    setTab(`step-${active.order}`);
  }, [steps, tab]);

  useEffect(() => {
    activeItemRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [tab]);

  const isRunning = Boolean(
    run?.is_running || run?.status === "running" || run?.status === "setting_up",
  );

  async function startAction(path: string) {
    if (busyRef.current || isRunning) {
      setError("A run is already in progress. Wait until it finishes.");
      return;
    }
    busyRef.current = true;
    followRunning.current = true;
    setError(null);
    const res = await apiFetch(path, { method: "POST" });
    if (!res.ok) {
      busyRef.current = false;
      setError(res.error || "Action failed.");
      return;
    }
    await load();
    void pollOnce();
  }

  const activeTab = tab || (steps[0] ? `step-${steps[0].order}` : null);
  const selectedStep = steps.find((s) => `step-${s.order}` === activeTab) || steps[0];
  const selectedIndex = selectedStep
    ? steps.findIndex((s) => s.order === selectedStep.order)
    : -1;

  const doneCount = steps.filter((s) => isCompleteStatus(s.status)).length;
  const failedCount = steps.filter((s) => s.status === "failed").length;
  const progressPct = steps.length ? Math.round((doneCount / steps.length) * 100) : 0;

  function selectStep(order: number) {
    followRunning.current = false;
    setTab(`step-${order}`);
  }

  const designLabel = run ? run.top_module_name || run.design_name : "";
  const hasDownloads =
    Boolean(selectedStep?.can_download_zip) ||
    Boolean(selectedStep?.can_download_svg) ||
    Boolean(selectedStep?.can_download_preview_source);
  const hasResults =
    Boolean(selectedStep) &&
    (hasOutputContent(selectedStep.output) || Boolean(selectedStep.summary));

  return (
    <AppShell username={username} title={`Flow steps — Run #${runId}`} wide>
      <div className="flow-page">
        <section className="flow-toolbar">
          <div className="flow-toolbar-copy">
            <p className="meta">
              <Link href={`/?id=${runId}`}>← Back to overview</Link>
            </p>
            <div className="flow-toolbar-title-row">
              <h1>
                Run #{runId}
                {designLabel ? ` — ${designLabel}` : ""}
                {run?.name && run.name !== designLabel ? ` (${run.name})` : ""}
              </h1>
              <p className={`status-pill status-${run?.status || "pending"}`}>
                {(run?.status || "pending").replaceAll("_", " ")}
              </p>
            </div>
            {run?.error_message ? <pre className="error">{run.error_message}</pre> : null}
            {error ? <pre className="error">{error}</pre> : null}
            <p className="meta flow-toolbar-meta">
              {isRunning
                ? run?.status === "setting_up"
                  ? "Configuring flow for this run…"
                  : "Step running…"
                : run?.work_dir
                  ? `Workdir: ${run.work_dir}`
                  : run?.artifacts_stored
                    ? "Artifacts stored in Postgres (workdir may have been pruned)."
                    : "Select a step to inspect results, or run the full flow."}
              {(run?.disk_bytes != null || run?.db_bytes != null) && (
                <>
                  {" "}
                  · Disk {formatBytes(run.disk_bytes)}, DB {formatBytes(run.db_bytes)}
                </>
              )}
            </p>
          </div>
          <button
            type="button"
            className="btn primary btn-run-all"
            disabled={isRunning || !steps.length}
            onClick={() => void startAction(`/api/runs/${runId}/run-all`)}
          >
            {isRunning ? "Running…" : "Run all steps"}
          </button>
        </section>

        <div className="flow-workspace">
          <aside className="flow-progress" aria-label="Flow steps">
            <div className="flow-progress-head">
              <div className="flow-progress-head-row">
                <strong>Flow progress</strong>
                <span className="meta">
                  {doneCount}/{steps.length || "…"}
                </span>
              </div>
              <div
                className="flow-meter"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progressPct}
                aria-label="Flow completion"
              >
                <div
                  className={`flow-meter-fill${failedCount ? " has-fail" : ""}${isRunning ? " is-running" : ""}`}
                  style={{ width: `${progressPct}%` }}
                />
              </div>
              <p className="meta flow-progress-caption">
                {failedCount
                  ? `${failedCount} failed · ${progressPct}% complete`
                  : isRunning
                    ? `${progressPct}% complete · in progress`
                    : `${progressPct}% complete`}
              </p>
            </div>

            <ol className="progress-list">
              {!steps.length ? (
                <li className="meta progress-empty">Loading steps…</li>
              ) : (
                steps.map((s, i) => {
                  const active = activeTab === `step-${s.order}`;
                  const next = steps[i + 1];
                  const connectorFilled = Boolean(
                    next && (isCompleteStatus(s.status) || s.status === "running"),
                  );
                  return (
                    <li
                      key={s.order}
                      className={`progress-item${i < steps.length - 1 ? " has-connector" : ""}`}
                    >
                      {i < steps.length - 1 ? (
                        <span
                          className={`progress-connector${connectorFilled ? " filled" : ""}`}
                          aria-hidden
                        />
                      ) : null}
                      <button
                        type="button"
                        ref={active ? activeItemRef : undefined}
                        className={`progress-step status-${s.status}${active ? " active" : ""}`}
                        onClick={() => selectStep(s.order)}
                        title={s.step_id}
                        aria-current={active ? "step" : undefined}
                      >
                        <span className={`progress-marker status-${s.status}`}>
                          <StepMarker status={s.status} index={i} />
                        </span>
                        <span className="progress-step-copy">
                          <span className="progress-step-title">{s.title}</span>
                          <span className="progress-step-status">
                            {STATUS_LABELS[s.status] || s.status}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })
              )}
            </ol>
          </aside>

          <div className="flow-detail">
            {!selectedStep ? (
              <p className="meta">Loading steps…</p>
            ) : (
              <article className={`step-panel status-${selectedStep.status}`}>
                <header className="step-panel-header">
                  <div className="step-panel-kicker">
                    <span>
                      Step {selectedIndex + 1} of {steps.length}
                    </span>
                    <span className={`status-pill status-${selectedStep.status}`}>
                      {STATUS_LABELS[selectedStep.status] || selectedStep.status}
                    </span>
                  </div>
                  <h2>{selectedStep.title}</h2>
                  <code className="step-id">{selectedStep.step_id}</code>
                  {selectedStep.description ? (
                    <p className="step-desc">{selectedStep.description}</p>
                  ) : null}
                </header>

                <div className="step-toolbar">
                  <button
                    type="button"
                    className="btn primary"
                    disabled={isRunning}
                    onClick={() =>
                      void startAction(`/api/runs/${runId}/steps/${selectedStep.order}/run`)
                    }
                  >
                    Run this step
                  </button>
                  {hasDownloads ? (
                    <div className="step-downloads">
                      {selectedStep.can_download_zip ? (
                        <a
                          className="btn"
                          href={`/runs/${runId}/steps/${selectedStep.order}/outputs.zip`}
                          download
                        >
                          Outputs (.zip)
                        </a>
                      ) : null}
                      {selectedStep.can_download_svg ? (
                        <a
                          className="btn"
                          href={`/runs/${runId}/steps/${selectedStep.order}/preview.svg?download=1`}
                          download
                        >
                          Preview (.svg)
                        </a>
                      ) : null}
                      {selectedStep.can_download_preview_source ? (
                        <a
                          className="btn"
                          href={`/runs/${runId}/steps/${selectedStep.order}/preview-source`}
                          download
                        >
                          Layout source
                          {selectedStep.preview_source_name
                            ? ` (${selectedStep.preview_source_name})`
                            : ""}
                        </a>
                      ) : null}
                    </div>
                  ) : null}
                </div>

                <section className="step-section">
                  <h3>Results</h3>
                  {hasResults ? (
                    <StepOutput runId={runId} step={selectedStep} />
                  ) : (
                    <div className="step-empty">
                      <p>No results yet.</p>
                      <p className="meta">
                        Run this step, or use <strong>Run all steps</strong> to execute the flow.
                      </p>
                    </div>
                  )}
                </section>

                {selectedStep.log ? (
                  <section className="step-section">
                    <h3>Log</h3>
                    <pre className="log step-log">{selectedStep.log}</pre>
                  </section>
                ) : null}
              </article>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
