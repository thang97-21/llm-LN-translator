# LLM Translator - DeepSeek/Qwen/OpenAI/Anthropic/Z.AI-Powered

**Selectable-provider pipeline** for Japanese → English light novel translation.
Extract, prep, translate, QC, build — and remember the series for next time.

Phase 2 selects one isolated translation route through `translation.provider`:
`deepseek`, `qwen`, `openai`, `anthropic`, or `glm`. Each route owns its native
client, prompt, conversation ledger, cache policy, continuation behavior, and
error handling; selecting one does not silently alter any of the others.

A content-safety refusal mid-volume does not end the run: the chapter is
handed to DeepSeek with the source provider's already-established translation
decisions inherited rather than reinvented. See
[Safety-Refusal Fallback](#safety-refusal-fallback).

---

## Navigation

- [What It Is](#what-it-is)
- [What It Is NOT](#what-it-is-not)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
  - [Providers](#providers)
  - [OpenAI Responses — Application and Cache Telemetry](#openai-responses--application-and-cache-telemetry)
  - [Proofreading Mode — Powered by Anthropic's Advisor Tool](#proofreading-mode--powered-by-anthropics-advisor-tool)
  - [Safety-Refusal Fallback](#safety-refusal-fallback)
  - [Prep — Cached Multi-turn Path](#prep--cached-multi-turn-path)
  - [Series Continuity — The Bible Writer](#series-continuity--the-bible-writer)
- [Configuration](#configuration)
- [File Structure](#file-structure)
- [CLI Reference](#cli-reference)
- [MCP Tools](#mcp-tools)
- [Requirements](#requirements)
- [context.xml](#contextxml)

---

## What It Is

A self-contained translation pipeline that ingests a Japanese EPUB, builds a
full character/voice/continuity reference through one cache-oriented
sequential prep conversation, translates every chapter through a
high-capacity language model with automatic prefix caching, runs a zero-cost
sanity gate, and outputs a finished English EPUB. On a sequel volume, it
recalls the previous volume's names and voices without being told twice.

## What It Is NOT

- It does **not** require vector stores or retrieval-augmented modules at
  runtime — the translation prompt carries all policy inline, and preparation
  is a bounded multi-turn metadata conversation, not a multi-phase pipeline.
- It does **not** run multi-model quality-evaluation fan-out — the post-
  translation gate is filesystem-only sanity checks with zero API cost.
- It does **not** maintain a publisher/author/anime-metadata database — series
  continuity is three flat JSON files (name locks, recurring phrase anchors,
  cross-volume state), a continuity aid, not a database.
- It does **not** run multiple providers per volume by default — every API
  call in a given run uses the same single configured provider unless a
  content-safety refusal triggers the DeepSeek fallback.

## Quick Start

```bash
# 1. Clone or copy into LLM Translator/
cd LLM Translator

# 2. Create a Python venv
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate   # Linux/macOS

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Set your API key(s) in .env, matching whichever provider(s) you use:
#    DEEPSEEK_API_KEY, QWEN_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, ZAI_API_KEY

# 5. Drop an EPUB in raw/
copy "C:\path\to\your-light-novel.epub" raw\      # Windows
# cp /path/to/your-light-novel.epub raw/            # Linux/macOS

# 6. Run the full pipeline
python scripts/mtl.py run raw/your-light-novel.epub

# Or step by step:
python scripts/mtl.py extract raw/your-light-novel.epub   # -> work/<vol_id>/JP/ + barebone context.xml
python scripts/mtl.py prep <vol_id>                        # -> context.xml via a cached multi-turn prep conversation
python scripts/mtl.py translate <vol_id>                   # -> work/<vol_id>/EN/
python scripts/mtl.py qc <vol_id>                           # -> QC report (stdout, zero API cost)
python scripts/mtl.py build <vol_id>                        # -> output/<vol_id>.epub
```

`--dry-run` on `translate`/`build` (and the equivalent MCP tool flags) assembles
the exact request payload without sending it or spending anything — no `EN/`
output, no manifest changes.

Writing the series bible (so the *next* volume in the series detects the
sequel automatically) is an MCP/IDE-agent action, not a CLI step — see
[Series Continuity](#series-continuity--the-bible-writer) below.

A TypeScript Ink/React terminal console (`ts/`) is available as an
alternative operator UI — `npm install` inside that directory, then
`mtl-ts.bat` / `mtl-ts.sh` from the project root.

---

## Architecture

```
raw/                          ← Drop EPUBs here
    │
    ▼
[Phase 1: Librarian]          ← src/utility/librarian/ (deterministic, zero LLM calls)
    JP chapter .md extraction, TOC parsing, illustration export
    Writes a barebone context.xml + manifest.json
    │
    ▼
[Prep: Cached Multi-turn]     ← src/utility/prep/
    One stable system prefix: prep policy + context shell + full JP source + bible + search evidence
    Sixteen JSON-node turns assembled deterministically into context.xml
    │
    ▼
[Phase 2: Translator]         ← src/Deepseek/, src/Qwen/, src/OpenAI/, src/Anthropic/, src/GLM/
    One isolated route per provider — own client, prompt, conversation ledger, cache policy
    │
    ▼
[QC Gate]                     ← src/utility/qc/ (filesystem-only, zero API cost)
    Completeness, truncation, token-count outliers, name-spelling drift, structural checks
    │
    ▼
[Bible Writer]                ← reads populated context.xml
    Merges into bibles/<series_id>/{term_lock,verbatim_anchors,series_pack}.json
    │
    ▼
[Phase 4: Builder]            ← src/builder/ (deterministic, zero LLM calls)
    EPUB3 packaging, XHTML, NCX/TOC
    │
    ▼
output/<vol_id>.epub          ← Finished English light novel
```

### Providers

Five isolated Phase 2 routes, selected by `translation.provider`. Specs as
configured in `config.yaml`:

| Provider | Model | Context Window | Max Output | Reasoning Control |
|---|---|---:|---:|---|
| DeepSeek | `deepseek-v4-pro` | 1M tokens | 384K tokens | 64K thinking budget + DRDI (EPS-band reasoning directives injected into the user turn — this route has no native effort parameter) |
| Qwen | `qwen3.7-max` | 1M tokens | 128K tokens | Native thinking, 56K token budget |
| OpenAI | `gpt-5.6-terra` (Astra-ready) | 1.05M tokens (922K configured input guard) | 128K tokens | `reasoning.mode: pro`, `reasoning.effort: xhigh`, `context: all_turns` |
| Anthropic | `claude-sonnet-5` | 1M tokens | 128K tokens | Adaptive thinking (`type: adaptive`), `effort: high` |
| Z.AI | `GLM-5.3-FLASH` | 1M tokens | 128K tokens | Deep Thinking , `effort: max` |

Every route receives the same prepared `context.xml` (voice guidance,
terminology locks, scene/emotional guidance) and enforces the same
source-first contract; only the transport, cache policy, and reasoning
control differ, per provider API.

### OpenAI Responses — Application and Cache Telemetry

The OpenAI route is a native Responses API implementation under `src/OpenAI/`,
not an OpenAI-compatible wrapper around another provider. `agent.py` assembles
chapter requests and continuations, `client.py` owns the wire payload and cache
monitor, `conversation.py` maintains local replay state, and `response.py`
normalizes Responses output and usage into the shared MTLS boundary types.

**Request and conversation shape.** The route uses `store: false` and persists
the complete replay ledger at the configured path. Astra and fallback profiles use
model-scoped ledgers such as `.context/openai_conversation_gpt_6_astra.json`. Every request
starts with a developer message containing the OpenAI master prompt plus the
volume's prepared `context.xml`, followed by up to two recent chapter turns and
the current Japanese chapter. Replay retains complete output items—including
encrypted reasoning content required by the next Responses turn—while only
visible output text is written as translated prose.

**Explicit fixed-prefix caching.** The default cache policy is `explicit` with
a 30-minute TTL. MTLS adds one explicit breakpoint to the developer
`input_text` block and sends a deterministic `prompt_cache_key` derived from
the configured model, prompt profile, and that complete developer prefix. The key contains no
title or user-identifying text and changes automatically when the model,
master prompt, or injected context changes. Conversation replay and the
current chapter come after the breakpoint, so they remain ordinary input under
this policy. Consequently, low total cache coverage can be normal even when
the intended breakpoint is recovered perfectly.

OpenAI cache reads and writes are provider-side facts. MTLS reads
`usage.input_tokens_details.cached_tokens` and `cache_write_tokens` from the
API response; local tokenization cannot reconstruct either value.

| OpenAI metric | Calculation | Interpretation |
|---|---|---|
| **Breakpoint Success Rate** | Successful recoveries ÷ eligible repeat calls | Health of the explicit developer-prefix breakpoint. The cold first call is excluded. |
| **Prefix Recovery** | Recovered expected-prefix tokens ÷ total expected-prefix opportunity tokens | Weighted measure of how completely the fixed prefix was recovered. |
| **Total Cache Coverage** | Cache-read tokens ÷ all input tokens | Cost-coverage information, not an OpenAI health warning. |
| **Cache Economics** | Read, write, and ordinary input tokens plus baseline cost, actual cost, and net savings | Shows whether later reads repay the priced cache write. Also reports the read-to-write token ratio. |

The OpenAI monitor warns only when **Breakpoint Success Rate** or **Prefix
Recovery** falls below `0.90`, after at least two eligible repeat calls. It does
not apply the `0.70` total-hit threshold used by growing-prefix provider routes.
A healthy fixed-prefix run may therefore report 100% breakpoint success, 100%
prefix recovery, and only about 15% total coverage; those values describe a
narrow but fully functioning cache rather than a cache failure.

Astra remains opt-in in `config.yaml`; when selected, an eligible access or
transport failure can pin the volume to the configured GPT-5.6 fallback. A
fallback after an already committed Astra chapter records `fallback_pending`
instead of silently mixing model-native conversation state. Set
`translation.openai.fallback.resume_pending: true` only when explicitly
resuming that boundary on GPT-5.6.

**Artifacts.** Each OpenAI translator call adds cache reads, cache writes,
ordinary input, output, the three cumulative ratios, net cache savings, and
total estimated cost to `LOG/token_log_<run-stamp>.md`. Credential-free dry
runs report the cache mode, a SHA-256 digest of the key, and explicit breakpoint
count without exposing the key itself. Dry runs remain single-turn and make no
API call, so they cannot report cache hits, writes, or billed savings.

Logs created before `cache_write_tokens` telemetry was added retain valid cache
read counts, but their write cost and net savings cannot be reconstructed
exactly. Use a new live run when billing-accurate cache economics are required.

**OpenAI Batch migration (Astra-ready).** `translation.openai.batch.enabled`
is the explicit opt-in for the GPT-5.6 family. When `translation.openai.model`
is set to `gpt-6-astra`, `batch.auto_for_astra: true` enables Batch by default;
set it to `false` to force synchronous Astra calls. The checked-in route remains
`gpt-5.6-terra` until Astra credentials are provisioned.

Batch requests follow the Anthropic Fable 5.1 wave pattern while retaining
OpenAI's persistent Responses reasoning ledger. Each wave freezes one replay
prefix, submits chapter requests as JSONL to `/v1/responses`, and commits
completed raw output (including encrypted reasoning items) in chapter order
before the next wave is assembled. Chapters in the same wave intentionally do
not see one another; lower `batch.wave_size` when continuity matters more than
throughput. A one-chapter seed wave (`seed_chapters: 1`) establishes the first
translation decisions without abandoning Batch pricing.

`batch.persistence_file` stores submitted job IDs and file IDs atomically. An
interrupted run drains that ledger before submitting new work, so it does not
duplicate accepted jobs. Missing or transient result lines remain pending for a
later retry; model-access failures may switch at a clean boundary to the
configured GPT-5.6 fallback. If an Astra chapter has already committed and
mixed-model continuation is disabled, the route records `fallback_pending`
instead of silently combining incompatible reasoning state.

### Proofreading Mode — Powered by Anthropic's Advisor Tool

**Proofreading Mode** is the formal name for `translation.anthropic.advisor` —
the label an operator actually sees, in this README and in the TypeScript
console's Configuration screen (`Translation → Anthropic → Proofreading Mode`)
alike. Under the hood it is powered by Anthropic's beta `advisor_20260301`
tool: a second, higher-intelligence model consulted mid-generation by the
executor itself, mid-turn, on its own judgment of need — not a second pass,
not a separate QC stage. One consult serves both purposes Runs 1–11 validated
independently and then confirmed work together in the same call: real-world
reference grounding (does a claimed subculture reference, public figure, or
historical detail actually check out) and craft/proofreading judgment
(name-form drift, locked-anchor collisions, register calls the executor is too
close to its own prose to catch). `enabled: true` by default — eleven manual
dry runs (`docs/anthropic-advisor-mode-plan.md`,
`docs/anthropic-advisor-mode-spec.md`) closed out the validation this ships on.

**Model choice.** `advisor.model` selects which model does the consulting,
among four options, each on Anthropic's own executor/advisor compatibility
table (`ADVISOR_COMPATIBILITY` in `src/Anthropic/client.py`, checked fail-fast
at startup):

| Advisor model | Result type | Notes |
|---|---|---|
| `claude-opus-4-8` (default) | plaintext `advisor_result`, human-readable | Validated in Runs 8–11. Requires executor `claude-sonnet-5`. |
| `claude-sonnet-5` | plaintext | Same-tier peer to a `claude-sonnet-5` executor. |
| `claude-opus-5` | encrypted `advisor_redacted_result` | Not compatible with `claude-opus-4-8` as executor. |
| `claude-fable-5-1` | encrypted | Anthropic's own recommendation for maximum quality lift on a Sonnet executor; the only advisor valid against any of this route's three supported executors. |

The default executor is `claude-sonnet-5` specifically because it is the only
one of the route's supported executors this project actually validated
Proofreading Mode against, and the only one compatible with the
plaintext-readable `claude-opus-4-8` advisor default. Changing the executor
away from `claude-sonnet-5` requires either an advisor model one of the opus-5
/ fable-5-1 pairings accepts, or `advisor.enabled: false`.

**Economic gating.** `advisor.require_signal: true` (default) turns
Proofreading Mode on for a request only when the chapter actually carries a
`chapter_signals` risk entry — ambiguity, ateji, multi-speaker, voice-contrast,
or a subculture reference — combined with a WARM/HOT EPS band. EPS band alone
is deliberately not sufficient; this session's evidence measured that gate as
uneconomical and rejected it (`src/Anthropic/agent.py::_tools_qualify`,
`optimization.py::build_advisor_guidance`). `max_uses: 2` caps consults per
request; `max_tokens: 24000` bounds each consult's own output.

**Pause-turn resumption.** A deep consult can end the turn early with
`stop_reason: pause_turn` rather than completing generation. Both the
synchronous and Batch paths resend the unchanged assistant message
(`agent.py::_resolve_pauses`) until the turn actually finishes or
`max_pause_resumes` (default 2) is exhausted — content already written before
the pause is preserved and accumulated across every resumed response, never
discarded. Verified directly against Anthropic's Batch documentation: server
tools including `advisor` run inside Batch, and a batch-path pause is resolved
via one synchronous follow-up call per pause rather than a second batch job,
since Batch already runs more agentic-loop iterations before pausing than the
synchronous path does.

**Master-prompt coupling.** Proofreading Mode's craft judgment is measured
against the same standard the executor itself writes to: a
`<proofreading_discipline>` block in `master_prompt_anthropic_en.md` (a
sibling to `<anti_translationese>`) codifying recurring LLM-output patterns
this project's own QC audits have caught — em-dash overuse as a default
connector rather than a deliberate device, nonstandard ellipsis runs,
hedge-word monotony, reflexive "somehow" translations, italics doing double
duty for both interiority and emphasis, and uniform acknowledgment tags. The
advisor is directed to weigh in on these the same way it weighs in on
name-form drift or a locked-anchor collision.

**`web_search` (opt-in, independent of Proofreading Mode).** A separate,
non-beta tool (`translation.anthropic.web_search`, `enabled: false` by
default) a chapter can need independently of, alongside, or instead of
Proofreading Mode — Run 9 confirmed all four combinations behave correctly.
Off by default because its $10/1,000-search cost and query-level economy
guardrail remain unvalidated at the same standard Proofreading Mode cleared.

### Safety-Refusal Fallback

Any non-DeepSeek provider can refuse a chapter on content-safety grounds. The
refusal is treated as deterministic — retrying the identical payload against
the identical gate fails identically — so the route does not retry. Instead:

1. The refusal code is captured for the audit trail.
2. An **inheritance agent** (one DeepSeek call on the `prep` config) reads
   every EN chapter already produced and summarizes the decisions the source
   provider actually established — names, honorific practice as observed,
   landmarks, epithets, voice, terminology.
3. That summary is injected into a `<translation_inheritance>` block in
   `context.xml`, so the DeepSeek run that finishes the volume inherits those
   decisions instead of reinventing them.
4. If the inheritance step itself fails, the original refusal is re-raised
   rather than falling through to a cold translation — a run that stops is
   recoverable; a volume that silently drifts in terminology mid-book is not.

Every firing writes `work/<vol_id>/QC/inheritance_translator.json` — the exact
refusal, every chapter completed before it, and the governing config. Its
presence makes a cross-provider consistency copypass mandatory in QC.

On by default for every non-DeepSeek provider (`translation.safety_fallback.enabled`,
one shared switch, not per-provider) — see `src/Deepseek/common/safety_fallback.py`.

**Why refusals happen at all.** Each non-DeepSeek provider runs its own
content-safety classifier, tuned to that provider's own policy — this
pipeline does not control or configure it. Ordinary light-novel conventions
sit close enough to that boundary that behavior varies by provider and by
material: a comedic beat built on a risqué joke or teasing innuendo can trip
one provider's classifier while another treats it as unremarkable genre
content; material that reads as more explicitly sexual trips a refusal far
more consistently, across most providers. Neither outcome reflects a defect
in this pipeline — it is the provider's own policy surface, encountered live.

In practice, DeepSeek shows the lowest refusal rate of the four routes and
the highest tolerance for ordinary genre material — including, in observed
runs, chapters that a different route declined outright, which DeepSeek
completed as ordinary translation work with no intervention needed.

This is a current observation, not a guarantee. A provider's safety
classifier is that provider's own policy, on that provider's own release
schedule — not this codebase's. A route that handles a given volume cleanly
today is not bound to keep doing so after a future model or policy update,
on any provider, DeepSeek included.

### Prep — Cached Multi-turn Path

`src/utility/prep/agent.py::run_prep()` builds `context.xml` before
translation starts. Multi-turn is the active default path
(`prep.multi_turn.enabled`); parallel cache-warmed fan-out remains available
as an explicitly selected recovery strategy, and a one-shot unified call is
the final fallback. The active path fails closed — a turn or assembly error
preserves its JSON artifacts for explicit retry rather than silently
spending on a second strategy.

**The 17-block shell.** The Librarian (`librarian/agent.py::_write_context_placeholder`)
writes `context.xml` immediately after extraction: `<opf_metadata>` populated
in place from the EPUB's own OPF data, plus seventeen further blocks seeded
`<pending/>`, each tagged with its owning agent. Of those seventeen, fifteen
are filled by the multi-turn prep conversation in one persisted sequential
DeepSeek conversation — one JSON-node turn per block
(`block_prompts.py::MULTITURN_BLOCK_ORDER`), assembled and schema-validated
into XML after every turn — plus `chapter_titles_en`, a block prep creates
fresh rather than inheriting from the Librarian's shell, for sixteen turns
total. The remaining two stay outside prep's own accounting: `character_attribute_anchors`
is never generated by any path in this lightweight client (a block name
inherited from the original heavier pipeline, permanently omitted here — see
`block_prompts.py`'s own ownership comment), and `translation_inheritance`
stays `<pending/>` unless the Safety-Refusal Fallback above actually fires.

### Prep — OpenAI Responses / Batch Path

Selecting `prep.provider: openai` uses the same `build_multiturn_system_prompt()`
and `build_multiturn_task_suffix()` contract as the DeepSeek prep pipeline. One
native Responses request is created for every active prep block; with
`prep.openai.batch.enabled: true`, all of them are submitted in one durable
OpenAI Batch job and tracked in `prep.openai.batch.persistence_file`. Completed
JSON nodes are written under `.context/openai_prep/`, so an interrupted run can
resume accepted work without resubmitting completed blocks. Set the flag to
`false` to use the same block loop through synchronous Responses calls.

**Metadata search.** Before the conversation starts, prep resolves the
volume's Japanese title/author against `web_search_chain.py`'s
`MetadataSearchChain` — retrieval kept deterministic and separate from model
reasoning, so blocks that need real-world grounding (series continuity,
publisher conventions, cast canon) work from actual search results rather
than a model guess. It tries the locked Bookwalker search URL directly first
(`bookwalker_direct`), then falls through configured providers in fixed
order on the first hit — Brave (queried as `site:bookwalker.jp <keyword>`),
Japan's National Diet Library catalog (NDL), SerpAPI, Tavily, Exa. Every
result is host-allowlisted to `bookwalker.jp` and `ndlsearch.ndl.go.jp`
only — no other domain is ever returned as evidence — and provider API keys
are read from environment variables only, never serialized into the
evidence trail. The chain's result folds into the conversation's stable
system prefix as grounding evidence and is persisted to
`.context/prep_web_search.json` for audit.

Every model turn — prep's fifteen and the search chain's provider calls
alike — writes a row to the per-volume cost ledger under
`work/<volume_id>/LOG/`.

### Series Continuity — The Bible Writer

`bibles/<series_id>/` is three flat, human-readable JSON files:

| File | Content |
|------|---------|
| `term_lock.json` | JP→EN name table for characters and every other locked term |
| `verbatim_anchors.json` | Recurring signature phrases and locked wording |
| `series_pack.json` | Volumes processed, merged voice fingerprints, last-known emotional-proximity band per character |

On the *next* volume of a series, prep matches its JP title against every
`series_pack.json`; a match loads the bible and injects it so the model
reuses locked names and voices instead of reinventing them. Writing the bible
is deliberately MCP/IDE-agent-only, not a CLI command.

---

## Configuration

One control surface per provider in `config.yaml`, selected by
`translation.provider`:

```yaml
translation:
  provider: deepseek        # deepseek | qwen | openai | anthropic | glm

  deepseek:
    model: deepseek-v4-pro
    thinking:
      enabled: true
      budget_tokens: 48000

  safety_fallback:
    enabled: true            # default ON for every non-DeepSeek provider
```

See `config.yaml` for the full documented parameter set per provider —
generation limits, thinking/effort controls, caching, conversation
compaction, and continuation behavior are all configured independently per
route.

---

## File Structure

```
mtls/
├── config.yaml                 ← Master config: one section per provider + shared knobs
├── requirements.txt            ← Python dependencies
├── README.md                   ← This file
├── mtl.bat / mtl.sh             ← CLI launcher (Windows / Linux+macOS)
│
├── src/
│   ├── Deepseek/                ← Shared infrastructure (common/, mcp/) + the DeepSeek route
│   │   └── common/
│   │       ├── config.py, atomic_io.py, llm_types.py
│   │       ├── token_telemetry.py    ← Shared pricing table, per-run cost log
│   │       └── safety_fallback.py    ← Shared provider→DeepSeek content-refusal fallback
│   ├── Qwen/                    ← Qwen route
│   ├── OpenAI/                  ← OpenAI Responses route
│   ├── Anthropic/               ← Anthropic Messages route
│   ├── GLM/                     ← OpenAI Messages route
│   ├── utility/
│   │   ├── librarian/           ← Phase 1: EPUB extraction
│   │   ├── prep/                ← context.xml builder
│   │   └── qc/                  ← Filesystem-only sanity gate
│   └── builder/                 ← Phase 4: EPUB assembly
│
├── scripts/
│   └── mtl.py                   ← CLI: extract, prep, translate, qc, build, run, list, status
│
└── ts/                          ← TypeScript Ink/React terminal console (optional)
```

---

## CLI Reference

```bash
python scripts/mtl.py extract <epub_path>     # Phase 1: EPUB → JP chapters + barebone context.xml
python scripts/mtl.py prep <vol_id>           # Fills context.xml via the configured prep conversation
python scripts/mtl.py translate <vol_id>      # Phase 2: JP → EN via the configured provider
python scripts/mtl.py translate <vol_id> --dry-run   # Assemble the payload, send nothing
python scripts/mtl.py qc <vol_id>             # Filesystem-only sanity gate (zero API cost)
python scripts/mtl.py build <vol_id>          # Phase 4: EN → EPUB
python scripts/mtl.py run <epub_path>         # Full pipeline: extract → prep → translate → qc → build
python scripts/mtl.py list                    # List volumes in work/
python scripts/mtl.py status <vol_id>         # Pipeline state + chapter completion summary
```

No `bible` CLI command — see [Series Continuity](#series-continuity--the-bible-writer).

---

## MCP Tools

An MCP server exposes every phase (Librarian, Prep, Translator, QC, Bible
Writer, Builder) as a callable tool for IDE-agent integration — the agent
calls one tool per phase instead of manually orchestrating sub-steps.

| Server | Phase |
|--------|-------|
| Librarian | 1 |
| Prep — `prep_volume(volume_id, series_id=None)` | 1.P |
| Translator — `translate_chapter`, `run_translator` (both take `dry_run`) | 2 |
| QC — `qc_volume(volume_id)` | Gate |
| Bible — `write_bible(volume_id, series_id=None)` | Continuity |
| Builder | 4 |

Start the server: `python -m src.Deepseek.mcp.server`

---

## Requirements

- **Python** 3.11+
- **Node.js** 20+ (TypeScript console only)
- An API key for whichever provider(s) you configure, set via `.env`

Python packages: `anthropic`, `openai`, `pyyaml`, `python-dotenv`, `lxml`, `beautifulsoup4`, `Pillow`, `tiktoken`, `mcp`

---

## context.xml

Every volume gets a `context.xml` at `work/<volume_id>/context.xml` — the
translator's entire continuity/voice/terminology reference. `extract` writes
a barebone version; `prep` fills the generated blocks through a cached
sequential conversation. Root shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<mtls_project_context schema_version="1.0" volume_id="..." series_id="..." target_language="en">
  <opf_metadata format="json">{...raw OPF passthrough...}</opf_metadata>
  <validation_audit status="passed">...</validation_audit>
  <volume_identity>...</volume_identity>
  <world_setting type="...">...</world_setting>
  <character_roster>...</character_roster>
  <name_map>...</name_map>
  <relationship_graph>...</relationship_graph>
  <verbatim_anchors>...</verbatim_anchors>
  <character_attribute_anchors status="pending"><pending/></character_attribute_anchors>
  <voice_fingerprints>...</voice_fingerprints>
  <cultural_glossary>...</cultural_glossary>
  <eps_arc_tracker>...</eps_arc_tracker>
  <scene_plans>...</scene_plans>
  <eps_signals>...</eps_signals>
  <illustration_context>...</illustration_context>
  <translation_brief>...</translation_brief>
  <translation_inheritance status="pending"><pending/></translation_inheritance>
</mtls_project_context>
```

`character_attribute_anchors` and `translation_inheritance` stay `<pending/>`
until their owning feature runs — the former is out of scope for this client,
the latter only populates on a safety-refusal fallback. You can translate
without running prep at all (the master prompt carries enough inline
guidance), but the translator logs a loud warning when it does, since
character voice, locked names, and continuity anchors are all off in that
mode.
