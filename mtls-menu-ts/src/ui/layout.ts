export type LayoutMode = 'three-pane' | 'two-pane' | 'single-pane';

export function layoutForColumns(columns: number): LayoutMode {
  if (columns >= 120) return 'three-pane';
  if (columns >= 90) return 'two-pane';
  return 'single-pane';
}
