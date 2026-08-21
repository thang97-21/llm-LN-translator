import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { CLI_CAPABILITIES, MCP_TOOL_OVERLAY, effectiveRisk, hydrateMcpCapabilities, serializeCli, serializeMcp, validateCapability } from '../src/core/capabilities.js';
import { appendConsole, browseConsole, createConsole, jumpConsole, setConsoleMode, visibleConsoleEntries } from '../src/core/console.js';
import { loadConfigFields } from '../src/core/configFile.js';
import { CONFIG_FIELDS, activeProvider, buildConfigRenderLines, filterFieldsForProvider, formatConfigValue, validateConfigInput } from '../src/core/configSchema.js';
import { loadRuntimeConfigLines, loadVolumeDetailFrom, loadVolumesFrom } from '../src/core/mtls.js';
import { resolvePreflightRequirements } from '../src/core/preflight.js';
import { asVolumeId } from '../src/core/types.js';
import { layoutForColumns } from '../src/ui/layout.js';

function capability(id: string) { const found = [...CLI_CAPABILITIES, ...MCP_TOOL_OVERLAY].find((item) => item.id === id); assert.ok(found, `missing capability ${id}`); return found; }

const extract = capability('extract');
assert.deepEqual(serializeCli(extract, { epub_path: 'raw/book.epub', volume_id_override: 'VOL-01' }), ['extract', 'raw/book.epub', '--volume-id', 'VOL-01', '--force-rerun']);
assert.deepEqual(serializeCli(capability('run'), { epub_path: 'raw/book.epub' }), ['run', 'raw/book.epub', '--force-rerun']);
assert.deepEqual(serializeCli(capability('prep'), { volume_id: 'VOL-01', series_id: 'series-a' }), ['prep', 'VOL-01', '--series-id', 'series-a']);
assert.deepEqual(serializeCli(capability('translate'), { volume_id: 'VOL-01', chapters: 'CHAPTER_01, CHAPTER_02' }), ['translate', 'VOL-01', '--chapters', 'CHAPTER_01', 'CHAPTER_02']);
assert.deepEqual(serializeCli(capability('build'), { volume_id: 'VOL-01', output: 'clean.epub' }), ['build', 'VOL-01', '--output', 'clean.epub']);

assert.equal(MCP_TOOL_OVERLAY.length, 20, 'every existing MCP tool needs UI metadata');
const hydrated = hydrateMcpCapabilities(MCP_TOOL_OVERLAY.map((item) => ({ name: item.route.tool })));
assert.equal(hydrated.filter((item) => item.available).length, 20);
assert.equal(hydrateMcpCapabilities([{ name: 'qc_volume' }]).filter((item) => item.available).length, 1);

const splitPayload = serializeMcp(capability('split_content'), { spine_items: '[{"content":"hello"}]', content: '', max_tokens: '2000', min_tokens: '800' });
assert.deepEqual(splitPayload, { spine_items: [{ content: 'hello' }], max_tokens: 2000, min_tokens: 800 });
assert.deepEqual(serializeMcp(capability('merge_translated_shards'), { volume_id: 'VOL-01', target_language: 'en', apply_manifest: true }), { volume_id: 'VOL-01', target_language: 'en', apply_manifest: true });
assert.equal(effectiveRisk(capability('merge_translated_shards'), { volume_id: 'VOL-01', apply_manifest: true }), 'overwrite');
assert.equal(effectiveRisk(capability('package_epub'), { volume_id: 'VOL-01', skip_qc: true }), 'overwrite');
assert.match(validateCapability(capability('split_content'), { spine_items: '', content: '', max_tokens: '0', min_tokens: 'x' }, path.resolve('project')).map((item) => item.message).join(' '), /Provide content or spine_items/);
assert.equal(validateCapability(capability('generate_opf'), { manifest: '{"chapters":[]}', volume_id: '' }, path.resolve('project')).length, 0);
assert.ok(validateCapability(capability('parse_toc'), { nav_path: '..\\outside.xhtml' }, path.resolve('project')).length > 0);

