import { closeSync, existsSync, openSync, readSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

// A volume's identity (title / author / publisher / series) is written by the
// Python pipeline into three different artifacts, each appearing at a
// different phase, so no single file is authoritative for a volume in every
// state it can be found in:
//
//   context.xml  <volume_identity>     — Phase 1.P prep. Curated English.
//   manifest.json metadata/metadata_en — written from Phase 1 onward.
//   _epub_extracted/**/*.opf           — the publisher's own Japanese
//                                        metadata, present from extraction.
//
// Resolution is therefore PER FIELD rather than per file. A volume that has
// not run prep still writes a `<volume_identity status="pending">` block
// containing nothing but `<pending>`, so a source-level chain ("context.xml
// exists, use it") would stop at an empty block and render nothing. Each
// field independently takes the first source that actually supplies it,
// which is why an un-prepped volume still shows its Japanese title from the
// OPF instead of falling back to its directory name.
export type ResolvedIdentity = { title: string; author: string; publisher: string; series: string; hasEnglishTitle: boolean };

// English and native values stay in separate slots until the final resolve.
// The pipeline writes both (`title_en` beside `title_jp`), and collapsing them
// at read time would throw away the one bit VolumeSummary.hasEnTitle exists to
// carry: whether the title on screen is a translation or the original.
type SourceFields = { titleEn?: string; titleNative?: string; authorEn?: string; authorNative?: string; publisher?: string; series?: string };

// `</volume_identity>` closes by byte 6,614 in the worst of the 20 volumes on
// disk (the largest context.xml is 167 KB); `</metadata>` closes by byte
// 2,468 in every OPF. These caps are ~5x and ~6x the observed worst case, and
// they exist because loadVolumesFrom() re-runs on every file-watcher tick —
// parsing whole context.xml files there would mean re-reading megabytes a
// second to render four short strings.
const CONTEXT_HEAD_BYTES = 32_768;
const OPF_HEAD_BYTES = 16_384;
const CONTAINER_HEAD_BYTES = 8_192;

// Reads at most `limit` bytes from the front of a file. A cut that lands mid
// codepoint yields one U+FFFD at the very end of the string, which is
// harmless: every element read here closes far inside the cap.
function readHead(file: string, limit: number): string {
  let handle: number | null = null;
  try {
    handle = openSync(file, 'r');
    const buffer = Buffer.alloc(limit);
    const read = readSync(handle, buffer, 0, limit, 0);
    return buffer.subarray(0, read).toString('utf8');
  } catch {
    return '';
  } finally {
    if (handle !== null) { try { closeSync(handle); } catch { /* already gone */ } }
  }
}

const fromCodePoint = (value: number): string => {
  if (!Number.isInteger(value) || value < 0 || value > 0x10ffff) return '';
  try { return String.fromCodePoint(value); } catch { return ''; }
};

// `&amp;` is decoded last so that `&amp;lt;` round-trips to the literal text
// `&lt;` rather than being decoded twice into `<`.
function decodeXml(raw: string): string {
  return raw
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex: string) => fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal: string) => fromCodePoint(Number.parseInt(decimal, 10)))
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&')
    .trim();
}

// The prep model sometimes fills a field it could not answer with a literal
// non-answer ("none", "N/A") instead of omitting it. Taken at face value those
// read as real values and stop the chain dead — both volumes on disk that do
// this have their true publisher sitting in the OPF, one fallback away. The
// list stays deliberately short: only strings that cannot plausibly be a real
// title, author or publisher.
const PLACEHOLDERS = new Set(['none', 'n/a', 'na', 'null', 'nil', 'nan', 'undefined', 'unknown', 'tbd', 'pending', '-', '--', '?']);
const usable = (value: string): string => (value && !PLACEHOLDERS.has(value.toLowerCase()) ? value : '');

