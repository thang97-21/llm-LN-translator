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
const ENDPOINT_CHOICES = ['https://api.deepseek.com/anthropic', 'https://api.deepseek.com'] as const;
const ENDPOINT_LABELS = ['Anthropic', 'OpenAI'] as const;
const EFFORT_CHOICES = ['max', 'high', 'medium', 'low'] as const;

export const CONFIG_FIELDS: readonly ConfigFieldSpec[] = [
  // ── Project ────────────────────────────────────────────────────────────
  { path: 'project.target_language', section: 'Project', label: 'Target Language', kind: 'text',
    description: "Not actually read at runtime — get_target_language() in src/common/config.py always returns 'en' regardless of this value." },
  { path: 'project.name', section: 'Project', label: 'Project Name', kind: 'text', description: 'Display name for this pipeline instance. Cosmetic only.' },
  { path: 'project.version', section: 'Project', label: 'Config Version', kind: 'text', description: 'Version tag for this config file. Cosmetic only, not read for compatibility checks.' },

  // ── File System Paths ─────────────────────────────────────────────────
  { path: 'paths.input_dir', section: 'File System Paths', label: 'Input Directory', kind: 'text', description: 'Where extract/run look for source EPUBs.' },
  { path: 'paths.work_dir', section: 'File System Paths', label: 'Work Directory', kind: 'text', description: 'Per-volume working directories (JP/EN/manifest.json/etc.) live under here.' },
  { path: 'paths.output_dir', section: 'File System Paths', label: 'Output Directory', kind: 'text', description: 'Final assembled EPUBs are written here.' },
  { path: 'paths.prompt_dir', section: 'File System Paths', label: 'Prompt Directory', kind: 'text', description: 'Base directory for the master translation prompt XML files.' },
  { path: 'paths.log_dir', section: 'File System Paths', label: 'Log Directory', kind: 'text', description: 'Debug and cost logs are written here.' },

  // ── Prep — Context Build ──────────────────────────────────────────────
  { path: 'prep.model', section: 'Prep — Context Build', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS,
    description: 'Model for the single call that fills context.xml. Flash trades instruction-following for throughput — Pro is required for reliable 15-block structured output.' },
  { path: 'prep.endpoint', section: 'Prep — Context Build', label: 'Endpoint', kind: 'enum', choices: ENDPOINT_CHOICES, choiceLabels: ENDPOINT_LABELS,
    description: 'The client only speaks the Anthropic SDK request format — leave this on Anthropic.' },
  { path: 'prep.api_key_env', section: 'Prep — Context Build', label: 'API Key Variable', kind: 'text', description: 'Environment variable holding the DeepSeek API key for prep calls.' },
  { path: 'prep.prompt', section: 'Prep — Context Build', label: 'Prompt File', kind: 'text', description: "Prep prompt XML that fills the Librarian's barebone context.xml." },
  { path: 'prep.thinking_budget', section: 'Prep — Context Build', label: 'Thinking Budget', kind: 'integer', min: 0, description: 'Reasoning token budget for the prep call.' },
  { path: 'prep.max_output_tokens', section: 'Prep — Context Build', label: 'Maximum Output Tokens', kind: 'integer', min: 1, description: "Output ceiling for the prep call's structured XML response." },
  { path: 'prep.http_timeout_seconds', section: 'Prep — Context Build', label: 'Request Timeout', kind: 'integer', min: 1, description: 'HTTP timeout for the single prep API call, in seconds.' },
  { path: 'prep.bible_dir', section: 'Prep — Context Build', label: 'Series Bible Directory', kind: 'text', description: 'Cross-volume series bibles live here; sequels load prior term_lock/voice data from here.' },
  { path: 'prep.parallel.enabled', section: 'Prep — Context Build', label: 'Parallel Context Build', kind: 'boolean',
    description: 'Cache-warmed 14-call Pro+Flash fan-out (PARALLEL_PREP_GUIDE.md) instead of the single unified Pro call above. Falls back to the unified call on failure if fallback_to_unified stays on.' },

  // ── Translation — Prompts ─────────────────────────────────────────────
  { path: 'translation.master_prompt', section: 'Translation — Prompts', label: 'Primary Translation Prompt', kind: 'text',
    description: 'Base master prompt. Consolidates all RAG-module policy inline — no external RAG store at runtime.' },
  { path: 'translation.master_prompt_v2', section: 'Translation — Prompts', label: 'Continuity Translation Prompt', kind: 'text',
    description: "Literacy-anchor variant, auto-selected for sequels whose context.xml carries prior_volume_anchors." },

  // ── Translation — Thinking Log ────────────────────────────────────────
  { path: 'translation.thinking_log.enabled', section: 'Translation — Thinking Log', label: 'Status', kind: 'boolean',
    description: "Archive DeepSeek's reasoning_content per chapter to disk. Reasoning tokens are billed either way — this only controls whether the audit trail is saved." },
  { path: 'translation.thinking_log.output_dir', section: 'Translation — Thinking Log', label: 'Output Directory', kind: 'text',
    description: "Subdirectory (under the volume's work dir) where thinking logs are written." },

  // ── Translator — Model & Connection ───────────────────────────────────
  { path: 'translation.translator.model', section: 'Translator — Model & Connection', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS,
    description: 'Phase 2 translation model. Flash is ~3x cheaper throughput; Pro is the production quality tier.' },
  { path: 'translation.translator.endpoint', section: 'Translator — Model & Connection', label: 'Endpoint', kind: 'enum', choices: ENDPOINT_CHOICES, choiceLabels: ENDPOINT_LABELS,
    description: 'Same Anthropic-vs-OpenAI route choice as Prep, scoped to translation. The client only builds Anthropic-format requests.' },
  { path: 'translation.translator.api_key_env', section: 'Translator — Model & Connection', label: 'API Key Variable', kind: 'text', description: 'Environment variable holding the API key for translation calls.' },
  { path: 'translation.translator.http_timeout_seconds', section: 'Translator — Model & Connection', label: 'Request Timeout', kind: 'integer', min: 1,
    description: 'Per-call HTTP timeout, in seconds. Must cover the worst-case max-output chapter with thinking enabled.' },

  // ── Translator — Generation ───────────────────────────────────────────
  { path: 'translation.translator.generation.max_output_tokens', section: 'Translator — Generation', label: 'Maximum Output Tokens', kind: 'integer', min: 1,
    description: "Output ceiling per translation turn. 384K is DeepSeek V4's hard cap." },
  { path: 'translation.translator.generation.top_p', section: 'Translator — Generation', label: 'Sampling Nucleus (top_p)', kind: 'float', min: 0, max: 1,
    description: 'Nucleus sampling; higher = more creative. Temperature is omitted automatically when thinking is enabled — top_p still applies.' },

  // ── Translator — Thinking ─────────────────────────────────────────────
  { path: 'translation.translator.thinking.enabled', section: 'Translator — Thinking', label: 'Status', kind: 'boolean',
    description: "Master toggle for DeepSeek's chain-of-thought. When off, the budget and effort fields below are skipped entirely." },
  { path: 'translation.translator.thinking.budget_tokens', section: 'Translator — Thinking', label: 'Thinking Token Budget', kind: 'integer', min: 0,
    description: 'Reasoning token budget per turn. 48K is production; lower it for cheaper COLD-band chapters.' },
  { path: 'translation.translator.thinking.effort', section: 'Translator — Thinking', label: 'Effort', kind: 'enum', choices: EFFORT_CHOICES,
    description: 'Anthropic-format effort level. DeepSeek has no native effort routing — actual per-chapter intensity comes from DRDI, not this.' },

  // ── Translator — Caching ──────────────────────────────────────────────
  { path: 'translation.translator.caching.enabled', section: 'Translator — Caching', label: 'Status', kind: 'boolean',
    description: "DeepSeek's automatic longest-stable-prefix caching. No cache_control markers needed — the system prompt + context.xml form the stable prefix." },
  { path: 'translation.translator.caching.cache_monitor.enabled', section: 'Translator — Caching — Cache Monitor', label: 'Status', kind: 'boolean',
    description: 'Track prefix stability and cache-hit ratio per chapter.' },
  { path: 'translation.translator.caching.cache_monitor.warn_threshold_cache_hit_ratio', section: 'Translator — Caching — Cache Monitor', label: 'Cache Warning Threshold', kind: 'float', min: 0, max: 1,
    description: 'Log a warning when the cache hit ratio drops below this fraction.' },
  { path: 'translation.translator.caching.thinking_analytics.enabled', section: 'Translator — Caching — Thinking Analytics', label: 'Status', kind: 'boolean',
    description: 'Track thinking-token count vs EPS intensity for cost/quality analytics.' },

  // ── Translator — Conversation ─────────────────────────────────────────
  { path: 'translation.translator.conversation.enabled', section: 'Translator — Conversation', label: 'Status', kind: 'boolean',
    description: 'Persistent, append-only transcript across chapters for voice/plot continuity. Off = fully independent per-chapter translation.' },
  { path: 'translation.translator.conversation.recent_verbatim_chapters', section: 'Translator — Conversation', label: 'Recent Verbatim Chapters', kind: 'integer', min: 0,
    description: 'How many most-recent chapters are included full JP+EN verbatim each turn; older ones summarize into a checkpoint.' },
  { path: 'translation.translator.conversation.include_exact_jp_task', section: 'Translator — Conversation', label: 'Include Exact JP Source', kind: 'boolean',
    description: 'Include the exact JP source of recent chapters, not just EN output, so DeepSeek can cross-reference its own prior translations.' },
  { path: 'translation.translator.conversation.persistence_file', section: 'Translator — Conversation', label: 'Conversation Ledger Path', kind: 'text',
    description: "Path (under the volume's work dir) where the conversation transcript is persisted." },
  { path: 'translation.translator.conversation.checkpoint_trigger_ratio', section: 'Translator — Conversation', label: 'Checkpoint Trigger', kind: 'float', min: 0, max: 1,
    description: 'Fraction of the context window at which the paid checkpoint summarizer fires and resets the prefix cache.' },
  { path: 'translation.translator.conversation.checkpoint_max_output_tokens', section: 'Translator — Conversation', label: 'Checkpoint Maximum Output', kind: 'integer', min: 0,
    description: "Output ceiling for the checkpoint summarizer's own compressed continuity XML." },
  { path: 'translation.translator.conversation.checkpoint_stuck_guard_turns', section: 'Translator — Conversation', label: 'Checkpoint Stuck Guard', kind: 'integer', min: 0,
    description: 'Pause auto-checkpointing after this many consecutive checkpoint-triggering turns so the prefix cache can warm back up. 0 disables the guard.' },
  { path: 'translation.translator.conversation.soft_notice_ratio', section: 'Translator — Conversation', label: 'Soft Notice Threshold', kind: 'float', min: 0, max: 1,
    description: 'Lowest compaction-ladder rung: log a warning only, no mutation, no cost.' },
  { path: 'translation.translator.conversation.tool_snip_ratio', section: 'Translator — Conversation', label: 'Tool Trim Threshold', kind: 'float', min: 0, max: 1,
    description: 'Compaction-ladder rung: drop stale tool round-trips from turns outside the recent-verbatim window.' },
  { path: 'translation.translator.conversation.checkpoint_force_ratio', section: 'Translator — Conversation', label: 'Forced Checkpoint Threshold', kind: 'float', min: 0, max: 1,
    description: 'Highest compaction-ladder rung: override the stuck guard. Must stay strictly below 1.0.' },
  { path: 'translation.translator.conversation.fail_closed_on_checkpoint_error', section: 'Translator — Conversation', label: 'Fail Closed On Checkpoint Error', kind: 'boolean',
    description: 'If the checkpoint summarizer itself fails: on aborts the translation (safe), off continues without a checkpoint (risks context overflow).' },

  // ── Translator — Optimizations ────────────────────────────────────────
  { path: 'translation.translator.optimizations.drdi.enabled', section: 'Translator — Optimizations — DRDI', label: 'Status', kind: 'boolean',
    description: 'Reasoning Directive Injection — EPS-band CoT scaffolding injected into the user turn, since DeepSeek ignores the Anthropic effort parameter natively.' },
  { path: 'translation.translator.optimizations.dovb.enabled', section: 'Translator — Optimizations — DOVB', label: 'Status', kind: 'boolean',
    description: 'Voice Block — explicit contraction/forbidden-vocab/register templates. Off = voice guidance comes only from context.xml and the master prompt.' },
  { path: 'translation.translator.optimizations.concurrent_chapters.enabled', section: 'Translator — Optimizations — Concurrent Chapters', label: 'Status', kind: 'boolean',
    description: 'Opt-in parallel chapter translation. Not wired into the default flow — conversation history is ordered/append-only and cannot be shared across concurrent chapters.' },
  { path: 'translation.translator.optimizations.concurrent_chapters.max_concurrent', section: 'Translator — Optimizations — Concurrent Chapters', label: 'Maximum Concurrent Chapters', kind: 'integer', min: 1,
    description: 'Parallel chapter cap. Forced to 1 in code regardless of this value — kept for a future safe implementation.' },

  // ── Translator — Streaming ────────────────────────────────────────────
  { path: 'translation.translator.streaming.enabled', section: 'Translator — Streaming', label: 'Status', kind: 'boolean',
    description: 'Stream text/thinking deltas as they arrive instead of waiting for the full response.' },

  // ── Translator — Post-Processing ──────────────────────────────────────
  { path: 'translation.translator.post_processing.scene_break_formatting', section: 'Translator — Post-Processing', label: 'Scene-Break Formatting', kind: 'boolean',
    description: 'Normalize scene breaks (＊ ＊ ＊ → centered asterism, etc.) in translated output.' },
  { path: 'translation.translator.post_processing.cjk_cleanup', section: 'Translator — Post-Processing', label: 'CJK Cleanup', kind: 'boolean',
    description: 'Strip CJK artifact characters (fullwidth punctuation, ideographic spaces) that leak into EN output.' },
  { path: 'translation.translator.post_processing.salvage_reasoning_leaked_answer', section: 'Translator — Post-Processing', label: 'Recover Leaked Reasoning Answers', kind: 'boolean',
    description: 'Recover chapters DeepSeek accidentally emitted into reasoning_content instead of content — a known API edge case.' },

  // ── Translator — Retry ────────────────────────────────────────────────
  { path: 'translation.translator.retry.max_retries', section: 'Translator — Retry', label: 'Maximum Retries', kind: 'integer', min: 0, description: 'Maximum retry attempts on a failed API call.' },
  { path: 'translation.translator.retry.base_delay_ms', section: 'Translator — Retry', label: 'Retry Base Delay (ms)', kind: 'integer', min: 0, description: 'Starting backoff delay between retries.' },
  { path: 'translation.translator.retry.max_delay_ms', section: 'Translator — Retry', label: 'Retry Maximum Delay (ms)', kind: 'integer', min: 0, description: 'Backoff delay ceiling.' },
  { path: 'translation.translator.retry.jitter_factor', section: 'Translator — Retry', label: 'Retry Jitter', kind: 'float', min: 0, max: 1, description: 'Random jitter fraction applied to each backoff delay, to avoid retry storms.' },
  { path: 'translation.translator.retry.max_529_retries', section: 'Translator — Retry', label: 'Maximum Overload Retries', kind: 'integer', min: 0, description: 'Separate, tighter retry cap specifically for 529 Overloaded responses.' },

  // ── Builder ────────────────────────────────────────────────────────────
  { path: 'builder.fonts.enabled', section: 'Builder — Fonts', label: 'Status', kind: 'boolean', description: 'Embed CJK-coverage fonts into the output EPUB.' },
  { path: 'builder.images.max_width_px', section: 'Builder — Images', label: 'Maximum Width (px)', kind: 'integer', min: 1, description: 'Downscale ceiling for cover/kuchie/illustration width.' },
  { path: 'builder.images.max_height_px', section: 'Builder — Images', label: 'Maximum Height (px)', kind: 'integer', min: 1, description: 'Downscale ceiling for cover/kuchie/illustration height.' },
  { path: 'builder.images.jpeg_quality', section: 'Builder — Images', label: 'JPEG Quality', kind: 'integer', min: 1, max: 100, description: 'JPEG re-encode quality (1-100) for optimized images.' },

  // ── Logging ────────────────────────────────────────────────────────────
  { path: 'logging.level', section: 'Logging', label: 'Log Level', kind: 'enum', choices: ['DEBUG', 'INFO', 'WARNING', 'ERROR'], description: 'Log verbosity for the whole pipeline.' },
  { path: 'logging.file', section: 'Logging', label: 'Log File', kind: 'text', description: "Path to the pipeline's log file." },
  { path: 'logging.cost_tracking', section: 'Logging', label: 'Cost Tracking', kind: 'boolean', description: 'Log per-chapter API cost (input/output/cache tokens → USD) alongside normal logging.' },

  // ── MCP Harness ────────────────────────────────────────────────────────
  { path: 'mcp_harness.persona.file', section: 'MCP Harness — Persona', label: 'Persona File', kind: 'text', description: 'Persona file prepended to every LLM system instruction the MCP server builds.' },
  { path: 'mcp_harness.persona.enabled', section: 'MCP Harness — Persona', label: 'Status', kind: 'boolean', description: 'Whether the persona file is actually prepended — the one knob here meant to be casually toggled.' },
  { path: 'mcp_harness.subagents.prep.enabled', section: 'MCP Harness — Subagents — Prep', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's prep subagent is active." },
  { path: 'mcp_harness.subagents.prep.model', section: 'MCP Harness — Subagents — Prep', label: 'Model', kind: 'enum', choices: MODEL_CHOICES, choiceLabels: MODEL_LABELS, description: "Model used by the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.thinking_budget', section: 'MCP Harness — Subagents — Prep', label: 'Thinking Budget', kind: 'integer', min: 0, description: "Reasoning token budget for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.effort', section: 'MCP Harness — Subagents — Prep', label: 'Effort', kind: 'enum', choices: EFFORT_CHOICES, description: "Effort level for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.prep.max_output_tokens', section: 'MCP Harness — Subagents — Prep', label: 'Maximum Output Tokens', kind: 'integer', min: 1, description: "Output ceiling for the MCP harness's prep subagent." },
  { path: 'mcp_harness.subagents.translator.harness_managed', section: 'MCP Harness — Subagents — Translator', label: 'Harness Managed', kind: 'boolean',
    description: 'Off = DeepSeekClient owns translation.translator directly (current default). On = the harness would own it instead.' },
  { path: 'mcp_harness.subagents.qc.enabled', section: 'MCP Harness — Subagents — QC', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's QC subagent is active. Filesystem-only — no LLM call regardless." },
  { path: 'mcp_harness.subagents.bible_writer.enabled', section: 'MCP Harness — Subagents — Bible Writer', label: 'Status', kind: 'boolean', description: "Whether the MCP harness's bible-writer subagent is active. Filesystem-only — no LLM call regardless." },
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
  | { kind: 'gap' }
  | { kind: 'group'; label: string }
  | { kind: 'field'; fieldIndex: number };

// Sections are already fully-qualified strings on each spec (e.g.
// "Translator — Caching — Cache Monitor"), so grouping is just "insert a
// header whenever the section changes" — no separate tree structure needed.
export function buildConfigRenderLines(fields: readonly { section: string }[]): ConfigRenderLine[] {
  const lines: ConfigRenderLine[] = [];
  let lastSection: string | null = null;
  fields.forEach((field, fieldIndex) => {
    if (field.section !== lastSection) {
      if (lastSection !== null) lines.push({ kind: 'gap' });
      lines.push({ kind: 'group', label: field.section });
      lastSection = field.section;
    }
    lines.push({ kind: 'field', fieldIndex });
  });
  return lines;
}
