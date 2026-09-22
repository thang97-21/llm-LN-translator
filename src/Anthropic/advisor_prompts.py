"""Layer 1 escalation text for the proofreading advisor.

Ratified by `docs/anthropic-advisor-mode-plan.md` (the evidence log, Runs 1-11)
and `docs/anthropic-advisor-mode-spec.md` §4/§4.1 (the implementation spec).
Every clause traces to a specific run's finding -- do not re-paraphrase this
text; treat it as frozen except through the same run-then-document process
that produced it.

ESCALATION_BLOCK is Run 8's verbatim bullet list (the configuration that
produced a well-scoped, unleaked consult with a real risk catch), with two
validated amendments folded in directly rather than left as a historical
footnote:

1. The `subculture_reference` bullet is Run 9/10's BROADENED wording (a
   real-world reference of any kind, not just music) -- Runs 1-8 only ever
   tested music references, so the original narrow wording was an accident of
   test-fixture content, not a deliberate scope limit. Run 10's own thinking
   trace independently applied this wider reading unprompted (flagging 夜好性
   as real idol-group branding), evidence the executor already reaches for it
   once the door is open.
2. The closing paragraph is Run 9's ratified `web_search` text -- the simpler,
   single-direction form that actually resolved 壱雫空 in Run 9, not Run 10's
   unvalidated bilingual revision (which Run 10 itself showed does not change
   query behavior; see plan §1 Run 10, §8 item 8).

Deliberately NOT included: Run 7's literal "My question for the advisor is:"
requirement (the one wording variant this session measured and rejected -- it
leaked at effort="medium"); §2.5's query-level-economy paragraph and Run 10's
bilingual-search paragraph (both explicitly unvalidated, no run this session
included either).

AMENDMENT 2026-09-22 -- five clauses added, on evidence from volume 646941
(17 chapters, 15 consults; see that volume's QC/proofreading_mode_analysis.md
and docs/anthropic-advisor-mode-plan.md §3). Each traces to something the run
measured, not to a wish list:

1. Irreversible-decision focus (device ENGLISH CARRIER, multi-register scenes).
   The advisor did both unprompted and the results held in shipped output --
   Ch.6's 茶番/本番 pairing rendered consistently so the punchline landed as a
   callback, Ch.11's thrice-repeated irony formula kept to one fixed frame,
   Ch.15's repeated Theodor line preserved as escalation. Naming them makes a
   behaviour that happened to emerge into one that is asked for.
2. Reading-order discipline. The advisor spontaneously flagged Ch.3's unnamed
   crossroads girl as a character not to let the wording conflate with Raika.
   The same hazard is live volume-wide: the brief requires Akito's
   misunderstanding to be played straight until Ch.15, so a "helpful" early
   resolution using later context would destroy the book.
3. Previous-chapter review. THE finding of that run: the advisor already reads
   finished chapters -- Ch.5's consult grades Ch.1's shipped draft, Ch.6's
   grades Ch.5's -- yet it called Ch.1 "shipped cleanly" while a duplicated
   transition sat three lines from that chapter's end. Capability was never the
   gap; nobody had asked it to look. This costs no extra call.
4. Abstention. Already emergent (Ch.6, 13, 14 and 16 each told the executor to
   skip the call), and worth stabilising: advisor spend on that volume exceeded
   the translation it advised, $2.31 against $2.04.
5. Literary uplift. The ONE clause here shipping unvalidated -- Proofreading
   Mode was never run with it active, so no measurement exists. It is paired
   deliberately with clause 4, whose job is to counterweight the failure mode
   it introduces: an advisor talking a correct draft into a change. Measure
   this one specifically; see the plan's §4.
"""

from __future__ import annotations

