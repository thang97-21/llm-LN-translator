import { Box, Text } from 'ink';
import type { Preflight, VolumeSummary } from '../core/types.js';
import { asVolumeId } from '../core/types.js';
import { effectiveRisk, previewCapability } from '../core/capabilities.js';
import { listChapters } from '../core/mtls.js';
import { riskColor } from './components.js';
import type { FormState } from './workspaceMachine.js';

function basename(value: string): string { return value.split(/[\\/]/).pop() ?? value; }

export function FormPanel({ form, activeVolume, preflight, epubs, recentVolumes }: { form: FormState; activeVolume: string | null; preflight: Preflight; epubs: readonly string[]; recentVolumes: readonly VolumeSummary[] }) {
  const risk = effectiveRisk(form.spec, form.values); let preview = '(complete required fields to preview)'; try { preview = previewCapability(form.spec, form.values, preflight.python); } catch { /* validation gives the useful error */ }
  const chapters = activeVolume ? listChapters(asVolumeId(activeVolume)) : [];
  return <Box flexDirection="column" borderStyle="round" borderColor={risk === 'overwrite' ? 'red' : 'cyan'} paddingX={1}><Text bold>{form.spec.label} <Text color={riskColor(risk)}>[{risk}]</Text></Text><Text color="gray" wrap="truncate-end">{form.spec.detail}</Text>{form.spec.fields.map((field, index) => <Box key={field.key} flexDirection="column"><Text inverse={form.cursor === index} color={form.cursor === index ? 'cyan' : 'white'}>{' '}{field.label}: <Text color="yellow">{field.kind === 'epub' && form.values[field.key] ? basename(String(form.values[field.key])) : String(form.values[field.key] ?? '') || '—'}</Text>{field.required ? ' *' : ''}</Text>{form.cursor === index && field.kind === 'chapter-list' && chapters.length > 0 ? <Text color="gray">  JP: {chapters.join(', ')} · press a for all, or type comma-separated IDs</Text> : null}{form.cursor === index && field.kind === 'epub' ? <Text color="gray">  raw/: {epubs.length ? epubs.map(basename).join(', ') : '(empty — drop an EPUB in raw/)'} · Space/Enter cycles, or type a path</Text> : null}{form.cursor === index && field.kind === 'volume' ? <Text color="gray">  recent: {recentVolumes.length ? recentVolumes.map((volume) => volume.id).join(', ') : '(none yet — run extract first)'} · Space/Enter cycles the 10 latest, or type any volume ID</Text> : null}</Box>)}<Text color="gray" wrap="truncate-end">review: {preview}</Text>{form.issues.map((issue) => <Text key={issue} color="red">! {issue}</Text>)}<Text inverse={form.cursor === form.spec.fields.length} color={risk === 'overwrite' ? 'red' : 'green'}>{' '}▶ {form.confirmation === 0 ? 'Review and confirm' : form.confirmation === 1 ? risk === 'overwrite' ? 'Press Enter for overwrite warning' : 'Press Enter to launch' : 'Press Enter to acknowledge overwrite'}{' '}</Text><Text color="gray">Up/Down fields · type to edit · Space toggles booleans · Enter advances · Esc closes</Text></Box>;
}
