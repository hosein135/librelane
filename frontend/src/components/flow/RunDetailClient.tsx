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

type DetailTab = "overview" | "results" | "log";

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

function isRunBusy(run: FlowRun | null | undefined) {
  return Boolean(
    run?.is_running || run?.status === "running" || run?.status === "setting_up",
  );
}

/** Prefer in-progress / failed / next pending — never "whatever finished last". */
function pickDefaultStep(steps: FlowStep[]): FlowStep {
  return (
    steps.find((s) => s.status === "running") ||
    steps.find((s) => s.status === "failed") ||
    steps.find((s) => s.status === "pending") ||
    steps[0]
  );
}

function RingProgress({
  value,
  running,
  failed,
}: {
  value: number;
  running: boolean;
  failed: boolean;
}) {
  const r = 34;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, value));
  const offset = c - (clamped / 100) * c;
  return (
    <div
      className={`flow-ring${running ? " is-running" : ""}${failed ? " has-fail" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={clamped}
      aria-label="Flow completion"
    >
      <svg viewBox="0 0 80 80" width="88" height="88" aria-hidden>
        <circle className="flow-ring-track" cx="40" cy="40" r={r} />
        <circle
          className="flow-ring-value"
          cx="40"
          cy="40"
          r={r}
          strokeDasharray={c}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="flow-ring-label">
        <strong>{clamped}%</strong>
        <span>done</span>
      </div>
    </div>
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
        <div className="stat-chip">
          <span className="stat-chip-label">Elapsed</span>
          <strong>{String(output.elapsed_s)}s</strong>
        </div>
      ) : null}
      {output.preview_svg ? (
        <div className="output-block output-preview">
          <div className="output-block-title">Layout preview</div>
          <div className="preview-svg-wrap">
            <object
              key={`${runId}-${step.order}-${String(output.preview_svg)}`}
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
          <div className="output-block-title">Metrics</div>
          <div className="metrics-grid">
            {Object.keys(metrics).map((key) => (
              <div className="metric-card" key={key}>
                <span className="metric-name">{formatMetricName(key)}</span>
                <code className="metric-value">{String(metrics[key])}</code>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {artifacts.length ? (
        <div className="output-block">
          <div className="output-block-title">Output files</div>
          <ul className="artifact-list">
            {artifacts.map((a) => (
              <li key={a.path}>
                <code>{a.path}</code>
                <span className="meta">{formatBytes(a.size)}</span>
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
  const [detailTab, setDetailTab] = useState<DetailTab>("overview");
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
  const runBusy = isRunBusy(run);

  useEffect(() => {
    if (!steps.length) return;

    if (followRunning.current) {
      const running = steps.find((s) => s.status === "running");
      if (running) {
        setTab(`step-${running.order}`);
        return;
      }
      // Between steps the run is still busy — keep the current tab.
      // When the run goes idle, land on failed/pending/first (not the last
      // step that just finished, which is a race with poll timing).
      if (!runBusy) {
        const land = pickDefaultStep(steps);
        setTab(`step-${land.order}`);
        followRunning.current = false;
      }
      return;
    }

    if (tab != null) return;
    setTab(`step-${pickDefaultStep(steps).order}`);
  }, [steps, tab, runBusy]);

  useEffect(() => {
    activeItemRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [tab]);

  useEffect(() => {
    setDetailTab("overview");
  }, [tab]);

  const isRunning = runBusy;

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
  const selectedIndex = steps.findIndex((s) => `step-${s.order}` === activeTab);
  const selectedStep = selectedIndex >= 0 ? steps[selectedIndex] : steps[0];
  const prevStep = selectedIndex > 0 ? steps[selectedIndex - 1] : null;
  const nextStep =
    selectedIndex >= 0 && selectedIndex < steps.length - 1
      ? steps[selectedIndex + 1]
      : null;

  const doneCount = steps.filter((s) => isCompleteStatus(s.status)).length;
  const failedCount = steps.filter((s) => s.status === "failed").length;
  const runningCount = steps.filter((s) => s.status === "running").length;
  const progressPct = steps.length ? Math.round((doneCount / steps.length) * 100) : 0;

  function selectStep(order: number) {
    followRunning.current = false;
    setTab(`step-${order}`);
  }

  const designLabel = run ? run.top_module_name || run.design_name : "";
  const hasDownloads =
    Boolean(selectedStep?.can_download_zip) ||
    Boolean(selectedStep?.can_download_preview_source);
  const hasResults =
    Boolean(selectedStep) &&
    (hasOutputContent(selectedStep.output) || Boolean(selectedStep.summary));
  const hasLog = Boolean(selectedStep?.log);

  return (
    <AppShell username={username} title={`Flow steps — Run #${runId}`} wide>
      <div className="flow-page">
        <section className="flow-toolbar">
          <div className="flow-toolbar-copy">
            <Link href={`/?id=${runId}`} className="flow-back">
              ← Overview
            </Link>
            <div className="flow-toolbar-title-row">
              <h1>
                {designLabel || `Run #${runId}`}
                {run?.name && run.name !== designLabel ? (
                  <span className="flow-run-alias"> · {run.name}</span>
                ) : null}
              </h1>
              <span className={`status-pill status-${run?.status || "pending"}`}>
                {(run?.status || "pending").replaceAll("_", " ")}
              </span>
            </div>
            <p className="meta flow-toolbar-meta">
              Run #{runId}
              {isRunning
                ? run?.status === "setting_up"
                  ? " · configuring flow…"
                  : " · step in progress"
                : ""}
              {(run?.disk_bytes != null || run?.db_bytes != null) && (
                <>
                  {" "}
                  · Disk {formatBytes(run.disk_bytes)} · DB {formatBytes(run.db_bytes)}
                </>
              )}
            </p>
            {run?.error_message ? <pre className="error">{run.error_message}</pre> : null}
            {error ? <pre className="error">{error}</pre> : null}
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
          <aside className="flow-nav" aria-label="Flow steps">
            <div className="flow-nav-head">
              <RingProgress
                value={progressPct}
                running={isRunning}
                failed={failedCount > 0}
              />
              <div className="flow-nav-stats">
                <div>
                  <strong>{doneCount}</strong>
                  <span>complete</span>
                </div>
                <div>
                  <strong>{runningCount}</strong>
                  <span>running</span>
                </div>
                <div>
                  <strong>{failedCount}</strong>
                  <span>failed</span>
                </div>
              </div>
              <div className="flow-segment-bar" aria-hidden>
                {steps.map((s) => (
                  <span
                    key={s.order}
                    className={`flow-segment status-${s.status}${
                      activeTab === `step-${s.order}` ? " active" : ""
                    }`}
                    title={s.title}
                  />
                ))}
              </div>
            </div>

            <div className="flow-nav-list-wrap">
              <p className="flow-nav-label">Steps</p>
              <ol className="flow-nav-list">
                {!steps.length ? (
                  <li className="meta progress-empty">Loading steps…</li>
                ) : (
                  steps.map((s, i) => {
                    const active = activeTab === `step-${s.order}`;
                    return (
                      <li key={s.order}>
                        <button
                          type="button"
                          ref={active ? activeItemRef : undefined}
                          className={`flow-nav-item status-${s.status}${active ? " active" : ""}`}
                          onClick={() => selectStep(s.order)}
                          title={s.step_id}
                          aria-current={active ? "step" : undefined}
                        >
                          <span className="flow-nav-index">
                            {String(i + 1).padStart(2, "0")}
                          </span>
                          <span className="flow-nav-copy">
                            <span className="flow-nav-title">{s.title}</span>
                          </span>
                              <span className={`flow-nav-badge status-${s.status}`}>
                            {s.status === "running" ? (
                              <span className="progress-spinner" aria-hidden />
                            ) : null}
                            {STATUS_LABELS[s.status] || s.status}
                          </span>
                        </button>
                      </li>
                    );
                  })
                )}
              </ol>
            </div>
          </aside>

          <div className="flow-detail">
            {!selectedStep ? (
              <p className="meta step-loading">Loading steps…</p>
            ) : (
              <article
                key={selectedStep.order}
                className={`step-panel status-${selectedStep.status}`}
              >
                <header className="step-hero">
                  <div className="step-hero-top">
                    <div className="step-hero-meta">
                      <span className="step-index-chip">
                        {selectedIndex + 1} / {steps.length}
                      </span>
                      <span className={`status-pill status-${selectedStep.status}`}>
                        {STATUS_LABELS[selectedStep.status] || selectedStep.status}
                      </span>
                    </div>
                    <div className="step-pager">
                      <button
                        type="button"
                        className="btn"
                        disabled={!prevStep}
                        onClick={() => prevStep && selectStep(prevStep.order)}
                      >
                        ← Prev
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={!nextStep}
                        onClick={() => nextStep && selectStep(nextStep.order)}
                      >
                        Next →
                      </button>
                    </div>
                  </div>
                  <h2>{selectedStep.title}</h2>
                  <code className="step-id">{selectedStep.step_id}</code>
                  {selectedStep.description ? (
                    <p className="step-desc">{selectedStep.description}</p>
                  ) : null}

                  <div className="step-hero-actions">
                    <button
                      type="button"
                      className="btn primary"
                      disabled={isRunning}
                      onClick={() =>
                        void startAction(
                          `/api/runs/${runId}/steps/${selectedStep.order}/run`,
                        )
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
                </header>

                <div className="step-tabs" role="tablist" aria-label="Step content">
                  {(
                    [
                      ["overview", "Overview"],
                      ["results", "Results"],
                      ["log", "Log"],
                    ] as const
                  ).map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      role="tab"
                      aria-selected={detailTab === id}
                      className={`step-tab${detailTab === id ? " active" : ""}`}
                      onClick={() => setDetailTab(id)}
                    >
                      {label}
                      {id === "results" && hasResults ? (
                        <span className="step-tab-dot" />
                      ) : null}
                      {id === "log" && hasLog ? <span className="step-tab-dot" /> : null}
                    </button>
                  ))}
                </div>

                <div className="step-tab-panels">
                  {detailTab === "overview" ? (
                    <section className="step-tab-panel">
                      <div className="overview-grid">
                        <div className="overview-card">
                          <span className="overview-label">Status</span>
                          <strong>
                            {STATUS_LABELS[selectedStep.status] || selectedStep.status}
                          </strong>
                        </div>
                        <div className="overview-card">
                          <span className="overview-label">Step ID</span>
                          <code>{selectedStep.step_id}</code>
                        </div>
                        <div className="overview-card">
                          <span className="overview-label">Position</span>
                          <strong>
                            {selectedIndex + 1} of {steps.length}
                          </strong>
                        </div>
                        <div className="overview-card">
                          <span className="overview-label">Outputs</span>
                          <strong>
                            {hasResults ? "Available" : "None yet"}
                            {hasLog ? " · log ready" : ""}
                          </strong>
                        </div>
                      </div>
                      {selectedStep.description ? (
                        <p className="overview-blurb">{selectedStep.description}</p>
                      ) : (
                        <p className="meta">No extra description for this step.</p>
                      )}
                    </section>
                  ) : null}

                  {detailTab === "results" ? (
                    <section className="step-tab-panel">
                      {hasResults ? (
                        <StepOutput runId={runId} step={selectedStep} />
                      ) : (
                        <div className="step-empty">
                          <div className="step-empty-icon" aria-hidden>
                            ▢
                          </div>
                          <p>No results for this step yet</p>
                          <p className="meta">
                            Run this step alone, or start the full flow from the top.
                          </p>
                        </div>
                      )}
                    </section>
                  ) : null}

                  {detailTab === "log" ? (
                    <section className="step-tab-panel">
                      {hasLog ? (
                        <pre className="log step-log">{selectedStep.log}</pre>
                      ) : (
                        <div className="step-empty">
                          <div className="step-empty-icon" aria-hidden>
                            ≡
                          </div>
                          <p>No log output yet</p>
                          <p className="meta">Logs appear after the step starts running.</p>
                        </div>
                      )}
                    </section>
                  ) : null}
                </div>
              </article>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
