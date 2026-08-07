# LLM Translator — Localization Policy

> Ported from the main MTLS pipeline's Phase 1.55 rich metadata cache.
> The **authoritative version** is the `<LOCALIZATION_POLICY>` block in
> `src/prompt/prep_prompt_deepseek_en.xml` — DeepSeek V4 Pro reads it
> directly in the prep system prompt. This `.md` file is the human-readable
> reference. Keep them in sync.
> Source: `pipeline/pipeline/metadata_processor/rich_metadata_cache.py`

---

## 1. Romanization Policy (BINDING)

All Japanese names MUST use **standard Hepburn WITHOUT macrons**:

| With Macron | Without Macron | Example |
|---|---|---|
| ō | o | Otsuki, not Ōtsuki |
| ū | u | Shuichi, not Shūichi |
| Ō | O | Ohara, not Ōhara |
| ā | a | Obasan, not Obāsan |

Long vowels are represented by doubling the vowel only when phonetically
distinct (e.g., "Tojo" not "Tōjō", "Ryota" not "Ryōta"). This rule is
non-negotiable — the main MTLS pipeline enforces this at the context.xml
validation gate. Any name with macrons is a prep failure.

## 2. Name Order Convention

- **Contemporary Japan settings:** Family-Given order (e.g., Otsuki Haruto, Tojo Ayaka).
  Honorifics retained as suffixes (-san, -kun, -chan, -senpai, -sensei, -sama).
- **Fantasy/non-contemporary settings:** Given-Family order, natural English equivalents.
  Honorifics transcreated to context-appropriate English (Lord, Lady, Master, Sir).
- **Isekai with Japanese-origin cast:** Family-Given for JP characters, honorifics
  retained. Given-Family for non-JP characters, no JP suffixes.

## 3. Honorific Policies

### Contemporary Japan (retain)
| Suffix | Policy |
|---|---|
| -san | Retain as suffix (e.g., Saki-san). Do not omit or translate. |
| -chan | Retain to preserve intimacy nuance (e.g., Emma-chan). |
| -kun | Retain (e.g., Yuuta-kun). Do not flatten to first-name only. |
| -sama | Retain for elevated politeness/register. |
| -senpai | Retain. Do not translate to "senior." |
| -sensei | Retain. Do not translate to "teacher/professor." |
| Onii- (お兄) | Retain Onii-chan/Onii-san/Nii-san as-is. Western LN readers know this. |
| Onee- (お姉) | Retain Onee-chan/Onee-san/Nee-san as-is. Do not translate to "big sister." |

### Fantasy / Non-Contemporary (transcreate)
| Suffix | Policy |
|---|---|
| -san | Convert to context-appropriate English address (Mr./Ms./title). |
| -chan | Express closeness through tone/nickname. Do not retain. |
| -kun | Convert to natural English peer/junior address. |
| -senpai | Convert to senior role/title in English context. |
| -sensei | Convert to Master/Instructor/Teacher by world context. |
| Onii- (お兄) | Transcreate to "big brother" or natural sibling address. Never retain in fantasy. |
| Onee- (お姉) | Transcreate to "big sister" or natural sibling address. Never retain in fantasy. |

### Noble / Aristocratic (transcreate to titles)
| Suffix | Policy |
|---|---|
| -sama | Transcreate to noble address (My Lord, My Lady, Your Grace/Highness by rank). |
| -san | Transcreate to Lord/Lady or formal title based on status. |
| -kun | Transcreate to Young Lord/Young Master. |
| -chan | Transcreate to Lady/Miss or affectionate noble equivalent. |
| Onii- (お兄) | Transcreate to "Elder Brother" (formal) or "Lord Brother" (noble). |
| Onee- (お姉) | Transcreate to "Elder Sister" (formal) or "Lady Sister" (noble). |

### Isekai with Japanese Cast (hybrid)
| Suffix | Policy |
|---|---|
| -san/-chan/-kun/-sama/-senpai/-sensei | Retain for JP characters. |
| (fantasy NPCs) | Natural English address. No JP suffixes. |

## 4. Character Archetype Taxonomy

Use these LN-native labels, not generic fiction labels:

| Archetype | Description | Speech Traits |
|---|---|---|
| tsundere | Hostile/cold exterior, gradually reveals warmth | Abrupt, denial-heavy |
| kuudere | Consistently flat, emotionless exterior | Short declarative, minimal reaction |
| dandere | Shy/mute in public, warm in private | Hesitant, fragmented when anxious |
| yandere | Possessively devoted, unstable when threatened | Switches sweet↔intense |
| gyaru | Energetic, fashion-forward, socially confident | Casual, high-frequency slang |
| ojou-sama | Aristocratic, formal, often sheltered | Elevated diction, polite register |
| osananajimi | Childhood friend | Familiar, often nama-yobi (no-suffix) |
| imouto | Younger sister (real or surrogate) | Affectionate suffix use, nii/onii-chan |
| senpai | Upperclassman mentor figure | More measured, occasionally paternalistic |
| chuunibyou | Middle-school-syndrome | Theatrical, archaic/fantasy loanwords |
| bokukko | Female who uses 'boku' (masculine pronoun) | Tomboyish, direct |
| himekko | Princess-complex type | Demanding, theatrical, unexpectedly sweet |

## 5. Cultural Term Policy

### Always Retain (JP loanwords — Western LN readers know these)
- senpai, kouhai, yandere, tsundere, kuudere, gyaru, mob, isekai
- onigiri, yukata, kimono, tatami, futon, kotatsu
- -san, -kun, -chan, -sama, -senpai, -sensei (in contemporary Japan settings)

### Context-Dependent (translate or retain based on setting)
- 購買部 → School Store (contemporary) / Merchant Guild (fantasy)
- 部活 → Club (contemporary) / Guild (fantasy)
- 文化祭 → Cultural Festival (contemporary) / Harvest Festival (fantasy)

### Default Cultural Term Translations
| JP | EN |
|---|---|
| 球技大会 | ball game tournament |
| 内申 | internal school record |
| 特別推薦 | special recommendation admission |
| 屋上 | rooftop |
| 体育館 | gymnasium |
| 保健室 | nurse's office |
| 図書室 | library room |
| 職員室 | faculty room |
| 部室 | club room |
| 廊下 | hallway |
| 昇降口 | shoe-locker entrance |

## 6. Genre-Specific Conventions

### Romcom
- Playful tone, contraction awareness
- Honorifics retained (setting-dependent)
- School-life vocabulary: 告白 (confession), フラグ (flag), デレ (dere moment)
- Keep: gyaru, senpai, kouhai, yandere, 負けヒロイン (losing heroine)

### Fantasy / Isekai
- Epic register, no modern slang unless character-appropriate
- Honorifics transcreated (unless isekai-with-JP-cast)
- Magic terminology: consistent, grounded in world-building
- Name order: Given-Family for fantasy-origin characters

### Memoir / Autobiography
- Reflective, introspective tone
- Honorifics retained if JP setting
- First-person voice fidelity is paramount

## 7. Prohibited Patterns

- **No macrons** in romanized names (ō → o, ū → u)
- **No literal translation** of filler phrases (〜件について, 〜というわけで)
- **No "gonna/wanna"** for characters with formal registers
- **No gyaru slang** using deprecated vocabulary: slay, no cap, vibe, lit, lowkey, highkey, bet, bussin, rizz
- **No English descriptions substituted** for retained JP terms (e.g., don't write "upperclassman" for "senpai" in contemporary Japan settings)
- **No flattening** of distinct character voices — each character's contraction rate, register, and speech pattern must be preserved
