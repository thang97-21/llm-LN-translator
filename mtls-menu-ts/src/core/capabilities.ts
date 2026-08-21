import path from 'node:path';
import type { CapabilityRisk, CapabilitySpec, FieldSpec, FormValues, McpRoute, ValidationIssue } from './types.js';

const volume: FieldSpec = { key: 'volume_id', label: 'Volume', kind: 'volume', required: true, positional: true };
const epub: FieldSpec = { key: 'epub_path', label: 'EPUB', kind: 'epub', required: true, positional: true, projectScoped: true };
const volumeId: FieldSpec = { key: 'volume_id_override', label: 'Volume ID override', kind: 'text', cliFlag: '--volume-id' };
const seriesId: FieldSpec = { key: 'series_id', label: 'Series ID', kind: 'text', cliFlag: '--series-id' };
// Developer flag — see DeveloperPanel (App.tsx) for the Dashboard-level
// default toggle. 'paid' risk stays on the capability regardless of this
// field's value: the field only affects what THIS launch does, and
// defaulting the whole capability to a lower risk tier would misrepresent
// every non-dry-run launch of the same form.
const dryRun: FieldSpec = { key: 'dry_run', label: 'Dry Run (assemble payload, send nothing)', kind: 'boolean', cliFlag: '--dry-run', defaultValue: false };
const buildDryRun: FieldSpec = { key: 'dry_run', label: 'Dry Run (assemble structure, package nothing)', kind: 'boolean', cliFlag: '--dry-run', defaultValue: false };
// Which reader hardware the EPUB is built for. The xteink profiles fit art to
// the panel, convert it to grayscale, force baseline JPEG (progressive JPEG
// does not decode on those devices at all) and ship a stylesheet limited to the
// nine CSS properties CrossPoint firmware actually implements. Source of truth
// is src/builder/device_profiles.py.
//
// The leading blank choice is deliberate and is NOT a missing default: it means
// "use builder.device_profile from config.yaml". serializeCli drops empty
// values, so leaving it blank omits --profile and the config stays
// authoritative — giving this field a defaultValue would silently override the
// operator's configured target on every launch.
const PROFILE_CHOICES = ['', 'standard', 'xteink-x3', 'xteink-x4', 'passthrough'];
const buildProfile: FieldSpec = { key: 'profile', label: 'Device profile (blank = config.yaml default)', kind: 'enum', choices: PROFILE_CHOICES, cliFlag: '--profile' };
const mcpProfile: FieldSpec = { key: 'profile', label: 'Device profile (blank = config.yaml default)', kind: 'enum', choices: PROFILE_CHOICES };
// Optional CrossPoint-native export, rendered by an external Node converter
// configured under builder.xtc. Pages are pre-rendered bitmaps: roughly 10x
// the file size of the EPUB, and the reader permanently loses font and text
// size control. Needs an xteink-* profile. A failed render is a warning, never
// a failed build — the .epub is produced either way.
const XTC_CHOICES = ['', 'xtc', 'xtch'];
const buildEmitXtc: FieldSpec = { key: 'emit_xtc', label: 'Also emit XTC (blank = EPUB only)', kind: 'enum', choices: XTC_CHOICES, cliFlag: '--emit-xtc' };
const mcpEmitXtc: FieldSpec = { key: 'emit_xtc', label: 'Also emit XTC (blank = EPUB only)', kind: 'enum', choices: XTC_CHOICES };
// This launcher cannot answer the Librarian's re-extraction prompt — the child
// process has no usable keyboard — so extract/run always pass --force-rerun: a
// re-extraction proceeds automatically into a NEW derived volume directory and
// the old workspace (translations included) is preserved, never overwritten.
// serializeCli appends the flag after the operator's own fields.

