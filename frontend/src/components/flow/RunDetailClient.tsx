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

/** ASIC flow phases for sidebar grouping (order matters). */
const FLOW_CATEGORIES: {
  id: string;
  label: string;
  short: string;
  blurb: string;
  match: (stepId: string) => boolean;
}[] = [
  {
    id: "synthesis",
    label: "Synthesis",
    short: "RTL → gates",
    blurb: "Logic synthesis from Verilog",
    match: (id) => id.startsWith("Yosys."),
  },
  {
    id: "floorplan",
    label: "Floorplan",
    short: "Die & power",
    blurb: "Chip outline, taps, I/O, PDN",
    match: (id) =>
      [
        "OpenROAD.Floorplan",
        "OpenROAD.TapEndcapInsertion",
        "OpenROAD.IOPlacement",
        "OpenROAD.GeneratePDN",
      ].includes(id),
  },
  {
    id: "placement",
    label: "Placement",
    short: "Cell sites",
    blurb: "Global then legalized placement",
    match: (id) =>
      id === "OpenROAD.GlobalPlacement" || id === "OpenROAD.DetailedPlacement",
  },
  {
    id: "clock",
    label: "Clock",
    short: "CTS",
    blurb: "Clock tree synthesis",
    match: (id) => id === "OpenROAD.CTS",
  },
  {
    id: "routing",
    label: "Routing",
    short: "Wires",
    blurb: "Global/detailed routes & fill",
    match: (id) =>
      [
        "OpenROAD.GlobalRouting",
        "OpenROAD.DetailedRouting",
        "OpenROAD.FillInsertion",
      ].includes(id),
  },
  {
    id: "signoff",
    label: "Signoff",
    short: "Verify",
    blurb: "Parasitics, timing, GDS, DRC, LVS",
    match: (id) =>
      [
        "OpenROAD.RCX",
        "OpenROAD.STAPostPNR",
        "KLayout.StreamOut",
        "Magic.DRC",
        "Magic.SpiceExtraction",
        "Netgen.LVS",
      ].includes(id),
  },
];

type DetailTab = "results" | "log";

type StepCategoryMeta = {
  id: string;
  label: string;
  short: string;
  blurb: string;
};

type StepCategoryGroup = StepCategoryMeta & {
  steps: Array<{ step: FlowStep; index: number }>;
  phase: number;
};

function categoryForStep(stepId: string): StepCategoryMeta {
  const found = FLOW_CATEGORIES.find((c) => c.match(stepId));
  if (found) {
    return {
      id: found.id,
      label: found.label,
      short: found.short,
      blurb: found.blurb,
    };
  }
  const tool = stepId.includes(".") ? stepId.split(".")[0] : "Other";
  return {
    id: "other",
    label: tool,
    short: tool,
    blurb: "Additional flow steps",
  };
}

function groupStepsByCategory(steps: FlowStep[]): StepCategoryGroup[] {
  const groups: StepCategoryGroup[] = [];
  const byId = new Map<string, StepCategoryGroup>();

  steps.forEach((step, index) => {
    const cat = categoryForStep(step.step_id);
    let group = byId.get(cat.id);
    if (!group) {
      group = {
        ...cat,
        phase: groups.length + 1,
        steps: [],
      };
      byId.set(cat.id, group);
      groups.push(group);
    }
    group.steps.push({ step, index });
  });

  return groups;
}

function toolFromStepId(stepId: string) {
  const dot = stepId.indexOf(".");
  return dot > 0 ? stepId.slice(0, dot) : stepId;
}

function actionFromStepId(stepId: string) {
  const dot = stepId.indexOf(".");
  return dot > 0 ? stepId.slice(dot + 1) : stepId;
}

function StatusGlyph({ status }: { status: string }) {
  if (status === "running") {
    return (
      <span className="flow-nav-glyph is-running" aria-hidden>
        <span className="progress-spinner" />
      </span>
    );
  }
  if (status === "done" || status === "skipped") {
    return (
      <span className="flow-nav-glyph is-done" aria-hidden>
        ✓
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="flow-nav-glyph is-failed" aria-hidden>
        !
      </span>
    );
  }
  return <span className="flow-nav-glyph is-pending" aria-hidden />;
}

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

