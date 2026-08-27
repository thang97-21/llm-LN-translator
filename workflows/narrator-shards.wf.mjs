// MTLS QC narrator-lane recovery — 4 chapter shards + merge agent.
// Authored after the round-3 dogfood: the full narrator lane exceeds a single
// child's reliable completion; shards keep each child light. Run via the
// `workflow` tool with args { vol_id, workDir } when child dispatch is
// available (the round-3 environmental failure blocked execution, not design).
//
// meta: { name: mtls-qc-narrator-recovery, description: "Four-chapter narrator shards merged into the doctrine's single qc-prose-narrator_report.json" }

const LIB = "D:/MTLS/.dsh/subagents";
const { vol_id, workDir } = args ?? {};

if (!vol_id || !workDir) throw new Error("args.vol_id and args.workDir required");

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

const SHARDS = [
  { part: "qc-prose-narrator_part1.json", chapters: "CHAPTERS 1-4 ONLY", range: "CHAPTER_01..04" },
  { part: "qc-prose-narrator_part2.json", chapters: "CHAPTERS 5-8 ONLY", range: "CHAPTER_05..08" },
  { part: "qc-prose-narrator_part3.json", chapters: "CHAPTERS 9-12 ONLY", range: "CHAPTER_09..12" },
  { part: "qc-prose-narrator_part4.json", chapters: "CHAPTERS 13-16 ONLY", range: "CHAPTER_13..16" }
];

function shardPrompt(shard) {
  return [
    `You are a shard of the QC leaf role <qc-prose-narrator> for MTLS volume ${vol_id} (idlm mode). You cover ${shard.chapters}`,
    `Read your role definition at ${LIB}/qc-prose-narrator.md — its check tables and severity mapping are binding; apply them to your four chapters only.`,
    `Work dir: ${workDir} (read JP/${shard.range}.md and EN/*_EN.md for those chapters, plus context.xml for voice fingerprints and manifest.json).`,
    `CRITICAL: write your complete shard report JSON to ${workDir}/QC/${shard.part} with the same schema the role defines (agent, checks, severity counts, recommendations), scoped to your chapters.`,
    `Return ONLY the summary JSON: {"agent":"qc-prose-narrator-shard","passed":true/false,"critical":n,"high":n,"medium":n,"low":n,"info":n,"summary":"one paragraph"}.`
  ].join("\n");
}

phase("narrator shards");
const shardResults = await parallel(SHARDS.map((shard) => () =>
  agent(shardPrompt(shard), { label: `shard:${shard.range}`, phase: "narrator shards", schema: SUMMARY_SCHEMA })
));

const shardOk = shardResults.filter(Boolean).length;
log(`shards completed: ${shardOk}/${SHARDS.length}`);

if (shardOk < SHARDS.length) {
  return { vol_id, shards_completed: shardOk, shards_total: SHARDS.length, merged: null, note: "incomplete shards — merge skipped" };
}

phase("narrator merge");
const mergePrompt = [
  `You are the qc-prose-narrator MERGE agent for MTLS volume ${vol_id}.`,
  `Read your role definition at ${LIB}/qc-prose-narrator.md — the consolidated report must follow its schema exactly.`,
  `Read the four shard reports from ${workDir}/QC/: qc-prose-narrator_part1.json .. part4.json.`,
  `Merge them into ONE consolidated report (agent: "qc-prose-narrator", per-check results aggregated across all 16 chapters, severity counts summed, recommendations consolidated) and write it to ${workDir}/QC/qc-prose-narrator_report.json.`,
  `Return ONLY the summary JSON: {"agent":"qc-prose-narrator","passed":true/false,"critical":n,"high":n,"medium":n,"low":n,"info":n,"summary":"one paragraph"}.`
].join("\n");

const merged = await agent(mergePrompt, { label: "merge:narrator", phase: "narrator merge", schema: SUMMARY_SCHEMA });

return {
  vol_id,
  shards_completed: shardOk,
  shards_total: SHARDS.length,
  merged,
  report: `${workDir}/QC/qc-prose-narrator_report.json`
};
