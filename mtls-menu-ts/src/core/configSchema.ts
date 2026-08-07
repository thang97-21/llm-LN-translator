// Every editable knob in config.yaml, hand-described rather than scraped
// from the file's own comments: the source comments run multi-paragraph in
// places (the compaction ladder alone is ~15 lines) and the Configuration
// screen needs one terse line per field, not a wall of prose under each row.
// `path` must match the dot-joined YAML key nesting exactly — it's the join
// key against configFile.ts's line index, so a typo here just silently
// fails to find the field rather than throwing.

export type ConfigFieldKind = 'boolean' | 'integer' | 'float' | 'enum' | 'text';

export type ConfigFieldSpec = {
  readonly path: string;
  readonly section: string;
  readonly menu: string;
  readonly label: string;
  readonly description: string;
  readonly kind: ConfigFieldKind;
  readonly choices?: readonly string[];
  readonly choiceLabels?: readonly string[];
  readonly min?: number;
  readonly max?: number;
};

const MODEL_CHOICES = ['deepseek-v4-pro', 'deepseek-v4-flash'] as const;
const MODEL_LABELS = ['DeepSeek V4 Pro', 'DeepSeek V4 Flash'] as const;
const PROVIDER_CHOICES = ['deepseek', 'qwen'] as const;
const PROVIDER_LABELS = ['DeepSeek', 'Qwen'] as const;
const QWEN_MODEL_CHOICES = ['qwen3.8-max', 'qwen3.7-plus', 'qwen3.7-flash'] as const;
const QWEN_MODEL_LABELS = ['Qwen 3.8 Max', 'Qwen 3.7 Plus', 'Qwen 3.7 Flash'] as const;
const ENDPOINT_CHOICES = ['https://api.deepseek.com/anthropic', 'https://api.deepseek.com'] as const;
const ENDPOINT_LABELS = ['Anthropic', 'OpenAI'] as const;
const EFFORT_CHOICES = ['max', 'high', 'medium', 'low'] as const;

