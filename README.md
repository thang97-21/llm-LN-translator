# LN Translation Client - DeepSeek-Powered

**Single-provider pipeline** for Japanese → English light novel translation.
Extract, prep, translate, QC, build — and remember the series for next time.

---

## What It Is

A self-contained translation pipeline that ingests a Japanese EPUB, builds a
full character/voice/continuity reference in a single structured API call,
translates every chapter through a high-capacity language model with automatic
prefix caching, runs a zero-cost sanity gate, and outputs a finished English
EPUB. On a sequel volume, it recalls the previous volume's names and voices
without being told twice.

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
# 1. Clone or copy into DeepSeek_MTLS/
cd DeepSeek_MTLS

# 2. Create a Python venv
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate   # Linux/macOS

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Set your API key
# Edit .env or set the environment variable:
set DEEPSEEK_API_KEY=sk-your-key-here

# 5. Drop an EPUB in raw/
copy "C:\path\to\your-light-novel.epub" raw\

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
mtl-ts.bat
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
opens them.

Primary workflows use the canonical CLI and expose every real argument:

| Workflow | Fields |
|---|---|
| Extract | EPUB, optional volume ID |
| Prep | volume, optional series ID |
| Translate | volume, optional chapter multi-select |
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
or session-wide `--mcp` modes are gone; `mtl.bat` remains the standalone CLI.

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

The MCP server does the orchestration; the agent just calls tools in sequence. The only prerequisite is `DEEPSEEK_API_KEY` in `.env` — nothing else, no main-pipeline path to configure.

**Persona + routing resources:** the server also exposes two MCP *resources* (not tools) via `src/mcp/harness.py` — `persona://instructions` (this project's `CLAUDE.md`, verbatim, so a remote or non-Claude-Code agent connecting over MCP gets the same operating context a local session gets automatically) and `routing://phases` (the phase-name → tool-name map, e.g. `"translate" → "run_translator"`, overridable via `config.yaml`'s `phase_routing` section). Neither is required reading to use the server — they exist for agents that want to introspect it.

---

## Architecture

