import { watch } from 'node:fs';
import path from 'node:path';
import { useEffect, useRef } from 'react';

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
    let watcher: ReturnType<typeof watch> | null = null;
    try {
      watcher = watch(dir, (_eventType, filename) => {
        if (filename && filename !== base) return;
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => onChangeRef.current(), debounceMs);
      });
    } catch {
      // Directory watching isn't available on every platform/filesystem —
      // manual 'r' refresh still works as the fallback in that case.
    }
    return () => {
      if (timer) clearTimeout(timer);
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
//      containers) — wrapped in try/catch, never a hard dependency.
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

    let watcher: ReturnType<typeof watch> | null = null;
    try {
      watcher = watch(dirPath, () => trigger());
    } catch {
      // fs.watch unavailable for this path on this platform/filesystem —
      // the poll below is the fallback, not a special case handled here.
    }

    const poll = setInterval(() => onChangeRef.current(), pollMs);

    return () => {
      if (timer) clearTimeout(timer);
      watcher?.close();
      clearInterval(poll);
    };
  }, [dirPath, debounceMs, pollMs]);
}
