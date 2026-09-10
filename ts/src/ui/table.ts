import stringWidth from 'string-width';

// Volume titles are Japanese light-novel titles. JS string length counts UTF-16
// code units, so it undercounts every full-width character by half — plain
// .padEnd()/.padStart() silently misaligns a column the instant CJK text
// appears in it. string-width measures actual terminal display columns instead.

export function truncateToWidth(text: string, width: number): string {
  if (stringWidth(text) <= width) return text;
  let result = '';
  for (const char of text) {
    if (stringWidth(result + char) > width) break;
    result += char;
  }
  return result;
}

export function padCell(text: string, width: number, align: 'left' | 'right' = 'left'): string {
  const clipped = truncateToWidth(text, width);
  const gap = ' '.repeat(Math.max(0, width - stringWidth(clipped)));
  return align === 'left' ? clipped + gap : gap + clipped;
}
