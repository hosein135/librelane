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
    if (tab != null) return;
    if (!steps.length) return;
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

  return (
    <AppShell username={username} title={`Flow steps — Run #${runId}`}>
      <section className="run-header">
        <p className="meta">
          <Link href={`/?id=${runId}`}>← Back to overview</Link>
        </p>
        <h1>
          Flow steps — Run #{runId}
          {run ? ` — ${run.top_module_name || run.design_name}` : ""}
          {run?.name &&
          run.name !== (run.top_module_name || run.design_name)
            ? ` (${run.name})`
            : ""}
        </h1>
        <p className={`status-pill status-${run?.status || "pending"}`}>
          {(run?.status || "pending").replaceAll("_", " ")}
        </p>
        {run?.error_message ? <pre className="error">{run.error_message}</pre> : null}
        {error ? <pre className="error">{error}</pre> : null}
        <span className="meta">
          {isRunning
            ? run?.status === "setting_up"
              ? "Configuring flow for this run…"
              : "Step running…"
            : run?.temp_folder_name
              ? `Temp folder: ${run.temp_folder_name}`
              : run?.artifacts_stored
                ? "Artifacts stored in Postgres (work folder kept on disk)."
                : ""}
        </span>
      </section>

      <div className="tabs panel">
        <nav className="tab-list" role="tablist" aria-label="Flow steps">
          {steps.map((s) => (
            <button
              key={s.order}
              type="button"
              className={`tab-btn tab-btn-step status-${s.status}${
                activeTab === `step-${s.order}` ? " active" : ""
              }`}
              onClick={() => setTab(`step-${s.order}`)}
              title={s.step_id}
            >
              {s.title}
            </button>
          ))}
        </nav>

        <div className="tab-content">
          {!steps.length ? (
            <p className="meta">Loading steps…</p>
          ) : (
            steps.map((step) => (
              <section
                key={step.order}
                className={`tab-panel${activeTab === `step-${step.order}` ? " active" : ""}`}
                hidden={activeTab !== `step-${step.order}`}
              >
                <article className={`step-card status-${step.status}`}>
                  <header className="step-header">
                    <h2>{step.title}</h2>
                    <code>{step.step_id}</code>
                    <span className={`status-pill status-${step.status}`}>
                      {STATUS_LABELS[step.status] || step.status}
                    </span>
                  </header>
                  <p className="step-desc">{step.description}</p>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={isRunning}
                    onClick={() =>
                      void startAction(`/api/runs/${runId}/steps/${step.order}/run`)
                    }
                  >
                    Run {step.title}
                  </button>
                  {step.log ? (
                    <div className="step-log-wrap">
                      <h3>Log</h3>
                      <pre className="log step-log">{step.log}</pre>
                    </div>
                  ) : null}
                  <div className="step-downloads">
                    {step.can_download_zip ? (
                      <a
                        className="btn"
                        href={`/runs/${runId}/steps/${step.order}/outputs.zip`}
                        download
                      >
                        Download all outputs (.zip)
                      </a>
                    ) : null}
                    {step.can_download_svg ? (
                      <a
                        className="btn"
                        href={`/runs/${runId}/steps/${step.order}/preview.svg?download=1`}
                        download
                      >
                        Download preview (.svg)
                      </a>
                    ) : null}
                    {step.can_download_preview_source ? (
                      <a
                        className="btn"
                        href={`/runs/${runId}/steps/${step.order}/preview-source`}
                        download
                      >
                        Download layout source
                        {step.preview_source_name ? ` (${step.preview_source_name})` : ""}
                      </a>
                    ) : null}
                  </div>
                  <div className="step-summary-wrap">
                    <h3>Results</h3>
                    <StepOutput runId={runId} step={step} />
                  </div>
                </article>
              </section>
            ))
          )}
        </div>
      </div>
    </AppShell>
  );
}
