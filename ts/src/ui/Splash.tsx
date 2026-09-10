import { useEffect } from 'react';
import { Box, Text } from 'ink';
import Gradient from 'ink-gradient';
import BigText from 'ink-big-text';
import { layoutForColumns } from './layout.js';

const SPLASH_DURATION_MS = 1000;
const MARK = 'MTL';

// Dismissal on keypress lives in App's own useInput (it already fires on
// every key regardless of splash state) rather than a second useInput here —
// one handler deciding "is the splash still up" beats two hooks racing to
// answer the same question on the same keystroke.
export function Splash({ columns, rows, onDone }: { columns: number; rows: number; onDone: () => void }) {
  useEffect(() => {
    const timer = setTimeout(onDone, SPLASH_DURATION_MS);
    return () => clearTimeout(timer);
  }, [onDone]);

  // ink-big-text's font is wide per character — the full product name would
  // overflow a narrow terminal's width. Below the single-pane threshold, skip
  // it entirely rather than let it wrap into an unreadable banner.
  const compact = layoutForColumns(columns) === 'single-pane';

  return (
    <Box flexDirection="column" alignItems="center" justifyContent="center" width={columns} height={rows}>
      {compact ? (
        <Gradient name="atlas"><Text bold>{MARK} · Operator Console</Text></Gradient>
      ) : (
        <Box flexDirection="column" alignItems="center">
          <BigText text={MARK} font="tiny" />
          <Gradient name="atlas"><Text>Operator Console</Text></Gradient>
        </Box>
      )}
    </Box>
  );
}
