"use client";

import Link from "next/link";
import {
  DragEvent,
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { apiFetch, type HomePayload } from "@/lib/api";
import {
  analyzeVerilogFiles,
  readVerilogFiles,
  VERILOG_UPLOAD_LIMITS,
  type VerilogAnalysis,
} from "@/lib/verilog";
import { ModuleHierarchyTree } from "@/components/flow/ModuleHierarchyTree";

function stepsHref(runId: number): string {
  return `/runs/${runId}`;
}

function formatBytes(bytes: number | undefined | null): string {
  if (bytes == null || Number.isNaN(Number(bytes))) return "0 B";
  let n = Number(bytes);
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (n < 1024 || unit === "TB") {
      return unit === "B" ? `${Math.round(n)} ${unit}` : `${n.toFixed(1)} ${unit}`;
    }
    n /= 1024;
  }
  return `${bytes} B`;
}

function shouldShowProgress(run: { status: string }): boolean {
  return run.status !== "completed";
}

function progressLabel(run: {
  progress_pct?: number;
  steps_done?: number;
  steps_total?: number;
}): string {
  const pct =
    run.progress_pct != null && !Number.isNaN(Number(run.progress_pct))
      ? Math.max(0, Math.min(100, Math.round(Number(run.progress_pct))))
      : 0;
  if (run.steps_done != null && run.steps_total != null && run.steps_total > 0) {
    return `${pct}% (${run.steps_done}/${run.steps_total})`;
  }
  return `${pct}%`;
}