const fixture = mkdtempSync(path.join(os.tmpdir(), 'llm-translator-console-'));
try {
  const volume = path.join(fixture, 'VOL-01'); mkdirSync(path.join(volume, 'JP'), { recursive: true }); mkdirSync(path.join(volume, 'EN')); mkdirSync(path.join(volume, 'QC'));
  writeFileSync(path.join(volume, 'manifest.json'), JSON.stringify({ metadata: { title: 'JP Title', author: 'Author' }, metadata_en: { title_en: 'English Title', author_en: 'Writer' }, pipeline_state: { librarian: { status: 'completed' }, prep: { status: 'completed' }, translator: { status: 'running' } }, chapters: [{ translation_status: 'completed' }, { translation_status: 'pending' }] }));
  writeFileSync(path.join(volume, 'JP', 'CHAPTER_01.md'), 'jp'); writeFileSync(path.join(volume, 'EN', 'CHAPTER_01_EN.md'), 'one two'); writeFileSync(path.join(volume, 'QC', 'report.json'), '{}'); writeFileSync(path.join(volume, 'translation_log.json'), JSON.stringify({ chapters: [{ chapter_id: 'CHAPTER_01', input_tokens: 10, output_tokens: 20, success: true, quality: { passed: true, ai_ism_count: 0 } }] }));
  const volumes = loadVolumesFrom(fixture); assert.equal(volumes.length, 1); assert.equal(volumes[0]?.title, 'English Title'); assert.equal(volumes[0]?.translatedCount, 1);
  const detail = loadVolumeDetailFrom(fixture, asVolumeId('VOL-01')); assert.equal(detail.jpChapters, 1); assert.equal(detail.enWords, 2); assert.equal(detail.qcReports, 1); assert.equal(detail.totalOutputTokens, 20);
  // Boundary behavior: a corrupt manifest must surface as a degraded entry
  // with manifestError set — never vanish silently into an empty list.
  const corrupt = path.join(fixture, 'VOL-CORRUPT'); mkdirSync(corrupt);
  writeFileSync(path.join(corrupt, 'manifest.json'), '{"metadata": "this should be an object"}');
  const withCorrupt = loadVolumesFrom(fixture);
  const degraded = withCorrupt.find((volume) => volume.id === 'VOL-CORRUPT');
  assert.ok(degraded, 'corrupt manifest must still produce a volume entry');
  assert.ok(degraded.manifestError?.includes('schema violation'), 'degraded entry must carry the parse failure');
  assert.equal(degraded.chapterCount, 0);
  // And invalid JSON entirely — not just schema-violating JSON.
  writeFileSync(path.join(corrupt, 'manifest.json'), '{not json at all');
  const stillThere = loadVolumesFrom(fixture).find((volume) => volume.id === 'VOL-CORRUPT');
  assert.ok(stillThere?.manifestError?.includes('not valid JSON'));
} finally { rmSync(fixture, { recursive: true, force: true }); }

assert.equal(layoutForColumns(120), 'three-pane'); assert.equal(layoutForColumns(90), 'two-pane'); assert.equal(layoutForColumns(89), 'single-pane');
// Dashboard's grouped ConfigLine[] tree (Runtime configuration panel).
// Asserts against whichever provider config.yaml's translation.provider
// actually activates — "the active provider menu, not a stale amalgam of
// every route" (see mtls.ts's own comment on loadRuntimeConfigLines).
const runtimeConfig = loadRuntimeConfigLines();
assert.ok(runtimeConfig.some((line) => line.kind === 'value' && line.label === 'Model' && line.value === 'GPT-5.6 Luna'));
assert.ok(runtimeConfig.some((line) => line.kind === 'value' && line.label === 'Endpoint' && line.value === 'OpenAI Responses'));
assert.ok(!runtimeConfig.some((line) => line.kind === 'value' && /api[_ -]?key/i.test(line.label)));
assert.ok(!runtimeConfig.some((line) => line.kind === 'group' && (line.label === 'Prep' || line.label === 'Builder')));
// Every boolean-shaped value line must carry a boolState for the Dashboard's
// green/red color-coding — checked structurally, not against any specific
// field's current true/false (that's live state the Configuration screen
// lets a user flip, so it isn't a stable thing for this test to assume).
assert.ok(runtimeConfig.every((line) => line.kind !== 'value' || (line.value !== 'Enabled' && line.value !== 'Disabled') || line.boolState !== null));