export const CLI_CAPABILITIES: readonly CapabilitySpec[] = [
  { id: 'extract', label: 'Extract EPUB', detail: 'Extract an EPUB and create its working volume. Re-running creates a NEW volume ID; the existing workspace is never overwritten.', group: 'Workflows', route: { transport: 'cli', command: 'extract' }, fields: [epub, volumeId], risk: 'write' },
  { id: 'prep', label: 'Prep Volume', detail: 'Build context.xml through a paid DeepSeek preparation call.', group: 'Workflows', route: { transport: 'cli', command: 'prep' }, fields: [volume, seriesId], risk: 'paid' },
  { id: 'translate', label: 'Translate Volume', detail: 'Translate selected chapters, or every pending chapter when none are chosen.', group: 'Workflows', route: { transport: 'cli', command: 'translate' }, fields: [volume, { key: 'chapters', label: 'Chapters', kind: 'chapter-list', cliFlag: '--chapters' }, dryRun], risk: 'paid' },
  { id: 'qc', label: 'QC Volume', detail: 'Run the read-only filesystem quality gate.', group: 'Workflows', route: { transport: 'cli', command: 'qc' }, fields: [volume], risk: 'read' },
  { id: 'build', label: 'Build EPUB', detail: 'Package translated chapters into an EPUB.', group: 'Workflows', route: { transport: 'cli', command: 'build' }, fields: [volume, { key: 'output', label: 'Output filename', kind: 'text', cliFlag: '--output' }, buildDryRun, buildProfile, buildEmitXtc], risk: 'write' },
  { id: 'run', label: 'Full Pipeline', detail: 'Extract, prep, translate, QC, and build as one canonical CLI workflow. Re-running creates a NEW volume ID; the existing workspace is never overwritten.', group: 'Workflows', route: { transport: 'cli', command: 'run' }, fields: [epub, volumeId, seriesId], risk: 'paid' },
];

type ToolOverlay = Omit<CapabilitySpec, 'route' | 'available' | 'unavailableReason'> & { route: McpRoute };

const text = (key: string, label: string, required = false): FieldSpec => ({ key, label, kind: 'text', required });
const projectPath = (key: string, label: string, required = true): FieldSpec => ({ key, label, kind: 'project-path', required, projectScoped: true });
const bool = (key: string, label: string): FieldSpec => ({ key, label, kind: 'boolean', defaultValue: false });
const integer = (key: string, label: string, defaultValue: string, min = 1): FieldSpec => ({ key, label, kind: 'integer', defaultValue, min });