export function HomeClient({
  username,
  selectedRunId,
}: {
  username: string;
  selectedRunId?: number | null;
}) {
  const router = useRouter();
  const [data, setData] = useState<HomePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyLocal, setBusyLocal] = useState(false);
  const [loading, setLoading] = useState(true);
  const busyRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const selectedFilesRef = useRef<File[]>([]);
  const dragDepthRef = useRef(0);

  const [analysis, setAnalysis] = useState<VerilogAnalysis | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [topModule, setTopModule] = useState("");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [dragActive, setDragActive] = useState(false);

  function clearSelectedFiles() {
    selectedFilesRef.current = [];
    setSelectedFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  const loadHome = useCallback(async (opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoading(true);
    const res = await apiFetch<HomePayload>("/api/home");
    if (!res.ok) {
      // Autoreload / brief Django restarts should not spam the overview.
      if (!opts?.silent) {
        setError(res.error || `Failed to load home (HTTP ${res.status}).`);
        setLoading(false);
      }
      return;
    }
    setData(res.data);
    setError(null);
    if (!opts?.silent) setLoading(false);
  }, []);

  useEffect(() => {
    void loadHome();
  }, [loadHome]);

  useEffect(() => {
    if (!data?.busy) return;
    const id = window.setInterval(() => {
      void loadHome({ silent: true });
    }, 2500);
    return () => window.clearInterval(id);
  }, [data?.busy, loadHome]);

  async function onFilesChosen(fileList: FileList | File[] | null) {
    setAnalyzeError(null);
    setAnalysis(null);
    setTopModule("");
    // Copy first — clearing the input empties a live FileList from <input onChange>.
    const list = fileList && fileList.length > 0 ? Array.from(fileList) : [];
    selectedFilesRef.current = [];
    setSelectedFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (list.length === 0) return;

    setAnalyzing(true);
    try {
      const { files, errors } = await readVerilogFiles(list);
      if (errors.length) {
        setAnalyzeError(errors.join(" "));
        return;
      }
      const result = analyzeVerilogFiles(files);
      setAnalysis(result);
      if (!result.ok) {
        setAnalyzeError(result.errors.join(" "));
        return;
      }
      selectedFilesRef.current = list;
      setSelectedFiles(list);
      const pick = result.autoTop || result.moduleNames[0] || "";
      setTopModule(pick);
      if (result.warnings.length) {
        setAnalyzeError(result.warnings.join(" "));
      }
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : "Could not analyze Verilog.");
      clearSelectedFiles();
    } finally {
      setAnalyzing(false);
    }
  }

  function onDragEnter(ev: DragEvent<HTMLDivElement>) {
    ev.preventDefault();
    ev.stopPropagation();
    if (locked || analyzing) return;
    dragDepthRef.current += 1;
    setDragActive(true);
  }

  function onDragLeave(ev: DragEvent<HTMLDivElement>) {
    ev.preventDefault();
    ev.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }

  function onDragOver(ev: DragEvent<HTMLDivElement>) {
    ev.preventDefault();
    ev.stopPropagation();
  }

  function onDrop(ev: DragEvent<HTMLDivElement>) {
    ev.preventDefault();
    ev.stopPropagation();
    dragDepthRef.current = 0;
    setDragActive(false);
    if (locked || analyzing) return;
    void onFilesChosen(ev.dataTransfer.files);
  }

  function onClearFiles() {
    setAnalyzeError(null);
    setAnalysis(null);
    setTopModule("");
    clearSelectedFiles();
  }

  async function onCreate(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busyRef.current || data?.busy) {
      setError("Finish your current run before starting a new one.");
      return;
    }
    const files = selectedFilesRef.current;
    if (!files.length || !analysis?.ok) {
      setError("Upload and validate Verilog files before creating a run.");
      return;
    }
    const chosenTop = topModule.trim();
    if (!chosenTop) {
      setError("Select a top module.");
      return;
    }
    if (!analysis.moduleNames.includes(chosenTop)) {
      setError(`Top module ${chosenTop} was not found in the uploaded files.`);
      return;
    }

    busyRef.current = true;
    setBusyLocal(true);
    setError(null);
    const form = e.currentTarget;
    const values = new FormData(form);
    const fd = new FormData();
    fd.set("top_module_name", chosenTop);
    fd.set("design_name", chosenTop);
    fd.set("pdk", String(values.get("pdk") || data?.default_pdk || "sky130A"));
    fd.set("clock_period", String(values.get("clock_period") || 10));
    for (const file of files) {
      fd.append("verilog_files", file, file.name);
    }
    try {
      const res = await apiFetch<{ run?: { id: number }; error?: string }>("/api/runs/new", {
        method: "POST",
        body: fd,
      });
      if (!res.ok) {
        setError(res.error || `Could not create run (HTTP ${res.status}).`);
        return;
      }
      const id = res.data.run?.id;
      selectedFilesRef.current = [];
      setSelectedFiles([]);
      setAnalysis(null);
      setTopModule("");
      setAnalyzeError(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadHome();
      if (!id) {
        setError("Run was created but no id was returned. Refreshing list…");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create run.");
    } finally {
      busyRef.current = false;
      setBusyLocal(false);
    }
  }

  async function onRunAll(runId: number) {
    if (busyRef.current || data?.busy) {
      setError("A run is already in progress. Wait until it finishes.");
      return;
    }
    const target = data?.runs?.find((r) => r.id === runId);
    if (target?.status === "completed") {
      setError("This run is already completed.");
      return;
    }
    busyRef.current = true;
    setBusyLocal(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/runs/${runId}/run-all`, { method: "POST" });
      if (!res.ok) {
        setError(res.error || "Could not start run-all.");
        return;
      }
      await loadHome();
    } finally {
      busyRef.current = false;
      setBusyLocal(false);
    }
  }

  async function onDelete(runId: number) {
    if (busyRef.current) return;
    const ok = window.confirm(
      `Delete run #${runId}? This removes the database record, stored files, and any temporary folder for this run.`,
    );
    if (!ok) return;
    busyRef.current = true;
    setBusyLocal(true);
    try {
      const res = await apiFetch(`/api/runs/${runId}/delete`, { method: "POST" });
      if (!res.ok) {
        setError(res.error || "Could not delete run.");
        return;
      }
      if (selectedRunId === runId) {
        router.push("/");
      }
      await loadHome();
    } finally {
      busyRef.current = false;
      setBusyLocal(false);
    }
  }

  const locked = Boolean(data?.busy) || busyLocal;
  const runs = data?.runs || [];
  const canCreate =
    Boolean(analysis?.ok) && Boolean(topModule.trim()) && !analyzing && !locked;

  return (
    <AppShell username={username}>
      <section className="hero">
        <h1>LibreLane Colab, in your browser</h1>
        <p>
          Upload Verilog, pick a top module, and walk the implementation flow — synthesis
          through LVS — with a live step-by-step workspace.
        </p>
        <p className="meta">
          LibreLane version in environment:{" "}
          <strong>{data?.librelane_version || (loading ? "…" : "unknown")}</strong>
        </p>
        {data?.busy ? (
          <p className="meta">
            You have a run in progress — finish it before starting another.
          </p>
        ) : null}
      </section>

      {data?.storage ? (
        <section className="panel">
          <h2>Storage</h2>
          <p className="meta">
            Workspaces: <code>{data.storage.runs_root}/user_&lt;id&gt;/run_&lt;id&gt;/</code>
          </p>
          <ul className="config-list">
            <li>
              Your usage: <code>{formatBytes(data.storage.user_total_bytes)}</code> of{" "}
              <code>{formatBytes(data.storage.user_quota_bytes)}</code> (
              {data.storage.user_used_pct}%) — disk{" "}
              <code>{formatBytes(data.storage.user_disk_bytes)}</code>, DB{" "}
              <code>{formatBytes(data.storage.user_db_bytes)}</code>
            </li>
            <li>
              Free on volume: <code>{formatBytes(data.storage.free_bytes)}</code> (min{" "}
              <code>{formatBytes(data.storage.min_free_bytes)}</code>)
            </li>
            <li>
              Per-run disk budget: <code>{formatBytes(data.storage.run_budget_bytes)}</code>
            </li>
            <li>
              Workdir retention:{" "}
              <code>
                {data.storage.retention_days > 0
                  ? `${data.storage.retention_days} days after finish (DB archive kept)`
                  : "until you delete the run"}
              </code>
            </li>
          </ul>
        </section>
      ) : null}

      <section className="panel new-run-panel">
        <div className="new-run-header">
          <div>
            <h2>Start a new run</h2>
            <p className="meta new-run-lede">
              Upload Verilog, choose a top module and PDK, then create a workspace for the
              flow.
            </p>
          </div>
          {analysis?.ok ? (
            <span className="new-run-ready-pill">Ready to create</span>
          ) : null}
        </div>

        <form className="new-run-form" onSubmit={onCreate}>
          <div className="new-run-step">
            <div className="new-run-step-head">
              <span className="new-run-step-num" aria-hidden="true">
                1
              </span>
              <div>
                <h3 className="new-run-step-title">Upload Verilog</h3>
                <p className="meta">
                  Drop <code>.v</code> / <code>.sv</code> sources. Modules are detected in
                  the browser before upload.
                </p>
              </div>
            </div>

            <div className="verilog-upload">
              <div
                className={[
                  "verilog-dropzone",
                  dragActive ? "is-dragover" : "",
                  selectedFiles.length && analysis?.ok ? "has-files" : "",
                  analyzing ? "is-analyzing" : "",
                  locked ? "is-disabled" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onDragEnter={onDragEnter}
                onDragLeave={onDragLeave}
                onDragOver={onDragOver}
                onDrop={onDrop}
              >
                <input
                  ref={fileInputRef}
                  id="verilog-files-input"
                  className="verilog-file-input"
                  type="file"
                  accept={VERILOG_UPLOAD_LIMITS.accept}
                  multiple
                  disabled={locked || analyzing}
                  onChange={(ev) => void onFilesChosen(ev.target.files)}
                />
                {selectedFiles.length === 0 ? (
                  <label htmlFor="verilog-files-input" className="verilog-dropzone-body">
                    <span className="verilog-dropzone-icon" aria-hidden="true">
                      <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
                        <path
                          d="M12 16V4m0 0 4 4m-4-4-4 4"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                        <path
                          d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>
                    <span className="verilog-dropzone-title">
                      {dragActive
                        ? "Drop files to analyze"
                        : analyzing
                          ? "Analyzing Verilog…"
                          : "Drop .v / .sv files here"}
                    </span>
                    <span className="verilog-dropzone-hint">
                      or <span className="verilog-browse">browse</span> — up to{" "}
                      {VERILOG_UPLOAD_LIMITS.maxFiles} files,{" "}
                      {VERILOG_UPLOAD_LIMITS.maxFileBytes / (1024 * 1024)} MiB each
                    </span>
                  </label>
                ) : (
                  <div className="verilog-file-list-wrap">
                    <div className="verilog-file-list-head">
                      <span>
                        {selectedFiles.length} file
                        {selectedFiles.length === 1 ? "" : "s"} ready
                      </span>
                      <div className="verilog-file-list-actions">
                        <button
                          type="button"
                          className="btn"
                          disabled={locked || analyzing}
                          onClick={() => {
                            if (fileInputRef.current) {
                              fileInputRef.current.value = "";
                              fileInputRef.current.click();
                            }
                          }}
                        >
                          Replace
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={locked || analyzing}
                          onClick={onClearFiles}
                        >
                          Clear
                        </button>
                      </div>
                    </div>
                    <ul className="verilog-file-list">
                      {selectedFiles.map((file) => (
                        <li key={`${file.name}:${file.size}:${file.lastModified}`}>
                          <span className="verilog-file-name" title={file.name}>
                            {file.name}
                          </span>
                          <span className="verilog-file-size meta">
                            {formatBytes(file.size)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>

            {analyzeError ? (
              <pre className={analysis?.ok ? "warn" : "error"}>{analyzeError}</pre>
            ) : null}

            {analysis?.ok ? (
              <div className="module-analysis">
                <div className="module-analysis-head">
                  <div>
                    <strong className="module-analysis-title">Detected hierarchy</strong>
                    <p className="meta">
                      Click any module to choose or change the top used for synthesis. The
                      run is named after that module; if the name already exists, a number
                      is appended (for example <code>design_2</code>).
                    </p>
                  </div>
                  {topModule ? (
                    <div className="module-top-banner" aria-live="polite">
                      <span className="module-top-banner-label">Selected top</span>
                      <code className="module-top-banner-name">{topModule}</code>
                      <span className="meta">Click another node to change it</span>
                    </div>
                  ) : null}
                </div>
                <ModuleHierarchyTree
                  nodes={analysis.tree}
                  selectedTop={topModule}
                  suggestedTops={analysis.suggestedTops}
                  onSelect={setTopModule}
                />
              </div>
            ) : null}
          </div>

          <div className={`new-run-step${analysis?.ok ? "" : " is-pending"}`}>
            <div className="new-run-step-head">
              <span className="new-run-step-num" aria-hidden="true">
                2
              </span>
              <div>
                <h3 className="new-run-step-title">Configure run</h3>
                <p className="meta">
                  Set the process kit and clock period. The top module is chosen in the
                  hierarchy above.
                </p>
              </div>
            </div>

            <div className="new-run-fields">
              <label>
                PDK variant
                <select
                  name="pdk"
                  defaultValue={data?.default_pdk || "sky130A"}
                  disabled={locked}
                >
                  {(data?.pdk_variants?.length
                    ? data.pdk_variants
                    : ["sky130A", "gf180mcuD", "ihp-sg13g2"]
                  ).map((variant) => (
                    <option key={variant} value={variant}>
                      {variant}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Clock period (ns)
                <input
                  name="clock_period"
                  type="number"
                  step="0.1"
                  min="0.1"
                  defaultValue={10}
                  disabled={locked}
                />
              </label>
            </div>
          </div>

          {error ? <pre className="error">{error}</pre> : null}

          <div className="new-run-footer">
            <p className="meta new-run-footer-hint">
              {locked
                ? "Finish the current run before starting another."
                : analyzing
                  ? "Analyzing uploaded Verilog…"
                  : analysis?.ok && topModule
                    ? `Ready — top module ${topModule}. Create a run to add it to the list below.`
                    : analysis?.ok
                      ? "Select a top module in the hierarchy above."
                      : "Upload and validate Verilog to enable create."}
            </p>
            <button type="submit" className="btn primary new-run-submit" disabled={!canCreate}>
              {locked ? "Busy…" : analyzing ? "Analyzing…" : "Create run"}
            </button>
          </div>
        </form>
      </section>

      <section className="panel">
        <h2>Recent runs</h2>
        <p className="meta">Click a run name to open its flow steps.</p>
        {loading && !data ? (
          <p className="meta">Loading runs…</p>
        ) : runs.length === 0 ? (
          <p className="meta">No runs yet. Create one above to get started.</p>
        ) : (
          <div className="table-wrap">
            <table className="run-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>PDK variant</th>
                  <th>Clock period (ns)</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const selected = selectedRunId === run.id;
                  const runTop = run.top_module_name || run.design_name;
                  return (
                    <tr key={run.id} className={selected ? "selected" : undefined}>
                      <td>
                        <Link href={stepsHref(run.id)} className="run-name-link">
                          {run.name || runTop}
                        </Link>
                      </td>
                      <td>
                        <div className="run-status-cell">
                          <span className={`status-pill status-${run.status}`}>
                            {run.status.replaceAll("_", " ")}
                          </span>
                          {shouldShowProgress(run) ? (
                            <span className="run-progress" title="Step progress">
                              {progressLabel(run)}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td>{run.pdk}</td>
                      <td>{run.clock_period}</td>
                      <td className="run-table-actions">
                        <button
                          type="button"
                          className="btn primary"
                          disabled={
                            run.status === "running" ||
                            run.status === "setting_up" ||
                            run.status === "completed" ||
                            locked
                          }
                          onClick={() => void onRunAll(run.id)}
                        >
                          Run all steps
                        </button>
                        {run.status === "completed" && run.can_download_all_files ? (
                          <a
                            className="btn"
                            href={`/runs/${run.id}/all-files.zip`}
                            download
                          >
                            Download all
                          </a>
                        ) : null}
                        <button
                          type="button"
                          className="btn danger"
                          disabled={
                            run.status === "running" ||
                            run.status === "setting_up" ||
                            locked
                          }
                          onClick={() => void onDelete(run.id)}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Flow steps description</h2>
        <p className="meta">
          Notebook order at a glance. Open a run by name to execute and inspect each step.
        </p>
        <ol className="step-catalog">
          {(data?.step_catalog || []).map((step) => (
            <li key={step.step_id}>
              <strong>{step.title}</strong> — <code>{step.step_id}</code>
              {step.description ? <p className="step-catalog-desc">{step.description}</p> : null}
            </li>
          ))}
        </ol>
      </section>
    </AppShell>
  );
}
