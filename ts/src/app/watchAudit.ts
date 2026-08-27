// Diagnostic bootstrap for the libuv fs-event assertion crash on Windows.
// Patches fs.watch so every watcher opened by this process is logged with
// its resolved path, whether it's an 8.3 short form, and the call stack.
// Purpose: identify which fs.watch handle is live when libuv aborts on
// `assert(!_wcsnicmp(filename, dir, dirlen))` (src/win/fs-event.c:72).
//
// Output: dev/watch-audit.log (JSON-lines) AND stderr (survives kill).
// Activated: set MTL_WATCH_AUDIT=1 before launching. No-op otherwise.
//
// Wire: imported as the very first import in src/app/index.tsx so it runs
// before any dependency opens an fs.watch handle.

if (process.env.MTL_WATCH_AUDIT === '1' && process.platform === 'win32') {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const { fileURLToPath } = await import('node:url');

  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);

  const logPath = path.resolve(__dirname, '..', '..', '..', 'dev', 'watch-audit.log');
  const write = (obj: Record<string, unknown>): void => {
    const line = JSON.stringify(obj);
    try { fs.appendFileSync(logPath, line + '\n'); } catch { /* best effort */ }
    try { process.stderr.write(`[watch-audit] ${line}\n`); } catch { /* TTY may be gone */ }
  };

  const isShortForm = (p: string): boolean => /~[0-9]/.test(p);
  const hasNonAscii = (p: string): boolean => /[^\x00-\x7F]/.test(p);

  // Patch the module object (`fs.default`), NOT the namespace: an ESM module
  // namespace is read-only, so `fs.watch = ...` throws
  // "Cannot assign to read only property 'watch' of object '[object Module]'"
  // and the audit silently never starts. Callers must also reach fs.watch
  // through the module object for the patch to be visible — a named
  // `import { watch } from 'node:fs'` is bound at instantiation and keeps the
  // original (see useFileWatcher.ts, which imports the default for this).
  const fsModule = fs.default;
  const originalWatch = fsModule.watch;
  type WatchFn = typeof originalWatch;
  fsModule.watch = function patchedWatch(target, ...rest) {
    let resolved = String(target);
    try { resolved = fs.realpathSync.native(resolved); } catch { /* keep raw */ }
    write({
      kind: 'fs.watch',
      target: String(target),
      resolved,
      shortForm: isShortForm(resolved),
      nonAscii: hasNonAscii(resolved),
      recursive: !!(rest[0] && typeof rest[0] === 'object' && (rest[0] as Record<string, unknown>).recursive),
      stack: new Error('watch-open').stack ?? '(no stack)',
    });
    return (originalWatch as (...args: unknown[]) => ReturnType<typeof originalWatch>)(target, ...rest);
  } as WatchFn;

  write({ kind: 'audit-start', cwd: process.cwd(), argv: process.argv, node: process.version, uv: process.versions.uv });
}