// Configuration screen's full-file schema: every hand-authored dot-path
// must actually resolve against the real config.yaml, or the field is
// silently unreachable/unsaveable in the TUI.
const configFields = loadConfigFields();
assert.equal(configFields.length, CONFIG_FIELDS.length);
const unresolved = configFields.filter((field) => field.lineIndex === null);
assert.equal(unresolved.length, 0, `unresolved config paths: ${unresolved.map((field) => field.path).join(', ')}`);
assert.equal(new Set(CONFIG_FIELDS.map((field) => field.path)).size, CONFIG_FIELDS.length, 'duplicate config field path in schema');
const drdiField = configFields.find((field) => field.path === 'translation.deepseek.optimizations.drdi.enabled')!;
assert.ok(drdiField);
assert.equal(formatConfigValue(drdiField, 'true'), 'Enabled');
assert.equal(formatConfigValue(drdiField, 'false'), 'Disabled');
assert.equal(validateConfigInput({ ...drdiField, kind: 'integer' }, '12').ok, true);
assert.equal(validateConfigInput({ ...drdiField, kind: 'integer' }, 'nope').ok, false);
assert.equal(validateConfigInput({ ...drdiField, kind: 'float', min: 0, max: 1 }, '1.5').ok, false);

// Provider-gated configuration menus: every area is its own submenu, and only
// the selected provider's settings are shown.
const providerField = configFields.find((field) => field.path === 'translation.provider')!;
assert.ok(providerField, 'translation.provider must be an editable field');
assert.equal(activeProvider(configFields), (providerField.rawValue ?? '').trim().toLowerCase());
const deepseekView = filterFieldsForProvider(CONFIG_FIELDS, 'deepseek');
assert.ok(deepseekView.some((field) => field.path.startsWith('translation.deepseek.')));
assert.ok(!deepseekView.some((field) => field.path.startsWith('translation.qwen.')));
const qwenView = filterFieldsForProvider(CONFIG_FIELDS, 'qwen');
assert.ok(qwenView.some((field) => field.path.startsWith('translation.qwen.')));
assert.ok(!qwenView.some((field) => field.path.startsWith('translation.deepseek.') || field.path.startsWith('translation.openai.')));
const openaiView = filterFieldsForProvider(CONFIG_FIELDS, 'openai');
assert.ok(openaiView.some((field) => field.path === 'translation.openai.reasoning.effort'));
assert.ok(openaiView.some((field) => field.path.endsWith('warn_threshold_breakpoint_success_rate')));
assert.ok(openaiView.some((field) => field.path.endsWith('warn_threshold_prefix_recovery')));
assert.ok(openaiView.some((field) => field.path.endsWith('min_repeat_calls_before_warning')));
assert.ok(!openaiView.some((field) => field.path === 'translation.openai.caching.cache_monitor.warn_threshold_cache_hit_ratio'));
assert.ok(!openaiView.some((field) => field.path.startsWith('translation.deepseek.') || field.path.startsWith('translation.qwen.')));
const anthropicView = filterFieldsForProvider(CONFIG_FIELDS, 'anthropic');
assert.ok(anthropicView.some((field) => field.path === 'translation.anthropic.thinking.effort'));
assert.ok(anthropicView.some((field) => field.path === 'translation.anthropic.batch.enabled'));
assert.ok(!anthropicView.some((field) => field.path.startsWith('translation.deepseek.') || field.path.startsWith('translation.qwen.') || field.path.startsWith('translation.openai.')));
const unsetView = filterFieldsForProvider(CONFIG_FIELDS, '');
assert.ok(!unsetView.some((field) => field.path.startsWith('translation.deepseek.') || field.path.startsWith('translation.qwen.') || field.path.startsWith('translation.openai.') || field.path.startsWith('translation.anthropic.')));
const menus = [...new Set(CONFIG_FIELDS.map((field) => field.menu))];
assert.ok(['Project', 'Paths', 'Prep', 'Translation', 'Builder', 'Logging', 'MCP', 'Bible'].every((menu) => menus.includes(menu)));
const render = buildConfigRenderLines(deepseekView);
assert.ok(render.some((line) => line.kind === 'menu' && line.label === 'Translation'));
assert.ok(!render.some((line) => line.kind === 'section' && line.label.startsWith('Qwen')));
// A section that merely repeats its menu name (Project, Logging) is suppressed
// so the menu header isn't duplicated in the UI.
const deduplicated = buildConfigRenderLines(CONFIG_FIELDS);
assert.ok(!deduplicated.some((line) => line.kind === 'section' && (line.label === 'Project' || line.label === 'Logging')));
assert.ok(deduplicated.some((line) => line.kind === 'menu' && line.label === 'Project'));
assert.ok(deduplicated.some((line) => line.kind === 'menu' && line.label === 'Logging'));

