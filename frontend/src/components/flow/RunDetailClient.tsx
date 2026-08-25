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
      <span className="progress-marker-icon" aria-hidden>
        ✓
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="progress-marker-icon" aria-hidden>
        !
      </span>
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

  const reachedIndex = useMemo(() => {
    let last = -1;
    steps.forEach((s, i) => {
      if (s.status === "done" || s.status === "running" || s.status === "failed") {
        last = i;
      }
    });
    return last;
  }, [steps]);

  const fillPct =
    steps.length <= 1 ? 0 : Math.max(0, reachedIndex) / Math.max(1, steps.length - 1);

  function selectStep(order: number) {
    followRunning.current = false;
    setTab(`step-${order}`);
  }

  const designLabel = run ? run.top_module_name || run.design_name : "";

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
          <nav className="flow-progress" aria-label="Flow steps">
            <div className="progress-list-wrap">
              {steps.length > 1 ? (
                <>
                  <div className="progress-track" />
                  <div
                    className="progress-track-fill"
                    style={{ height: `calc(${fillPct} * (100% - 2rem))` }}
                  />
                </>
              ) : null}
              <ol className="progress-list">
                {!steps.length ? (
                  <li className="meta">Loading steps…</li>
                ) : (
                  steps.map((s, i) => {
                    const active = activeTab === `step-${s.order}`;
                    return (
                      <li key={s.order}>
                        <button
                          type="button"
                          className={`progress-step status-${s.status}${active ? " active" : ""}`}
                          onClick={() => selectStep(s.order)}
                          title={s.step_id}
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
            </div>
          </nav>

          <div className="flow-detail">
            {!selectedStep ? (
              <p className="meta">Loading steps…</p>
            ) : (
              <article className={`step-card status-${selectedStep.status}`}>
                <header className="step-header">
                  <h2>{selectedStep.title}</h2>
                  <code>{selectedStep.step_id}</code>
                  <span className={`status-pill status-${selectedStep.status}`}>
                    {STATUS_LABELS[selectedStep.status] || selectedStep.status}
                  </span>
                </header>
                <p className="step-desc">{selectedStep.description}</p>
                <div className="step-actions">
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
                </div>
                <div className="step-downloads">
                  {selectedStep.can_download_zip ? (
                    <a
                      className="btn"
                      href={`/runs/${runId}/steps/${selectedStep.order}/outputs.zip`}
                      download
                    >
                      Download all outputs (.zip)
                    </a>
                  ) : null}
                  {selectedStep.can_download_svg ? (
                    <a
                      className="btn"
                      href={`/runs/${runId}/steps/${selectedStep.order}/preview.svg?download=1`}
                      download
                    >
                      Download preview (.svg)
                    </a>
                  ) : null}
                  {selectedStep.can_download_preview_source ? (
                    <a
                      className="btn"
                      href={`/runs/${runId}/steps/${selectedStep.order}/preview-source`}
                      download
                    >
                      Download layout source
                      {selectedStep.preview_source_name
                        ? ` (${selectedStep.preview_source_name})`
                        : ""}
                    </a>
                  ) : null}
                </div>
                {selectedStep.log ? (
                  <div className="step-log-wrap">
                    <h3>Log</h3>
                    <pre className="log step-log">{selectedStep.log}</pre>
                  </div>
                ) : null}
                <div className="step-summary-wrap">
                  <h3>Results</h3>
                  {hasOutputContent(selectedStep.output) || selectedStep.summary ? (
                    <StepOutput runId={runId} step={selectedStep} />
                  ) : (
                    <p className="meta">No results yet. Run this step or run all steps.</p>
                  )}
                </div>
              </article>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
