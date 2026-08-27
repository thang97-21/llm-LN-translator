import { useEffect, useRef } from 'react';
import { useStdin } from 'ink';

export type WheelDirection = 'up' | 'down';

// Ink has no mouse support at all — grep node_modules/ink/build for "mouse"
// turns up nothing. Terminals report wheel scroll as SGR mouse escape
// sequences (`\x1b[<Cb;Cx;CyM`, button code 64=up/65=down, only sent once
// the terminal has mouse tracking enabled — see enableMouseTracking in
// app/index.tsx) that arrive on stdin exactly like keystrokes.
//
// This does NOT attach its own listener to stdin: Ink's <App> already owns
// stdin in paused/'readable' mode (`stdin.read()` in a `readable` handler —
// see node_modules/ink/build/components/App.js). A stream can only be in
// one read mode at a time, so a second `stdin.on('data', ...)` listener
// would flip it into flowing mode and start racing Ink for bytes. Instead
// this taps `internal_eventEmitter`, the same already-decoded-chunk
// broadcast `useInput` itself subscribes to (App.js's handleReadable emits
// 'input' with the raw chunk before any key-parsing happens). Mouse escape
// bytes don't match any known key pattern, so `useInput`'s own parser just
// no-ops on them — this is a strictly additive tap, not an interception.
//
// `internal_eventEmitter` is prefixed "internal_" — not officially public
// API — so this is coupled to Ink's current internals (v5.2.x at the time
// of writing) more tightly than a public hook would be. If a future Ink
// major version restructures input handling, this is the first place to
// check.
const SGR_MOUSE_RE = /\[<(\d+);(\d+);(\d+)[Mm]/g;

export function useMouseScroll(onScroll: (direction: WheelDirection, notches: number) => void, isActive = true): void {
  const { internal_eventEmitter } = useStdin();
  const handlerRef = useRef(onScroll);
  handlerRef.current = onScroll;

  useEffect(() => {
    if (!isActive || !internal_eventEmitter) return;

    const handleChunk = (chunk: string): void => {
      let up = 0;
      let down = 0;
      let match: RegExpExecArray | null;
      SGR_MOUSE_RE.lastIndex = 0;
      while ((match = SGR_MOUSE_RE.exec(chunk)) !== null) {
        const buttonCode = Number(match[1]);
        if ((buttonCode & 64) === 0) continue; // click/drag, not wheel — ignore
        if ((buttonCode & 1) === 0) up += 1;
        else down += 1;
      }
      if (up) handlerRef.current('up', up);
      if (down) handlerRef.current('down', down);
    };

    internal_eventEmitter.on('input', handleChunk);
    return () => {
      internal_eventEmitter.removeListener('input', handleChunk);
    };
  }, [isActive, internal_eventEmitter]);
}