ESCALATION_BLOCK = """Call the advisor before committing to a rendering whenever THIS chapter's source — regardless of
what project context does or doesn't flag — contains any of:

- A real-world reference of any kind (subculture_reference) — a band, song, work, or media title; a
  real person or place named for more than plain geography; a historical or current event; a brand or
  product; or a phrase/line that echoes a famous quote, meme, or catchphrase a reader might recognize
  from outside the story. Do not infer a romanization or translated title from meaning; confirm
  whether an established form exists.
- A forced/non-standard kanji reading, ateji, or a written-vs-spoken mismatch (ateji).
- An unresolved referent — pronoun, gender, or speaker attribution you cannot pin down from
  context alone (ambiguity).
- Three or more active speakers whose voices must stay distinct in the same scene
  (multi_speaker / voice_contrast).
- A sentence or paragraph whose source structure resists a natural English mapping without
  losing meaning (structural).
- A descriptive passage carrying literary weight — not limited to a character's physical
  description. Includes a landmark or setting rendered for atmosphere rather than plain
  geography, and a character's emotional interior (internal psychological state, not just
  externally observable action or dialogue).
- A scene whose surface dialogue carries a deeper thematic or symbolic structure (a confession
  disguised as wordplay, a callback the reader is meant to recognize, a motif payoff).
- A scene running at high emotional intensity (EPS band WARM or HOT).

This applies whether or not project context data marks the chapter as high-risk. Use your own
reading of the source, every time.

Finding one instance of any category above is evidence there may be more of the SAME kind nearby —
a shiritori chain, a discography list, a quiz, a run of dialogue from the same ambiguous speaker.
Before writing, scan for every sibling instance in the chapter and apply the identical policy to
all of them, not only the one you noticed first. A consult that resolves one instance does not
discharge the category — treat it as a standing rule for the rest of the chapter, not a checkbox
you tick once.

Before declaring the chapter finished, verify every decision in this category was actually applied
consistently across the whole chapter — not just at the point where you first noticed it.

The advisor answers whatever it can see in the conversation so far — it is not handed a question;
it reads everything up to the point you call it. Before you call it, identify — in your own
reasoning, not in your visible response — the specific point you are unsure of: the exact line,
word, or choice you don't yet have an answer for, not a general sense that the chapter is
difficult. The advisor exists to confirm, correct, or fill that one gap — not to solve the chapter
for you. You are the sole author of the final translation, every sentence of it. Whatever the
advisor tells you gets folded into your own prose, in your own voice — never transcribed as its
wording, and never treated as a substitute for having made the call yourself first.

The advisor is a model, the same as you are — not a verified source. Weigh its answer against the
source text and this chapter's project data; do not treat it as settled fact merely because it was
confident. If it directs you to check something against an existing list or lexicon and that check
comes up empty, that is not license to invent a rendering — fall back to the project's own
established rule instead: when a real-world reference cannot be confirmed by either of you, keep it
in its native or romanized form rather than guessing at an English equivalent.

When you do consult, spend the call on the decision that is hardest to reverse. Two are worth naming.
First, the ENGLISH CARRIER for any CLASS A or CLASS B device: everything downstream inherits that
choice, and a device that dies there dies silently while every sentence stays superficially accurate.
Second, a scene where three or more registers sit back to back with few action tags — settle which
signature each speaker holds, and where they are likeliest to collide, before you draft it rather than
after. Both ride the consult the gate already triggered; neither is a reason to call a second time.

Resolve ambiguity, referents, and intent using only what is knowable at this chapter's position in the
reading order. A gap the source deliberately holds open — a misunderstanding the POV character has not
yet corrected, a fact no character on the page knows yet — is a structure to protect, not a defect to
repair. Project context may reveal how something resolves later; that is not licence to change how this
chapter reads, and an earlier chapter is not inconsistent merely because a later one explains it.

The advisor can see the chapters you have already finished, not only the one you are drafting. Use
that: when you consult, ask it to report anything actually wrong in the PREVIOUS chapter's finished
English — a seam, an unintended repetition across a paragraph break, a register slip, a device that
died in the crossing, or any of the copyedit patterns the craft policy already lists. Findings only,
each naming its line; you apply your own fix or record why you declined, and its wording never becomes
yours. Nothing else in this pipeline reads finished English, so a defect nobody names here ships.

Not consulting is a real answer. If your own read is already correct, or the only open question is one
the source or the project data settles, say so in your reasoning and draft — do not spend a call to
have a conclusion you already reached confirmed. If a reply tells you nothing you had not already
decided, that is a signal to consult less often, not to go looking for something to change. A correct
draft talked into a change is a worse outcome than a call you never made.

Once you are already consulting about a flagged passage, you may also ask whether your planned
rendering can be sharpened past a literal-but-correct baseline — tighter rhythm, a stronger verb, a
more natural clause order — using only what the source, the established voice profiles, and the locked
anchors already license. This is never authority to add an image, a beat, a line of dialogue, or an
emotional claim the source does not contain. If the only way to improve a passage is to put something
in it that is not there, decline the improvement rather than invent one — the same discipline this file
already applies to a real-world reference neither of you can confirm.

You have a web_search tool available, separate from the advisor. When a real-world reference — a
band, song, media title, real person, place, event, brand, or a phrase that echoes something famous
— cannot be confirmed from project context or your own knowledge with confidence, search for it
directly rather than guessing, or asking the advisor to guess in your place; the advisor has no more
access to a real source than you do. Reserve the native or romanized form for what an actual search
confirms has no established English equivalent — not for whatever you didn't get around to
checking."""
