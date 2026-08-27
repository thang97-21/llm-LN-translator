// MTLS QC gate dispatch — leaf roles + prose aggregator.
// Invoked via the `workflow` tool by the mtls-qc-dispatch skill.
//
// meta: { name: mtls-qc-gate, description: "Dispatch the MTLS QC leaf roles, aggregate prose lanes, collect per-agent summaries." }
// args: { vol_id: string, workDir: string, mode: "pre_translation"|"idlm", flags: {is_sequel?, has_bible?, has_vrec?, has_inheritance_handoff?} }
//
// Leaf roles WRITE their full JSON reports to workDir/QC/ themselves
// (ground truth per mtl-qc §7). This script collects summaries; the
// invoking agent reads the JSON files from disk and assembles the locked
// POST_TRANSLATION_AUDIT.md.

const LIB = "D:/MTLS/.dsh/subagents";

const SUMMARY_SCHEMA = {
  type: "object",
  properties: {
    agent: { type: "string" },
    passed: { type: "boolean" },
    critical: { type: "number" },
    high: { type: "number" },
    medium: { type: "number" },
    low: { type: "number" },
    info: { type: "number" },
    summary: { type: "string" }
  },
  required: ["agent", "passed", "critical", "high", "medium", "low", "info", "summary"],
  additionalProperties: false
};

const { vol_id, workDir, mode, flags = {} } = args ?? {};

if (!vol_id || !workDir || !mode) {
  throw new Error("mtls-qc-gate: args.vol_id, args.workDir and args.mode are required");
}
if (mode !== "pre_translation" && mode !== "idlm") {
  throw new Error(`mtls-qc-gate: mode must be pre_translation|idlm, got "${mode}"`);
}

phase("QC gate dispatch");

const LEAVES = mode === "pre_translation"
  ? [{ name: "qc-structural", role: "qc-structural" }]
  : [
      { name: "qc-structural", role: "qc-structural" },
      { name: "qc-names", role: "qc-names" },
      { name: "qc-linguistic", role: "qc-linguistic" },
      { name: "qc-prose-character", role: "qc-prose-character" },
      { name: "qc-prose-narrator", role: "qc-prose-narrator" },
      { name: "qc-prose-emotional-peak", role: "qc-prose-emotional-peak" }
    ];

function promptFor(leaf) {
  return [
    `You are the QC leaf role <${leaf.name}> for MTLS volume ${vol_id} (mode: ${mode}).`,
    `Read your role definition at ${LIB}/${leaf.role}.md and follow it EXACTLY — its check tables and severity mapping are binding.`,
    `Flags: is_sequel=${flags.is_sequel ?? false}, has_bible=${flags.has_bible ?? false}, has_vrec=${flags.has_vrec ?? false}, has_inheritance_handoff=${flags.has_inheritance_handoff ?? false}.`,
    `Work dir: ${workDir} (read JP/ and EN/ chapters, context.xml, manifest.json; read golden samples from .dsh/skills/mtl-quality-evaluator/ references if your role requires calibration).`,
    `CRITICAL: write your complete JSON report to ${workDir}/QC/${leaf.name}_report.json — the orchestrator reads these files as ground truth; no JSON file, no gate clearance.`,
    `Then return ONLY the summary JSON object with your agent name, passed verdict, severity counts, and a one-paragraph summary.`
  ].join("\n");
}

// IDLM prose lanes write their own JSONs; the qc-prose aggregator then reads
// them (register provenance §E.0, aggregation, calibration) and writes
// qc-prose_report.json. Pre-translation mode skips the prose layer entirely.
async function runLeaves() {
  const settled = await parallel(LEAVES.map((leaf) => () =>
    agent(promptFor(leaf), {
      label: `qc:${leaf.name}`,
      phase: mode === "idlm" ? "qc leaves" : "qc structural gate",
      schema: SUMMARY_SCHEMA
    })
  ));
  return settled.map((value, idx) => ({ agent: LEAVES[idx].name, value }));
}

const leafResults = await runLeaves();

let prose = null;
if (mode === "idlm") {
  phase("qc-prose aggregation");
  const prosePrompt = [
    `You are the qc-prose aggregator for MTLS volume ${vol_id}.`,
    `Read your role definition at ${LIB}/qc-prose.md and follow it EXACTLY.`,
    `Read the three prose lane JSON reports from ${workDir}/QC/: qc-prose-character_report.json, qc-prose-narrator_report.json, qc-prose-emotional-peak_report.json.`,
    `Run §0.4 Register Provenance Detection BEFORE metrics, aggregate the lanes, apply SSP calibration against the golden-sample corpus, and write the consolidated ${workDir}/QC/qc-prose_report.json.`,
    `Return ONLY the summary JSON object: agent "qc-prose", your passed verdict, severity counts, and a one-paragraph provenance+quality summary.`
  ].join("\n");
  prose = await agent(prosePrompt, { label: "qc:qc-prose", phase: "qc prose aggregation", schema: SUMMARY_SCHEMA });
}

const agents = leafResults.map(({ agent, value }) => ({
  agent,
  passed: value?.passed ?? false,
  critical: value?.critical ?? 0,
  high: value?.high ?? 0,
  medium: value?.medium ?? 0,
  low: value?.low ?? 0,
  info: value?.info ?? 0,
  summary: value?.summary ?? "(no summary returned)"
}));

const leafFailed = agents.filter((a) => !a.passed || a.critical > 0).map((a) => a.agent);
log(`leaf agents: ${agents.length}, gate-blocking: ${leafFailed.length}`);

return {
  vol_id,
  mode,
  flags,
  agents,
  prose: prose ?? null,
  gate_blocking: leafFailed,
  reports_dir: `${workDir}/QC`
};
