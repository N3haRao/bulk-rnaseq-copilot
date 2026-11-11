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
  source: "upload" | "example" | "geo" | "fastq";
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
  source: "model" | "rules" | "example" | "chat" | "setup_chat"; // chat = a rerun proposed in the Claude chat
  model: string | null;
  changed_by_user: boolean;
  setup_chat?: string; // id of the setup conversation that proposed it
}

// ---- Setup chat (open-weight model) ----
export interface SetupChatItem {
  role: "user" | "assistant";
  text: string;
  at?: string;
  setup?: Settings;
  changes?: string[];
  questions?: string[];
  source?: "model" | "rules";
  model?: string | null;
  problems?: string[];
  guardrail?: { name: string; matched: string; instead: string; message: string }[];
  fallback_reason?: string;
}

export interface SetupChatState {
  id: string;
  transcript: SetupChatItem[];
  setup: Settings;
  source: "model" | "rules";
  model: string | null;
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
  blocked?: boolean; // stopped by the guardrails (not a code-bank method), never ran
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

// ---- Optional analyses on a run (whatever the code bank's manifest offers) ----
export type ExtraModule = string;

export interface ExtraFormField {
  name: string; type: "number" | "integer" | "enum" | "bool" | "text" | "run_group"; label?: string; help?: string;
  default?: unknown; min?: number; max?: number; options?: { value: string; label: string }[];
}
export interface ExtraForm {
  module: string; title: string; description: string;
  inputs: { name: string; label?: string; accept?: string[]; multiple?: boolean; required?: boolean; help?: string }[];
  form: ExtraFormField[];
}
export interface ExtraColumn {
  label: string; value?: string; fallback?: string; template?: string;
  format?: "num0" | "num2" | "p" | "flag"; style?: "small" | "sign";
}

export interface ExtraAnalysis {
  id: string;
  module: ExtraModule;
  title: string;
  status: "queued" | "running" | "success" | "success_with_errors" | "failed" | "interrupted";
  created: string;
  finished: string | null;
  error: string | null;
  note: string | null;
  settings: Record<string, unknown>;
  inputs: { input: string; files: string[]; used: Record<string, string> }[];
  steps: { name: string; description: string; status: string }[];
  run_id?: string;
  headline?: string[];
  tiles?: { label: string; value: number | string | null; style?: "up" | "down" | null }[];
  figures?: Figure[];
  top_tables?: { title: string; columns: ExtraColumn[]; empty: string; rows: Record<string, string>[] }[];
  methods_md?: string | null;
  summary?: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  tables?: string[];
  validation?: { checks: Check[] } | null;
}

// ---- Cross-study comparison ----
export interface CompareRun { id: string; label: string; dataset: string | null; comparison: string; created: string }
export interface StudySpec {
  kind: "run" | "table"; label: string; flip?: boolean; job_id?: string; file?: number;
  organism?: string; comparison?: string; dataset?: string | null; file_name?: string;
}
export interface ComparisonItem { id: string; title: string; status: string; created: string; finished: string | null; studies: StudySpec[] }
export interface Corr { r: number | null; p: number | null; n: number }
export interface Fisher { p: number | null; odds_ratio: number | null }
export interface RrhoBest { quadrant: string; overlap: number; expected: number; padj: number; neg_log10_padj: number; top_genes: string[]; meaning: string; genes_file?: string }
export interface PathwayPair {
  collection: string; n_pathways: number; spearman_NES: number | null; n_significant_a: number; n_significant_b: number;
  n_significant_both: number; n_same_direction: number; n_opposite_direction: number; figure: string | null;
  top_shared: { pathway: string; NES_a: number; NES_b: number }[];
}
export interface ComparePair {
  study_a: string; study_b: string; n_shared_genes: number; n_significant_a: number; n_significant_b: number;
  n_significant_both: number; n_same_direction: number; n_opposite_direction: number;
  up_up: number; down_down: number; up_down: number; down_up: number; expected_up_up: number; expected_down_down: number;
  fisher_up_up: Fisher; fisher_down_down: Fisher; jaccard: number | null; direction_agreement: number | null;
  pearson_log2FC: Corr; spearman_log2FC: Corr; spearman_log2FC_significant_either: Corr;
  top_shared_up: { gene: string; log2FC_a: number; log2FC_b: number }[];
  top_shared_down: { gene: string; log2FC_a: number; log2FC_b: number }[];
  top_opposite: { gene: string; log2FC_a: number; log2FC_b: number }[];
  rrho: { status?: string; reason?: string; pattern?: string; best?: Record<string, RrhoBest>; n_genes?: number };
  pathways: PathwayPair[];
  figures: { scatter: string | null; overlap: string | null; rrho: string | null };
  tables: string[];
}
export interface StudyResult {
  label: string; source: string; comparison: string; organism: string; gene_id_type: string; flipped: boolean;
  n_genes: number; n_matched: number; n_significant: number; n_up: number; n_down: number;
}
export interface Comparison extends ComparisonItem {
  error: string | null; settings: { padj_cutoff: number; log2fc_cutoff: number; compare_pathways: boolean };
  steps: { name: string; description: string; status: string }[];
  run_id?: string; study_results?: StudyResult[]; match_method?: string; rank_by?: string; significance_rule?: string;
  n_genes_in_all_studies?: number; pairs?: ComparePair[]; figures?: { path: string; title: string; caption: string }[];
  pathway_collections?: { collection: string; n_pathways: number; n_significant_in_two_or_more: number;
    n_consistent_in_two_or_more: number; heatmap: string | null; table: string | null }[];
  pathways_status?: { status: string; reason?: string; gsea_source?: Record<string, string>; padj_cutoff?: number } | null;
  recurrent_genes?: Record<string, string>[]; warnings?: string[]; methods_md?: string | null; tables?: string[];
  validation?: { checks: Check[] } | null;
}

// ---- Import from GEO ----
export interface GeoInfo {
  accession: string;
  title: string;
  summary: string;
  overall_design: string;
  organisms: string[];
  library_strategies: string[];
  n_samples: number;
  url: string;
  samples: { geo_accession: string; title: string; characteristics: Record<string, string> }[];
  counts_options: { id: string; label: string; source: "ncbi" | "supplementary"; file: string; likely: boolean }[];
  recommended: string | null;
  warnings: string[];
}

export interface KeyStatus {
  configured: boolean;
  source: "environment" | "keychain" | "file" | null;
  masked: string | null;
  model: string;
  can_change: boolean;
  storage: string | null;
}

export class ApiError extends Error {}

// ---- Start from FASTQ (files already on the server) ----
export interface FromFastq { job_id: string; folder: string; modules: Record<string, { available: boolean; why?: string }> }
export interface FastqReference {
  species: string; label: string; files_ready: boolean; star_index: boolean; salmon_index: boolean; dir: string;
  download: { status?: string; file?: string | null; error?: string };
}
export interface FastqFolderItem { name: string; n_files: number; gb: number; modified: string }
export interface FastqHome {
  incoming: string; folders: FastqFolderItem[]; references: FastqReference[]; steps: { id: string; label: string }[];
  required_steps: string[]; max_threads: number; jobs: FastqJob[];
}
export interface FastqSample { sample: string; r1: string | null; r2: string | null; gb: number }
export interface FastqFolder {
  folder: string; path: string; samples: FastqSample[]; layout: string; problems: string[]; gb: number;
  suggestion: { groups: Record<string, string>; control: string | null; origin: string };
}
export interface FastqAssist {
  groups: Record<string, string>; control: string | null; batch: Record<string, string>; species: string | null; note: string; origin: string;
}
export interface FastqSpec {
  folder: string; groups: Record<string, string>; batch: Record<string, string>; control: string | null; species: string | null;
  steps: string[]; threads: number; description: string; setup_origin?: string | null;
}
export interface FastqCheck {
  ok: boolean; lines: { status: "pass" | "note" | "fail"; text: string }[]; steps: string[]; threads: number;
  estimate_hours: number; need_gb: number; free_gb: number;
}
export interface FastqStep { id: string; label: string; status: string; seconds: number | null; facts: string[] }
export interface FastqJob {
  id: string; status: string; created: string; started: string | null; finished: string | null; error: string | null;
  folder: string; species: string; control: string; groups: Record<string, string>; steps: string[]; threads: number;
  layout: string; n_samples: number; gb: number; description: string; upload_id: string | null;
  progress?: number; step_list?: FastqStep[]; current?: string | null; files?: { label: string; path: string }[];
  comparisons?: string[]; log_tail?: string[];
}

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
  setupChat: (id: string) => request<SetupChatState>(`/api/uploads/${id}/setup-chat`),
  sendSetupChat: (id: string, text: string, current?: Settings) =>
    request<SetupChatState>(`/api/uploads/${id}/setup-chat`, json({ text, current: current ?? null })),
  resetSetupChat: (id: string) => request<SetupChatState>(`/api/uploads/${id}/setup-chat`, { method: "DELETE" }),
  agentStatus: () => request<{ available: boolean; model: string; detail: string }>("/api/agent/status"),
  agentSetup: (id: string, condition?: string) =>
    request<AgentSetup>(`/api/uploads/${id}/agent/setup` + (condition ? `?condition=${encodeURIComponent(condition)}` : ""),
      { method: "POST" }),
  agentExplainCheck: (id: string) => request<AgentExplain>(`/api/uploads/${id}/agent/explain-check`, { method: "POST" }),
  settings: () => request<{ anthropic: KeyStatus }>("/api/settings"),
  saveKey: (key: string) => request<{ anthropic: KeyStatus; message: string }>("/api/settings/anthropic-key", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }),
  }),
  deleteKey: () => request<{ anthropic: KeyStatus }>("/api/settings/anthropic-key", { method: "DELETE" }),
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
  geoInspect: (accession: string) => request<GeoInfo>("/api/geo/inspect", json({ accession })),
  geoImport: (accession: string, countsOption: string) =>
    request<Upload & { description: string }>("/api/geo/import", json({ accession, counts_option: countsOption })),
  extras: (jobId: string) =>
    request<{ available: Record<string, string>; forms: Record<string, ExtraForm>; analyses: Record<string, ExtraAnalysis | null>;
      from_fastq: FromFastq | null }>(`/api/runs/${jobId}/extras`),
  startExtraFromFastq: (jobId: string, module: ExtraModule) =>
    request<ExtraAnalysis>(`/api/runs/${jobId}/extras/${module}/from-fastq`, { method: "POST" }),
  fastqHome: () => request<FastqHome>("/api/fastq/folders"),
  fastqFolder: (name: string) => request<FastqFolder>(`/api/fastq/folders/${encodeURIComponent(name)}`),
  fastqAssist: (folder: string, description: string) => request<FastqAssist>("/api/fastq/assist", json({ folder, description })),
  fastqReference: (species: string) => request<FastqReference>(`/api/fastq/references/${species}`, { method: "POST" }),
  fastqCheck: (spec: FastqSpec) => request<FastqCheck>("/api/fastq/check", json(spec)),
  fastqStart: (spec: FastqSpec) => request<FastqJob>("/api/fastq/jobs", json(spec)),
  fastqJob: (id: string) => request<FastqJob>(`/api/fastq/jobs/${id}`),
  fastqCancel: (id: string) => request<FastqJob>(`/api/fastq/jobs/${id}/cancel`, { method: "POST" }),
  fastqToDataset: (id: string) => request<Upload>(`/api/fastq/jobs/${id}/dataset`, { method: "POST" }),
  extra: (jobId: string, module: ExtraModule) => request<ExtraAnalysis>(`/api/runs/${jobId}/extras/${module}`),
  startExtra: (jobId: string, module: ExtraModule, files: Record<string, File[]>, fields: Record<string, string>) => {
    const form = new FormData();
    Object.entries(files).forEach(([name, fs]) => fs.forEach((f) => form.append(name, f)));
    Object.entries(fields).forEach(([k, v]) => form.append(k, v));
    return request<ExtraAnalysis>(`/api/runs/${jobId}/extras/${module}`, { method: "POST", body: form });
  },
  comparisons: () => request<{ available: boolean; comparisons: ComparisonItem[]; runs: CompareRun[] }>("/api/comparisons"),
  comparison: (id: string) => request<Comparison>(`/api/comparisons/${id}`),
  startComparison: (spec: { title?: string; studies: StudySpec[]; padj_cutoff: number; log2fc_cutoff: number; compare_pathways: boolean },
    files: File[]) => {
    const form = new FormData();
    form.append("spec", JSON.stringify(spec));
    files.forEach((f) => form.append("files", f));
    return request<Comparison>("/api/comparisons", { method: "POST", body: form });
  },
  options: () =>
    request<{ parameters: Record<string, { type: string; values?: string[]; default: unknown; description?: string }> }>("/api/options"),
};

export const chatFileUrl = (jobId: string, convId: string, path: string, download = false) =>
  `/api/runs/${jobId}/chats/${convId}/files/${path}` + (download ? "?download=true" : "");

export const fileUrl = (jobId: string, path: string, download = false) =>
  `/api/runs/${jobId}/files/${path}` + (download ? "?download=true" : "");

export const codeZipUrl = (jobId: string, includeData = false) =>
  `/api/runs/${jobId}/code` + (includeData ? "?include_data=true" : "");

export const extraFileUrl = (jobId: string, module: ExtraModule, path: string, download = false) =>
  `/api/runs/${jobId}/extras/${module}/files/${path}` + (download ? "?download=true" : "");

export const compareFileUrl = (id: string, path: string, download = false) =>
  `/api/comparisons/${id}/files/${path}` + (download ? "?download=true" : "");
