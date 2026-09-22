import type { ConfigFieldState } from './configFile.js';

type Rates = Readonly<{
  input: number;
  cacheRead: number | null;
  cacheWrite: number | null;
  output: number;
}>;

type Promotion = Readonly<{
  label: string;
  description: string;
  startsAt?: string;
  endsAt?: string;
  rates: Rates;
  explicitCacheRead?: number;
}>;

type ModelPricing = Readonly<{
  providerLabel: string;
  displayName: string;
  source: string;
  sourceLabel: string;
  standardRates: Rates;
  explicitCacheRead?: number;
  promotion?: Promotion;
  note?: string;
}>;

export type TranslationPricingStatus = Readonly<{
  provider: string;
  model: string;
  providerLabel: string;
  displayName: string;
  rates: Rates | null;
  rateLabel: string;
  condition: string | null;
  cacheReadLabel: string;
  cacheWriteLabel: string;
  source: string | null;
  sourceLabel: string | null;
  note: string | null;
}>;

const PRICING: Readonly<Record<string, ModelPricing>> = {
  'gpt-6-astra': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Astra', source: 'https://developers.openai.com/api/docs/models/gpt-6-astra', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 10, cacheRead: 1, cacheWrite: 12.5, output: 50 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×. Batch and Flex are 50% of Standard rates.',
  },
  'gpt-6-sol': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Sol', source: 'https://developers.openai.com/api/docs/models/gpt-6-sol', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 10 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-6-luna': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Luna', source: 'https://developers.openai.com/api/docs/models/gpt-6-luna', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 0.1, cacheRead: 0.01, cacheWrite: 0.125, output: 0.5 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'deepseek-v4-flash': {
    providerLabel: 'DeepSeek', displayName: 'DeepSeek-V4-Flash', source: 'https://api-docs.deepseek.com/quick_start/pricing/', sourceLabel: 'DeepSeek API pricing docs',
    standardRates: { input: 0.14, cacheRead: 0.0028, cacheWrite: null, output: 0.28 },
    note: 'Current official API tariff is flat; no peak-hour window is published.',
  },
  'deepseek-v4-pro': {
    providerLabel: 'DeepSeek', displayName: 'DeepSeek-V4-Pro', source: 'https://api-docs.deepseek.com/quick_start/pricing/', sourceLabel: 'DeepSeek API pricing docs',
    standardRates: { input: 0.435, cacheRead: 0.003625, cacheWrite: null, output: 0.87 },
    note: 'Current official API tariff is flat; no peak-hour window is published.',
  },
  'qwen3.7-max': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Max', source: 'https://www.qwencloud.com/models/qwen3.7-max', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 2.5, cacheRead: 0.5, cacheWrite: 3.125, output: 7.5 }, explicitCacheRead: 0.25,
    promotion: {
      label: 'Limited-time 50% off',
      description: 'International Qwen 3.7 Max promotion; provider console remains the authority for eligibility.',
      rates: { input: 1.25, cacheRead: 0.25, cacheWrite: 1.5625, output: 3.75 },
      explicitCacheRead: 0.125,
    },
    note: 'Explicit cache read is $0.125/M during the promotion; implicit cache read is $0.25/M.',
  },
  'qwen3.8-max': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.8 Max', source: 'https://www.qwencloud.com/models/qwen3.8-max', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 2, cacheRead: 0.25, cacheWrite: 2.5, output: 6 }, explicitCacheRead: 0.17,
    note: 'Explicit cache read is the Qwen 3.8 Max exception ($0.17/M); implicit cache read is $0.25/M.',
  },
  'qwen3.7-plus': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Plus', source: 'https://www.qwencloud.com/models/qwen3.7-plus', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 0.4, cacheRead: 0.08, cacheWrite: 0.5, output: 1.6 }, explicitCacheRead: 0.04,
    promotion: {
      label: 'Limited-time 20% off', description: 'Current QwenCloud promotional rate; the provider controls its end date.',
      rates: { input: 0.32, cacheRead: 0.064, cacheWrite: 0.4, output: 1.28 },
      explicitCacheRead: 0.032,
    },
    note: 'Explicit cache read is $0.032/M during the promotion; implicit cache read is $0.064/M.',
  },
  'qwen3.7-flash': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Flash', source: 'https://www.qwencloud.com/models/qwen3.7-flash', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 0.03, cacheRead: 0.006, cacheWrite: 0.038, output: 0.13 }, explicitCacheRead: 0.003,
    note: 'Explicit cache read is $0.003/M; implicit cache read is $0.006/M.',
  },
  'glm-5.3': {
    providerLabel: 'Z.AI GLM', displayName: 'GLM-5.3', source: 'https://docs.z.ai/guides/overview/pricing', sourceLabel: 'Z.AI pricing',
    standardRates: { input: 1.4, cacheRead: 0.26, cacheWrite: 0, output: 4.4 },
    note: 'Cached-input storage is limited-time free; Z.AI does not price a separate cache-creation token class.',
  },
  'glm-5.3-flash': {
    providerLabel: 'Z.AI GLM', displayName: 'GLM-5.3-Flash', source: 'https://docs.z.ai/guides/overview/pricing', sourceLabel: 'Z.AI pricing',
    standardRates: { input: 0.15, cacheRead: 0.03, cacheWrite: 0, output: 0.5 },
    promotion: {
      label: 'Limited-time 50% off', startsAt: '2026-01-01T00:00:00Z', endsAt: '2026-09-09T16:00:00Z',
      description: 'Active until 10 September 2026, 00:00 Singapore time (UTC+8).',
      rates: { input: 0.075, cacheRead: 0.015, cacheWrite: 0, output: 0.25 },
    },
    note: 'Cached-input storage is limited-time free; the promotion boundary is evaluated in Singapore time.',
  },
  'gpt-5.6-sol': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Sol', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-sol', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 4, cacheRead: 0.4, cacheWrite: 5, output: 20 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-5.6-terra': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Terra', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-terra', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 12 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-5.6-luna': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Luna', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-luna', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 0.2, cacheRead: 0.02, cacheWrite: 0.25, output: 1.2 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'claude-opus-5-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Opus 5.5', source: 'https://platform.claude.com/docs/en/models/opus-5-5/overview', sourceLabel: 'Anthropic Opus 5.5 model page',
    standardRates: { input: 4, cacheRead: 0.2, cacheWrite: 5, output: 20 },
    note: 'Cache read is 5% of input ($0.20/MTok) — half the family 10% multiplier, a per-model rate. Cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $8/MTok. Batch: $2 / $10.',
  },
  // The $2/$10 launch price became the standard price: Anthropic's pricing
  // page states the increase scheduled for 2026-09-01 "will not occur". The
  // old card flipped to $3/$15 on that date and over-quoted by 50% since.
  'claude-sonnet-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Sonnet 5', source: 'https://platform.claude.com/docs/en/about-claude/pricing', sourceLabel: 'Anthropic pricing page',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 10 },
    note: 'Cache read is 10% of input; cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $4/MTok.',
  },
  'claude-opus-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Opus 5', source: 'https://www.anthropic.com/news/claude-opus-5', sourceLabel: 'Anthropic Opus 5 announcement',
    standardRates: { input: 5, cacheRead: 0.5, cacheWrite: 6.25, output: 25 },
    note: 'Cache read is 10% of input; cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $10/MTok.',
  },
  'claude-fable-5-1': {
    providerLabel: 'Anthropic', displayName: 'Claude Fable 5.1', source: 'https://platform.claude.com/docs/en/models/fable-5-1/overview', sourceLabel: 'Anthropic Fable 5.1 model page',
    standardRates: { input: 10, cacheRead: 0.25, cacheWrite: 12.5, output: 50 },
    note: 'Cache read is 2.5% of input — a per-model rate Fable 5.1 states outright, NOT the family 10% multiplier the other two follow. Cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $20/MTok.',
  },
};

