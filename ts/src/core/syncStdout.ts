const SYNC_BEGIN = '\x1b[?2026h';
const SYNC_END = '\x1b[?2026l';

// Ink (via log-update) writes exactly one string per frame: an ANSI erase-lines
// sequence immediately followed by the new frame's content, in a single
// stream.write() call. Flicker happens when the terminal's own refresh lands
// between painting the erase and painting the redraw, even though Ink handed
// it both halves atomically. DEC private mode 2026 ("Synchronized Output")
// tells a supporting terminal to buffer everything between the begin/end
// markers and paint it as one frame. An unsupported terminal silently ignores
// an unrecognized DEC private mode by design — this degrades to a no-op there,
// not a visible artifact, so no capability sniffing is needed.
export function synchronizedOutputStdout(target: NodeJS.WriteStream = process.stdout): NodeJS.WriteStream {
  if (!target.isTTY) return target;
  return new Proxy(target, {
    get(obj, prop, receiver) {
      if (prop === 'write') {
        return (chunk: unknown, ...rest: unknown[]) => {
          if (typeof chunk !== 'string') return obj.write(chunk as never, ...(rest as []));
          return obj.write(SYNC_BEGIN + chunk + SYNC_END, ...(rest as []));
        };
      }
      const value = Reflect.get(obj, prop, receiver);
      return typeof value === 'function' ? value.bind(obj) : value;
    },
  });
}