// This overlay is deliberately explicit. An MCP schema tells us a field's shape;
// it cannot tell the operator whether a call spends money or rewrites an image.
export const MCP_TOOL_OVERLAY: readonly ToolOverlay[] = [
  { id: 'extract_epub', label: 'Extract EPUB', detail: 'Run Librarian extraction.', group: 'Librarian', route: { transport: 'mcp', tool: 'extract_epub' }, fields: [projectPath('epub_path', 'EPUB'), text('volume_id', 'Volume ID'), { key: 'source_lang', label: 'Source language', kind: 'enum', choices: ['ja'], defaultValue: 'ja' }, { key: 'target_lang', label: 'Target language', kind: 'enum', choices: ['en'], defaultValue: 'en' }, bool('ref_validate', 'Reference validate')], risk: 'write' },
  { id: 'parse_opf_metadata', label: 'Parse OPF metadata', detail: 'Read OPF metadata without changing it.', group: 'Librarian', route: { transport: 'mcp', tool: 'parse_opf_metadata' }, fields: [projectPath('opf_path', 'OPF path')], risk: 'read' },
  { id: 'parse_toc', label: 'Parse table of contents', detail: 'Read an EPUB navigation file.', group: 'Librarian', route: { transport: 'mcp', tool: 'parse_toc' }, fields: [projectPath('nav_path', 'Navigation path')], risk: 'read' },
  { id: 'convert_xhtml_to_markdown', label: 'Convert XHTML to Markdown', detail: 'Convert supplied XHTML in memory.', group: 'Librarian', route: { transport: 'mcp', tool: 'convert_xhtml_to_markdown' }, fields: [{ key: 'xhtml_content', label: 'XHTML content', kind: 'multiline', required: true }, text('source_file', 'Source filename'), text('chapter_title', 'Chapter title'), text('publisher_profile', 'Publisher profile')], risk: 'read' },
  { id: 'detect_publisher', label: 'Detect publisher', detail: 'Match publisher metadata to a local profile.', group: 'Librarian', route: { transport: 'mcp', tool: 'detect_publisher' }, fields: [{ key: 'metadata', label: 'Metadata JSON', kind: 'json-object', required: true }], risk: 'read' },
  { id: 'catalog_images', label: 'Catalog images', detail: 'Inventory extracted EPUB images.', group: 'Librarian', route: { transport: 'mcp', tool: 'catalog_images' }, fields: [projectPath('epub_dir', 'EPUB directory'), text('publisher', 'Publisher')], risk: 'read' },
  { id: 'split_content', label: 'Split content', detail: 'Split supplied content into token-bounded parts.', group: 'Librarian', route: { transport: 'mcp', tool: 'split_content' }, fields: [{ key: 'spine_items', label: 'Spine items JSON', kind: 'json-array' }, { key: 'content', label: 'Content', kind: 'multiline' }, integer('max_tokens', 'Maximum tokens', '2000'), integer('min_tokens', 'Minimum tokens', '800')], risk: 'read' },
  { id: 'run_librarian', label: 'Run Librarian', detail: 'Extract and return a manifest.', group: 'Librarian', route: { transport: 'mcp', tool: 'run_librarian' }, fields: [projectPath('epub_path', 'EPUB'), text('volume_id', 'Volume ID'), { key: 'source_lang', label: 'Source language', kind: 'enum', choices: ['ja'], defaultValue: 'ja' }, { key: 'target_lang', label: 'Target language', kind: 'enum', choices: ['en'], defaultValue: 'en' }], risk: 'write' },
  { id: 'prep_volume', label: 'Prep volume', detail: 'Build context.xml through DeepSeek.', group: 'Prep', route: { transport: 'mcp', tool: 'prep_volume' }, fields: [volume, text('series_id', 'Series ID')], risk: 'paid' },
  { id: 'translate_chapter', label: 'Translate chapter', detail: 'Translate one selected JP chapter through the provider selected in config.yaml.', group: 'Translator', route: { transport: 'mcp', tool: 'translate_chapter' }, fields: [volume, { key: 'chapter_id', label: 'Chapter', kind: 'chapter-list', required: true }, bool('dry_run', 'Dry Run (assemble payload, send nothing)')], risk: 'paid' },
  { id: 'run_translator', label: 'Run translator', detail: 'Translate selected or all JP chapters through the provider selected in config.yaml.', group: 'Translator', route: { transport: 'mcp', tool: 'run_translator' }, fields: [volume, { key: 'chapters', label: 'Chapters', kind: 'chapter-list' }, bool('dry_run', 'Dry Run (assemble payload, send nothing)')], risk: 'paid' },
  { id: 'qc_volume', label: 'QC volume', detail: 'Run read-only filesystem QC.', group: 'QC', route: { transport: 'mcp', tool: 'qc_volume' }, fields: [volume], risk: 'read' },
  { id: 'write_bible', label: 'Write series bible', detail: 'Merge volume continuity into the selected series bible.', group: 'Continuity', route: { transport: 'mcp', tool: 'write_bible' }, fields: [volume, text('series_id', 'Series ID')], risk: 'write' },
  { id: 'markdown_to_xhtml', label: 'Markdown to XHTML', detail: 'Render Markdown in memory.', group: 'Builder', route: { transport: 'mcp', tool: 'markdown_to_xhtml' }, fields: [{ key: 'md_content', label: 'Markdown content', kind: 'multiline', required: true }, text('chapter_id', 'Chapter ID'), bool('skip_illustrations', 'Skip illustrations')], risk: 'read' },
  { id: 'generate_opf', label: 'Generate OPF preview', detail: 'Preview an OPF document from a manifest or volume.', group: 'Builder', route: { transport: 'mcp', tool: 'generate_opf' }, fields: [{ key: 'manifest', label: 'Manifest JSON', kind: 'json-object' }, { ...volume, required: false }], risk: 'read' },
  { id: 'generate_nav', label: 'Generate NAV preview', detail: 'Preview navigation from a manifest or volume.', group: 'Builder', route: { transport: 'mcp', tool: 'generate_nav' }, fields: [{ key: 'manifest', label: 'Manifest JSON', kind: 'json-object' }, { ...volume, required: false }], risk: 'read' },
  { id: 'merge_translated_shards', label: 'Merge translated shards', detail: 'Merge translated shard files into the spine.', group: 'Builder', route: { transport: 'mcp', tool: 'merge_translated_shards' }, fields: [volume, { key: 'target_language', label: 'Target language', kind: 'enum', choices: ['en'], defaultValue: 'en' }, bool('apply_manifest', 'Apply manifest changes')], risk: 'write' },
  { id: 'optimize_image', label: 'Optimize image', detail: 'Overwrite an image with an optimized version. A device profile supersedes Maximum width and also sets colour depth, JPEG quality and baseline encoding.', group: 'Builder', route: { transport: 'mcp', tool: 'optimize_image' }, fields: [projectPath('image_path', 'Image path'), integer('max_width', 'Maximum width', '1600'), mcpProfile], risk: 'overwrite' },
  { id: 'package_epub', label: 'Package EPUB', detail: 'Build the EPUB through the MCP builder.', group: 'Builder', route: { transport: 'mcp', tool: 'package_epub' }, fields: [volume, text('output_filename', 'Output filename'), bool('skip_qc', 'Skip QC'), bool('include_header_illustrations', 'Include header illustrations'), bool('dry_run', 'Dry Run (assemble structure, package nothing)'), mcpProfile, mcpEmitXtc], risk: 'write' },
  { id: 'run_builder', label: 'Run builder', detail: 'Build the EPUB through the canonical builder route.', group: 'Builder', route: { transport: 'mcp', tool: 'run_builder' }, fields: [volume, text('output_filename', 'Output filename'), bool('skip_qc', 'Skip QC'), bool('include_header_illustrations', 'Include header illustrations'), bool('dry_run', 'Dry Run (assemble structure, package nothing)'), mcpProfile, mcpEmitXtc], risk: 'write' },
];

