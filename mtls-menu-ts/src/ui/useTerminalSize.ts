import { useEffect, useState } from 'react';
import { useStdout } from 'ink';

export type TerminalSize = {
  rows: number;
  columns: number;
};

// Track the live terminal dimensions so list/log windows can size to the screen
// instead of a hardcoded height, and re-render on resize.
export function useTerminalSize(): TerminalSize {
  const { stdout } = useStdout();
  const read = (): TerminalSize => ({
    rows: stdout.rows ?? 24,
    columns: stdout.columns ?? 80,
  });
  const [size, setSize] = useState<TerminalSize>(read);

  useEffect(() => {
    const onResize = (): void => setSize(read);
    stdout.on('resize', onResize);
    return () => {
      stdout.off('resize', onResize);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stdout]);

  return size;
}
