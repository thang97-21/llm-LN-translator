// MTLS prep — cache-loop orchestration (MiniMax passive prompt caching).
//
// The sixteen block-fills are NOT a fan-out anymore. One byte-identical system
// prefix (`.dsh/prep/prefix.md`) is sent with sixteen per-block suffixes by
// `src/Deepseek/prep/prep_cache_client.py`; MiniMax-M3 caches the repeated
// prefix automatically (passive caching, >=512 input tokens, no API change —
// see platform.minimax.io/docs/api-reference/text-prompt-caching), and the
// client returns machine-readable JSON fragments the assembler concatenates
// into context.xml. The workflow itself cannot run the client's HTTP calls
// (no network in this runtime), so it dispatches ONE runner child whose only
// job is to execute the client and report the telemetry.
//
// meta: { name: mtls-prep-volume, description: "Run the prep block-fill cache loop and report fragments + cache telemetry" }
// args: { vol_id: string, series_id?: string, workDir: string, cacheMode?: "passive"|"explicit", model?: string, baseUrl?: string, apiKeyEnv?: string, maxTokens?: number }

const PY = "D:/MTLS/.venv/Scripts/python.exe";
const CLIENT = "src.Deepseek.prep.prep_cache_client";

const { vol_id, series_id = "", workDir, cacheMode = "passive", model = "", baseUrl = "https://zhi-api.com/v1", apiKeyEnv = "ZHI_API_API_KEY", maxTokens = 128000 } = args ?? {};

if (!vol_id || !workDir) {
  throw new Error("mtls-prep-volume: args.vol_id and args.workDir are required");
}

phase("prep cache loop");

const RUNNER_SCHEMA = {
  type: "object",
  properties: {
    ok: { type: "boolean" },
    blocks_completed: { type: "number" },
    blocks_failed: { type: "array", items: { type: "string" } },
    cache_read_ratio: { type: "number" },
    telemetry: {
      type: "object",
      properties: {
        cache_creation_input_tokens: { type: "number" },
        cache_read_input_tokens: { type: "number" },
        input_tokens: { type: "number" },
        output_tokens: { type: "number" }
      },
      required: ["cache_creation_input_tokens", "cache_read_input_tokens", "input_tokens", "output_tokens"],
      additionalProperties: false
    },
    summary: { type: "string" }
  },
  required: ["ok", "blocks_completed", "blocks_failed", "cache_read_ratio", "telemetry", "summary"],
  additionalProperties: false
};

const modelArg = model ? `--model ${model}` : `--cache-mode ${cacheMode}`;
const runnerPrompt = [
  `You are the prep cache-loop runner for MTLS volume ${vol_id}.`,
  `Execute the block-fill cache client via your shell tool (pwsh on this host), from working directory D:/MTLS:`,
  ``,
  `    ${PY} -m ${CLIENT} --vol-id ${vol_id} --work-dir ${workDir} --series-id "${series_id}" ${modelArg} --base-url ${baseUrl} --api-key-env ${apiKeyEnv} --max-tokens ${maxTokens}`,
  ``,
  `The client sends the byte-identical system context (global prefix + the volume source package — both cached under MiniMax passive caching) with a self-contained per-block suffix (embedded schema, blk-17 web results when provided), fills every block, writes fragments to ${workDir}/.context/prep_blocks/ and a receipt to ${workDir}/.receipts/prep_cache_*.json, and prints the report JSON to stdout.`,
  `If the shell call fails because the API key is unset or the endpoint is unreachable, DO NOT fabricate: return ok=false with the blocks_failed list and the exact error in the summary.`,
  `Return ONLY the summary JSON per the schema: ok, blocks_completed, blocks_failed (array), cache_read_ratio, telemetry (the four token fields from the report), summary.`
].join("\n");

const runner = await agent(runnerPrompt, { label: "runner:prep-cache", phase: "prep cache loop", schema: RUNNER_SCHEMA });

if (!runner) {
  return { vol_id, ok: false, note: "runner child failed — cache loop not executed" };
}

log(`blocks completed: ${runner.blocks_completed}, failed: ${runner.blocks_failed.length}, cache read ratio: ${runner.cache_read_ratio}`);

return {
  vol_id,
  ok: runner.ok,
  cache_mode: cacheMode,
  blocks_completed: runner.blocks_completed,
  blocks_failed: runner.blocks_failed,
  cache_read_ratio: runner.cache_read_ratio,
  telemetry: runner.telemetry,
  fragments_dir: `${workDir}/.context/prep_blocks`,
  receipt_pattern: `${workDir}/.receipts/prep_cache_*.json`,
  summary: runner.summary,
  assembly: "Inject fragments into context.xml, run self-validation, write the manifest title bridge and prep receipt (mtls-prep-dispatch skill §3)."
};
