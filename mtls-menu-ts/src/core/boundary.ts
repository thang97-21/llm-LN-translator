import { z } from 'zod';

// Boundary validation for every JSON artifact the pipeline writes to disk
// and this console reads back. The pipeline is Python; these files are
// contracts, not objects — anything the Python side emits that drifts from
// these shapes must surface as a legible parse failure here, not as a
// silent `asRecord` collapse into empty defaults three renders later.

// ── manifest.json ─────────────────────────────────────────────────────────

const manifestChapterSchema = z.looseObject({
  translation_status: z.string().optional(),
  state: z.string().optional(),
});

const manifestMetadataSchema = z.looseObject({
  title: z.string().optional(),
  author: z.string().optional(),
});

const manifestMetadataEnSchema = z.looseObject({
  title_en: z.string().optional(),
  author_en: z.string().optional(),
});

const manifestPhaseSchema = z.looseObject({
  status: z.string().optional(),
});

const manifestPipelineStateSchema = z.looseObject({
  librarian: manifestPhaseSchema.optional(),
  prep: manifestPhaseSchema.optional(),
  translator: manifestPhaseSchema.optional(),
  builder: manifestPhaseSchema.optional(),
});

// `looseObject` throughout: the Python pipeline owns these files and adds
// fields freely (context block counts, QC summaries, page maps). Strict
// objects would make every additive pipeline change a console breakage;
// passthrough keeps the contract one-directional — we validate what we
// read, we don't police what they write.
export const manifestSchema = z.looseObject({
  metadata: manifestMetadataSchema.optional(),
  metadata_en: manifestMetadataEnSchema.optional(),
  pipeline_state: manifestPipelineStateSchema.optional(),
  chapters: z.array(manifestChapterSchema).optional(),
});

// ── translation_log.json ──────────────────────────────────────────────────

const translationLogQualitySchema = z.looseObject({
  passed: z.boolean().optional(),
  ai_ism_count: z.number().finite().optional(),
});

const translationLogChapterSchema = z.looseObject({
  chapter_id: z.string().optional(),
  input_tokens: z.number().finite().optional(),
  output_tokens: z.number().finite().optional(),
  success: z.boolean().optional(),
  error: z.string().nullable().optional(),
  quality: translationLogQualitySchema.optional(),
});

export const translationLogSchema = z.looseObject({
  chapters: z.array(translationLogChapterSchema).optional(),
});

// ── Parse results ─────────────────────────────────────────────────────────

// Every boundary read returns one of these instead of throwing or silently
// defaulting. Callers that can render a partial view (a volume whose
// manifest is half-corrupt still has a directory name and a mtime) take
// `issues` alongside `data`; callers that cannot proceed at all check `ok`.
export type ParseIssue = { path: string; message: string };
export type BoundaryResult<T> =
  | { ok: true; data: T; issues: readonly ParseIssue[] }
  | { ok: false; error: string };

function issuesFrom(error: z.ZodError): ParseIssue[] {
  return error.issues.map((issue) => ({
    path: issue.path.map((segment) => String(segment)).join('.') || '(root)',
    message: issue.message,
  }));
}

export function parseBoundary<S extends z.ZodType>(schema: S, raw: string, source: string): BoundaryResult<z.infer<S>> {
  let json: unknown;
  try {
    json = JSON.parse(raw);
  } catch (error) {
    return { ok: false, error: `${source}: not valid JSON — ${error instanceof Error ? error.message : String(error)}` };
  }
  const result = schema.safeParse(json);
  if (!result.success) {
    const summary = issuesFrom(result.error).map((issue) => `${issue.path}: ${issue.message}`).join('; ');
    return { ok: false, error: `${source}: schema violation — ${summary}` };
  }
  return { ok: true, data: result.data, issues: [] };
}
