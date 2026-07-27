import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { CLI_CAPABILITIES, MCP_TOOL_OVERLAY, effectiveRisk, hydrateMcpCapabilities, serializeCli, serializeMcp, validateCapability } from '../src/core/capabilities.js';
import { appendConsole, browseConsole, createConsole, jumpConsole, setConsoleMode, visibleConsoleEntries } from '../src/core/console.js';
import { loadRuntimeConfigLines, loadVolumeDetailFrom, loadVolumesFrom } from '../src/core/mtls.js';
import { layoutForColumns } from '../src/ui/layout.js';

function capability(id: string) { const found = [...CLI_CAPABILITIES, ...MCP_TOOL_OVERLAY].find((item) => item.id === id); assert.ok(found, `missing capability ${id}`); return found; }

const extract = capability('extract');
assert.deepEqual(serializeCli(extract, { epub_path: 'raw/book.epub', volume_id_override: 'VOL-01' }), ['extract', 'raw/book.epub', '--volume-id', 'VOL-01']);
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

const fixture = mkdtempSync(path.join(os.tmpdir(), 'deepseek-mtls-console-'));
try {
  const volume = path.join(fixture, 'VOL-01'); mkdirSync(path.join(volume, 'JP'), { recursive: true }); mkdirSync(path.join(volume, 'EN')); mkdirSync(path.join(volume, 'QC'));
  writeFileSync(path.join(volume, 'manifest.json'), JSON.stringify({ metadata: { title: 'JP Title', author: 'Author' }, metadata_en: { title_en: 'English Title', author_en: 'Writer' }, pipeline_state: { librarian: { status: 'completed' }, prep: { status: 'completed' }, translator: { status: 'running' } }, chapters: [{ translation_status: 'completed' }, { translation_status: 'pending' }] }));
  writeFileSync(path.join(volume, 'JP', 'CHAPTER_01.md'), 'jp'); writeFileSync(path.join(volume, 'EN', 'CHAPTER_01_EN.md'), 'one two'); writeFileSync(path.join(volume, 'QC', 'report.json'), '{}'); writeFileSync(path.join(volume, 'translation_log.json'), JSON.stringify({ chapters: [{ chapter_id: 'CHAPTER_01', input_tokens: 10, output_tokens: 20, success: true, quality: { passed: true, ai_ism_count: 0 } }] }));
  const volumes = loadVolumesFrom(fixture); assert.equal(volumes.length, 1); assert.equal(volumes[0]?.title, 'English Title'); assert.equal(volumes[0]?.translatedCount, 1);
  const detail = loadVolumeDetailFrom(fixture, 'VOL-01'); assert.equal(detail.jpChapters, 1); assert.equal(detail.enWords, 2); assert.equal(detail.qcReports, 1); assert.equal(detail.totalOutputTokens, 20);
} finally { rmSync(fixture, { recursive: true, force: true }); }

assert.equal(layoutForColumns(120), 'three-pane'); assert.equal(layoutForColumns(90), 'two-pane'); assert.equal(layoutForColumns(89), 'single-pane');
const runtimeConfig = loadRuntimeConfigLines(); assert.ok(runtimeConfig.some((line) => line.trim() === 'project:')); assert.ok(runtimeConfig.some((line) => line.includes('deepseek-v4-pro'))); assert.ok(!runtimeConfig.some((line) => line.trim().startsWith('#')));
let consoleState = createConsole(); for (let index = 0; index < 5_010; index += 1) consoleState = appendConsole(consoleState, `line ${index}`, 'cli');
assert.equal(consoleState.entries.length, 5_000); assert.equal(consoleState.entries[0]?.text, 'line 10');
consoleState = browseConsole(consoleState, 20, 20); assert.equal(consoleState.mode, 'browse'); assert.ok(consoleState.offset > 0); consoleState = setConsoleMode(consoleState, 'input'); assert.equal(consoleState.mode, 'input'); consoleState = jumpConsole(consoleState, 'end', 20); assert.equal(consoleState.mode, 'follow'); assert.equal(consoleState.offset, 0); assert.equal(visibleConsoleEntries(consoleState, 3).length, 3);

const packagePath = path.resolve('package.json'); const packageJson = JSON.parse(readFileSync(packagePath, 'utf8')) as { scripts: Record<string, string> }; assert.equal(packageJson.scripts.legacy, undefined); assert.equal(existsSync(path.resolve('src/legacy-launcher.cjs')), false); const entry = readFileSync(path.resolve('src/app/index.tsx'), 'utf8'); assert.ok(!entry.includes('--legacy') && !entry.includes('--mcp')); const app = readFileSync(path.resolve('src/ui/App.tsx'), 'utf8'); assert.ok(app.includes("key.ctrl && key.shift && key.escape")); assert.ok(app.includes("workspace.nav !== 'dashboard'")); assert.ok(app.includes("type: 'navCursor'")); assert.ok(!app.includes('capability{visible.length'));
console.log('operator-console tests passed');
