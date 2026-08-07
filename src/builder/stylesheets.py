"""
Stylesheets for EPUB output, selected per device profile.

DEFAULT_CSS lives in agent.py and targets ordinary 300 PPI readers. This module
holds EINK_CSS, which targets CrossPoint firmware on the XTEINK X3/X4 and looks
suspiciously short. It is short on purpose.

CrossPoint's CSS engine implements exactly nine properties:

    block   margin, padding, text-indent, text-align, direction
    inline  font-weight, font-style, text-decoration, vertical-align

Everything else is parsed and silently discarded. Selector support is a four
level priority list -- inline style attribute, then class, then tag, then
built-in default -- which is not a cascade. There are no descendant, child,
attribute or namespaced selectors, and no !important to lean on.

So the rules DEFAULT_CSS relies on to do its job do not survive the trip:

    section > div > p:first-of-type    child + pseudo-class, never matches
    p[class="section-break"]           attribute selector, never matches
    section[epub|type~="toc"] ...      namespaced attribute, never matches

That last pair is why scene breaks are unstyled on this hardware today -- the
rule is guarded by an attribute selector and !important, and the device
understands neither. Here it is a bare class, which is a selector form the
engine actually implements.

Two deliberate omissions, both because the firmware treats them as USER
settings that key its layout cache -- overriding them from CSS fights the
reader's own choice and forces every cached section to be laid out again:

  - no font-family / font-size / line-height
  - no text-align on body or p (alignment is a device setting; we only set it
    on elements where centring is semantic, like headings and scene breaks)

And no geometry at all -- max-width, height, object-fit, page-break-*, @media.
Images are sized by the engine as scale = min(maxW/w, maxH/h, 1.0), so CSS has
no say and every byte spent telling it otherwise is parse time on a 160 MHz
core. Complex stylesheets are a documented OOM source on this firmware; the
cheapest rule is the one we never ship.
"""

EINK_CSS = '''/* CrossPoint / XTEINK stylesheet - nine supported properties only */
@charset "UTF-8";

/* Body text. No text-align: the device owns alignment. */
p {
  text-indent: 1em;
  margin: 0;
}

.noindent {
  text-indent: 0;
}

.centerp {
  text-align: center;
  text-indent: 0;
}

/* Bare class selector, not p[class="..."] - this is the form that matches. */
.section-break {
  text-align: center;
  text-indent: 0;
  margin: 1em 0;
}

.blockquote {
  text-indent: 0;
  margin: 0.35em 1em;
}

.lyric {
  text-indent: 0;
  text-align: left;
  margin: 0.05em 0;
}

.lyric-break {
  text-indent: 0;
  margin: 0.35em 0;
}

/* Headings. Centring is semantic here, not a typographic preference. */
h1 {
  text-align: center;
  text-indent: 0;
  margin: 1.2em 0 0.8em 0;
}

h2 {
  text-align: center;
  text-indent: 0;
  margin: 1em 0 0.5em 0;
}

/* Image wrappers. Sizing is the engine's job; we only centre the box. */
.cover-image {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.illustration {
  text-align: center;
  text-indent: 0;
  margin: 0.5em 0;
}

.kuchie-image {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.image_full {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.horizontal-kuchie {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

/* Emphasis */
em {
  font-style: italic;
}

strong {
  font-weight: bold;
}

/* Footnotes. The firmware picks up internal links as footnotes on its own;
   these rules only keep the markers from looking like body text. */
a {
  text-decoration: none;
}

.noteref {
  vertical-align: super;
  text-decoration: none;
}

.footnote-backref {
  text-decoration: none;
}

.footnotes {
  margin: 1.2em 0 0 0;
}

/* Table of contents */
.toc-list {
  text-indent: 0;
  margin: 0;
}
'''


# Properties CrossPoint's CSS engine actually implements. device_budgets.py
# uses these to audit whatever stylesheet we ship, so a future well-meaning
# addition gets caught at build time instead of on the device.
EINK_SUPPORTED_BLOCK_PROPERTIES = frozenset({
    "margin",
    "padding",
    "text-indent",
    "text-align",
    "direction",
})

EINK_SUPPORTED_INLINE_PROPERTIES = frozenset({
    "font-weight",
    "font-style",
    "text-decoration",
    "vertical-align",
})

EINK_SUPPORTED_PROPERTIES = (
    EINK_SUPPORTED_BLOCK_PROPERTIES | EINK_SUPPORTED_INLINE_PROPERTIES
)
