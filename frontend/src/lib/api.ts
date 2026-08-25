export const DJANGO_ORIGIN =
  process.env.DJANGO_ORIGIN || process.env.BACKEND_URL || "http://127.0.0.1:8000";

export type StepCatalogItem = {
  step_id: string;
  title: string;
  description: string;
};

export type FlowStep = {
  order: number;
  step_id: string;
  title: string;
  status: string;
  log: string;
  summary: string;
  output: Record<string, unknown>;
  has_output?: boolean;
  can_download_zip?: boolean;
  can_download_preview_source?: boolean;
  preview_source_name?: string;
  description?: string;
};

export type StorageInfo = {
  user_disk_bytes: number;
  user_db_bytes: number;
  user_total_bytes: number;
  user_quota_bytes: number;
  run_budget_bytes: number;
  min_free_bytes: number;
  free_bytes: number;
  retention_days: number;
  runs_root: string;
  user_used_pct: number;
};

export type FlowRun = {
  id: number;
  name: string;
  status: string;
  design_name: string;
  top_module_name?: string;
  pdk: string;
  pdk_family: string;
  pdk_root: string;
  clock_period: number;
  work_dir: string;
  temp_folder_name: string;
  current_step_index: number;
  error_message: string;
  setup_log: string;
  artifacts_stored: boolean;
  disk_bytes?: number;
  db_bytes?: number;
  created_at: string | null;
  updated_at: string | null;
  is_running: boolean;
  owner_username?: string | null;
  steps?: FlowStep[];
  verilog_source?: string;
  verilog_files?: string[];
  module_names?: string[];
};

export type HomePayload = {
  username: string;
  librelane_version: string;
  default_pdk: string;
  pdk_variants: string[];
  storage?: StorageInfo;
  step_catalog: StepCatalogItem[];
  busy: boolean;
  runs: FlowRun[];
};

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<{ ok: boolean; status: number; data: T; error?: string; redirect?: string }> {
  const isFormData =
    typeof FormData !== "undefined" && init?.body instanceof FormData;
  const res = await fetch(path, {
    credentials: "same-origin",
    redirect: "follow",
    ...init,
    headers: {
      Accept: "application/json",
      "X-Requested-With": "XMLHttpRequest",
      ...(init?.body && !isFormData ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });
  const text = await res.text();
  let data = {} as T & { error?: string; redirect?: string };
  try {
    data = (text ? JSON.parse(text) : {}) as T & { error?: string; redirect?: string };
  } catch {
    data = {
      error: res.ok
        ? "Server returned a non-JSON response."
        : `Request failed (HTTP ${res.status}). Is the Django API running on :8000?`,
    } as T & { error?: string; redirect?: string };
  }
  if (res.status === 401 && data.redirect) {
    window.location.assign(data.redirect);
  }
  return {
    ok: res.ok,
    status: res.status,
    data,
    error: data.error,
    redirect: data.redirect,
  };
}
