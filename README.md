# LLM Translator - DeepSeek/Qwen-Powered

**Selectable-provider pipeline** for Japanese → English light novel translation.
Extract, prep, translate, QC, build — and remember the series for next time.

Phase 2 translation can route between isolated DeepSeek and Qwen implementations
through `translation.provider` in `config.yaml`. DeepSeek remains the default;
set it to `qwen` to use the Anthropic-format Qwen client, Qwen prompts,
conversation ledger, and optimization policy. Set `DASHSCOPE_API_KEY` for live
Qwen calls. Use `dry_run` to inspect a Qwen payload without credentials or
network access.

Qwen official-spec support includes: Anthropic Messages endpoint, thinking
budgets, explicit `cache_control` markers, streaming, retry/backoff, moderation
error classification, conversation compaction, thinking-aware partial
continuation, and Qwen pricing entries in token telemetry.

A content-moderation refusal mid-volume does not end the run: the chapter is
handed to DeepSeek with Qwen's already-established translation decisions
inherited rather than reinvented. See
[Safety-Refusal Fallback](#safety-refusal-fallback--qwen--deepseek).

---

## Navigation

- [LLM Translator - DeepSeek/Qwen-Powered](#llm-translator---deepseekqwen-powered)
  - [Navigation](#navigation)
  - [Glossary](#glossary)
  - [What It Is](#what-it-is)
  - [What It Is NOT](#what-it-is-not)
  - [Quick Start](#quick-start)
    - [TypeScript Terminal UI](#typescript-terminal-ui)
    - [IDE Agent (MCP Tools) — One-Line E2E](#ide-agent-mcp-tools--one-line-e2e)
  - [Architecture](#architecture)
    - [The Translator — What Makes It "Bare"](#the-translator--what-makes-it-bare)
    - [Safety-Refusal Fallback — Qwen → DeepSeek](#safety-refusal-fallback--qwen--deepseek)
    - [Prep — Two Paths](#prep--two-paths)
    - [Series Continuity — The Bible Writer](#series-continuity--the-bible-writer)
  - [Telemetry \& Developer Tools](#telemetry--developer-tools)
    - [Token \& Cost Log](#token--cost-log)
    - [THINKING Density Map](#thinking-density-map)
    - [Dry Run](#dry-run)
    - [Local Tokenizer](#local-tokenizer)
  - [Why DeepSeek V4 Model?](#why-deepseek-v4-model)
    - [Specifications](#specifications)
    - [How this maps onto LLM Translator](#how-this-maps-onto-llm-translator)
    - [Real cost telemetry](#real-cost-telemetry)
  - [Configuration](#configuration)
    - [Key Parameters](#key-parameters)
  - [File Structure](#file-structure)
  - [CLI Reference](#cli-reference)
  - [MCP Tools](#mcp-tools)
    - [MCP Tool Design Notes](#mcp-tool-design-notes)
  - [Requirements](#requirements)
  - [context.xml](#contextxml)

---

## Glossary

Pipeline-specific acronyms used throughout this document:

| Acronym | Full Form | What It Means |
|---------|-----------|---------------|
| **CAA** | Character Attribute Anchors | Cross-volume character trait tracking subsystem (stays `pending` in this client) |
| **CCT** | Concurrent Chapter Translation | Async parallel chapter processing (opt-in, not wired by default) |
| **CJK** | Chinese, Japanese, Korean | Script family; the pipeline prohibits CJK leakage into English output |
| **CoT** | Chain of Thought | Internal reasoning tokens the model spends before producing output |
| **DOVB** | DeepSeek-Optimized Voice Block | Structured character voice templates with contraction rules, archetypes, and forbidden vocabulary |
| **DRDI** | DeepSeek Reasoning Directive Injection | EPS-band → CoT scaffolding injected into the user turn, replacing the absent native `effort` parameter |
| **EPS** | Emotional Proximity Scale | Per-chapter per-character emotional intensity band: COLD → COOL → NEUTRAL → WARM → HOT |
| **MoE** | Mixture of Experts | Model architecture where only a subset of parameters activates per token |

---

## What It Is

A self-contained translation pipeline that ingests a Japanese EPUB, builds a
full character/voice/continuity reference (either a single structured API
call, or a cache-warmed parallel fan-out — see [Prep — Two Paths](#prep--two-paths)),
translates every chapter through a high-capacity language model with automatic
prefix caching, runs a zero-cost sanity gate, and outputs a finished English
EPUB. On a sequel volume, it recalls the previous volume's names and voices
without being told twice. Every API call across both phases lands in a
per-project, per-run cost ledger — see [Telemetry & Developer Tools](#telemetry--developer-tools).

## What It Is NOT

- It does **not** require vector stores or retrieval-augmented modules at
  runtime — the translation prompt carries all policy inline, and preparation
  is a single structured call, not a multi-phase metadata pipeline
- It does **not** run multi-model quality-evaluation fan-out — the post-
  translation gate is filesystem-only sanity checks with zero API cost
- It does **not** maintain a publisher/author/anime-metadata database — the
  series continuity layer is three flat JSON files (name locks, recurring
  phrase anchors, and cross-volume state), a continuity aid, not a database
- It does **not** depend on multiple AI providers — extraction and EPUB
  assembly are deterministic, and every API call in between uses the same
  single provider

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

# 4. Set your API key
# Edit .env or set the environment variable:
set DEEPSEEK_API_KEY=sk-your-key-here      # Windows
# export DEEPSEEK_API_KEY=sk-your-key-here   # Linux/macOS

# 5. Drop an EPUB in raw/
copy "C:\path\to\your-light-novel.epub" raw\      # Windows
# cp /path/to/your-light-novel.epub raw/            # Linux/macOS

# 6. Run the full pipeline
python scripts/mtl.py run raw/your-light-novel.epub

# Or step by step:
python scripts/mtl.py extract raw/your-light-novel.epub   # -> work/<vol_id>/JP/ + barebone context.xml
python scripts/mtl.py prep <vol_id>                        # -> context.xml fully populated (one DeepSeek call)
python scripts/mtl.py translate <vol_id>                   # -> work/<vol_id>/EN/
python scripts/mtl.py qc <vol_id>                          # -> QC report (stdout, zero API cost)
python scripts/mtl.py build <vol_id>                       # -> output/<vol_id>.epub
```

Writing the series bible (so the *next* volume in the series detects the sequel automatically) is an MCP/IDE-agent action, not a CLI step — see [Series Continuity](#series-continuity--the-bible-writer) below.

### TypeScript Terminal UI

A responsive Ink/React operator console for the pipeline:

```bash
# Install Node.js deps (one-time)
cd mtls-menu-ts
npm install

# Launch from project root
mtl-ts.bat      # Windows
./mtl-ts.sh     # Linux/macOS
```

The console has Dashboard, Workflows, Volumes, Inputs, Advanced Toolbox,
Console, and Diagnostics views. At 120 columns it uses navigation, workspace,
and inspector panes; it collapses to two panes at 90–119 and a breadcrumbed
single pane below 90. `list` and `status` have been removed from the console:
the Dashboard and Volume Workbench already read the same local data without
the absurd detour through a Python subprocess.
Dashboard displays only the effective Translator entries from `config.yaml`;
credential metadata is omitted. `Up`/`Down` select a workspace in the rail and
`Enter` opens it. Inside a workspace, those same keys select items and `Enter`
opens them. The Dashboard also carries a **Developer** panel — press `d` to
toggle a session-only Dry Run default (never written to `config.yaml`, and
reset every restart on purpose) that pre-checks Dry Run the next time a
Translate Volume form opens. See [Dry Run](#dry-run).

The volume list refreshes on three independent layers rather than one: right
after any launched action finishes, from a directory watcher on `work/` that
catches a volume created by a separate process entirely, and a periodic poll
underneath both — the console can't miss a volume that appeared while it was
already open, no matter which of those layers happens to catch it first.

Primary workflows use the canonical CLI and expose every real argument:

| Workflow | Fields |
|---|---|
| Extract | EPUB, optional volume ID |
| Prep | volume, optional series ID |
| Translate | volume, optional chapter multi-select, Dry Run toggle |
| QC | volume |
| Build | volume, optional output name |
| Full Pipeline | EPUB, optional volume ID and series ID |

Advanced Toolbox uses MCP `listTools()` plus a static safety/UI overlay to
cover all 20 MCP tools. The console never guesses an executable route for an
unknown tool, and a known tool missing from the live handshake remains visibly
disabled. Each capability owns exactly one transport—CLI, MCP, or read-only
local view—so a failed MCP call cannot quietly fall back to a mutating CLI run.

Every form shows its exact CLI command or MCP JSON payload before launch.
Writes and paid DeepSeek calls need confirmation; overwrite/bypass paths such
as image optimization, `apply_manifest`, and `skip_qc` need a second warning.
There is intentionally no arbitrary “extra flags” field.

Diagnostics checks interpreter discovery, required imports, MCP readiness, and
API-key presence without showing the key. If a dependency such as `lxml` is
missing, Advanced Toolbox is disabled and the repair command is shown instead:

```bash
python -m pip install -r requirements.txt
```

Console output is a structured 5,000-entry ring buffer rather than a 200-line
tail. `Tab` enters terminal focus; arrows/PageUp/PageDown/Home/End browse,
`f` follows the tail, and `i` sends raw input only to a CLI child. `Esc` goes
back (and asks before cancelling a running action); `Ctrl+C` stops the active
action while leaving the console open. Only `Ctrl+Shift+Esc` exits the TUI. The
process child or MCP transport is cleaned up on interruption and normal exit.
Legacy Python TUI launching and `--legacy`
or session-wide `--mcp` modes are gone; `mtl.bat`/`mtl.sh` remain the standalone CLI.

### IDE Agent (MCP Tools) — One-Line E2E

Drop an EPUB in `raw/`, then tell the IDE agent ONE thing. It chains the MCP tools in a single session — no manual phase-by-phase commands, no waiting between steps:

```
YOU:  "Translate raw/hikari-vol3.epub"

AGENT:
  extract_epub("raw/hikari-vol3.epub")     → JP chapters, barebone context.xml
  prep_volume("HKR03")                     → ONE DeepSeek call fills all 14 in-scope context.xml blocks
  run_translator("HKR03")                  → DeepSeek V4 Pro, direct Python binding, no subprocess
  qc_volume("HKR03")                       → completeness / truncation / name-drift (zero API cost)
  write_bible("HKR03", series_id="hikari") → merges into bibles/hikari/{term_lock,verbatim_anchors,series_pack}.json
  package_epub("HKR03")                    → output/hikari-vol3.epub

  → "Done. 12 chapters, 94K EN words, QC passed, bible updated for volume 4. ~$0.20 total."
```

The MCP server does the orchestration; the agent just calls tools in sequence. The only prerequisite is `DEEPSEEK_API_KEY` in `.env` — nothing else to configure.

**Persona + routing resources:** the server also exposes two MCP *resources* (not tools) via `src/mcp/harness.py` — `persona://instructions` (this project's local operating-persona file, verbatim, so a remote or non-Claude-Code agent connecting over MCP gets the same operating context a local session gets automatically) and `routing://phases` (the phase-name → tool-name map, e.g. `"translate" → "run_translator"`, overridable via `config.yaml`'s `phase_routing` section). Neither is required reading to use the server — they exist for agents that want to introspect it.

---

## Architecture

```
raw/                          ← Drop EPUBs here
    │
    ▼
[Phase 1: Librarian]          ← src/librarian/          (deterministic, zero LLM calls)
    JP chapter .md extraction, TOC parsing, illustration export
    Writes a barebone 15-block context.xml (every block <pending/>) + manifest.json
    │
    ▼
[Prep: Two Paths]             ← src/prep/                (own client, not DeepSeekClient)
    Unified   — one DeepSeek V4 Pro call fills all 14 in-scope blocks in a single turn
    Parallel  — one Pro call (roots character_roster + name_map, warms the disk cache)
                → one sequential Flash cache-warming call → 12 Flash calls in parallel
                (config-gated, off by default; falls back to Unified on any failure)
    Either way: reads barebone context.xml + all JP chapters (+ series bible, if a sequel) → fills:
    volume_identity, world_setting, character_roster, name_map, relationship_graph,
    verbatim_anchors, voice_fingerprints, cultural_glossary, eps_arc_tracker, eps_signals,
    scene_plans, illustration_context, translation_brief, validation_audit
    Writes manifest.json["metadata"]/["metadata_en"] for Builder OPF assembly + TRANSLATION_BRIEF.md
    (character_attribute_anchors stays <pending/> — that block belongs to a subsystem out of scope here)
    │
    ▼
[Phase 2: Translator]         ← src/translator/          (direct Python import, DeepSeek V4 Pro)
    Anthropic-format endpoint, auto-prefix cache, DRDI (DeepSeek Reasoning Directive Injection) reasoning directives, DOVB (DeepSeek-Optimized Voice Block) voice blocks
    │
    ▼
[QC Gate]                     ← src/qc/                  (filesystem-only, zero API cost, <5s)
    Completeness, truncation, token-count outliers, name-spelling drift, structural checks
    │
    ▼
[Bible Writer]                ← src/bible/                (reads populated context.xml)
    Merges into bibles/<series_id>/{term_lock,verbatim_anchors,series_pack}.json
    Cumulative cross-volume continuity — makes the NEXT volume's sequel detection possible
    │
    ▼
[Phase 4: Builder]            ← src/builder/               (deterministic, zero LLM calls)
    Reads manifest.json["metadata"] for OPF assembly; EPUB3 packaging, XHTML, NCX/TOC
    │
    ▼
output/<vol_id>.epub          ← Finished English light novel
```

### The Translator — What Makes It "Bare"

This one is ~200 lines across 4 core files:

| File | Purpose |
|------|---------|
| `deepseek_client.py` | Anthropic SDK wrapper → DeepSeek endpoint; streaming, thinking, fail-safe salvage |
| `deepseek_conversation.py` | Persistent multi-turn state; prefix shape tracking; compaction ladder |
| `deepseek_optimization.py` | DRDI (EPS→CoT injection), DOVB (voice templates), CCT (parallel opt-in) |
| `agent.py` | Orchestrator: load prompt → inject context.xml → per-chapter send → receive → write |

Everything else is **gone**. The DeepSeek master prompt carries all translation policy inline.

### Safety-Refusal Fallback — Qwen → DeepSeek

Qwen's content-moderation gate will refuse a chapter outright (`QwenModerationError`, HTTP 400 family — e.g. `code="InvalidParameter"` with *"inappropriate content"*, classified as `data_inspection_failed`). Light novels trip it on material that is entirely ordinary for the genre, and the refusal is **deterministic**: the same payload against the same gate fails identically every time. So the Qwen route does not retry it — `is_retryable()` returns `False` for moderation errors, and a backoff loop here would only buy the same refusal three times more slowly.

The chapter goes to DeepSeek instead. The interesting part is that it does not go **cold**.

Handing chapter 9 of a volume to a second model with no history is how a heroine acquires a new name halfway through a book, honorifics start appearing where they had been dropped, and a running epithet quietly changes wording. So before the DeepSeek payload is built:

1. The exact refusal code is captured from the exception for the audit trail.
2. An **inheritance agent** — one DeepSeek call running on the *prep* config (`prep.model` / `endpoint` / `api_key_env`, via `_call_deepseek_prep`) — reads every EN chapter Qwen has already produced and summarizes the decisions Qwen actually established: names, honorific practice as *observed* rather than as stated, landmarks, epithets, voice, terminology.
3. That summary is upserted into a `<translation_inheritance>` block in `context.xml`, carrying a marker stating the run succeeds from a Qwen refusal and all decisions must inherit from Qwen. The block is seeded `<pending/>` by the Librarian and left pending by prep — it is populated only at runtime, only when a refusal actually fires.
4. DeepSeek translates the chapter. Because the DeepSeek route already embeds `context.xml` verbatim in its system prompt, the block reaches the model with no prompt surgery at all.

Injection is idempotent — re-running replaces the existing block rather than stacking a second one — and `context.xml` is written atomically.

**It fails loudly.** If the inheritance agent or the injection step fails for any reason, the original moderation error is re-raised rather than falling through to a cold translation. A run that stops is recoverable; a volume that silently changes its own terminology at chapter 9 is not.

Every firing also writes `WORK/<vol_id>/QC/inheritance_translator.json`: the exact refusal (code, message, HTTP status), every chapter completed *before* the refusal, and the governing inheritance config. This is a translator-side input to QC, never a QC report. `mtl-qc` reads it before dispatching any sub-agent; its presence sets `has_inheritance_handoff` and makes a **cross-provider consistency copypass mandatory** — post-refusal chapters are checked against both the recorded decisions and the pre-refusal Qwen chapters, routed through `qc-names` with prior Qwen output winning any metadata conflict, and reported in its own audit section.

```yaml
translation:
  safety_fallback:            # sibling of translation.qwen, not nested under it
    enabled: true
    marker: "This run succeeds from Qwen's safety refusal. All translation decisions must inherit from Qwen."
    inherit_agent:
      enabled: true
      prompt: src/Qwen/prompts/inherit_decisions_prompt.xml
```

Two keys in that block, `fallback_provider` and `block_name`, are **not read by anything** — the provider and the block name are both fixed in `src/Qwen/safety_fallback.py`. They describe the design rather than configure it; changing them has no effect until something wires them up.

One consequence worth stating plainly: a volume that hits a refusal is **translated by two different models**, and no amount of decision inheritance makes that free. The inheritance summary and the mandatory copypass are mitigations, not a guarantee — the QC artifact exists precisely because the seam deserves a human look.

### Prep — Two Paths

Neither path is built on `DeepSeekClient` — that class is tuned for the chaptered, conversation-aware translation loop (prefix cache monitor, persistent conversation manager). Prep has none of that: `src/prep/agent.py` and `src/prep/parallel_agent.py` open their own minimal Anthropic-SDK connections so prep's behavior can't silently drift whenever someone tunes translator config.

Both fill the same 15-block `<mtls_project_context schema_version="1.0">` schema — see [context.xml](#contextxml) below — with the same one deliberate gap: `character_attribute_anchors` stays `<pending/>`, since that block belongs to a cross-volume CAA (Character Attribute Anchors) subsystem this client doesn't run. The full block-by-block spec lives in `src/prompt/prep_prompt_deepseek_en.xml`; `src/prep/block_prompts.py` builds the parallel path's per-block prompts from that same file rather than re-authoring 14 blocks' worth of instructions a second time.

**Unified** (`prep.parallel.enabled: false`, the default) — one Pro call, full document in, full document out. Simple, proven, and the only path that ever runs unless you opt into the other one.

**Parallel** (`prep.parallel.enabled: true`) — trades one large call for many small ones, riding DeepSeek's automatic disk-based prompt caching:

1. One Pro call reads the JP chapters and extracts `character_roster` + `name_map` — this also happens to warm the cache for the (long) JP-chapters prefix every later call shares.
2. One sequential Flash call fills a single block, warming the cache for the *exact* prefix (JP chapters + the just-extracted roster) every remaining call will reuse.
3. Twelve more Flash calls fire in parallel, each returning one small block instead of the whole document — every one of them should hit the now-warm cache for the shared prefix and only pay fresh for its own short block-specific instruction.

`fallback_to_unified: true` (the default) means any failure in the parallel path — a malformed block, a truncated response, an assembly error — falls straight back to the proven Unified call rather than failing prep outright. A real production run surfaced exactly that: one Flash-filled block truncated on a long volume because its output ceiling was too tight, assembly failed, and the fallback fired — at the cost of paying for both attempts. `prep.parallel.flash_max_output_tokens` was raised in response; see `config.yaml`'s inline comment on that key for the numbers.

Every call in both paths — Unified's one call, Parallel's fourteen — writes a row to the per-volume cost ledger. See [Token & Cost Log](#token--cost-log).

### Series Continuity — The Bible Writer

`bibles/<series_id>/` is three flat, human-readable JSON files — grep-able, diff-able, editable by hand:

| File | Content | Source |
|------|---------|--------|
| `term_lock.json` | JP→EN name table for characters + everything else in `name_map` | context.xml's `character_roster` + `name_map` |
| `verbatim_anchors.json` | Recurring signature phrases, locked wording, which volumes they appeared in | context.xml's `verbatim_anchors` |
| `series_pack.json` | Volumes processed, merged voice fingerprints, last-known EPS (Emotional Proximity Scale) band per character | context.xml's `voice_fingerprints` + `eps_arc_tracker` |

No database, no ChromaDB, no vector store — just three flat JSON files per series. On the *next* volume of a series, `prep_volume` matches the new volume's JP title against every `series_pack.json`'s `series_title_jp`; a match loads the bible and injects it into the prep call so DeepSeek reuses locked names and voices instead of reinventing them. Writing the bible is deliberately MCP/IDE-agent-only, not a CLI/TUI command — it should follow someone actually looking at the QC report, not run unconditionally on every `run`.

---

## Telemetry & Developer Tools

Every artifact in this section is **per-project** — written under `work/<volume_id>/`, never a shared pipeline-root file. An earlier revision of the cost ledger got this wrong (one file at the repo root, every volume's calls interleaved with no clean way to tell them apart); it's per-volume now, and every mechanism below follows the same rule.

### Token & Cost Log

`work/<volume_id>/LOG/token_log_<run-stamp>.md` — one row per API call, written by both prep paths and the translator through a single shared module (`src/common/token_telemetry.py`), so the pricing table and the logging format can't drift into two different implementations.

- **One file per run, not per volume.** The run stamp (date, time, a short random tie-breaker) is established once, the first time a process logs a call for a volume, and reused for every call in that same run — one prep run's fourteen calls, or one translate run's N chapters, land in one file. A genuinely separate invocation later is a new process, gets a new stamp, and never overwrites or blends into an earlier run's log.
- **Self-identifying header** — pulled from `context.xml`'s own `<opf_metadata>` block (title, author, publisher — whatever Librarian already extracted from the EPUB's OPF at extraction time, before prep or translation ever runs), plus the raw JP source size (character count across `JP/*.md`) and an estimated whole-volume input-token count.
- **Cached-token counts always come from the API's own `usage` object**, never derived locally — that's server-side knowledge (which bytes DeepSeek's disk cache actually served) no local tokenizer can reconstruct.

### THINKING Density Map

Translator-only (prep's calls are single-shot structured extraction, not the multi-round conversational loop this measures the shape of). `work/<volume_id>/THINKING/density_map.html` — rebuilt after every chapter from every `THINKING/*.md` file on disk, gated by `translation.thinking_log.density_map.enabled` (on by default, and a no-op if `thinking_log` itself is off — nothing to analyze without it).

Measures two things per chapter, both grounded in that volume's own `context.xml`, never a hardcoded schema: how *long* the chapter's silent reasoning ran, and how much of the canon graph it actually *referenced* — which context.xml blocks, which named characters, by keyword/regex against the real reasoning text (which does not reliably follow the master prompt's aspirational tagged format). Compiled into a self-contained HTML report: a bubble map (chapter × reasoning length, dot size = blocks referenced) sharing an axis with a block-activation heatmap underneath, plus a plain data table fallback. A chapter with near-zero block references is not necessarily a bug — sometimes the model correctly coasts on stable conversational context — but it's a real signal worth being able to see.

### Dry Run

A developer flag, not a production one: `--dry-run` on `translate` (CLI), `dry_run` on the MCP translator tools, or the Dashboard's session-only "Developer" toggle in the TypeScript TUI (press `d` on the Dashboard screen — pre-checks Dry Run on the next Translate Volume form, never written to `config.yaml`).

Assembles the exact request payload a chapter's translation call would send — model, system prompt, user turn, thinking/effort configuration — and writes it to `work/<volume_id>/DRY_RUN/<run-stamp>/<chapter_id>.md` instead of sending it. Zero API cost, no `EN/` output, no manifest changes. One deliberate tradeoff: when multi-turn conversation mode is enabled, a dry run shows the raw single-turn payload, not the conversation-accumulated one — assembling the real accumulated shape requires running the conversation manager's turn-preparation step for real, which can itself trigger a genuine, billed checkpoint-summarization call. Skipping that step entirely is what keeps "dry" an actual guarantee instead of a probabilistic one.

### Local Tokenizer

All local token counting (`src/common/token_telemetry.py`) runs on a bundled `tiktoken` encoding — no network call, no account, no credential. An earlier revision routed through DeepSeek's own tokenizer repos on HuggingFace for a tighter estimate; reverted after it turned out to need a HuggingFace Hub token in practice (rate-limited or gated, depending on the repo), which is a dependency a token-counting utility has no business silently acquiring. The tradeoff is explicit everywhere this number surfaces: it is an approximation, useful for cost estimates and context-budget tracking, never presented as an exact per-model count.

---

## Why DeepSeek V4 Model?

### Specifications

**IMPORTANT UPDATE**: DeepSeek has confirmed they will raise the API pricing significantly, at an unknown date. This repo reflects only the current pricing, when price increases we will update accordingly

DeepSeek V4 reached **general availability in July 2026**, closing out a preview period that began April 24, 2026. GA brought a hard cutover: as of July 24, 2026, the legacy aliases (`deepseek-chat`, `deepseek-reasoner`) stopped resolving entirely — every call has to name a real GA model ID. This client only ever used the GA IDs (`deepseek-v4-pro`, `deepseek-v4-flash`) throughout, so that cutover was a non-event here. Two tiers, both text-only:

| | V4 Pro | V4 Flash |
|---|---|---|
| Total parameters | 1.6T | 284B |
| Active parameters/token (MoE) | ~49B | ~13B |
| Context window | 1M tokens | 1M tokens |
| Input, cache miss | $0.435 / MTok | $0.14 / MTok |
| Input, cache hit | $0.003625 / MTok | $0.0028 / MTok |
| Output | $0.87 / MTok | $0.28 / MTok |

GA also announced a peak/off-peak pricing surcharge (UTC+8 peak hours) that is **not live** as of this writing — no percentage or start date has been published, and every rate above is still one flat per-million-token price regardless of time of day. Worth instrumenting for later, not something to plan a translation schedule around yet.

Architecturally, V4 pairs **DeepSeek Sparse Attention** (compressed sparse attention + hierarchical/coarse-grained attention — context gets compressed into summaries, relevant regions selected, full attention applied only there) with **Manifold-Constrained Hyper-Connections**, a training-stabilization framework that caps signal amplification under 2x at ~6.7% compute overhead. Net effect: near-linear rather than quadratic cost growth against context length, which is what makes a 1M-token window economically viable per call instead of a marketing number nobody actually fills. Prompt caching is automatic on DeepSeek's side — no cache-control headers, no opt-in flag, no SDK changes; the server caches the longest stable byte-prefix of each request on its own.

Sources: [DeepSeek V4 GA — Surge Pricing & Migration](https://deepseek.ai/blog/deepseek-v4-ga-surge-pricing-migration), [DeepSeek V4 — 1T Params, Benchmarks & Pricing](https://deepseek.ai/deepseek-v4), [DeepSeek API Pricing](https://deepseek.ai/pricing), [DeepSeek V4 Pro — OpenRouter](https://openrouter.ai/deepseek/deepseek-v4-pro)

### How this maps onto LLM Translator

| V4 characteristic | Where it lands in this codebase |
|---|---|
| Automatic server-side prefix caching | `translation.deepseek.caching` in `config.yaml`; `deepseek_client.py`'s `cache_monitor` warns below a 70% hit ratio — see the real number below |
| DRDI/DOVB placed in the *user* turn, not the system prompt | `deepseek_optimization.py` — keeps the system prompt + context.xml prefix byte-stable across chapters so the cache actually hits; moving them into the system instruction would invalidate the prefix every single chapter |
| 1M context window | `deepseek_conversation.py`'s compaction ladder (`checkpoint_trigger_ratio: 0.85`) exists because the window is large but not infinite — it fires *before* a volume's accumulated conversation would blow past it, not preemptively on every chapter |
| 384K max output ceiling | `translation.deepseek.generation.max_output_tokens` in `config.yaml` |
| Thinking/reasoning mode, effort routing | `thinking.budget_tokens` / `thinking.effort` in `config.yaml`; DeepSeek has no native effort parameter at the API level, which is *why* DRDI exists at all — it's prose-injected reasoning scaffolding standing in for a routing knob the model doesn't expose |
| No dedicated Anthropic-Messages endpoint in most public docs (DeepSeek's own docs describe an OpenAI-compatible path) | `deepseek_client.py` targets `https://api.deepseek.com/anthropic` anyway — DeepSeek also exposes an Anthropic Messages-format endpoint, which is what lets this client reuse the `anthropic` Python SDK instead of carrying a second HTTP client for one provider |
| Pro tier vs Flash tier pricing gap | `translation.deepseek.model` defaults to Pro but Flash is a one-line config change for throughput-over-craft runs; prep's unified path is Pro-only for full-document structured output, but the opt-in parallel path (`prep.parallel.enabled`) routes 12 of its 14 calls to Flash — see [Prep — Two Paths](#prep--two-paths) |
| Multi-turn conversation + prefix caching, used together | `deepseek_conversation.py` isn't just a cost lever — it's the **consistency mechanism**. Every chapter's turn carries the still-cached context.xml prefix plus the last few chapters' JP+EN pairs verbatim, so character voice, locked names, and terminology from context.xml get *extended* turn-to-turn instead of re-derived from scratch each time. Caching is what makes carrying that growing history affordable; the history is what makes 1M-token context actually buy something instead of just being a bigger number |

### Real cost telemetry

Not a benchmark, not an estimate — actual `cost_audit_last_run.json` / `translation_log.json` records from a 5-volume, 48-chapter production run (averaging ~10 chapters per volume) using the identical DeepSeek client and pricing model this repo ships. Model was `deepseek-v4-pro` throughout, no exceptions.

| Vol | Chapters | Input tokens | Output tokens | Cache-read tokens | Cost (USD) |
|---|---|---|---|---|---|
| 1 | 8 | 1,053,277 | 96,919 | 834,688 | $0.18243 |
| 2 | 9 | 1,243,934 | 95,441 | 1,023,232 | $0.18275 |
| 3 | 11 | 1,397,403 | 105,354 | 1,192,320 | $0.18519 |
| 4 | 11 | 1,190,878 | 114,356 | 966,528 | $0.20059 |
| 5 | 9 | 1,112,635 | 88,449 | 920,448 | $0.16389 |
| **Total** | **48** | **5,998,127** | **500,519** | **4,937,216** | **$0.91485** |

**What that works out to:**
- **82.3% cache hit ratio** on input tokens (4,937,216 of 5,998,127) — comfortably clear of the 70% warning threshold `cache_monitor` watches for, every volume, without hand-tuning. That's the multi-turn-conversation-plus-caching design from the table above paying off in a real ledger, not just in theory — and the same mechanism responsible for voice/name/terminology consistency across chapters.
- Caching cut the **input-side** bill specifically by **81.6%** — $0.4794 actually paid vs. $2.6092 it would have cost at the flat cache-miss rate for the same 5,998,127 input tokens. Output tokens aren't cacheable by nature (they're generated, not repeated), so this saving is on input only; the more chapters share a stable prefix, the more of a volume's total cost this shrinks.
- **≈$0.183/volume**, **≈$0.019/chapter** average across all 48 chapters — a full light-novel volume translated for less than the price of a bus fare, on the Pro tier, with thinking enabled throughout.
- Zero `cache_creation_tokens` recorded anywhere in the 5-volume run. DeepSeek doesn't bill cache writes as a separate line item the way some providers do — a cache-establishing turn (chapter 1 of a volume, or right after a compaction-ladder rung resets the prefix) is priced as an ordinary cache-miss input, not an extra surcharge on top.

Same client, same endpoint, same pricing table — `PRICING_PER_MTOK` in `src/common/token_telemetry.py` (the shared module both prep paths and the translator log cost through — see [Token & Cost Log](#token--cost-log)) matches these rates exactly — so the economics transfer directly to what running this pipeline will actually cost.

**Parallel prep, first real run.** Separately from the translator numbers above: the cache-warmed parallel prep path's first production run (not a synthetic benchmark) confirmed the design works — calls 3 through 14 landed a **95.7% aggregate cache hit ratio**, matching the projected ~97% from its own design notes. The same run is also where the token-ceiling issue mentioned under [Prep — Two Paths](#prep--two-paths) turned up: one block's output ran to within 776 tokens of a too-tight ceiling, truncated mid-tag, failed assembly, and triggered the Unified fallback — meaning that run paid for both the (wasted) parallel attempt and the fallback that actually shipped, a real 2.5x overpay on that one volume's prep cost. `flash_max_output_tokens` was raised in direct response, and the incident is exactly why `fallback_to_unified` exists as a hard default rather than an opt-in — the alternative was prep failing outright.

---

## Configuration

The main control surfaces in `config.yaml`, one per DeepSeek-touching phase (plus prep's opt-in parallel path and the translator's telemetry toggles):

```yaml
prep:
  model: deepseek-v4-pro           # Flash is not reliable enough for structured XML on its own
  thinking_budget: 48000
  max_output_tokens: 128000
  effort: high                     # output_config.effort for every prep Pro call
  bible_dir: bibles/

  parallel:                        # Cache-warmed parallel path — see Prep — Two Paths
    enabled: false                 # off by default; unified path runs unless this is true
    fallback_to_unified: true      # any failure retries with the proven unified call
    flash_model: deepseek-v4-flash
    flash_thinking_budget: 8000
    flash_max_output_tokens: 32000
    flash_effort: high
    warming_block: voice_fingerprints
    max_concurrent_flash_calls: 12

translation:
  thinking_log:
    enabled: true
    density_map:
      enabled: true                # rebuilds THINKING/density_map.html per chapter

  translator:
    model: deepseek-v4-pro           # or deepseek-v4-flash
    endpoint: https://api.deepseek.com/anthropic

    thinking:
      enabled: true
      budget_tokens: 48000
      effort: max

    caching:
      enabled: true                   # auto-prefix cache

    conversation:
      enabled: true
      recent_verbatim_chapters: 3
      checkpoint_trigger_ratio: 0.85

    optimizations:
      drdi:                           # Reasoning Directive Injection
        enabled: true
      dovb:                           # Optimized Voice Blocks
        enabled: true

    streaming:
      enabled: true

    post_processing:
      scene_break_formatting: true
      cjk_cleanup: true
      salvage_reasoning_leaked_answer: true
```

`phase_routing` is a third, optional block — see [MCP Tools](#mcp-tools) below.

See `config.yaml` for the full documented parameter set.

### Key Parameters

| Parameter | Default | What It Controls |
|-----------|---------|------------------|
| `prep.model` | `deepseek-v4-pro` | The unified path is always Pro — structured 15-block output in one shot needs the instruction-following |
| `prep.parallel.enabled` | `false` | Opt into the cache-warmed parallel prep path instead of the unified call |
| `prep.parallel.flash_max_output_tokens` | `32000` | Per-block output ceiling for Flash calls — see the real-incident note under Prep — Two Paths for why this isn't lower |
| `translation.deepseek.model` | `deepseek-v4-pro` | Pro ($0.435/$0.87 per MTok) vs Flash ($0.14/$0.28) |
| `thinking.budget_tokens` (both) | `48000` | CoT (Chain of Thought) token budget per turn |
| `translation.thinking_log.density_map.enabled` | `true` | Rebuild the THINKING density map after every chapter |
| `conversation.recent_verbatim_chapters` | `3` | How many prior chapters are included verbatim |
| `conversation.checkpoint_trigger_ratio` | `0.85` | Context fullness at which a paid checkpoint fires |
| `optimizations.drdi.enabled` | `true` | EPS-band reasoning scaffolding in user turn |
| `optimizations.dovb.enabled` | `true` | Character voice templates (contraction rules, archetypes) |
| `post_processing.salvage_reasoning_leaked_answer` | `true` | Recovers chapters emitted into `reasoning_content` |

---

## File Structure

```
LLM Translator/
├── config.yaml                 ← Master config: prep + translator + phase_routing knobs
├── .env                        ← DEEPSEEK_API_KEY (single secret, shared by prep and translate)
├── requirements.txt            ← Python deps (anthropic, pyyaml, lxml, bs4, Pillow, tiktoken, mcp)
├── PLANNING.md                 ← Full implementation plan
├── README.md                   ← This file
├── mtl.bat / mtl.sh             ← CLI launcher (Windows / Linux+macOS)
├── mtl-ts.bat / mtl-ts.sh       ← TypeScript TUI launcher (Windows / Linux+macOS)
├── .mcp.json                   ← MCP server definition
│
├── raw/                        ← Drop input EPUBs here
├── work/                       ← Per-volume working directories (auto-created)
│   └── <volume_id>/
│       ├── context.xml, manifest.json, JP/, EN/, QC/
│       ├── LOG/                 ← token_log_<run-stamp>.md — see Token & Cost Log
│       ├── THINKING/            ← per-chapter reasoning + density_map.html
│       └── DRY_RUN/             ← <run-stamp>/<chapter_id>.md — see Dry Run
├── bibles/                     ← Cross-volume series continuity (auto-created on first write_bible)
├── output/                     ← Final assembled EPUBs
├── dev/                        ← Standalone diagnostic scripts, not part of any pipeline run
│
├── src/
│   ├── common/
│   │   ├── config.py, os_config.py, llm_types.py, atomic_io.py, interactive.py
│   │   └── token_telemetry.py  ← Shared pricing table, local tokenizer, per-run cost log — used by both prep paths and the translator
│   ├── librarian/              ← Phase 1: EPUB extraction (deterministic, zero LLM calls)
│   │   └── publisher_profiles/ ← 11 publisher JSON profiles
│   ├── prep/                   ← context.xml builder — two paths, see Prep — Two Paths
│   │   ├── agent.py            ← run_prep() — unified path + dispatch to the parallel path below
│   │   ├── parallel_agent.py   ← run_parallel_prep() — cache-warmed Pro+Flash fan-out
│   │   ├── block_prompts.py    ← Per-block prompt assembly, shared prefix/system-prompt builders
│   │   └── block_assembler.py  ← Merges block fragments into one context.xml, cross-block validation
│   ├── translator/             ← Phase 2: Bare DeepSeek send-and-receive
│   │   ├── agent.py                 ← ~200-line orchestrator
│   │   ├── deepseek_client.py       ← Anthropic SDK wrapper
│   │   ├── deepseek_conversation.py ← Persistent conversation + prefix tracking
│   │   ├── deepseek_optimization.py ← DRDI, DOVB, CCT
│   │   ├── prompt_loader.py         ← Loads master prompt, injects context.xml
│   │   ├── context_manager.py       ← Reads context.xml from work dir
│   │   ├── scene_break_formatter.py
│   │   ├── thinking_density.py      ← Builds THINKING/density_map.html
│   │   └── dry_run.py               ← Renders a dry-run payload to markdown
│   ├── qc/                     ← Filesystem-only sanity gate (run_qc())
│   ├── bible/                  ← Bible Writer (run_write_bible())
│   ├── builder/                ← Phase 4: EPUB assembly (deterministic, zero LLM calls)
│   ├── prompt/                 ← master_prompt_deepseek_en(.xml/_v2.xml), prep_prompt_deepseek_en.xml, localization_policy.md
│   └── mcp/                    ← Slim MCP server (6 tool servers, 20 tools, 2 resources)
│       ├── server.py           ← Main MCP entry (stdio)
│       ├── harness.py          ← Persona propagation (local instructions file) + phase->tool routing (config.yaml)
│       ├── servers/
│       │   ├── librarian_server.py  ← 8 tools
│       │   ├── prep_server.py       ← 1 tool: prep_volume
│       │   ├── translator_server.py ← 2 tools: translate_chapter, run_translator (both take dry_run)
│       │   ├── qc_server.py         ← 1 tool: qc_volume
│       │   ├── bible_server.py      ← 1 tool: write_bible
│       │   └── builder_server.py    ← 7 tools
│       └── runtime.py          ← Shared MCP infrastructure
│
├── scripts/
│   └── mtl.py                  ← CLI: extract, prep, translate, qc, build, run, list, status
│
├── mtls-menu-ts/                ← TypeScript Ink/React TUI
│   ├── package.json             ← ink, react, tsx, @modelcontextprotocol/sdk
│   ├── src/core/capabilities.ts ← typed CLI/MCP capability registry and validation
│   ├── src/core/console.ts      ← 5,000-entry structured terminal ring buffer
│   ├── src/core/mcpClient.ts    ← per-capability JSON-RPC client over stdio
│   ├── src/core/preflight.ts    ← interpreter/import/key/MCP readiness checks
│   ├── src/ui/App.tsx           ← reducer-driven responsive operator workspace, Dashboard's Developer panel
│   ├── src/ui/useFileWatcher.ts ← File and directory watchers backing the Dashboard's live views and volume-list refresh
│   └── src/app/index.tsx        ← alternate-screen entry point and cleanup
│
└── .github/
    ├── AGENTS.md
    ├── skills/deepseek-translator/SKILL.md
    └── agents/deepseek-translator.agent.md
```

---

## CLI Reference

```bash
python scripts/mtl.py extract <epub_path>     # Phase 1: EPUB → JP chapters + barebone context.xml
python scripts/mtl.py prep <vol_id>           # Unified DeepSeek call: fills context.xml
python scripts/mtl.py translate <vol_id>      # Phase 2: JP → EN (DeepSeek V4 Pro)
python scripts/mtl.py translate <vol_id> --dry-run   # Assemble the payload, send nothing — see Dry Run
python scripts/mtl.py qc <vol_id>             # Filesystem-only sanity gate (zero API cost)
python scripts/mtl.py build <vol_id>          # Phase 4: EN → EPUB
python scripts/mtl.py run <epub_path>         # Full pipeline: extract → prep → translate → qc → build
python scripts/mtl.py list                    # List volumes in work/
python scripts/mtl.py status <vol_id>         # Pipeline state + chapter completion summary
```

No `bible` CLI command — see [Series Continuity](#series-continuity--the-bible-writer).

---

## MCP Tools

The MCP server exposes 6 tool groups (20 tools) and 2 resources for IDE-agent integration. Every phase this client runs (Prep, Translator, QC, Bible Writer) is **reified as an MCP tool** — the IDE agent calls one tool per phase instead of reading a skill file and manually orchestrating sub-steps.

| Server | Tools | Phase |
|--------|-------|-------|
| Librarian | `extract_epub`, `parse_opf_metadata`, `parse_toc`, `convert_xhtml_to_markdown`, `detect_publisher`, `catalog_images`, `split_content`, `run_librarian` | 1 |
| **Prep** | `prep_volume(volume_id, series_id=None)` — direct binding to `src.prep.agent.run_prep`, one DeepSeek call, no subprocess, no Gemini | 1.P |
| **Translator** | `translate_chapter`, `run_translator` — direct Python bindings to `src.Deepseek.translator.agent.DeepSeekTranslator`. No subprocess, no CLI, no skill delegation. Both take `dry_run` — see [Dry Run](#dry-run). | 2 |
| **QC** | `qc_volume(volume_id)` — completeness, truncation, token sanity, name-drift, structural checks (filesystem-only, &lt;5s) | Gate |
| **Bible** | `write_bible(volume_id, series_id=None)` — merges context.xml into `bibles/<series_id>/*.json` | Continuity |
| Builder | `markdown_to_xhtml`, `generate_opf`, `generate_nav`, `merge_translated_shards`, `optimize_image`, `package_epub`, `run_builder` | 4 |

**Resources** (`src/mcp/harness.py`): `persona://instructions` (this project's local operating-persona file), `routing://phases` and `routing://phases/{phase}` (phase-name → tool-name map, overridable via `config.yaml`'s `phase_routing` section).

### MCP Tool Design Notes

| MCP Tool | Design Note |
|---|---|
| `prep_volume` | One DeepSeek call, no Gemini, no ChromaDB, no subprocess |
| `run_translator` | Bare send-and-receive, prompt-inline policy |
| `qc_volume` | Filesystem-only, &lt;5 sec, zero API cost |
| `write_bible` | Flat JSON continuity merge, not a database |

The corresponding `.github/skills/` files are thin wrappers — they document the tool interface and workflow order; the actual execution happens server-side in the MCP tool.

Start the server: `python -m src.Deepseek.mcp.server`

---

## Requirements

- **Python** 3.11+
- **Node.js** 20+ (TypeScript TUI only)
- **DeepSeek API key** (set via `.env` or `DEEPSEEK_API_KEY` env var) — the only secret this client needs, used by both prep and translate

Python packages: `anthropic`, `pyyaml`, `python-dotenv`, `lxml`, `beautifulsoup4`, `Pillow`, `tiktoken`, `mcp`

Node packages (TUI only): `ink`, `ink-spinner`, `react`, `tsx`, `typescript`, `@modelcontextprotocol/sdk`

---

## context.xml

Every volume gets a `context.xml` at `work/<volume_id>/context.xml` — a 15-block document that's the translator's entire continuity/voice/terminology reference. `extract` writes a barebone version (every block `<pending/>`); `prep` fills 14 of them in one DeepSeek call. Root shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<mtls_project_context schema_version="1.0" volume_id="..." series_id="..." target_language="en" generated_by="deepseek_prep" generated_at="...">
  <opf_metadata format="json">{...raw OPF passthrough, untouched by prep...}</opf_metadata>
  <validation_audit status="passed">...</validation_audit>
  <volume_identity>...</volume_identity>
  <world_setting type="...">...</world_setting>
  <character_roster>...</character_roster>
  <name_map>...</name_map>
  <relationship_graph>...</relationship_graph>
  <verbatim_anchors>...</verbatim_anchors>
  <character_attribute_anchors status="pending"><pending>Awaiting owning agent.</pending></character_attribute_anchors>
  <voice_fingerprints>...</voice_fingerprints>
  <cultural_glossary>...</cultural_glossary>
  <eps_arc_tracker>...</eps_arc_tracker>
  <scene_plans>...</scene_plans>
  <eps_signals>...</eps_signals>
  <illustration_context>...</illustration_context>
  <translation_brief generated_by="deepseek_prep"><raw>...</raw></translation_brief>
</mtls_project_context>
```

`character_attribute_anchors` stays `<pending/>` on purpose — that block belongs to a cross-volume CAA subsystem out of scope for this client; every other block gets real content or an honest `<pending/>` if prep genuinely lacked signal (e.g. a volume with no illustrations). The exact per-block schema — attributes, nesting, what counts as valid — is documented in full in `src/prompt/prep_prompt_deepseek_en.xml`.

You can also translate without running prep at all — the master prompt carries enough inline guidance to produce a translation from the barebone context.xml — but `DeepSeekTranslator` logs a loud warning when it does, because character voice, locked names, and continuity anchors are all off in that mode.
