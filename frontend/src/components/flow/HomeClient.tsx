"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { apiFetch, type HomePayload } from "@/lib/api";
import {
  analyzeVerilogFiles,
  formatModuleTree,
  readVerilogFiles,
  VERILOG_UPLOAD_LIMITS,
  type VerilogAnalysis,
} from "@/lib/verilog";

function stepsHref(runId: number, watch = false): string {
  const q = watch ? "?watch=1" : "";
  return `/runs/${runId}${q}`;
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

  const [analysis, setAnalysis] = useState<VerilogAnalysis | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [topModule, setTopModule] = useState("");
  const [fileLabel, setFileLabel] = useState("No files selected");

  const loadHome = useCallback(async () => {
    setLoading(true);
    const res = await apiFetch<HomePayload>("/api/home");
    if (!res.ok) {
      setError(res.error || `Failed to load home (HTTP ${res.status}).`);
      setLoading(false);
      return;
    }
    setData(res.data);
    setError(null);
    setLoading(false);
  }, []);

  useEffect(() => {
    void loadHome();
  }, [loadHome]);

  async function onFilesChosen(fileList: FileList | null) {
    setAnalyzeError(null);
    setAnalysis(null);
    setTopModule("");
    selectedFilesRef.current = [];
    if (!fileList || fileList.length === 0) {
      setFileLabel("No files selected");
      return;
    }
    setAnalyzing(true);
    setFileLabel(
      fileList.length === 1 ? fileList[0].name : `${fileList.length} Verilog files`,
    );
    try {
      const { files, errors } = await readVerilogFiles(fileList);
      if (errors.length) {
        setAnalyzeError(errors.join(" "));
        setFileLabel("No files selected");
        if (fileInputRef.current) fileInputRef.current.value = "";
        return;
      }
      const result = analyzeVerilogFiles(files);
      setAnalysis(result);
      if (!result.ok) {
        setAnalyzeError(result.errors.join(" "));
        selectedFilesRef.current = [];
        setFileLabel("No files selected");
        if (fileInputRef.current) fileInputRef.current.value = "";
        return;
      }
      selectedFilesRef.current = Array.from(fileList);
      const pick = result.autoTop || result.moduleNames[0] || "";
      setTopModule(pick);
      if (result.warnings.length) {
        setAnalyzeError(result.warnings.join(" "));
      }
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : "Could not analyze Verilog.");
      selectedFilesRef.current = [];
      setFileLabel("No files selected");
      if (fileInputRef.current) fileInputRef.current.value = "";
    } finally {
      setAnalyzing(false);
    }
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
    fd.set("name", String(values.get("name") || chosenTop || "run"));
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
      setAnalysis(null);
      setTopModule("");
      setFileLabel("No files selected");
      setAnalyzeError(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (id) {
        router.push(stepsHref(id));
        await loadHome();
        return;
      }
      setError("Run was created but no id was returned. Refreshing list…");
      await loadHome();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create run.");
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
          This web app mirrors <code>notebook.ipynb</code>: install LibreLane via Nix,
          enable supported PDKs under <code>~/.ciel</code>, upload your Verilog design,
          pick a top module, and run each implementation step (synthesis through LVS).
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

      <section className="panel">
        <h2>Start a new run</h2>
        <form onSubmit={onCreate}>
          <div className="form-grid">
            <label>
              Run name
              <input
                name="name"
                defaultValue=""
                placeholder={topModule || "my_design"}
                disabled={locked}
              />
            </label>
            <label className="span-2">
              Verilog files
              <input
                ref={fileInputRef}
                type="file"
                accept={VERILOG_UPLOAD_LIMITS.accept}
                multiple
                disabled={locked || analyzing}
                onChange={(ev) => void onFilesChosen(ev.target.files)}
              />
              <span className="meta">
                {fileLabel}. Multiple <code>.v</code> / <code>.sv</code> (max{" "}
                {VERILOG_UPLOAD_LIMITS.maxFiles} files,{" "}
                {VERILOG_UPLOAD_LIMITS.maxFileBytes / (1024 * 1024)} MiB each). Checked in
                the browser before upload.
              </span>
            </label>
            <label>
              Top module
              <select
                name="top_module_name"
                value={topModule}
                disabled={locked || !analysis?.ok}
                onChange={(ev) => setTopModule(ev.target.value)}
                required
              >
                {!analysis?.ok ? (
                  <option value="">Upload files first…</option>
                ) : (
                  analysis.moduleNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                      {analysis.suggestedTops.includes(name) ? " (suggested top)" : ""}
                    </option>
                  ))
                )}
              </select>
              <span className="meta">
                Auto-detected when possible; choose manually if you prefer another module.
              </span>
            </label>
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
                defaultValue={10}
                disabled={locked}
              />
            </label>
          </div>

          {analyzing ? <p className="meta">Analyzing Verilog…</p> : null}
          {analyzeError ? (
            <pre className={analysis?.ok ? "warn" : "error"}>{analyzeError}</pre>
          ) : null}
          {analysis?.ok ? (
            <div className="module-analysis">
              <p className="meta">
                Modules: <code>{analysis.moduleNames.join(", ")}</code>
                {analysis.autoTop ? (
                  <>
                    {" "}
                    — auto top: <code>{analysis.autoTop}</code>
                  </>
                ) : null}
              </p>
              <pre className="module-tree" aria-label="Module hierarchy">
                {formatModuleTree(analysis.tree) || "(no hierarchy)"}
              </pre>
            </div>
          ) : null}

          {error ? <pre className="error">{error}</pre> : null}
          <button type="submit" className="btn primary" disabled={!canCreate}>
            {locked ? "Busy…" : analyzing ? "Analyzing…" : "Create run"}
          </button>
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
                  <th>ID</th>
                  <th>Status</th>
                  <th>Created</th>
                  <th></th>
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
                      <td>#{run.id}</td>
                      <td>
                        <span className={`status-pill status-${run.status}`}>{run.status}</span>
                      </td>
                      <td className="meta">{run.created_at}</td>
                      <td className="run-table-actions">
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
