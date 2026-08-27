// fs.watch is imported through the module object, not as a named binding, so
// src/app/watchAudit.ts's diagnostic patch can actually intercept these calls
// — a named `import { watch }` from a builtin is bound at instantiation and
// never sees a later reassignment.
import fs from 'node:fs';
import type { FSWatcher, WatchListener } from 'node:fs';
import path from 'node:path';
import { useEffect, useRef } from 'react';

// Give libuv's Windows watcher a path it can round-trip, or nothing at all.
//
// libuv keeps the watched directory string verbatim (fs-event.c:
// `handle->dirw = pathw`), then for every event builds `dirw + "\" + name`,
// runs it through GetLongPathNameW, and requires the result to still start
// with dirw — uv__relative_path's `assert(!_wcsnicmp(filename, dir, dirlen))`.
// When dirw contains an 8.3 alias for any component, GetLongPathNameW expands
// that component, the prefix check fails, and libuv ABORTS the process. Not
// an exception — an abort, uncatchable from JS:
//   "Assertion failed: !_wcsnicmp(filename, dir, dirlen),
//    file src\win\fs-event.c, line 72"
// Reproduced exactly on node 24.18.1 / libuv 1.52.1 by watching a directory
// via its short path (`...\C--USE~2\B06818~1\...`) and touching one file in
// it. libuv v1.x has since replaced the assert with a graceful fallback, but
// 1.52.1 — what node 24 ships — still aborts.
//
// The previous attempt here converted watch paths TO the 8.3 short form. That
// is the crash, not the cure: it manufactures precisely the dirw libuv chokes
// on. (It never actually fired — its `powershell -Command` argument loses its
// backslashes in transit, so every call threw and fell back to the original
// path — meaning the app paid a synchronous PowerShell spawn per watcher for
// nothing.) The real defense is the opposite direction: canonicalize to the
// long, real path with GetFinalPathNameByHandleW via realpathSync.native,
// which also settles casing and reparse points, the other two ways dirw and
// GetLongPathNameW can disagree.
//
// Returns null when watching should be skipped: the directory is gone, or
// MTL_NO_FS_WATCH=1 is set. Since a bad watch path kills the process rather
// than raising, that env switch is the escape hatch if this ever recurs —
// both hooks below keep working off their polls.
function canonicalWatchDir(dir: string): string | null {
  if (process.env.MTL_NO_FS_WATCH === '1') return null;
  try {
    return fs.realpathSync.native(dir);
  } catch {
    return null;
  }
}

// Open a watcher on the canonical form of `dir`, or null if unavailable.
// The 'error' listener is not optional: an async watcher failure with no
// listener is an unhandled 'error' event, which takes the process down. Every
// caller has a poll or a manual refresh behind it, so degrading is survivable.
function openWatcher(dir: string, listener: WatchListener<string>): FSWatcher | null {
  const target = canonicalWatchDir(dir);
  if (target === null) return null;
  try {
    const watcher = fs.watch(target, listener);
    watcher.on('error', () => { /* degraded: caller falls back to poll/manual */ });
    return watcher;
  } catch {
    // Not available on every platform/filesystem (network drives, some
    // containers) — never a hard dependency.
    return null;
  }
}

// Watches the PARENT DIRECTORY of filePath, not the file itself. fs.watch
// on a file handle dies the moment an editor saves via atomic write (temp
// file + rename over the original) — which is how VS Code, vim, and most
// editors actually save, not just this app's own in-place writeFileSync.
// Watching the directory and filtering by basename survives that; the
// callback also fires for the app's own saves, so a Configuration-screen
// edit and an external hand-edit look identical from here.
export function useFileWatcher(filePath: string, onChange: () => void, debounceMs = 200): void {
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    let timer: ReturnType<typeof setTimeout> | null = null;
    const watcher = openWatcher(dir, (_eventType, filename) => {
      if (filename && filename !== base) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => onChangeRef.current(), debounceMs);
    });
    // Only when there is no watcher (MTL_NO_FS_WATCH, an absent directory, an
    // unsupported filesystem): a stat poll, so live reload degrades in speed
    // rather than disappearing and leaving manual 'r' as the only path.
    let poll: ReturnType<typeof setInterval> | null = null;
    if (watcher === null) {
      // `primed` rather than a sentinel value for `seen`: '' is the legitimate
      // reading for "file absent", and a file appearing later is exactly the
      // change worth reporting, not a first observation to swallow.
      let primed = false;
      let seen = '';
      poll = setInterval(() => {
        let stamp = '';
        try { const stats = fs.statSync(filePath); stamp = `${stats.mtimeMs}:${stats.size}`; } catch { stamp = ''; }
        if (!primed) { primed = true; seen = stamp; return; }
        if (stamp === seen) return;
        seen = stamp;
        onChangeRef.current();
      }, 4000);
    }
    return () => {
      if (timer) clearTimeout(timer);
      if (poll) clearInterval(poll);
      watcher?.close();
    };
  }, [filePath, debounceMs]);
}

// Watches a DIRECTORY itself (not one file) for any direct child appearing,
// renaming, or disappearing — e.g. a new volume folder landing under work/
// mid-session. Built for exactly one bug: the volume list is a one-time
// snapshot from loadVolumes() at app startup (see App.tsx's `volumes`
// useState initializer), and nothing was refreshing it — not after a run
// this app itself launched (Extract EPUB finishing has no wiring to
// re-scan), and definitely not when a volume was created by an entirely
// separate process (a CLI extract in another terminal) the app has no
// other way to learn about. loadVolumesFrom() (mtls.ts) itself was never
// the problem — it finds and correctly ranks a brand-new volume by
// updatedAt the instant it's asked to look; the app just wasn't asking.
//
// Non-recursive by choice, not by platform limitation: recursive watching
// on work/ would also fire on every chapter file a translate run writes
// (EN/*.md, THINKING/*.md, manifest.json per chapter), turning one
// volume-list refresh into a refresh storm mid-translation. A direct-child
// create/rename event is everything "a new volume just appeared" needs —
// Librarian's extract finishes as one blocking call, so by the time the
// new work/<vol>/ directory is even visible to a watcher, manifest.json is
// already inside it.
//
// Three resilience layers, weakest link last:
//   1. fs.watch on the directory — near-instant, but fs.watch is known to
//      silently miss events under rapid successive writes, and isn't
//      guaranteed on every platform/filesystem (network drives, some
//      containers) — opened through openWatcher, never a hard dependency,
//      and absent entirely under MTL_NO_FS_WATCH=1.
//   2. A periodic poll (pollMs) — the actual fallback: catches whatever the
//      watcher missed or never had, independent of whether layer 1 is
//      working at all. This is what makes detection resilient rather than
//      "usually works."
//   3. The caller's own manual refresh (App.tsx's 'r' key) — always
//      available, zero dependencies, the fallback of last resort.
export function useDirectoryWatcher(
  dirPath: string,
  onChange: () => void,
  options?: { debounceMs?: number; pollMs?: number },
): void {
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const debounceMs = options?.debounceMs ?? 500;
  const pollMs = options?.pollMs ?? 8000;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const trigger = (): void => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => onChangeRef.current(), debounceMs);
    };

    // openWatcher returns null when watching is unavailable or switched off;
    // the poll below is the fallback, not a special case handled here.
    const watcher = openWatcher(dirPath, () => trigger());

    const poll = setInterval(() => onChangeRef.current(), pollMs);

    return () => {
      if (timer) clearTimeout(timer);
      watcher?.close();
      clearInterval(poll);
    };
  }, [dirPath, debounceMs, pollMs]);
}