const openaiPreflight = resolvePreflightRequirements(`
translation:
  provider: openai
  openai:
    api_key_env: OPENAI_TRANSLATION_KEY
`);
assert.deepEqual(openaiPreflight, { provider: 'openai', apiKeyEnv: 'OPENAI_TRANSLATION_KEY', imports: 'lxml,bs4,PIL,yaml,mcp,openai' });

const anthropicPreflight = resolvePreflightRequirements(`
translation:
  provider: anthropic
  anthropic:
    api_key_env: ANTHROPIC_TRANSLATION_KEY
`);
assert.deepEqual(anthropicPreflight, { provider: 'anthropic', apiKeyEnv: 'ANTHROPIC_TRANSLATION_KEY', imports: 'lxml,bs4,PIL,yaml,mcp,anthropic' });
let consoleState = createConsole(); for (let index = 0; index < 5_010; index += 1) consoleState = appendConsole(consoleState, `line ${index}`, 'cli');
assert.equal(consoleState.entries.length, 5_000); assert.equal(consoleState.entries[0]?.text, 'line 10');
consoleState = browseConsole(consoleState, 20, 20); assert.equal(consoleState.mode, 'browse'); assert.ok(consoleState.offset > 0); consoleState = setConsoleMode(consoleState, 'input'); assert.equal(consoleState.mode, 'input'); consoleState = jumpConsole(consoleState, 'end', 20); assert.equal(consoleState.mode, 'follow'); assert.equal(consoleState.offset, 0); assert.equal(visibleConsoleEntries(consoleState, 3).length, 3);

const packagePath = path.resolve('package.json'); const packageJson = JSON.parse(readFileSync(packagePath, 'utf8')) as { scripts: Record<string, string> }; assert.equal(packageJson.scripts.legacy, undefined); assert.equal(existsSync(path.resolve('src/legacy-launcher.cjs')), false); const entry = readFileSync(path.resolve('src/app/index.tsx'), 'utf8'); assert.ok(!entry.includes('--legacy') && !entry.includes('--mcp')); const app = readFileSync(path.resolve('src/ui/App.tsx'), 'utf8'); assert.ok(app.includes("key.ctrl && key.shift && key.escape")); assert.ok(app.includes("workspace.nav !== 'dashboard'")); assert.ok(app.includes("type: 'navCursor'")); assert.ok(!app.includes('capability{visible.length'));
console.log('operator-console tests passed');
