"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { apiFetch, type FlowRun, type HomePayload } from "@/lib/api";

function stepsHref(runId: number, watch = false): string {
  const q = watch ? "?watch=1" : "";
  return `/runs/${runId}${q}`;
}

function overviewHref(runId: number): string {
  return `/?id=${runId}`;
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
  const [selectedRun, setSelectedRun] = useState<FlowRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyLocal, setBusyLocal] = useState(false);
  const [loading, setLoading] = useState(true);
  const busyRef = useRef(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  const loadSelectedRun = useCallback(async (runId: number) => {
    const res = await apiFetch<FlowRun>(`/api/runs/${runId}`);
    if (!res.ok) {
      setSelectedRun(null);
      setError(res.error || "Failed to load run.");
      return null;
    }
    setSelectedRun(res.data);
    setError(null);
    return res.data;
  }, []);

  const pollSelected = useCallback(
    async (runId: number) => {
      const res = await apiFetch<FlowRun>(`/api/runs/${runId}/status`);
      if (!res.ok) return;
      setSelectedRun(res.data);
      const stillBusy =
        res.data.is_running ||
        res.data.status === "setting_up" ||
        res.data.status === "running";
      if (stillBusy) {
        const delay = res.data.status === "setting_up" ? 2000 : 4000;
        pollTimer.current = setTimeout(() => void pollSelected(runId), delay);
      } else {
        busyRef.current = false;
        pollTimer.current = null;
        void loadHome();
      }
    },
    [loadHome],
  );

  useEffect(() => {
    void loadHome();
  }, [loadHome]);

  useEffect(() => {
    if (pollTimer.current) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
    if (!selectedRunId) {
      setSelectedRun(null);
      return;
    }
    void loadSelectedRun(selectedRunId).then((run) => {
      if (run?.is_running || run?.status === "setting_up" || run?.status === "running") {
        void pollSelected(selectedRunId);
      }
    });
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [selectedRunId, loadSelectedRun, pollSelected]);

  async function onCreate(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busyRef.current || data?.busy) {
      setError("Finish your current run before starting a new one.");
      return;
    }
    busyRef.current = true;
    setBusyLocal(true);
    setError(null);
    const form = new FormData(e.currentTarget);
    const topModule = String(form.get("top_module_name") || "spm").trim();
    const body = {
      name: String(form.get("name") || topModule || "spm"),
      top_module_name: topModule,
      design_name: topModule,
      pdk: String(form.get("pdk") || data?.default_pdk || "sky130A"),
      clock_period: Number(form.get("clock_period") || 10),
    };
    try {
      const res = await apiFetch<{ run?: { id: number }; error?: string }>("/api/runs/new", {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        setError(res.error || `Could not create run (HTTP ${res.status}).`);
        return;
      }
      const id = res.data.run?.id;
      if (id) {
        router.push(overviewHref(id));
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
      if (selectedRunId === runId) setSelectedRun(null);
    } finally {
      busyRef.current = false;
      setBusyLocal(false);
    }
  }

  async function startAction(path: string, opts?: { openSteps?: boolean }) {
    if (!selectedRunId) return;
    if (busyRef.current || selectedRun?.is_running) {
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
    if (opts?.openSteps) {
      router.push(stepsHref(selectedRunId, true));
      return;
    }
    await loadSelectedRun(selectedRunId);
    await loadHome();
    void pollSelected(selectedRunId);
  }

  const locked = Boolean(data?.busy) || busyLocal;
  const runs = data?.runs || [];
  const isRunning = Boolean(
    selectedRun?.is_running ||
      selectedRun?.status === "running" ||
      selectedRun?.status === "setting_up",
  );

  return (
    <AppShell username={username}>
      <section className="hero">
        <h1>LibreLane Colab, in your browser</h1>
        <p>
          This web app mirrors <code>notebook.ipynb</code>: install LibreLane via Nix,
          enable supported PDKs under <code>~/.ciel</code>, configure the serial-parallel
          multiplier (<code>spm</code>), and run each implementation step (synthesis through
          LVS).
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
              <input name="name" defaultValue="spm" required disabled={locked} />
            </label>
            <label>
              Top module name
              <input
                name="top_module_name"
                defaultValue="spm"
                required
                disabled={locked}
                pattern="[A-Za-z_][A-Za-z0-9_$]*"
                title="Must match a module declared in designs/<name>.v"
              />
              <span className="meta">
                Must exist as <code>module …</code> in <code>designs/&lt;name&gt;.v</code>
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
          {error && !selectedRunId ? <pre className="error">{error}</pre> : null}
          <button type="submit" className="btn primary" disabled={locked}>
            {locked ? "Busy…" : "Create run"}
          </button>
        </form>
      </section>

      <section className="panel">
        <h2>Recent runs</h2>
        <p className="meta">
          Click a run name to open its flow steps. Use Overview for configuration, Verilog, and
          actions on this page.
        </p>
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
                  const topModule = run.top_module_name || run.design_name;
                  return (
                    <tr key={run.id} className={selected ? "selected" : undefined}>
                      <td>
                        <Link href={stepsHref(run.id)} className="run-name-link">
                          {run.name || topModule}
                        </Link>
                      </td>
                      <td>#{run.id}</td>
                      <td>
                        <span className={`status-pill status-${run.status}`}>{run.status}</span>
                      </td>
                      <td className="meta">{run.created_at}</td>
                      <td className="run-table-actions">
                        <Link href={overviewHref(run.id)} className="btn">
                          Overview
                        </Link>
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

      {selectedRunId ? (
        <>
          <section className="run-header panel">
            <h2>
              Run #{selectedRunId}
              {selectedRun
                ? ` — ${selectedRun.top_module_name || selectedRun.design_name}${
                    selectedRun.name &&
                    selectedRun.name !==
                      (selectedRun.top_module_name || selectedRun.design_name)
                      ? ` (${selectedRun.name})`
                      : ""
                  }`
                : ""}
            </h2>
            <p className={`status-pill status-${selectedRun?.status || "pending"}`}>
              {(selectedRun?.status || "pending").replaceAll("_", " ")}
            </p>
            {selectedRun?.error_message ? (
              <pre className="error">{selectedRun.error_message}</pre>
            ) : null}
            {error ? <pre className="error">{error}</pre> : null}
            <p className="meta">
              {isRunning
                ? selectedRun?.status === "setting_up"
                  ? "Configuring flow for this run…"
                  : "Step running…"
                : selectedRun?.work_dir
                  ? `Workdir: ${selectedRun.work_dir}`
                  : selectedRun?.artifacts_stored
                    ? "Artifacts stored in Postgres (workdir may have been pruned)."
                    : ""}
            </p>
            {(selectedRun?.disk_bytes != null || selectedRun?.db_bytes != null) && (
              <p className="meta">
                Run size — disk: <code>{formatBytes(selectedRun.disk_bytes)}</code>, DB:{" "}
                <code>{formatBytes(selectedRun.db_bytes)}</code>
              </p>
            )}
            <div className="actions">
              <Link href={stepsHref(selectedRunId)} className="btn primary">
                Open flow steps
              </Link>
            </div>
          </section>

          <section className="panel">
            <h2>Actions</h2>
            <div className="actions">
              <button
                type="button"
                className="btn"
                disabled={isRunning}
                onClick={() => void startAction(`/api/runs/${selectedRunId}/setup`)}
              >
                Setup PDK
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={isRunning}
                onClick={() =>
                  void startAction(`/api/runs/${selectedRunId}/run-all`, { openSteps: true })
                }
              >
                Run full flow
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={isRunning}
                onClick={() => void onDelete(selectedRunId)}
              >
                Delete run
              </button>
            </div>
          </section>

          <section className="panel">
            <h2>Configuration</h2>
            <ul className="config-list">
              <li>
                Top module:{" "}
                <code>{selectedRun?.top_module_name || selectedRun?.design_name}</code>
              </li>
              <li>
                PDK variant: <code>{selectedRun?.pdk}</code>
              </li>
              <li>
                Clock port/net: <code>clk</code>
              </li>
              <li>
                Clock period: <code>{selectedRun?.clock_period}</code> ns
              </li>
              <li>
                Workdir:{" "}
                <code>{selectedRun?.work_dir || "(created when the flow starts)"}</code>
              </li>
              <li>
                Disk / DB:{" "}
                <code>
                  {formatBytes(selectedRun?.disk_bytes)} / {formatBytes(selectedRun?.db_bytes)}
                </code>
              </li>
            </ul>
          </section>

          <section className="panel">
            <h2>
              Verilog —{" "}
              <code>
                {selectedRun?.top_module_name || selectedRun?.design_name || "…"}.v
              </code>
            </h2>
            <pre className="verilog">{selectedRun?.verilog_source || ""}</pre>
          </section>

          {selectedRun?.setup_log ? (
            <section className="panel">
              <h2>Setup log</h2>
              <pre className="log">{selectedRun.setup_log}</pre>
            </section>
          ) : null}
        </>
      ) : null}
    </AppShell>
  );
}
