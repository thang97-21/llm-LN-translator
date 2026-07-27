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
