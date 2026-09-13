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

You have a web_search tool available, separate from the advisor. When a real-world reference — a
band, song, media title, real person, place, event, brand, or a phrase that echoes something famous
— cannot be confirmed from project context or your own knowledge with confidence, search for it
directly rather than guessing, or asking the advisor to guess in your place; the advisor has no more
access to a real source than you do. Reserve the native or romanized form for what an actual search
confirms has no established English equivalent — not for whatever you didn't get around to
checking."""
