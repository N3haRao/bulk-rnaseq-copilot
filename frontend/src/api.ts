// Typed client for the FastAPI backend (backend/app/main.py).

export type Status = "pass" | "warning" | "fail";

export interface Check {
  id: string;
  status: Status;
  message: string;
  group: "files" | "design";
}

export interface Column {
  name: string;
  levels: Record<string, number>;
  n_levels: number;
  numeric: boolean;
  unique_per_sample: boolean;
  n_missing: number;
}

export interface Proposal {
  sample_id_column: string;
  condition: string | null;
  reference_level: string | null;
  test_level: string | null;
  covariates: string[];
  reasons: string[];
  warnings: string[];
  reference_guessed: boolean;
}

export interface Upload {
  id: string;
  created: string;
  source: "upload" | "example";
  example: string | null;
  counts_original_name: string;
  metadata_original_name: string;
  metadata_preview: { columns: string[]; rows: string[][]; n_rows: number };
  counts_preview: { columns: string[]; n_columns: number; first_rows: string[][] };
  columns: Column[];
  proposal: Proposal;
  example_settings: Record<string, unknown> | null;
}

export interface Settings {
  condition: string;
  reference_level: string;
  test_level: string;
  covariates: string[];
  organism: string;
  run_name: string;
  advanced: Record<string, unknown>;
}

export interface CheckResult {
  status: Status;
  n_fail: number;
  n_warning: number;
  checks: Check[];
  settings: Settings;
}

export interface Step {
  id: number;
  name: string;
  description: string;
  critical: boolean;
  status: "pending" | "running" | "success" | "failed" | "skipped" | "cancelled";
  seconds: number | null;
}

export type JobStatus = "queued" | "running" | "success" | "success_with_errors" | "failed" | "cancelled" | "interrupted";

export interface Job {
  id: string;
  upload_id: string;
  status: JobStatus;
  created: string;
  started: string | null;
  finished: string | null;
  settings: Settings;
  dataset: string | null;
  error: string | null;
  steps?: Step[];
  codebank?: { version: string; commit: string | null; tag: string | null; uncommitted_changes: boolean };
}

export interface Example {
  name: string;
  title: string;
  available: boolean;
  make_command: string;
  config: Record<string, unknown>;
}

export interface Figure {
  path: string;
  title: string;
  caption: string;
}

export interface TopGene {
  gene_id: string;
  gene_symbol: string | null;
  log2FoldChange: number;
  padj: number;
}

export interface EnrichmentCollection {
  source: string;
  folder: string;
  n_gene_sets: number;
  n_gene_sets_tested: number;
  ora_up: string | { n_significant: number };
  ora_down: string | { n_significant: number };
  gsea: { n_significant: number };
}

export interface Results {
  run_id: string;
  summary: {
    status: string;
    comparison: string;
    design_formula: string;
    warnings: string[];
    n_samples: number;
    group_sizes: Record<string, number>;
    n_genes_tested: number;
    significance_rule: string;
    n_significant: number;
    n_up: number;
    n_down: number;
    top_up: TopGene[];
    top_down: TopGene[];
    pca_percent_variance: Record<string, number>;
  };
  resolved_params: Record<string, unknown> & {
    condition: string;
    reference_level: string;
    test_level: string;
    covariates: string[];
    organism: string;
  };
  qc: { summary: Record<string, unknown> | null; metrics: Record<string, string>[] };
  exploratory: {
    summary: {
      percent_variance: Record<string, number>;
      pc_association: { variable: string; r2_PC1: number; r2_PC2: number }[];
      flagged_samples: { sample_id: string; flag: string; message: string }[];
    } | null;
    outliers: Record<string, string>[];
  };
  de: { padj_cutoff: number; log2fc_cutoff: number; lfc_shrinkage_used: string; sign_convention: string } | null;
  enrichment: {
    status: string;
    reason?: string;
    collections?: Record<string, EnrichmentCollection>;
  } | null;
  provenance: {
    packages: Record<string, string>;
    r_version: string;
    code_bank: { version: string; commit: string | null; tag: string | null };
    rerun_command: string;
  } | null;
  figures: Record<"qc" | "samples" | "de" | "heatmap", Figure[]>;
  methods_md: string | null;
  report_available: boolean;
}

export interface DeRow {
  gene_id: string;
  gene_symbol: string;
  gene_name: string;
  baseMean: number | null;
  log2FoldChange: number | null;
  padj: number | null;
  pvalue: number | null;
  significant: boolean;
  direction: string;
  note: string;
  [key: string]: unknown;
}

export interface DeTable {
  columns: string[];
  total: number;
  page: number;
  page_size: number;
  rows: DeRow[];
}

// ---- Executor agent ----
export interface AgentSetup {
  source: "model" | "rules";
  model: string | null;
  condition: string | null;
  reference_level: string | null;
  test_level: string | null;
  covariates: string[];
  explanation: string;
  reasons: string[];
  questions: string[];
  warnings: string[];
  fallback_reason?: string;
  differs_from_rules?: boolean;
}

export interface AgentExplain {
  source: "model" | "rules";
  model: string | null;
  items: { id: string; explanation: string; how_to_fix: string }[];
  fallback_reason?: string;
}

export interface AgentWarnings {
  source: "model" | "rules";
  model: string | null;
  headline: string | null;
  items: {
    index: number;
    title: string | null;
    explanation: string;
    what_to_do: string | null;
    severity: "info" | "caution" | "serious";
    original: string;
  }[];
  fallback_reason?: string;
}

