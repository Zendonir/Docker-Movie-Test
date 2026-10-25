export type FileStatus = "unchecked" | "ok" | "warning" | "error" | "critical" | "incomplete";
export type Severity = "info" | "incomplete" | "warning" | "error" | "critical";
export type Method = "quick" | "container" | "full";
export type Precision = "exact" | "range" | "approx" | "unknown";

export interface MediaFileSummary {
  id: number;
  library_id: number;
  library: string | null;
  filename: string;
  rel_path: string;
  directory: string;
  path: string;
  extension: string;
  size: number;
  duration: number | null;
  container_format: string | null;
  video_codec: string | null;
  video_resolution: string | null;
  audio_codecs: string | null;
  status: FileStatus;
  status_label: string;
  check_level: string;
  check_level_label: string;
  methods_done: Method[];
  error_count: number;
  warning_count: number;
  last_checked_at: string | null;
  first_seen_at: string;
  present: boolean;
  missing_since: string | null;
  changed_at: string | null;
  moved_from: string | null;
  status_note: string | null;
}

export interface StreamInfo {
  index: number;
  type: string;
  codec_name: string | null;
  codec_long_name: string | null;
  profile: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  pix_fmt: string | null;
  bit_depth: number | null;
  channels: number | null;
  channel_layout: string | null;
  sample_rate: number | null;
  language: string | null;
  title: string | null;
  bit_rate: number | null;
  duration: number | null;
  is_default: boolean;
  forced: boolean;
  attached_pic: boolean;
  decodable: boolean;
}

export interface DetectedError {
  id: number;
  result_id: number;
  method: Method;
  method_label: string;
  error_type: string;
  error_type_label: string;
  severity: Severity;
  heuristic: boolean;
  stream_index: number | null;
  stream_type: string | null;
  description: string;
  time_start: number | null;
  time_end: number | null;
  time_precision: Precision;
  time_source: string | null;
  byte_offset: number | null;
  byte_end: number | null;
  tool: string;
  raw_message: string | null;
  occurrences: number;
  extra: Record<string, unknown>;
}

export interface ScanResult {
  id: number;
  method: Method;
  method_label: string;
  scanner_version: number;
  outcome: string;
  outcome_label: string;
  status: FileStatus;
  status_label: string;
  is_current: boolean;
  job_id: number | null;
  started_at: string;
  finished_at: string;
  duration_s: number;
  critical_count: number;
  error_count: number;
  warning_count: number;
  summary: string | null;
  tools: Record<string, unknown>;
  details: Record<string, unknown>;
  decoded_duration: number | null;
  container_duration: number | null;
  has_log: boolean;
  raw_message_count: number;
  fingerprint_version: number;
}

export interface Assessment {
  category: "replace" | "repair" | "optional" | "ok" | "unchecked";
  label: string;
  action: string;
  reasons: string[];
  reason_text: string;
}

export interface FileDetail extends MediaFileSummary {
  assessment: Assessment;
  quick_hash: string | null;
  full_hash: string | null;
  fingerprint_version: number;
  bit_rate: number | null;
  streams: StreamInfo[];
  current_results: Partial<Record<Method, ScanResult>>;
  errors: DetectedError[];
  history: ScanResult[];
  repair_enabled: boolean;
  repair_actions: Record<string, string>;
}

export interface Library {
  id: number;
  name: string;
  path: string;
  enabled: boolean;
  exclude_patterns: string[];
  created_at: string;
  last_inventory_at: string | null;
  last_inventory_stats: Record<string, unknown> | null;
  file_count: number;
  total_size: number;
  storage: { total: number; free: number; used: number } | null;
  reachable: boolean;
}

export interface Job {
  id: number;
  name: string;
  task: string;
  task_label: string;
  methods: Method[];
  library_ids: number[];
  file_count_selected: number;
  force: boolean;
  priority: number;
  state: string;
  phase: string;
  pause_requested: boolean;
  cancel_requested: boolean;
  trigger: string;
  schedule_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  total_files: number;
  done_files: number;
  skipped_files: number;
  failed_files: number;
  total_bytes: number;
  done_bytes: number;
  files_with_errors: number;
  files_with_warnings: number;
  findings_count: number;
  resume_count: number;
  active_seconds: number;
  message: string | null;
}

export interface JobDetail extends Job {
  item_counts: Record<string, number>;
  events: { ts: string; level: string; message: string }[];
}

export interface LiveJob {
  id: number;
  name: string;
  state: string;
  phase: string;
  priority: number;
  methods: Method[];
  total_files: number;
  done_files: number;
  skipped_files: number;
  failed_files: number;
  total_bytes: number;
  processed_bytes: number;
  files_with_errors: number;
  files_with_warnings: number;
  findings_count: number;
  speed_bps: number | null;
  eta_seconds: number | null;
  pause_requested: boolean;
  parallel: number;
  pausing_after_file: boolean;
  current: { file_id: number; filename: string | null; size: number; method: string | null; fraction: number; paused: boolean }[];
}

export interface LiveState {
  jobs: LiveJob[];
  running: number;
}