```
raw/                          ← Drop EPUBs here
    │
    ▼
[Phase 1: Librarian]          ← src/librarian/          (copied verbatim from main pipeline)
    JP chapter .md extraction, TOC parsing, illustration export
    Writes a barebone 15-block context.xml (every block <pending/>) + manifest.json
    │
    ▼
[Prep: Unified DeepSeek Call] ← src/prep/                (single DeepSeek V4 Pro API call, own client)
    Reads barebone context.xml + all JP chapters (+ series bible, if a sequel) → fills 14 blocks:
    volume_identity, world_setting, character_roster, name_map, relationship_graph,
    verbatim_anchors, voice_fingerprints, cultural_glossary, eps_arc_tracker, eps_signals,
    scene_plans, illustration_context, translation_brief, validation_audit
    Writes manifest.json["metadata"]/["metadata_en"] for Builder OPF assembly + TRANSLATION_BRIEF.md
    (character_attribute_anchors stays <pending/> — that block belongs to a subsystem out of scope here)
    │
    ▼
[Phase 2: Translator]         ← src/translator/          (direct Python import, DeepSeek V4 Pro)
    Anthropic-format endpoint, auto-prefix cache, DRDI reasoning directives, DOVB voice blocks
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
[Phase 4: Builder]            ← src/builder/               (copied verbatim from main pipeline)
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

### Prep — What Makes It "Unified"

Prep is a single stateless DeepSeek API call, deliberately **not** built on `DeepSeekClient` — that class is tuned for the chaptered, conversation-aware translation loop (prefix cache monitor, persistent conversation manager). Prep has none of that: `src/prep/agent.py` opens its own minimal Anthropic-SDK connection so its behavior can't silently drift whenever someone tunes translator config.

It fills the exact 15-block schema the main pipeline's `scripts/build_context_xml.py` produces (same root `<mtls_project_context schema_version="1.0">`, same block names and order), with one deliberate gap: `character_attribute_anchors` stays `<pending/>` — that block belongs to a cross-volume CAA subsystem this client doesn't run. The full block-by-block spec lives in `src/prompt/prep_prompt_deepseek_en.xml`.

### Series Continuity — The Bible Writer

`bibles/<series_id>/` is three flat, human-readable JSON files — grep-able, diff-able, editable by hand:

| File | Content | Source |
|------|---------|--------|
| `term_lock.json` | JP→EN name table for characters + everything else in `name_map` | context.xml's `character_roster` + `name_map` |
| `verbatim_anchors.json` | Recurring signature phrases, locked wording, which volumes they appeared in | context.xml's `verbatim_anchors` |
| `series_pack.json` | Volumes processed, merged voice fingerprints, last-known EPS band per character | context.xml's `voice_fingerprints` + `eps_arc_tracker` |

No database, no ChromaDB, no vector store — this is not the main pipeline's publisher/author/anime-metadata `bibles/*.json`. On the *next* volume of a series, `prep_volume` matches the new volume's JP title against every `series_pack.json`'s `series_title_jp`; a match loads the bible and injects it into the prep call so DeepSeek reuses locked names and voices instead of reinventing them. Writing the bible is deliberately MCP/IDE-agent-only, not a CLI/TUI command — it should follow someone actually looking at the QC report, not run unconditionally on every `run`.

---

## Why DeepSeek V4 Model?

### Specifications

DeepSeek V4 launched April 24, 2026 in two tiers, both text-only:

| | V4 Pro | V4 Flash |
|---|---|---|
| Total parameters | 1.6T | 284B |
| Active parameters/token (MoE) | ~49B | ~13B |
| Context window | 1M tokens | 1M tokens |
| Input, cache miss | $0.435 / MTok | $0.14 / MTok |
| Input, cache hit | $0.003625 / MTok | $0.0028 / MTok |
| Output | $0.87 / MTok | $0.28 / MTok |

Architecturally, V4 pairs **DeepSeek Sparse Attention** (compressed sparse attention + hierarchical/coarse-grained attention — context gets compressed into summaries, relevant regions selected, full attention applied only there) with **Manifold-Constrained Hyper-Connections**, a training-stabilization framework that caps signal amplification under 2x at ~6.7% compute overhead. Net effect: near-linear rather than quadratic cost growth against context length, which is what makes a 1M-token window economically viable per call instead of a marketing number nobody actually fills. Prompt caching is automatic on DeepSeek's side — no cache-control headers, no opt-in flag, no SDK changes; the server caches the longest stable byte-prefix of each request on its own.

Sources: [DeepSeek V4 — 1T Params, Benchmarks & Pricing](https://deepseek.ai/deepseek-v4), [DeepSeek API Pricing](https://deepseek.ai/pricing), [DeepSeek V4 Pro — OpenRouter](https://openrouter.ai/deepseek/deepseek-v4-pro)

### How this maps onto DeepSeek_MTLS

| V4 characteristic | Where it lands in this codebase |
|---|---|
| Automatic server-side prefix caching | `translation.translator.caching` in `config.yaml`; `deepseek_client.py`'s `cache_monitor` warns below a 70% hit ratio — see the real number below |
| DRDI/DOVB placed in the *user* turn, not the system prompt | `deepseek_optimization.py` — keeps the system prompt + context.xml prefix byte-stable across chapters so the cache actually hits; moving them into the system instruction would invalidate the prefix every single chapter |
| 1M context window | `deepseek_conversation.py`'s compaction ladder (`checkpoint_trigger_ratio: 0.85`) exists because the window is large but not infinite — it fires *before* a volume's accumulated conversation would blow past it, not preemptively on every chapter |
| 384K max output ceiling | `translation.translator.generation.max_output_tokens` in `config.yaml` |
| Thinking/reasoning mode, effort routing | `thinking.budget_tokens` / `thinking.effort` in `config.yaml`; DeepSeek has no native effort parameter at the API level, which is *why* DRDI exists at all — it's prose-injected reasoning scaffolding standing in for a routing knob the model doesn't expose |
| No dedicated Anthropic-Messages endpoint in most public docs (DeepSeek's own docs describe an OpenAI-compatible path) | `deepseek_client.py` targets `https://api.deepseek.com/anthropic` anyway — DeepSeek also exposes an Anthropic Messages-format endpoint, which is what lets this client reuse the `anthropic` Python SDK instead of carrying a second HTTP client for one provider |
| Pro tier vs Flash tier pricing gap | `prep.model` is hardcoded to `deepseek-v4-pro` (structured 15-block XML output isn't reliable on Flash); `translation.translator.model` defaults to Pro but Flash is a one-line config change for throughput-over-craft runs |

### Real cost telemetry — 5 volumes, one series

Not a benchmark, not an estimate — actual `cost_audit_last_run.json` / `translation_log.json` records from the main MTLS pipeline's production run of *家事代行のアルバイトを始めたら学園一の美少女の家族に気に入られちゃいました* (*The Housekeeper and the School Idol's Family*), volumes 1–5. (Volume 6 exists in that workspace but never reached the translator — Librarian-only, no cost data — so it's excluded rather than padded in as a zero.) Model was `deepseek-v4-pro` across all 48 chapters, no exceptions.

| Vol | Chapters | Input tokens | Output tokens | Cache-read tokens | Cost (USD) |
|---|---|---|---|---|---|
| 1 | 8 | 1,053,277 | 96,919 | 834,688 | $0.18243 |
| 2 | 9 | 1,243,934 | 95,441 | 1,023,232 | $0.18275 |
| 3 | 11 | 1,397,403 | 105,354 | 1,192,320 | $0.18519 |
| 4 | 11 | 1,190,878 | 114,356 | 966,528 | $0.20059 |
| 5 | 9 | 1,112,635 | 88,449 | 920,448 | $0.16389 |
| **Total** | **48** | **5,998,127** | **500,519** | **4,937,216** | **$0.91485** |

(Volume 4's own `cost_audit_last_run.json` on disk only covers a 2-chapter retry after an earlier failure — its `total_cost_usd` field is *not* the volume total. The figures above for Vol. 4 are summed from `translation_log.json`'s full 11-chapter record instead, and the reconstructed aggregate cross-checks against the sum of all five volumes' reported totals to within half a cent, so the substitution didn't introduce drift.)

**What that works out to:**
- **82.3% cache hit ratio** on input tokens (4,937,216 of 5,998,127) — comfortably clear of the 70% warning threshold `cache_monitor` watches for, every volume, without hand-tuning. That's the DRDI/DOVB-in-user-turn design from the table above paying off in a real ledger, not just in theory.
- Caching cut the **input-side** bill specifically by **81.6%** — $0.4794 actually paid vs. $2.6092 it would have cost at the flat cache-miss rate for the same 5,998,127 input tokens. Output tokens aren't cacheable by nature (they're generated, not repeated), so this saving is on input only; the more chapters share a stable prefix, the more of a volume's total cost this shrinks.
- **≈$0.183/volume**, **≈$0.019/chapter** average across all 48 chapters — a full light-novel volume translated for less than the price of a bus fare, on the Pro tier, with thinking enabled throughout.
- Zero `cache_creation_tokens` recorded anywhere in the 5-volume run. DeepSeek doesn't bill cache writes as a separate line item the way some providers do — a cache-establishing turn (chapter 1 of a volume, or right after a compaction-ladder rung resets the prefix) is priced as an ordinary cache-miss input, not an extra surcharge on top.

This is main-pipeline telemetry, not DeepSeek_MTLS's own — the two are separate codebases that happen to share the identical `deepseek_client.py` lineage and pricing table (`_DEEPSEEK_RATES_PER_MTOK` in `src/translator/deepseek_client.py` matches these rates exactly), so the economics transfer directly to what running this lightweight client will actually cost.

---

## Configuration

Two control knobs in `config.yaml` — one per DeepSeek-touching phase:

```yaml
prep:
  model: deepseek-v4-pro           # Flash is not reliable enough for 14-block structured XML
  thinking_budget: 48000
  max_output_tokens: 128000
  bible_dir: bibles/

translation:
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
| `prep.model` | `deepseek-v4-pro` | Prep is always Pro — no Flash option, structured 15-block output needs the instruction-following |
| `translation.translator.model` | `deepseek-v4-pro` | Pro ($0.435/$0.87 per MTok) vs Flash ($0.14/$0.28) |
| `thinking.budget_tokens` (both) | `48000` | CoT token budget per turn |
| `conversation.recent_verbatim_chapters` | `3` | How many prior chapters are included verbatim |
| `conversation.checkpoint_trigger_ratio` | `0.85` | Context fullness at which a paid checkpoint fires |
| `optimizations.drdi.enabled` | `true` | EPS-band reasoning scaffolding in user turn |
| `optimizations.dovb.enabled` | `true` | Character voice templates (contraction rules, archetypes) |
| `post_processing.salvage_reasoning_leaked_answer` | `true` | Recovers chapters emitted into `reasoning_content` |

---

## File Structure

```
DeepSeek_MTLS/
├── config.yaml                 ← Master config: prep + translator + phase_routing knobs
├── .env                        ← DEEPSEEK_API_KEY (single secret, shared by prep and translate)
├── requirements.txt            ← Python deps (anthropic, pyyaml, lxml, bs4, Pillow, tiktoken, mcp)
├── PLANNING.md                 ← Full implementation plan
├── README.md                   ← This file
├── mtl.bat                     ← Windows Python CLI launcher
├── mtl-ts.bat                  ← Windows TypeScript TUI launcher
├── .mcp.json                   ← MCP server definition
│
├── raw/                        ← Drop input EPUBs here
├── work/                       ← Per-volume working directories (auto-created)
├── bibles/                     ← Cross-volume series continuity (auto-created on first write_bible)
├── output/                     ← Final assembled EPUBs
│
├── src/
│   ├── common/                 ← Shared: config.py, os_config.py, llm_types.py, atomic_io.py, interactive.py
│   ├── librarian/              ← Phase 1: EPUB extraction (copied verbatim from main pipeline)
│   │   └── publisher_profiles/ ← 11 publisher JSON profiles
│   ├── prep/                   ← Unified single-DeepSeek-call context.xml builder
│   │   └── agent.py            ← run_prep() — its own minimal DeepSeek client, not DeepSeekClient
│   ├── translator/             ← Phase 2: Bare DeepSeek send-and-receive
│   │   ├── agent.py                 ← ~200-line orchestrator
│   │   ├── deepseek_client.py       ← Anthropic SDK wrapper
│   │   ├── deepseek_conversation.py ← Persistent conversation + prefix tracking
│   │   ├── deepseek_optimization.py ← DRDI, DOVB, CCT
│   │   ├── prompt_loader.py         ← Loads master prompt, injects context.xml
│   │   ├── context_manager.py       ← Reads context.xml from work dir
│   │   └── scene_break_formatter.py
│   ├── qc/                     ← Filesystem-only sanity gate (run_qc())
│   ├── bible/                  ← Bible Writer (run_write_bible())
│   ├── builder/                ← Phase 4: EPUB assembly (copied verbatim from main pipeline)
│   ├── prompt/                 ← master_prompt_deepseek_en(.xml/_v2.xml), prep_prompt_deepseek_en.xml, localization_policy.md
│   └── mcp/                    ← Slim MCP server (6 tool servers, 20 tools, 2 resources)
│       ├── server.py           ← Main MCP entry (stdio)
│       ├── harness.py          ← Persona propagation (CLAUDE.md) + phase->tool routing (config.yaml)
│       ├── servers/
│       │   ├── librarian_server.py  ← 8 tools
│       │   ├── prep_server.py       ← 1 tool: prep_volume
│       │   ├── translator_server.py ← 2 tools: translate_chapter, run_translator
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
│   ├── src/ui/App.tsx           ← reducer-driven responsive operator workspace
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
python scripts/mtl.py qc <vol_id>             # Filesystem-only sanity gate (zero API cost)
python scripts/mtl.py build <vol_id>          # Phase 4: EN → EPUB
python scripts/mtl.py run <epub_path>         # Full pipeline: extract → prep → translate → qc → build
python scripts/mtl.py list                    # List volumes in work/
python scripts/mtl.py status <vol_id>         # Pipeline state + chapter completion summary
```

No `bible` CLI command — see [Series Continuity](#series-continuity--the-bible-writer).

---

## MCP Tools

The MCP server exposes 6 tool groups (20 tools) and 2 resources for IDE-agent integration. The main MTLS skills this client would otherwise need (Prep, Translator, QC, Bible Writer) are **reified as MCP tools** — the IDE agent calls one tool per phase instead of reading a skill file and manually orchestrating sub-steps.

| Server | Tools | Phase |
|--------|-------|-------|
| Librarian | `extract_epub`, `parse_opf_metadata`, `parse_toc`, `convert_xhtml_to_markdown`, `detect_publisher`, `catalog_images`, `split_content`, `run_librarian` | 1 |
| **Prep** | `prep_volume(volume_id, series_id=None)` — direct binding to `src.prep.agent.run_prep`, one DeepSeek call, no subprocess, no Gemini | 1.P |
| **Translator** | `translate_chapter`, `run_translator` — direct Python bindings to `src.translator.agent.DeepSeekTranslator`. No subprocess, no CLI, no skill delegation. | 2 |
| **QC** | `qc_volume(volume_id)` — completeness, truncation, token sanity, name-drift, structural checks (filesystem-only, &lt;5s) | Gate |
| **Bible** | `write_bible(volume_id, series_id=None)` — merges context.xml into `bibles/<series_id>/*.json` | Continuity |
| Builder | `markdown_to_xhtml`, `generate_opf`, `generate_nav`, `merge_translated_shards`, `optimize_image`, `package_epub`, `run_builder` | 4 |

**Resources** (`src/mcp/harness.py`): `persona://instructions` (this project's CLAUDE.md), `routing://phases` and `routing://phases/{phase}` (phase-name → tool-name map, overridable via `config.yaml`'s `phase_routing` section).

### Skill → MCP Tool Mapping

| Main MTLS Skill | DeepSeek_MTLS MCP Tool | What Changed |
|---|---|---|
| `mtls-prep-phase` (Phases 1→1.7, Gemini, ChromaDB) | `prep_volume` | One DeepSeek call, no Gemini, no ChromaDB, no subprocess into the main pipeline |
| `mtls-translator` (13 RAG modules) | `run_translator` | Bare send-and-receive, prompt-inline policy |
| `mtl-quality-evaluator` (3-model fan-out) | `qc_volume` | Filesystem-only, &lt;5 sec, zero API cost |
| *(new — no main-pipeline equivalent)* | `write_bible` | Flat JSON continuity merge, not the main pipeline's full bible system |

The corresponding `.github/skills/` files are thin wrappers — they document the tool interface and workflow order; the actual execution happens server-side in the MCP tool.

Start the server: `python -m src.mcp.server`

---

## Comparison to Main MTLS Pipeline

| Aspect | Main MTLS Pipeline | DeepSeek_MTLS |
|--------|-------------------|---------------|
| Python files | ~300 | ~55 |
| Translation code | ~45 files, ~2500-line agent | 4 core files, ~200-line agent |
| Providers | 6 (Anthropic, Gemini, DeepSeek, OpenAI, Kimi, MiMo) | 1 (DeepSeek V4 Pro), everywhere including prep |
| Phases | 1 → 1.5 → 1.51 → 1.52 → 1.55 → 1.56 → 1.6 → 1.7 → 2 → 2.5 → 3 → 4 | 1 → 1.P (prep) → 2 → QC gate → 4 |
| RAG modules | 13 (grammar, voice, EPS, bible, idiom, gap, anti-AI-ism…) | 0 (all folded into the master prompt; prep is one structured call, not RAG) |
| Vector stores | 4 ChromaDB collections | 0 |
| QC gates | 8 auditors + pre-translation gate + post-translation audit | 1 filesystem-only gate, zero API cost |
| Prep | 7 metadata phases (1.5–1.7), Gemini-backed | 1 unified DeepSeek call, no Gemini |
| Post-processing | Phase 2.5 (bible update, CJK clean, validators) | Scene break formatting + CJK cleanup only |
| Series continuity | Cross-volume bibles, VREC anchors, term locks, publisher/author metadata | 3 flat JSON files: term_lock, verbatim_anchors, series_pack |
| Multimodal/Visual | 22 visual analysis modules | 0 (illustration_context is text-only, marker-based) |
| CLI commands | ~25 | 8 |
| MCP tool servers | 8 (~42 tools) | 6 (20 tools + 2 resources) |
| Dependencies | 30+ Python packages | 7 Python packages |
| TypeScript TUI | 12 commands, 8 phases, subprocess-only | typed operator console: six CLI workflows, all 20 MCP tools, local status views |

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