export type McpListedTool = { name: string; description?: string; inputSchema?: unknown };

function schemaRequiredKeys(schema: unknown): ReadonlySet<string> {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return new Set();
  const record = schema as Record<string, unknown>;
  return new Set(Array.isArray(record.required) ? record.required.filter((key): key is string => typeof key === 'string') : []);
}

export function hydrateMcpCapabilities(tools: readonly McpListedTool[]): CapabilitySpec[] {
  const listed = new Map(tools.map((tool) => [tool.name, tool]));
  return MCP_TOOL_OVERLAY.map((overlay) => {
    const live = listed.get(overlay.route.tool);
    const required = schemaRequiredKeys(live?.inputSchema);
    return {
      ...overlay,
      fields: overlay.fields.map((field) => ({ ...field, required: field.required || required.has(field.key) })),
      available: Boolean(live),
      ...(live ? {} : { unavailableReason: 'Missing from current MCP handshake' }),
    };
  });
}

export function initialValues(spec: CapabilitySpec, activeVolume: string | null): FormValues {
  return Object.fromEntries(spec.fields.map((field) => [field.key, field.defaultValue ?? (field.kind === 'volume' ? activeVolume ?? '' : '')]));
}

export function effectiveRisk(spec: CapabilitySpec, values: FormValues): CapabilityRisk {
  if (values.apply_manifest === true || values.skip_qc === true || spec.risk === 'overwrite') return 'overwrite';
  return spec.risk;
}