function configuredValue(fields: readonly ConfigFieldState[], path: string): string {
  return fields.find((field) => field.path === path)?.rawValue.trim() ?? '';
}

function currentPromotion(pricing: ModelPricing, clock: Date): Promotion | undefined {
  const promotion = pricing.promotion;
  if (!promotion) return undefined;
  const now = clock.getTime();
  const start = promotion.startsAt ? Date.parse(promotion.startsAt) : Number.NEGATIVE_INFINITY;
  const end = promotion.endsAt ? Date.parse(promotion.endsAt) : Number.POSITIVE_INFINITY;
  return now >= start && now < end ? promotion : undefined;
}

export function translationPricingStatus(fields: readonly ConfigFieldState[], clock: Date = new Date()): TranslationPricingStatus {
  const provider = configuredValue(fields, 'translation.provider').toLowerCase();
  const model = configuredValue(fields, `translation.${provider}.model`).toLowerCase();
  const pricing = PRICING[model];
  if (!pricing) {
    const labels: Readonly<Record<string, string>> = { glm: 'Z.AI GLM', minimax: 'MiniMax', qwen: 'Qwen / Model Studio', deepseek: 'DeepSeek', openai: 'OpenAI', anthropic: 'Anthropic' };
    return {
      provider, model, providerLabel: labels[provider] ?? (provider || 'Unknown provider'), displayName: model || 'No configured model', rates: null,
      rateLabel: 'Official API rates unavailable', condition: null, source: null, sourceLabel: null,
      cacheReadLabel: 'cache read',
      cacheWriteLabel: 'cache write',
      note: model.startsWith('glm-5.3') ? 'Z.AI’s current public API pricing page does not publish GLM-5.3 token rates; a Coding Plan quota is not an API tariff.' : 'Add a first-party pricing entry before estimating this route.',
    };
  }
  const promotion = currentPromotion(pricing, clock);
  const explicitQwenCache = provider === 'qwen' && configuredValue(fields, 'translation.qwen.caching.explicit') === 'true';
  const batch = (provider === 'openai' || provider === 'anthropic') && configuredValue(fields, `translation.${provider}.batch.enabled`) === 'true';
  const baseRates = promotion?.rates ?? pricing.standardRates;
  const rates = batch ? {
    input: baseRates.input * 0.5,
    cacheRead: baseRates.cacheRead === null ? null : baseRates.cacheRead * 0.5,
    cacheWrite: baseRates.cacheWrite === null ? null : baseRates.cacheWrite * 0.5,
    output: baseRates.output * 0.5,
  } : baseRates;
  const explicitCacheRead = promotion?.explicitCacheRead ?? pricing.explicitCacheRead;
  return {
    provider, model, providerLabel: pricing.providerLabel, displayName: pricing.displayName,
    rates: explicitQwenCache && explicitCacheRead !== undefined ? { ...rates, cacheRead: explicitCacheRead } : rates,
    rateLabel: batch ? 'Batch API pricing' : promotion?.label ?? 'Standard API pricing',
    condition: promotion?.description ?? null,
    cacheReadLabel: provider === 'glm' ? 'cached input' : explicitQwenCache ? 'explicit cache read' : provider === 'qwen' ? 'implicit cache read' : 'cache read',
    cacheWriteLabel: provider === 'glm' ? 'cache storage' : 'cache write',
    source: pricing.source, sourceLabel: pricing.sourceLabel, note: pricing.note ?? null,
  };
}

export function usdPerMillion(value: number | null): string {
  return value === null ? '—' : `$${value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
}