export interface ReportStatus {
  available: boolean;
  source?: "model" | "rules";
  model?: string | null;
  section_sources?: Record<string, "model" | "rules">;
  n_removed?: number;
  fallback_reason?: string | null;
  written?: string;
  summary?: string;
  key_findings?: string[];
  pdf_ready?: boolean;
}

export interface SetupOrigin {
  source: "model" | "rules" | "example" | "chat";
  model: string | null;
  changed_by_user: boolean;
}

// ---- Chat (interpreter agent) ----
export interface ChatItem {
  id?: string; // tool call id while it runs
  role: "user" | "assistant" | "tool" | "error";
  text?: string;
  name?: string;
  title?: string;
  language?: "python" | "r" | null;
  code?: string | null;
  output?: string;
  ok?: boolean;
  error?: boolean;
  files?: string[];
  rerun?: { settings: Settings; changes: Record<string, unknown>; reason: string; upload_id: string };
  pending?: boolean;
}

export type ChatEvent =
  | { type: "text"; text: string }
  | { type: "thinking" }
  | { type: "tool_start"; id: string; name: string; title: string; code?: string | null; language?: string | null }
  | ({ type: "tool_end"; id: string } & ChatItem)
  | { type: "error"; text: string }
  | { type: "done" };

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  examples: () => request<Example[]>("/api/examples"),
  useExample: (name: string) => request<Upload>(`/api/examples/${name}`, { method: "POST" }),
  upload: (counts: File, metadata: File) => {
    const form = new FormData();
    form.append("counts", counts);
    form.append("metadata", metadata);
    return request<Upload>("/api/uploads", { method: "POST", body: form });
  },
  getUpload: (id: string, condition?: string) =>
    request<Upload>(`/api/uploads/${id}` + (condition ? `?condition=${encodeURIComponent(condition)}` : "")),
  check: (id: string, s: Settings) => request<CheckResult>(`/api/uploads/${id}/check`, json(s)),
  startRun: (uploadId: string, s: Settings, origin?: SetupOrigin) =>
    request<Job>("/api/runs", json({ upload_id: uploadId, ...s, setup_origin: origin ?? null })),
  agentStatus: () => request<{ available: boolean; model: string; detail: string }>("/api/agent/status"),
  agentSetup: (id: string, condition?: string) =>
    request<AgentSetup>(`/api/uploads/${id}/agent/setup` + (condition ? `?condition=${encodeURIComponent(condition)}` : ""),
      { method: "POST" }),
  agentExplainCheck: (id: string) => request<AgentExplain>(`/api/uploads/${id}/agent/explain-check`, { method: "POST" }),
  chatStatus: () => request<{ configured: boolean; model: string; detail: string }>("/api/chat/status"),
  chats: (jobId: string) => request<{ id: string; created: string; title: string | null }[]>(`/api/runs/${jobId}/chats`),
  newChat: (jobId: string) => request<{ id: string; transcript: ChatItem[] }>(`/api/runs/${jobId}/chats`, { method: "POST" }),
  chat: (jobId: string, id: string) => request<{ id: string; transcript: ChatItem[] }>(`/api/runs/${jobId}/chats/${id}`),
  // Streams Claude's answer: calls onEvent for every server-sent event until the answer is complete
  sendChat: async (jobId: string, id: string, text: string, onEvent: (e: ChatEvent) => void) => {
    const res = await fetch(`/api/runs/${jobId}/chats/${id}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    if (!res.ok || !res.body) throw new ApiError(`${res.status} ${res.statusText}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (line) onEvent(JSON.parse(line.slice(6)) as ChatEvent);
      }
    }
  },
  reportStatus: (jobId: string) => request<ReportStatus>(`/api/runs/${jobId}/report`),
  buildReport: (jobId: string, refresh = false) =>
    request<ReportStatus>(`/api/runs/${jobId}/report` + (refresh ? "?refresh=true" : ""), { method: "POST" }),
  agentRunWarnings: (jobId: string) => request<AgentWarnings>(`/api/runs/${jobId}/agent/warnings`, { method: "POST" }),
  runs: () => request<Job[]>("/api/runs"),
  run: (id: string) => request<Job>(`/api/runs/${id}`),
  cancel: (id: string) => request<Job>(`/api/runs/${id}/cancel`, { method: "POST" }),
  results: (id: string) => request<Results>(`/api/runs/${id}/results`),
  de: (id: string, q: Record<string, string | number>) =>
    request<DeTable>(`/api/runs/${id}/de?` + new URLSearchParams(Object.entries(q).map(([k, v]) => [k, String(v)]))),
  enrichment: (id: string, folder: string, kind: string) =>
    request<{ rows: Record<string, string>[]; figure: string | null }>(`/api/runs/${id}/enrichment/${folder}/${kind}`),
  options: () =>
    request<{ parameters: Record<string, { type: string; values?: string[]; default: unknown }> }>("/api/options"),
};

export const chatFileUrl = (jobId: string, convId: string, path: string, download = false) =>
  `/api/runs/${jobId}/chats/${convId}/files/${path}` + (download ? "?download=true" : "");

export const fileUrl = (jobId: string, path: string, download = false) =>
  `/api/runs/${jobId}/files/${path}` + (download ? "?download=true" : "");
