// Shared types for the stats payload returned by the FastAPI bridge.

export interface UsageEvent {
  model?: string;
  path?: string;
  status?: number;
  elapsed_seconds?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  recorded_at?: number;
  tuning_profile?: string;
  stream?: boolean;
  [key: string]: unknown;
}

// Consecutive same-path rows without token counts (e.g. /v1/models polling) rendered as one.
export interface CollapsedRow {
  collapsed_path: string;
  count: number;
  representative: UsageEvent;
}

export interface CatalogEntry {
  alias: string;
  model: string;
  revision: string;
  runtime: string;
  gpu: string;
  gpu_count: number;
  context_tokens: number;
  enabled: boolean;
  status: string;
  active_tuning: string;
  tuning: Record<string, unknown>;
  state: string;
}

export interface StatsPayload {
  deployment: {
    alias: string;
    model: string;
    revision: string;
    runtime: string;
    gpu: string;
    gpu_count: number;
    context_tokens: number;
    status: string;
    health: string;
    serving_detail?: string;
    container_id?: string;
    heartbeat_age_seconds?: number | null;
    active_tuning: string;
    tuning: Record<string, unknown>;
    slots?: {
      slots: Record<
        string,
        {
          task: number;
          phase: string;
          progress?: number;
          n_tokens?: number;
          n_gen?: number;
          elapsed_s?: number;
          tok_per_s?: number;
          total_s?: number;
          total_tokens?: number;
          truncated?: boolean;
        }
      >;
      completions: { task: number; total_s: number; total_tokens: number }[];
    } | null;
    gate?: { waiting: number; active: number; slots: number } | null;
    members?: string[] | null;
    gate_aliases?: Record<string, { waiting: number; active: number; slots: number }> | null;
    /** Hot set the deployed target is CONFIGURED to serve (catalog truth).
     *  Survives scale-to-zero, where `members` goes null. */
    configured_members?: string[];
    /** One entry per hot-set CHANGE (consecutive identical sets collapsed).
     *  Boot time is when a serve-group change lands. Newest first. */
    boot_history?: { container_id: string; registered_at: number; alias: string; members: string[] }[];
  };
  /** Every GPU lane the dashboard can see: live serving rows first, then
   *  stopped registrations from the last 12h. Plus the always-on burn rate. */
  gpu_fleet?: {
    rows: {
      container_id: string;
      alias: string;
      members: string[];
      runtime: string;
      gpu: string;
      gpu_count: number;
      status: "serving" | "stopped";
      heartbeat_age_seconds?: number | null;
      registered_at?: number;
      slots?: unknown;
      gate?: unknown;
    }[];
    alive_gpu_counts: Record<string, number>;
    always_on_usd_per_hour: number;
    gpus_billed_while_idle: string;
  };
  usage: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    gpu_seconds: number;
    metered_today_usd: number | null;
    metered_month_to_date_usd: number | null;
    workspace_billed_month_usd: number | null;
    workspace_credits_month_usd: number | null;
    billing_daily_breakdown?: { day: string; resources: Record<string, number> }[];
    billing_resource_split?: Record<string, number>;
    billing_error: string | null;
    workspace_disabled: boolean;
    billing_updated_at: number | null;
  };
  catalog: CatalogEntry[];
  recent: UsageEvent[];
  events: UsageEvent[];
  event_count: number;
  model_profiles: string[];
  cost_compare?: CostComparePayload;
}

export interface ExternalCompare {
  model: string;
  provider: string;
  same_mix_cost_usd: number;
  same_mix_cost_cached_input_usd: number;
  usd_per_m_this_mix?: number | null;
  usd_per_m_this_mix_cached?: number | null;
  multiplier: number | null;
  input_usd_per_m?: number;
  output_usd_per_m?: number;
  verdict: string;
}

export interface AliasCompare {
  ledger?: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    actual_gpu_cost_usd: number;
    actual_gpu_cost_basis?: string;
  };
  per_token?: {
    usd_per_m_all_tokens: number | null;
    usd_per_m_output_tokens?: number | null;
    rate_in_share?: number | null;
    rate_out_share?: number | null;
    estimated?: {
      input_usd_per_m: number | null;
      output_usd_per_m: number | null;
      gpu_time_share?: { input: number; output: number };
      method?: string;
    };
    basis?: string;
  };
  external?: ExternalCompare[];
  error?: string;
}

export interface CostCurvePoint {
  mix: string;
  tokens_per_day: number;
  usd_per_m_blended: number;
  idle_share_of_bill: number;
}

export interface CostComparePayload {
  metered_month_to_date_usd: number | null;
  serving_cost_usd?: number | null;
  cost_curve?: CostCurvePoint[];
  app_per_token?: {
    usd_per_m_all_tokens: number | null;
    note?: string;
  };
  models: Record<string, AliasCompare>;
}