// Matches `<tag>` and `<tag attr="...">` but never `<tag_suffix>`, so asking
// for `publisher` inside <volume_identity> cannot accidentally return
// `<publisher_jp>`. A self-closing or absent element yields ''.
function xmlText(scope: string, tag: string): string {
  const match = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`).exec(scope);
  return match ? usable(decodeXml(match[1] ?? '')) : '';
}

// Dublin Core elements are `dc:`-prefixed in every OPF here, but the prefix is
// bound by declaration rather than fixed by spec, so accept the bare name too.
const dcText = (scope: string, name: string): string => xmlText(scope, `dc:${name}`) || xmlText(scope, name);

function contextIdentity(file: string): SourceFields {
  const block = /<volume_identity(?:\s[^>]*)?>([\s\S]*?)<\/volume_identity>/.exec(readHead(file, CONTEXT_HEAD_BYTES));
  if (!block) return {};
  const scope = block[1] ?? '';
  return {
    titleEn: xmlText(scope, 'title_en'),
    titleNative: xmlText(scope, 'title_jp'),
    authorEn: xmlText(scope, 'author_en'),
    authorNative: xmlText(scope, 'author_jp'),
    publisher: xmlText(scope, 'publisher') || xmlText(scope, 'publisher_jp'),
    series: xmlText(scope, 'series_en') || xmlText(scope, 'series_jp'),
  };
}

const asString = (value: unknown): string => (typeof value === 'string' && value.trim() ? value.trim() : '');

function firstString(source: Record<string, unknown> | undefined, ...keys: readonly string[]): string {
  if (!source) return '';
  for (const key of keys) { const found = usable(asString(source[key])); if (found) return found; }
  return '';
}

// The English block keys off bare names (`title`, `author`), the Japanese
// block off suffixed ones (`title_en`, `title_jp`). The trailing `title_en` /
// `author_en` / `title` / `author` alternatives are the shapes this console
// used to (wrongly) expect on its own; they are retained so a hand-written or
// older manifest still resolves rather than silently reading as absent.
function manifestIdentity(metadata: Record<string, unknown> | undefined, metadataEn: Record<string, unknown> | undefined): SourceFields {
  return {
    titleEn: firstString(metadataEn, 'title', 'title_en') || firstString(metadata, 'title_en'),
    titleNative: firstString(metadata, 'title_jp', 'title'),
    authorEn: firstString(metadataEn, 'author', 'author_en') || firstString(metadata, 'author_en'),
    authorNative: firstString(metadata, 'author_jp', 'author'),
    publisher: firstString(metadataEn, 'publisher') || firstString(metadata, 'publisher', 'publisher_jp'),
    series: firstString(metadataEn, 'series') || firstString(metadata, 'series_en', 'series_jp'),
  };
}

// The OPF's location is not a constant: `item/standard.opf`, `OEBPS/content.opf`
// and a bare `content.opf` all occur across the volumes on disk. META-INF/
// container.xml names the real one and is present in every extracted EPUB, so
// resolve through it the way a reader would, and only guess if it is missing.
// context.xml embeds an `opf_path` too, but it is an absolute path recorded on
// whichever machine ran extraction (`D:\MTLS\...`) — useless as a locator here.
function locateOpf(extractedRoot: string): string | null {
  const container = path.join(extractedRoot, 'META-INF', 'container.xml');
  if (existsSync(container)) {
    const declared = /<rootfile\b[^>]*\bfull-path\s*=\s*"([^"]+)"/.exec(readHead(container, CONTAINER_HEAD_BYTES))?.[1];
    if (declared) {
      // container.xml is an untrusted archive member; refuse a full-path that
      // escapes the extraction root rather than following it out of the tree.
      const base = path.resolve(extractedRoot);
      const resolved = path.resolve(base, declared);
      if ((resolved === base || resolved.startsWith(base + path.sep)) && existsSync(resolved)) return resolved;
    }
  }
  return findOpf(extractedRoot, 2);
}

function findOpf(dir: string, depth: number): string | null {
  let entries;
  try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return null; }
  for (const entry of entries) if (entry.isFile() && entry.name.toLowerCase().endsWith('.opf')) return path.join(dir, entry.name);
  if (depth <= 0) return null;
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name === 'META-INF') continue;
    const found = findOpf(path.join(dir, entry.name), depth - 1);
    if (found) return found;
  }
  return null;
}

function opfIdentity(volumeDir: string): SourceFields {
  const extractedRoot = path.join(volumeDir, '_epub_extracted');
  if (!existsSync(extractedRoot)) return {};
  const opf = locateOpf(extractedRoot);
  if (!opf) return {};
  const head = readHead(opf, OPF_HEAD_BYTES);
  // Scope to <metadata> so a <dc:title> inside a nested collection cannot win
  // over the package's own title.
  const scope = /<metadata(?:\s[^>]*)?>([\s\S]*?)<\/metadata>/.exec(head)?.[1] ?? head;
  // Everything here is the publisher's own record, so it lands in the native
  // slots — this is a last resort that renders a real Japanese title rather
  // than a directory name, never a substitute for a translated one.
  return {
    titleNative: dcText(scope, 'title'),
    // Volumes here list the illustrator as a SECOND dc:creator carrying the
    // same marc:relators role `aut` as the author, so role cannot separate
    // them. Document order is what the display-seq refinement encodes, and
    // the first creator is the author in every OPF on disk.
    authorNative: dcText(scope, 'creator'),
    publisher: dcText(scope, 'publisher'),
    // An OPF has no series concept; series stays for deriveSeries() upstream.
  };
}

// loadVolumesFrom() rebuilds every volume on each file-watcher tick, so the
// same unchanged context.xml would otherwise be re-read and re-matched
// several times a second. The stamp covers both inputs that can change at
// runtime; the OPF is written once at extraction and never edited after.
const identityCache = new Map<string, { stamp: string; identity: ResolvedIdentity }>();

function stampOf(file: string): string {
  try { const stats = statSync(file); return `${stats.mtimeMs}:${stats.size}`; } catch { return '-'; }
}

const pick = (...values: readonly (string | undefined)[]): string => values.find((value) => value && value.trim())?.trim() ?? '';

// Returns '' for any field no source supplies; the caller owns the last-resort
// display fallbacks (directory name, 'unknown', deriveSeries) because those
// are presentation choices, not metadata.
export function resolveVolumeIdentity(volumeDir: string, metadata: Record<string, unknown> | undefined, metadataEn: Record<string, unknown> | undefined): ResolvedIdentity {
  const contextPath = path.join(volumeDir, 'context.xml');
  const stamp = `${stampOf(contextPath)}|${stampOf(path.join(volumeDir, 'manifest.json'))}`;
  const cached = identityCache.get(volumeDir);
  if (cached && cached.stamp === stamp) return cached.identity;

  const fromContext = existsSync(contextPath) ? contextIdentity(contextPath) : {};
  const fromManifest = manifestIdentity(metadata, metadataEn);
  // The OPF read is the only one that walks a directory tree, so skip it
  // entirely when the two cheap sources already answered everything it could
  // have contributed.
  const answered = Boolean(
    pick(fromContext.titleEn, fromContext.titleNative, fromManifest.titleEn, fromManifest.titleNative)
    && pick(fromContext.authorEn, fromContext.authorNative, fromManifest.authorEn, fromManifest.authorNative)
    && pick(fromContext.publisher, fromManifest.publisher),
  );
  const fromOpf = answered ? {} : opfIdentity(volumeDir);

  // English is preferred across ALL sources before any native value is
  // considered, so a curated title_en in context.xml is never beaten by the
  // title_jp sitting three lines above it in the same block.
  const titleEn = pick(fromContext.titleEn, fromManifest.titleEn);
  const authorEn = pick(fromContext.authorEn, fromManifest.authorEn);
  const identity: ResolvedIdentity = {
    title: titleEn || pick(fromContext.titleNative, fromManifest.titleNative, fromOpf.titleNative),
    author: authorEn || pick(fromContext.authorNative, fromManifest.authorNative, fromOpf.authorNative),
    publisher: pick(fromContext.publisher, fromManifest.publisher, fromOpf.publisher),
    series: pick(fromContext.series, fromManifest.series),
    hasEnglishTitle: Boolean(titleEn),
  };
  identityCache.set(volumeDir, { stamp, identity });
  return identity;
}