export interface Stats {
  total_files: number;
  total_size: number;
  fully_checked: number;
  fully_checked_bytes: number;
  checked_files: number;
  checked_bytes: number;
  unchecked: number;
  ok_full: number;
  status: Record<FileStatus, number>;
  missing: number;
  active_jobs: number;
  scanner_paused: boolean;
  storage: { library_id: number; name: string; path: string; storage: { total: number; free: number; used: number } | null }[];
}

export interface Schedule {
  id: number;
  name: string;
  enabled: boolean;
  kind: "daily" | "weekly" | "monthly" | "custom";
  cron: string;
  task: string;
  task_label: string;
  methods: Method[];
  library_ids: number[];
  priority: number;
  created_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
  last_job_id: number | null;
  last_message: string | null;
}

export type Settings = Record<string, unknown> & {
  max_concurrent_scans: number;
  ffmpeg_threads: number;
  nice_level: number;
  io_priority: string;
  read_rate_limit: number;
  hwaccel: string;
  strict_decode: boolean;
  pause_mode: "immediate" | "after_file";
  scanner_paused: boolean;
  default_methods: Method[];
  container_ebml: boolean;
  container_mkvmerge: boolean;
  container_ffmpeg_demux: boolean;
  container_remux_check: boolean;
  ts_jump_warning_s: number;
  ts_jump_error_s: number;
  track_gap_warning_s: number;
  duration_tolerance_s: number;
  duration_tolerance_pct: number;
  hash_mode: "none" | "quick" | "full";
  exclude_patterns: string[];
  extensions: string[];
  follow_symlinks: boolean;
  auto_resume_after_restart: boolean;
  max_findings_per_result: number;
  stall_timeout_s: number;
  keep_scan_logs: boolean;
  auth_enabled: boolean;
  auth_username: string | null;
  auth_password_set: boolean;
};

export interface Paged<T> {
  total: number;
  items: T[];
  page?: number;
  page_size?: number;
}

// ---------------------------------------------------------------- MKV-Optimizer
export interface OptTrack {
  id: number;
  type: string;
  codec: string | null;
  family: string | null;
  language: string;
  language_label: string;
  name: string | null;
  default: boolean;
  forced: boolean;
  channels: number | null;
}

export interface TrackRule {
  mode: "language" | "track_id";
  languages: string[];
  codecs: string[];
  track_ids: number[];
}

export interface OptRules {
  audio: TrackRule;
  subtitles: TrackRule;
}

export interface OptPlan {
  changed: boolean;
  skip_reason: string | null;
  warnings?: string[];
  decisions?: { track_id: number; type: string; keep: boolean; reason: string }[];
  kept?: number[];
  removed?: number[];
  default_track?: number | null;
  track_order?: number[] | string[] | null;
  predicted_saved?: number | null;
  predicted_exact?: boolean;
  predicted_unknown?: number;
  conversions?: { track_id: number; language: string; source_codec: string | null; channels_in: number | null; channels_out: number | null; track_name: string }[];
  mode?: string;
}

export interface OptFile {
  file_id: number;
  filename: string;
  directory: string;
  size: number;
  status: FileStatus;
  analyzed: boolean;
  ok: boolean | null;
  error: string | null;
  duration: number | null;
  title: string | null;
  tracks: OptTrack[];
  plan: OptPlan | null;
}

export interface Choice { value: string; label: string; count: number }

export interface OptPreview {
  total: number;
  changed_ids: number[];
  unanalyzed: number;
  choices: { audio_languages: Choice[]; audio_codecs: Choice[]; subtitle_languages: Choice[]; subtitle_codecs: Choice[] };
  items: OptFile[];
  predictions: Record<string, number | null>;
  prediction: { saved: number; files_with_unknown: number; exact: boolean };
}

export interface OptStatus {
  tools: { mkvmerge: string | null; ffmpeg: string | null };
  encoders: Record<string, string | null>;
  output_root: string;
  modes: Record<string, { label: string; available: boolean; reason: string | null }>;
  ops: Record<string, unknown> & { output_mode: string; protected_languages: string[] };
  allow_modify_originals: boolean;
  last_rules: OptRules;
  libraries: { id: number; name: string; path: string }[];
}

export interface AnalyzeState { running: boolean; total: number; done: number; analyzed: number; errors: number; message: string | null; elapsed: number | null }

export interface OptProfile {
  id: number;
  name: string;
  builtin: boolean;
  codec: string;
  codec_label: string;
  bitrate_kbps: number | null;
  max_channels: number | null;
  sample_rate: number | null;
  source_codecs: string[];
  source_languages: string[];
  mode: "replace" | "add";
  track_name: string | null;
  filename_pattern: string;
  encoder_available: boolean;
}

export interface OptJob {
  id: number;
  kind: "remux" | "audio" | "repair";
  name: string;
  state: string;
  pause_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  total_files: number;
  done_files: number;
  skipped_files: number;
  failed_files: number;
  total_bytes: number;
  done_bytes: number;
  saved_bytes: number;
  resume_count: number;
  output_mode: string;
  message: string | null;
  predicted_saved: number | null;
  predicted_unknown_files: number;
}

export interface OptLiveJob extends OptJob {
  processed_bytes: number;
  speed_bps: number | null;
  eta_seconds: number | null;
  current: { item_id: number; file_id: number; name: string; size: number; phase: string; fraction: number; eta_seconds: number | null; elapsed: number; paused: boolean } | null;
}
