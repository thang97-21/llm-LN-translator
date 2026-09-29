// MTLS QC gate: one evaluator skill, with the legacy artifact contract intact.
// meta: { name: mtls-qc-gate, description: "Run the shared translation-evaluator skill in pipeline-gate mode." }
// args: { vol_id: string, workDir: string }

const { vol_id, workDir } = args ?? {};
if (!vol_id || !workDir) {
  throw new Error("mtls-qc-gate: args.vol_id and args.workDir are required");
}

phase("QC gate");
const result = await agent(
  [
    "Load .agents/skills/translation-evaluator/SKILL.md and run its pipeline gate mode.",
    `Volume: ${vol_id}. Work directory: ${workDir}.`,
    "Use the active host model. Do not dispatch legacy qc-* agents or call DeepSeek.",
    "Determine pre-translation versus post-translation from EN/ presence.",
    "Write all required lane JSON, POST_TRANSLATION_AUDIT.md, lppm_scores.json, and manifest.json qc_status.",
    "Read the lane JSON back before deciding the gate; report missing artifacts as blocking."
  ].join("\n"),
  { label: "qc:translation-evaluator", phase: "QC gate" }
);
return { vol_id, workDir, result };