export const CONFIG_FIELDS: readonly ConfigFieldSpec[] = [
// ─── Project ─────────────────────────────────────────────────────────────────────
  { path: 'project.target_language', menu: 'Project', section: 'Project', label: 'Target Language', kind: 'text',
    description: "Not actually read at runtime — get_target_language() in src/common/config.py always returns 'en' regardless of this value." },
  { path: 'project.name', menu: 'Project', section: 'Project', label: 'Project Name', kind: 'text', description: 'Display name for this pipeline instance. Cosmetic only.' },
  { path: 'project.version', menu: 'Project', section: 'Project', label: 'Config Version', kind: 'text', description: 'Version tag for this config file. Cosmetic only, not read for compatibility checks.' },

// ─── Paths ───────────────────────────────────────────────────────────────────────
  { path: 'paths.input_dir', menu: 'Paths', section: 'File System Paths', label: 'Input Directory', kind: 'text', description: 'Where extract/run look for source EPUBs.' },
  { path: 'paths.work_dir', menu: 'Paths', section: 'File System Paths', label: 'Work Directory', kind: 'text', description: 'Per-volume working directories (JP/EN/manifest.json/etc.) live under here.' },
  { path: 'paths.output_dir', menu: 'Paths', section: 'File System Paths', label: 'Output Directory', kind: 'text', description: 'Final assembled EPUBs are written here.' },
  { path: 'paths.prompt_dir', menu: 'Paths', section: 'File System Paths', label: 'Prompt Directory', kind: 'text', description: 'Base directory for the master translation prompt XML files.' },
  { path: 'paths.log_dir', menu: 'Paths', section: 'File System Paths', label: 'Log Directory', kind: 'text', description: 'Debug and cost logs are written here.' },

// ─── Prep ────────────────────────────────────────────────────────────────────────
  { path: 'prep.model', menu: 'Prep', section: 'Context Build', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS,
    description: 'Model for the single call that fills context.xml. Flash trades instruction-following for throughput — Pro is required for reliable 15-block structured output.' },
  { path: 'prep.endpoint', menu: 'Prep', section: 'Context Build', label: 'Endpoint', kind: 'enum', choices: ENDPOINT_CHOICES, choiceLabels: ENDPOINT_LABELS,
    description: 'The client only speaks the Anthropic SDK request format — leave this on Anthropic.' },
  { path: 'prep.api_key_env', menu: 'Prep', section: 'Context Build', label: 'API Key Variable', kind: 'text', description: 'Environment variable holding the DeepSeek API key for prep calls.' },
  { path: 'prep.prompt', menu: 'Prep', section: 'Context Build', label: 'Prompt File', kind: 'text', description: "Prep prompt XML that fills the Librarian's barebone context.xml." },
  { path: 'prep.thinking_budget', menu: 'Prep', section: 'Context Build', label: 'Thinking Budget', kind: 'integer', min: 0, description: 'Reasoning token budget for the prep call.' },
  { path: 'prep.max_output_tokens', menu: 'Prep', section: 'Context Build', label: 'Maximum Output Tokens', kind: 'integer', min: 1, description: "Output ceiling for the prep call's structured XML response." },
  { path: 'prep.http_timeout_seconds', menu: 'Prep', section: 'Context Build', label: 'Request Timeout', kind: 'integer', min: 1, description: 'HTTP timeout for the single prep API call, in seconds.' },
  { path: 'prep.bible_dir', menu: 'Prep', section: 'Context Build', label: 'Series Bible Directory', kind: 'text', description: 'Cross-volume series bibles live here; sequels load prior term_lock/voice data from here.' },
  { path: 'prep.parallel.enabled', menu: 'Prep', section: 'Context Build', label: 'Parallel Context Build', kind: 'boolean',
    description: 'Cache-warmed 14-call Pro+Flash fan-out (PARALLEL_PREP_GUIDE.md) instead of the single unified Pro call above. Falls back to the unified call on failure if fallback_to_unified stays on.' },
  { path: 'prep.multi_turn.enabled', menu: 'Prep', section: 'Context Build', label: 'Multi-turn Context Build', kind: 'boolean',
    description: 'One persisted, sequential DeepSeek conversation (same prefix-cache mechanism the translator uses) — one call per block, in placeholder order, each turn able to see every earlier block it already committed. Checked before Parallel Context Build; do not enable both at once. Falls back to the unified call on failure if fallback_to_unified stays on.' },

// ─── Translation ─────────────────────────────────────────────────────────────────
  { path: 'translation.provider', menu: 'Translation', section: 'Provider', label: 'Provider', kind: 'enum', choices: PROVIDER_CHOICES, choiceLabels: PROVIDER_LABELS,
    description: 'Phase 2 translation provider. DeepSeek and Qwen use separate client, prompt, conversation, and optimization modules.' },
  { path: 'translation.master_prompt', menu: 'Translation', section: 'Prompts', label: 'Primary Translation Prompt', kind: 'text',
    description: 'Base master prompt. Consolidates all RAG-module policy inline — no external RAG store at runtime. DeepSeek route only: ignored when provider is qwen (which reads translation.qwen.master_prompt).' },
  { path: 'translation.master_prompt_v2', menu: 'Translation', section: 'Prompts', label: 'Continuity Translation Prompt', kind: 'text',
    description: "Literacy-anchor variant, auto-selected for sequels whose context.xml carries prior_volume_anchors." },
  { path: 'translation.thinking_log.enabled', menu: 'Translation', section: 'Thinking Log', label: 'Status', kind: 'boolean',
    description: "Archive DeepSeek's reasoning_content per chapter to disk. Reasoning tokens are billed either way — this only controls whether the audit trail is saved." },
  { path: 'translation.thinking_log.output_dir', menu: 'Translation', section: 'Thinking Log', label: 'Output Directory', kind: 'text',
    description: "Subdirectory (under the volume's work dir) where thinking logs are written." },
  { path: 'translation.deepseek.model', menu: 'Translation', section: 'DeepSeek — Model & Connection', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS,
    description: 'Phase 2 translation model. Flash is ~3x cheaper throughput; Pro is the production quality tier.' },
  { path: 'translation.deepseek.endpoint', menu: 'Translation', section: 'DeepSeek — Model & Connection', label: 'Endpoint', kind: 'enum', choices: ENDPOINT_CHOICES, choiceLabels: ENDPOINT_LABELS,
    description: 'Same Anthropic-vs-OpenAI route choice as Prep, scoped to translation. The client only builds Anthropic-format requests.' },
  { path: 'translation.deepseek.api_key_env', menu: 'Translation', section: 'DeepSeek — Model & Connection', label: 'API Key Variable', kind: 'text', description: 'Environment variable holding the API key for translation calls.' },
  { path: 'translation.deepseek.http_timeout_seconds', menu: 'Translation', section: 'DeepSeek — Model & Connection', label: 'Request Timeout', kind: 'integer', min: 1,
    description: 'Per-call HTTP timeout, in seconds. Must cover the worst-case max-output chapter with thinking enabled.' },
  { path: 'translation.deepseek.generation.max_output_tokens', menu: 'Translation', section: 'DeepSeek — Generation', label: 'Maximum Output Tokens', kind: 'integer', min: 1,
    description: "Output ceiling per translation turn. 384K is DeepSeek V4's hard cap." },
  { path: 'translation.deepseek.generation.top_p', menu: 'Translation', section: 'DeepSeek — Generation', label: 'Sampling Nucleus (top_p)', kind: 'float', min: 0, max: 1,
    description: 'Nucleus sampling; higher = more creative. Temperature is omitted automatically when thinking is enabled — top_p still applies.' },
  { path: 'translation.deepseek.thinking.enabled', menu: 'Translation', section: 'DeepSeek — Thinking', label: 'Status', kind: 'boolean',
    description: "Master toggle for DeepSeek's chain-of-thought. When off, the budget and effort fields below are skipped entirely." },
  { path: 'translation.deepseek.thinking.budget_tokens', menu: 'Translation', section: 'DeepSeek — Thinking', label: 'Thinking Token Budget', kind: 'integer', min: 0,
    description: 'Reasoning token budget per turn. 48K is production; lower it for cheaper COLD-band chapters.' },
  { path: 'translation.deepseek.thinking.effort', menu: 'Translation', section: 'DeepSeek — Thinking', label: 'Effort', kind: 'enum', choices: EFFORT_CHOICES,
    description: 'Anthropic-format effort level. DeepSeek has no native effort routing — actual per-chapter intensity comes from DRDI, not this.' },
  { path: 'translation.deepseek.caching.enabled', menu: 'Translation', section: 'DeepSeek — Caching', label: 'Status', kind: 'boolean',
    description: "DeepSeek's automatic longest-stable-prefix caching. No cache_control markers needed — the system prompt + context.xml form the stable prefix." },
  { path: 'translation.deepseek.caching.cache_monitor.enabled', menu: 'Translation', section: 'DeepSeek — Caching — Cache Monitor', label: 'Status', kind: 'boolean',
    description: 'Track prefix stability and cache-hit ratio per chapter.' },
  { path: 'translation.deepseek.caching.cache_monitor.warn_threshold_cache_hit_ratio', menu: 'Translation', section: 'DeepSeek — Caching — Cache Monitor', label: 'Cache Warning Threshold', kind: 'float', min: 0, max: 1,
    description: 'Log a warning when the cache hit ratio drops below this fraction.' },
  { path: 'translation.deepseek.caching.thinking_analytics.enabled', menu: 'Translation', section: 'DeepSeek — Caching — Thinking Analytics', label: 'Status', kind: 'boolean',
    description: 'Track thinking-token count vs EPS intensity for cost/quality analytics.' },
  { path: 'translation.deepseek.conversation.enabled', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Status', kind: 'boolean',
    description: 'Persistent, append-only transcript across chapters for voice/plot continuity. Off = fully independent per-chapter translation.' },
  { path: 'translation.deepseek.conversation.recent_verbatim_chapters', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Recent Verbatim Chapters', kind: 'integer', min: 0,
    description: 'How many most-recent chapters are included full JP+EN verbatim each turn; older ones summarize into a checkpoint.' },
  { path: 'translation.deepseek.conversation.include_exact_jp_task', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Include Exact JP Source', kind: 'boolean',
    description: 'Include the exact JP source of recent chapters, not just EN output, so DeepSeek can cross-reference its own prior translations.' },
  { path: 'translation.deepseek.conversation.persistence_file', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Conversation Ledger Path', kind: 'text',
    description: "Path (under the volume's work dir) where the conversation transcript is persisted." },
  { path: 'translation.deepseek.conversation.checkpoint_trigger_ratio', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Checkpoint Trigger', kind: 'float', min: 0, max: 1,
    description: 'Fraction of the context window at which the paid checkpoint summarizer fires and resets the prefix cache.' },
  { path: 'translation.deepseek.conversation.checkpoint_max_output_tokens', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Checkpoint Maximum Output', kind: 'integer', min: 0,
    description: "Output ceiling for the checkpoint summarizer's own compressed continuity XML." },
  { path: 'translation.deepseek.conversation.checkpoint_stuck_guard_turns', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Checkpoint Stuck Guard', kind: 'integer', min: 0,
    description: 'Pause auto-checkpointing after this many consecutive checkpoint-triggering turns so the prefix cache can warm back up. 0 disables the guard.' },
  { path: 'translation.deepseek.conversation.soft_notice_ratio', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Soft Notice Threshold', kind: 'float', min: 0, max: 1,
    description: 'Lowest compaction-ladder rung: log a warning only, no mutation, no cost.' },
  { path: 'translation.deepseek.conversation.tool_snip_ratio', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Tool Trim Threshold', kind: 'float', min: 0, max: 1,
    description: 'Compaction-ladder rung: drop stale tool round-trips from turns outside the recent-verbatim window.' },
  { path: 'translation.deepseek.conversation.checkpoint_force_ratio', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Forced Checkpoint Threshold', kind: 'float', min: 0, max: 1,
    description: 'Highest compaction-ladder rung: override the stuck guard. Must stay strictly below 1.0.' },
  { path: 'translation.deepseek.conversation.fail_closed_on_checkpoint_error', menu: 'Translation', section: 'DeepSeek — Conversation', label: 'Fail Closed On Checkpoint Error', kind: 'boolean',
    description: 'If the checkpoint summarizer itself fails: on aborts the translation (safe), off continues without a checkpoint (risks context overflow).' },
  { path: 'translation.deepseek.optimizations.drdi.enabled', menu: 'Translation', section: 'DeepSeek — Optimizations — DRDI', label: 'Status', kind: 'boolean',
    description: 'Reasoning Directive Injection — EPS-band CoT scaffolding injected into the user turn, since DeepSeek ignores the Anthropic effort parameter natively.' },
  { path: 'translation.deepseek.optimizations.dovb.enabled', menu: 'Translation', section: 'DeepSeek — Optimizations — DOVB', label: 'Status', kind: 'boolean',
    description: 'Voice Block — explicit contraction/forbidden-vocab/register templates. Off = voice guidance comes only from context.xml and the master prompt.' },
  { path: 'translation.deepseek.optimizations.concurrent_chapters.enabled', menu: 'Translation', section: 'DeepSeek — Optimizations — Concurrent Chapters', label: 'Status', kind: 'boolean',
    description: 'Opt-in parallel chapter translation. Not wired into the default flow — conversation history is ordered/append-only and cannot be shared across concurrent chapters.' },
  { path: 'translation.deepseek.optimizations.concurrent_chapters.max_concurrent', menu: 'Translation', section: 'DeepSeek — Optimizations — Concurrent Chapters', label: 'Maximum Concurrent Chapters', kind: 'integer', min: 1,
    description: 'Parallel chapter cap. Forced to 1 in code regardless of this value — kept for a future safe implementation.' },
  { path: 'translation.deepseek.streaming.enabled', menu: 'Translation', section: 'DeepSeek — Streaming', label: 'Status', kind: 'boolean',
    description: 'Stream text/thinking deltas as they arrive instead of waiting for the full response.' },
  { path: 'translation.deepseek.post_processing.scene_break_formatting', menu: 'Translation', section: 'DeepSeek — Post-Processing', label: 'Scene-Break Formatting', kind: 'boolean',
    description: 'Normalize scene breaks (＊ ＊ ＊ → centered asterism, etc.) in translated output.' },
  { path: 'translation.deepseek.post_processing.cjk_cleanup', menu: 'Translation', section: 'DeepSeek — Post-Processing', label: 'CJK Cleanup', kind: 'boolean',
    description: 'Strip CJK artifact characters (fullwidth punctuation, ideographic spaces) that leak into EN output.' },
  { path: 'translation.deepseek.post_processing.salvage_reasoning_leaked_answer', menu: 'Translation', section: 'DeepSeek — Post-Processing', label: 'Recover Leaked Reasoning Answers', kind: 'boolean',
    description: 'Recover chapters DeepSeek accidentally emitted into reasoning_content instead of content — a known API edge case.' },
  { path: 'translation.deepseek.retry.max_retries', menu: 'Translation', section: 'DeepSeek — Retry', label: 'Maximum Retries', kind: 'integer', min: 0, description: 'Maximum retry attempts on a failed API call.' },
  { path: 'translation.deepseek.retry.base_delay_ms', menu: 'Translation', section: 'DeepSeek — Retry', label: 'Retry Base Delay (ms)', kind: 'integer', min: 0, description: 'Starting backoff delay between retries.' },
  { path: 'translation.deepseek.retry.max_delay_ms', menu: 'Translation', section: 'DeepSeek — Retry', label: 'Retry Maximum Delay (ms)', kind: 'integer', min: 0, description: 'Backoff delay ceiling.' },
  { path: 'translation.deepseek.retry.jitter_factor', menu: 'Translation', section: 'DeepSeek — Retry', label: 'Retry Jitter', kind: 'float', min: 0, max: 1, description: 'Random jitter fraction applied to each backoff delay, to avoid retry storms.' },
  { path: 'translation.deepseek.retry.max_529_retries', menu: 'Translation', section: 'DeepSeek — Retry', label: 'Maximum Overload Retries', kind: 'integer', min: 0, description: 'Separate, tighter retry cap specifically for 529 Overloaded responses.' },
  { path: 'translation.qwen.model', menu: 'Translation', section: 'Qwen — Model & Connection', label: 'Model', kind: 'enum', choices: QWEN_MODEL_CHOICES, choiceLabels: QWEN_MODEL_LABELS,
    description: 'Qwen Phase 2 model. Qwen 3.8 Max is the quality tier; Plus and Flash trade quality for throughput.' },
  { path: 'translation.qwen.endpoint', menu: 'Translation', section: 'Qwen — Model & Connection', label: 'Endpoint', kind: 'text',
    description: 'Qwen Anthropic-compatible Messages endpoint. Keep the base URL at /apps/anthropic; the SDK appends /v1/messages.' },
  { path: 'translation.qwen.api_key_env', menu: 'Translation', section: 'Qwen — Model & Connection', label: 'API Key Variable', kind: 'text',
    description: 'Environment variable holding the Qwen/DashScope API key.' },
  { path: 'translation.qwen.master_prompt', menu: 'Translation', section: 'Qwen — Model & Connection', label: 'Primary Translation Prompt', kind: 'text',
    description: 'Qwen-route master prompt, used when translation.provider is qwen. The general translation.master_prompt field is DeepSeek-only and never reaches this route.' },
  { path: 'translation.qwen.thinking.enabled', menu: 'Translation', section: 'Qwen — Thinking', label: 'Status', kind: 'boolean',
    description: 'Enable Qwen reasoning for translation turns. Partial-prefix continuation is unavailable while thinking is enabled.' },
  { path: 'translation.qwen.thinking.budget_tokens', menu: 'Translation', section: 'Qwen — Thinking', label: 'Thinking Token Budget', kind: 'integer', min: 0,
    description: 'Maximum Qwen reasoning tokens per turn.' },
  { path: 'translation.qwen.caching.enabled', menu: 'Translation', section: 'Qwen — Caching', label: 'Status', kind: 'boolean',
    description: 'Enable Qwen prompt caching behavior.' },
  { path: 'translation.qwen.caching.explicit', menu: 'Translation', section: 'Qwen — Caching', label: 'Explicit Cache Markers', kind: 'boolean',
    description: 'Place Qwen ephemeral cache markers on the stable system prompt. Cache validity is provider-controlled and short-lived.' },

// ─── Builder ─────────────────────────────────────────────────────────────────────
  { path: 'builder.fonts.enabled', menu: 'Builder', section: 'Fonts', label: 'Status', kind: 'boolean', description: 'Embed CJK-coverage fonts into the output EPUB.' },
  { path: 'builder.images.max_width_px', menu: 'Builder', section: 'Images', label: 'Maximum Width (px)', kind: 'integer', min: 1, description: 'Downscale ceiling for cover/kuchie/illustration width.' },
  { path: 'builder.images.max_height_px', menu: 'Builder', section: 'Images', label: 'Maximum Height (px)', kind: 'integer', min: 1, description: 'Downscale ceiling for cover/kuchie/illustration height.' },
  { path: 'builder.images.jpeg_quality', menu: 'Builder', section: 'Images', label: 'JPEG Quality', kind: 'integer', min: 1, max: 100, description: 'JPEG re-encode quality (1-100) for optimized images.' },

// ─── Logging ─────────────────────────────────────────────────────────────────────
  { path: 'logging.level', menu: 'Logging', section: 'Logging', label: 'Log Level', kind: 'enum', choices: ['DEBUG', 'INFO', 'WARNING', 'ERROR'], description: 'Log verbosity for the whole pipeline.' },
  { path: 'logging.file', menu: 'Logging', section: 'Logging', label: 'Log File', kind: 'text', description: "Path to the pipeline's log file." },
  { path: 'logging.cost_tracking', menu: 'Logging', section: 'Logging', label: 'Cost Tracking', kind: 'boolean', description: 'Log per-chapter API cost (input/output/cache tokens → USD) alongside normal logging.' },

// ─── MCP ─────────────────────────────────────────────────────────────────────────
  { path: 'mcp_harness.persona.file', menu: 'MCP', section: 'Persona', label: 'Persona File', kind: 'text', description: 'Persona file prepended to every LLM system instruction the MCP server builds.' },
  { path: 'mcp_harness.persona.enabled', menu: 'MCP', section: 'Persona', label: 'Status', kind: 'boolean', description: 'Whether the persona file is actually prepended — the one knob here meant to be casually toggled.' },
  { path: 'mcp_harness.subagents.prep.enabled', menu: 'MCP', section: 'Subagents — Prep', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's prep subagent is active." },
  { path: 'mcp_harness.subagents.prep.model', menu: 'MCP', section: 'Subagents — Prep', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS, description: "Model used by the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.thinking_budget', menu: 'MCP', section: 'Subagents — Prep', label: 'Thinking Budget', kind: 'integer', min: 0, description: "Reasoning token budget for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.effort', menu: 'MCP', section: 'Subagents — Prep', label: 'Effort', kind: 'enum', choices: EFFORT_CHOICES, description: "Effort level for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.max_output_tokens', menu: 'MCP', section: 'Subagents — Prep', label: 'Maximum Output Tokens', kind: 'integer', min: 1, description: "Output ceiling for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.translator.harness_managed', menu: 'MCP', section: 'Subagents — Translator', label: 'Harness Managed', kind: 'boolean',
    description: 'Off = DeepSeekClient owns translation.deepseek directly (current default). On = the harness would own it instead.' },
  { path: 'mcp_harness.subagents.qc.enabled', menu: 'MCP', section: 'Subagents — QC', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's QC subagent is active. Filesystem-only — no LLM call regardless." },

// ─── Bible ───────────────────────────────────────────────────────────────────────
  { path: 'mcp_harness.subagents.bible_writer.enabled', menu: 'Bible', section: 'Writer', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's bible-writer subagent is active. Filesystem-only — no LLM call regardless." },
];

export function unquoteYamlScalar(raw: string): string {
  if (raw.length >= 2 && raw.startsWith('"') && raw.endsWith('"')) return raw.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\');
  if (raw.length >= 2 && raw.startsWith("'") && raw.endsWith("'")) return raw.slice(1, -1).replace(/''/g, "'");
  return raw;
}

function quoteYamlTextIfNeeded(value: string): string {
  const needsQuoting = value === '' || /^\s|\s$/.test(value) || /[:#{}[\],&*!|>'"%@`]/.test(value) || /^(true|false|null|~|yes|no)$/i.test(value) || /^-?\d+(\.\d+)?$/.test(value);
  if (!needsQuoting) return value;
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

export type ConfigInputResult = { ok: true; raw: string } | { ok: false; error: string };

// Only integer/float/text route through here — boolean/enum are toggled/
// cycled directly by the UI without ever entering a typed-edit mode.
export function validateConfigInput(spec: ConfigFieldSpec, text: string): ConfigInputResult {
  const trimmed = text.trim();
  if (spec.kind === 'integer') {
    if (!/^-?\d+$/.test(trimmed)) return { ok: false, error: 'Enter a whole number.' };
    const value = Number.parseInt(trimmed, 10);
    if (spec.min !== undefined && value < spec.min) return { ok: false, error: `Must be at least ${spec.min}.` };
    if (spec.max !== undefined && value > spec.max) return { ok: false, error: `Must be at most ${spec.max}.` };
    return { ok: true, raw: String(value) };
  }
  if (spec.kind === 'float') {
    if (!/^-?\d*\.?\d+$/.test(trimmed)) return { ok: false, error: 'Enter a number.' };
    const value = Number.parseFloat(trimmed);
    if (spec.min !== undefined && value < spec.min) return { ok: false, error: `Must be at least ${spec.min}.` };
    if (spec.max !== undefined && value > spec.max) return { ok: false, error: `Must be at most ${spec.max}.` };
    return { ok: true, raw: trimmed };
  }
  return { ok: true, raw: quoteYamlTextIfNeeded(text) };
}

export function formatConfigValue(spec: ConfigFieldSpec, rawValue: string): string {
  if (!rawValue) return '—';
  if (spec.kind === 'boolean') return rawValue === 'true' ? 'Enabled' : 'Disabled';
  if (spec.kind === 'enum' && spec.choiceLabels) { const index = (spec.choices ?? []).indexOf(rawValue); return spec.choiceLabels[index] ?? rawValue; }
  if (spec.kind === 'text') return unquoteYamlScalar(rawValue);
  return rawValue;
}

export type ConfigRenderLine =
  | { kind: 'menu'; label: string }
  | { kind: 'section'; label: string }
  | { kind: 'gap' }
  | { kind: 'field'; fieldIndex: number };

// Each field carries a top-level `menu` (Project, Prep, Translation, MCP, …)
// and a detail `section`. Menus render as submenu headers; sections and fields
// nest beneath the active menu. The Translation menu is gated by the Provider
// selector — the field list only ever contains the selected provider's
// settings, so only its sections render.
export function buildConfigRenderLines(fields: readonly { menu: string; section: string }[]): ConfigRenderLine[] {
  const lines: ConfigRenderLine[] = [];
  let lastMenu: string | null = null;
  let lastSection: string | null = null;
  fields.forEach((field, fieldIndex) => {
    if (field.menu !== lastMenu) {
      if (lastMenu !== null) lines.push({ kind: 'gap' });
      lines.push({ kind: 'menu', label: field.menu });
      lastMenu = field.menu;
      lastSection = null;
    }
    // A section that merely repeats its menu name (Project, Logging) is a
    // redundant sub-header — render those fields straight under the menu.
    if (field.section !== lastSection && field.section !== field.menu) {
      lines.push({ kind: 'section', label: field.section });
      lastSection = field.section;
    }
    lines.push({ kind: 'field', fieldIndex });
  });
  return lines;
}

// Reads the live translation.provider value from the loaded config fields.
export function activeProvider(fields: readonly { path: string; rawValue?: string }[]): string {
  const providerField = fields.find((field) => field.path === 'translation.provider');
  return (providerField?.rawValue ?? '').trim().toLowerCase();
}

// Keeps only the selected provider's settings; non-provider fields always stay.
export function filterFieldsForProvider<T extends { path: string }>(fields: readonly T[], provider: string): T[] {
  const selected = provider.trim().toLowerCase();
  return fields.filter((field) => {
    if (field.path.startsWith('translation.deepseek.')) return selected === 'deepseek';
    if (field.path.startsWith('translation.qwen.')) return selected === 'qwen';
    return true;
  });
}