export function validateCapability(spec: CapabilitySpec, values: FormValues, pipelineRoot: string): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  for (const field of spec.fields) {
    const value = values[field.key];
    if (field.required && (typeof value !== 'boolean') && !String(value ?? '').trim()) issues.push({ field: field.key, message: `${field.label} is required.` });
    if (field.kind === 'integer' && String(value ?? '').trim()) {
      const parsed = Number(value);
      if (!Number.isInteger(parsed) || (field.min !== undefined && parsed < field.min)) issues.push({ field: field.key, message: `${field.label} must be an integer of at least ${field.min ?? 0}.` });
    }
    if (field.kind === 'json-object' && String(value ?? '').trim()) {
      try { const parsed: unknown = JSON.parse(String(value)); if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(); } catch { issues.push({ field: field.key, message: `${field.label} must be a JSON object.` }); }
    }
    if (field.kind === 'json-array' && String(value ?? '').trim()) {
      try { if (!Array.isArray(JSON.parse(String(value)))) throw new Error(); } catch { issues.push({ field: field.key, message: `${field.label} must be a JSON array.` }); }
    }
    if (field.projectScoped && String(value ?? '').trim()) {
      const resolved = path.resolve(pipelineRoot, String(value));
      if (resolved !== pipelineRoot && !resolved.startsWith(`${pipelineRoot}${path.sep}`)) issues.push({ field: field.key, message: `${field.label} must stay inside this project.` });
    }
  }
  if (spec.id === 'split_content' && !String(values.content ?? '').trim() && !String(values.spine_items ?? '').trim()) issues.push({ message: 'Provide content or spine_items.' });
  if ((spec.id === 'generate_opf' || spec.id === 'generate_nav') && !String(values.manifest ?? '').trim() && !String(values.volume_id ?? '').trim()) issues.push({ message: 'Provide manifest or volume_id.' });
  return issues;
}

function nonEmpty(value: string | boolean | undefined): boolean { return typeof value === 'boolean' ? value : Boolean(value?.trim()); }

export function serializeCli(spec: CapabilitySpec, values: FormValues): string[] {
  if (spec.route.transport !== 'cli') throw new Error(`Capability ${spec.id} is not a CLI route.`);
  const argv = [spec.route.command];
  for (const field of spec.fields) {
    const value = values[field.key];
    if (!nonEmpty(value)) continue;
    if (field.positional) argv.push(String(value));
    else if (field.cliFlag) {
      argv.push(field.cliFlag);
      // argparse's boolean flags are action="store_true" — presence alone
      // means true, and a following "true"/"false" token would be parsed as
      // an unrelated positional/unrecognized argument, not consumed by the
      // flag. Never triggered before dry_run: every prior CLI boolean field
      // went through serializeMcp instead, which already handles kind ===
      // 'boolean' correctly — this was a latent gap, not a regression.
      if (field.kind === 'boolean') continue;
      if (field.kind === 'chapter-list') argv.push(...String(value).split(',').map((part) => part.trim()).filter(Boolean));
      else argv.push(String(value));
    }
  }
  // extract/run always carry --force-rerun from this launcher (see the comment
  // at CLI_CAPABILITIES) — appended last so the operator's own fields win.
  if (spec.route.command === 'extract' || spec.route.command === 'run') argv.push('--force-rerun');
  return argv;
}

export function serializeMcp(spec: CapabilitySpec, values: FormValues): Record<string, unknown> {
  if (spec.route.transport !== 'mcp') throw new Error(`Capability ${spec.id} is not an MCP route.`);
  const payload: Record<string, unknown> = {};
  for (const field of spec.fields) {
    const value = values[field.key];
    if (!nonEmpty(value)) continue;
    if (field.kind === 'boolean') payload[field.key] = value === true;
    else if (field.kind === 'integer') payload[field.key] = Number(value);
    else if (field.kind === 'chapter-list' || field.kind === 'string-list') payload[field.key] = String(value).split(',').map((part) => part.trim()).filter(Boolean);
    else if (field.kind === 'json-object' || field.kind === 'json-array') payload[field.key] = JSON.parse(String(value));
    else payload[field.key] = String(value);
  }
  return payload;
}

export function previewCapability(spec: CapabilitySpec, values: FormValues, python: string): string {
  if (spec.route.transport === 'cli') return `${python} scripts/mtl.py ${serializeCli(spec, values).map(quote).join(' ')}`;
  if (spec.route.transport === 'mcp') return `${spec.route.tool}(${JSON.stringify(serializeMcp(spec, values), null, 2)})`;
  return `local view: ${spec.route.view}`;
}

function quote(part: string): string { return /\s/.test(part) ? JSON.stringify(part) : part; }