function groupStatusSummary(group: StepCategoryGroup) {
  if (group.steps.some((g) => g.step.status === "running")) return "In progress";
  if (group.steps.some((g) => g.step.status === "failed")) return "Needs attention";
  if (group.steps.every((g) => isCompleteStatus(g.step.status))) return "Complete";
  if (group.steps.every((g) => g.step.status === "pending")) return "Waiting";
  return "Partial";
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
      <svg viewBox="0 0 80 80" width="78" height="78" aria-hidden>
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

function artifactBasename(path: string) {
  const slash = path.lastIndexOf("/");
  return slash >= 0 ? path.slice(slash + 1) : path;
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
    return (
      <div className="results-error">
        <p className="results-error-title">Step failed</p>
        <pre className="error">{String(output.error)}</pre>
      </div>
    );
  }

  const metrics = (output.metrics || {}) as Record<string, unknown>;
  const metricKeys = Object.keys(metrics);
  const artifacts = (output.artifacts || []) as Array<{ path: string; size?: number }>;
  const viewsUpdated = (output.views_updated || []) as string[];

  return (
    <div className="step-output">
      <div className="results-summary">
        {output.elapsed_s != null ? (
          <div className="results-stat">
            <span className="results-stat-label">Elapsed</span>
            <strong>{String(output.elapsed_s)}s</strong>
          </div>
        ) : null}
        {metricKeys.length ? (
          <div className="results-stat">
            <span className="results-stat-label">Metrics</span>
            <strong>{metricKeys.length}</strong>
          </div>
        ) : null}
        {artifacts.length ? (
          <div className="results-stat">
            <span className="results-stat-label">Files</span>
            <strong>{artifacts.length}</strong>
          </div>
        ) : null}
        {viewsUpdated.length ? (
          <div className="results-stat">
            <span className="results-stat-label">Views</span>
            <strong>{viewsUpdated.length}</strong>
          </div>
        ) : null}
      </div>

      {output.preview_svg ? (
        <section className="results-section results-preview">
          <header className="results-section-head">
            <h3>Layout preview</h3>
            <span className="results-section-hint">Interactive SVG</span>
          </header>
          <div className="preview-svg-wrap">
            <object
              key={`${runId}-${step.order}-${String(output.preview_svg)}`}
              type="image/svg+xml"
              data={`/runs/${runId}/steps/${step.order}/preview.svg`}
              className="layout-preview-svg"
              aria-label="Layout preview"
            />
          </div>
        </section>
      ) : null}

      {viewsUpdated.length ? (
        <section className="results-section">
          <header className="results-section-head">
            <h3>Views updated</h3>
          </header>
          <ul className="results-tag-list">
            {viewsUpdated.map((view) => (
              <li key={view}>
                <span className="results-tag">{view}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {metricKeys.length ? (
        <section className="results-section">
          <header className="results-section-head">
            <h3>Metrics</h3>
            <span className="results-section-hint">{metricKeys.length} values</span>
          </header>
          <div className="metrics-table-wrap">
            <table className="metrics-table">
              <thead>
                <tr>
                  <th scope="col">Metric</th>
                  <th scope="col">Value</th>
                </tr>
              </thead>
              <tbody>
                {metricKeys.map((key) => (
                  <tr key={key}>
                    <td className="metrics-table-name">{formatMetricName(key)}</td>
                    <td>
                      <code className="metrics-table-value">{String(metrics[key])}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {artifacts.length ? (
        <section className="results-section">
          <header className="results-section-head">
            <h3>Output files</h3>
            <span className="results-section-hint">{artifacts.length} artifacts</span>
          </header>
          <ul className="artifact-list">
            {artifacts.map((a) => (
              <li key={a.path}>
                <span className="artifact-icon" aria-hidden>
                  ▤
                </span>
                <span className="artifact-copy">
                  <span className="artifact-name">{artifactBasename(a.path)}</span>
                  <code className="artifact-path">{a.path}</code>
                </span>
                {a.size != null ? (
                  <span className="artifact-size">{formatBytes(a.size)}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {!hasOutputContent(output) && step.summary ? (
        <section className="results-section">
          <header className="results-section-head">
            <h3>Summary</h3>
          </header>
          <pre className="summary step-summary-fallback">{step.summary}</pre>
        </section>
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
  const [detailTab, setDetailTab] = useState<DetailTab>("results");
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
  const stepGroups = useMemo(() => groupStepsByCategory(steps), [steps]);
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
    setDetailTab("results");
  }, [tab]);

  const isRunning = runBusy;
  const isCompleted = run?.status === "completed";

  async function startAction(path: string) {
    if (busyRef.current || isRunning) {
      setError("A run is already in progress. Wait until it finishes.");
      return;
    }
    if (isCompleted) {
      setError("This run is already completed.");
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
  const selectedCategory = selectedStep
    ? categoryForStep(selectedStep.step_id)
    : null;
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
          <div className="flow-toolbar-actions">
            {isCompleted && run?.can_download_all_files ? (
              <a
                className="btn"
                href={`/runs/${runId}/all-files.zip`}
                download
              >
                Download all files
              </a>
            ) : null}
            <button
              type="button"
              className="btn primary btn-run-all"
              disabled={isRunning || isCompleted || !steps.length}
              onClick={() => void startAction(`/api/runs/${runId}/run-all`)}
            >
              {isRunning ? "Running…" : "Run all steps"}
            </button>
          </div>
        </section>

        <div className="flow-workspace">
          <aside className="flow-nav" aria-label="Flow steps">
            <div className="flow-nav-head">
              <div className="flow-nav-head-top">
                <RingProgress
                  value={progressPct}
                  running={isRunning}
                  failed={failedCount > 0}
                />
                <div className="flow-nav-head-copy">
                  <p className="flow-nav-kicker">Pipeline progress</p>
                  <p className="flow-nav-headline">
                    {doneCount}
                    <span className="flow-nav-headline-sep">/</span>
                    {steps.length || "—"}
                  </p>
                  <p className="flow-nav-subhead">
                    {isRunning
                      ? selectedStep
                        ? `Running · ${selectedStep.title}`
                        : "Flow in progress"
                      : failedCount
                        ? `${failedCount} step${failedCount === 1 ? "" : "s"} failed`
                        : doneCount === steps.length && steps.length
                          ? "All steps complete"
                          : "Ready to run"}
                  </p>
                  {selectedCategory ? (
                    <p className="flow-nav-focus">
                      <span className="flow-nav-focus-label">Category</span>
                      <span className="flow-nav-focus-value">
                        {selectedCategory.label}
                      </span>
                    </p>
                  ) : null}
                </div>
              </div>
              <div className="flow-nav-stats">
                <div className="is-done">
                  <strong>{doneCount}</strong>
                  <span>complete</span>
                </div>
                <div className={runningCount ? "is-running" : ""}>
                  <strong>{runningCount}</strong>
                  <span>running</span>
                </div>
                <div className={failedCount ? "is-failed" : ""}>
                  <strong>{failedCount}</strong>
                  <span>failed</span>
                </div>
              </div>
            </div>

            <div className="flow-nav-list-wrap">
              <div className="flow-nav-label-row">
                <p className="flow-nav-label">
                  Flow phases
                  {stepGroups.length ? (
                    <span className="flow-nav-label-count">{stepGroups.length}</span>
                  ) : null}
                </p>
                {steps.length ? (
                  <span className="flow-nav-label-meta">{steps.length} steps</span>
                ) : null}
              </div>
              {!steps.length ? (
                <p className="meta progress-empty">Loading steps…</p>
              ) : (
                <div className="flow-nav-groups">
                  {stepGroups.map((group) => {
                    const doneInGroup = group.steps.filter((g) =>
                      isCompleteStatus(g.step.status),
                    ).length;
                    const failedInGroup = group.steps.some(
                      (g) => g.step.status === "failed",
                    );
                    const runningInGroup = group.steps.some(
                      (g) => g.step.status === "running",
                    );
                    const firstIdx = group.steps[0].index + 1;
                    const lastIdx = group.steps[group.steps.length - 1].index + 1;
                    const rangeLabel =
                      firstIdx === lastIdx
                        ? `Step ${firstIdx}`
                        : `Steps ${firstIdx}–${lastIdx}`;
                    const summary = groupStatusSummary(group);
                    return (
                      <section
                        key={group.id}
                        className={`flow-nav-group${
                          runningInGroup ? " is-running" : ""
                        }${failedInGroup ? " has-fail" : ""}${
                          doneInGroup === group.steps.length ? " is-complete" : ""
                        }`}
                        aria-label={`${group.label} · ${doneInGroup} of ${group.steps.length} complete`}
                      >
                        <div className="flow-nav-group-head" aria-hidden>
                          <p className="flow-nav-group-label">
                            <span className="flow-nav-group-phase">
                              Phase {String(group.phase).padStart(2, "0")}
                            </span>
                            <span className="flow-nav-group-title">{group.label}</span>
                            <span className="flow-nav-group-count">
                              {doneInGroup}/{group.steps.length}
                            </span>
                          </p>
                          <p className="flow-nav-group-blurb">{group.blurb}</p>
                          <p className="flow-nav-group-meta">
                            <span>{rangeLabel}</span>
                            <span className="flow-nav-group-dot">·</span>
                            <span className="flow-nav-group-short">{group.short}</span>
                            <span className="flow-nav-group-dot">·</span>
                            <span
                              className={`flow-nav-group-summary${
                                runningInGroup
                                  ? " is-running"
                                  : failedInGroup
                                    ? " is-failed"
                                    : doneInGroup === group.steps.length
                                      ? " is-done"
                                      : ""
                              }`}
                            >
                              {summary}
                            </span>
                          </p>
                          <div
                            className="flow-nav-group-bar"
                            title={`${doneInGroup} of ${group.steps.length} complete`}
                          >
                            {group.steps.map(({ step: s }) => (
                              <span
                                key={s.order}
                                className={`flow-nav-group-seg status-${s.status}`}
                              />
                            ))}
                          </div>
                        </div>
                        <ol className="flow-nav-list">
                          {group.steps.map(({ step: s, index: i }, localIdx) => {
                            const active = activeTab === `step-${s.order}`;
                            const elapsed =
                              s.output?.elapsed_s != null
                                ? `${s.output.elapsed_s}s`
                                : null;
                            const isLast = localIdx === group.steps.length - 1;
                            return (
                              <li
                                key={s.order}
                                className={`flow-nav-li${isLast ? " is-last" : ""}`}
                              >
                                <button
                                  type="button"
                                  ref={active ? activeItemRef : undefined}
                                  className={`flow-nav-item status-${s.status}${
                                    active ? " active" : ""
                                  }`}
                                  onClick={() => selectStep(s.order)}
                                  title={
                                    s.description
                                      ? `${s.step_id} — ${s.description}`
                                      : s.step_id
                                  }
                                  aria-current={active ? "step" : undefined}
                                >
                                  <span className="flow-nav-rail" aria-hidden>
                                    <StatusGlyph status={s.status} />
                                  </span>
                                  <span className="flow-nav-copy">
                                    <span className="flow-nav-title-row">
                                      <span className="flow-nav-index">
                                        {String(i + 1).padStart(2, "0")}
                                      </span>
                                      <span className="flow-nav-title">{s.title}</span>
                                    </span>
                                    <span className="flow-nav-meta">
                                      <code className="flow-nav-id">
                                        {toolFromStepId(s.step_id)}.
                                        {actionFromStepId(s.step_id)}
                                      </code>
                                    </span>
                                    {active && s.description ? (
                                      <span className="flow-nav-desc">
                                        {s.description}
                                      </span>
                                    ) : null}
                                    <span className="flow-nav-foot">
                                      <span
                                        className={`flow-nav-badge status-${s.status}`}
                                      >
                                        {STATUS_LABELS[s.status] || s.status}
                                      </span>
                                      {elapsed ? (
                                        <span className="flow-nav-elapsed">
                                          {elapsed}
                                        </span>
                                      ) : null}
                                    </span>
                                  </span>
                                </button>
                              </li>
                            );
                          })}
                        </ol>
                      </section>
                    );
                  })}
                </div>
              )}
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
                      {selectedCategory ? (
                        <span className="step-category-chip">
                          {selectedCategory.label}
                          <span className="step-category-short">
                            {selectedCategory.short}
                          </span>
                        </span>
                      ) : null}
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
                      disabled={isRunning || isCompleted}
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
                            Step outputs (.zip)
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
