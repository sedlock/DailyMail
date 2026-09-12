# Phase 6 — mobile prose alignment, the Standing transition, calendar relevance

Companion to `docs/logical-families-and-sessions.md` (Phase 5), which remains
accurate. Three defects reached the reader in the 9 September 2026 digest; this
document is the account of each. Extra Editions are a fourth thread and have
their own document, `docs/extra-editions.md`.

---

## 1. Justified prose on a narrow viewport

### The defect

Announcement 6846,
`Student-Led Discussion of "Comparing Strategic and Systemic Periods of
Starvation" RCHGHR`, opens every paragraph of its stored `FullBody` with:

```html
<p style="margin-left:0in;text-align:justify;">
```

Full justification with no hyphenation engine stretches the inter-word spacing
of every line but the last. On a 390 px Outlook-mobile pane in dark reader mode
that opens rivers of whitespace down the column. The control, 6926
`September 11 Memorial Service — 25th Anniversary`, carries **no `style`
attribute at all** — it inherits the digest's own left alignment and read
correctly in the same client on the same morning.

The cause is source markup, nothing else: no wrapper alignment, no
`word-spacing`, no `text-align-last`, no `align="justify"`. Rowan's own editor
emitted `text-align:justify` per paragraph. 6694, the Provost's Town Hall, does
the same thing.

### The policy

The digest already owns normal body **colour** (Phase 5). It now owns normal body
**alignment** on the same terms, and for the same reason: `text-align` is inert,
so the security allowlist in `filter_style` correctly keeps it — but whether an
author's alignment is *legible in a 390 px card* is not a security question.

`sanitize.BodyAlignmentPolicy`, applied after `clean()` alongside the colour
policy:

* every prose block — `p`, `div`, `li`, `blockquote`, `h1`–`h6`, `dt`, `dd`,
  `pre`, `small` — is pinned to `text-align:left` **explicitly**, so an alignment
  set on a wrapper cannot leak into the blocks inside it;
* `justify` and arbitrary `right` are dropped from prose wherever they appear,
  in `style` or in the `align` attribute;
* `text-align-last`, `text-justify` and `word-spacing` are stripped from every
  element. None was ever in `ALLOWED_STYLE_PROPERTIES`, so `filter_style`
  already dropped them; this is the belt to those braces;
* the stored `FullBody` is untouched. This is a render derivative.

### What it deliberately does not touch

| Kept | Why |
|---|---|
| `td`, `th`, `table`, `tr`, `col`, `caption` | A right-aligned numeric column is the author saying something true about the data. |
| `figure`, `figcaption`, `img` | Image layout is not prose. |
| Compact centred blocks | A centred one-line call to action or a centred caption is a deliberate visual choice, and forcing it left looks broken. |

"Compact" is measured, not guessed: a centred block keeps its centring while its
own collapsed text is at most 200 characters — roughly two lines at the digest's
body size on the narrowest supported viewport. A centred block holding a whole
article is not compact and is treated as ordinary prose. A block that inherits
centring from a compact centred ancestor inherits its decision too, so a centred
banner does not come apart.

The pass is a buffered `HTMLParser` rather than a regex, because whether a
centred block stays centred depends on how much text it turns out to contain,
and a block that inherits depends on its ancestor's answer, which is later
still. Frames are resolved parent-first after parsing and each start tag is
patched in place. The result is idempotent.

---

## 2. Making the NEW → STANDING crossing obvious

The digest already labelled every card and already carried a
`STANDING — N continuing announcements` heading. On a long Outlook-mobile scroll
that was still easy to lose: the heading was a 7 px grey strip, and three screens
later nothing on the card in front of the reader said which half of the digest
they were in.

Two changes, because one was not enough.

**A one-time barrier**, at the crossing, built from the design's existing
vocabulary: 26 px of breathing room, the same 4 px gold rule the masthead uses, a
heavier label band at 14 px / 2 px tracking, and a caption band reading
*"Already sent to you before today. New information ends above."* The useful
section text is kept verbatim. No image, nothing above 14 px: a barrier, not a
second banner. Exactly one per digest, and none at all when there is nothing
standing.

**A persistent surface.** A Standing card sits on `#f4f2f1` against a New card's
`#ffffff`, with a correspondingly warmer rule. Only the surface moves. Body ink
stays `#1a1a1a` on both, the headline keeps its accent, and the NEW, STANDING and
UPDATED badges are untouched — so a continuing announcement reads as continuing,
never as disabled. Measured contrast of body ink on the Standing surface is
15.4:1, and the browser QA asserts ≥ 7:1 both normally and under the dark-mode
approximation.

The card follows `display_status`, not Rowan's own classification, so a logical
repeat shown as Standing gets the Standing surface.

---

## 3. Calendar relevance: what the scorer is allowed to read

### Two production results

Both from the deterministic fallback, because curation had timed out that
morning (§4).

```
6815  Hollybush Tour                       OFFERED  0.85
      deterministic relevance 0.85 (president, category:Glassboro Campus)

6783  Wellness Center Open House           WITHHELD 0.20
      deterministic relevance 0.20 (emergency, -free food)
```

`president` is in this sentence of 6815's body:

> the University's history through the legacy of its presidents, and the 1967
> summit between President Lyndon B. Johnson and Soviet Premier Alexei Kosygin

An ordinary building tour was promoted to a senior-leadership event by two
sentences about 1967. And 6783 — a broad student-service open house with a date,
an exact time and a room, covering Student Health, Counselling, AOD and EMS —
lost its button to `free food` in a list of what was on offer and `emergency`
from `Emergency Medical Services` in a list of who was attending.

The shape was the bug. The old scorer concatenated title, whole body, extracted
title and location into one haystack and searched it for every phrase it knew, so
a word anywhere counted as much as a word in the subject.

### The design

Relevance now reasons from what the event **is**, and evidence is graded by where
it sits, because where a phrase appears says how much it is claiming:

| Where | Weight | Rationale |
|---|---|---|
| Title, extracted event title, heading, location, category, audience | ×1.0 | In the title, the phrase *is* the event. |
| The announcement's own opening (first 260 characters) | ×0.5 | Its statement of purpose. Real evidence, not the headline. |
| The rest of the body | ×0.25, clamped to ±0.10 total | A detail. It may inform a decision; it may not make one. |

On top of that, the signals that say *who* an event belongs to — `president`,
`provost`, `town hall`, `chancellor`, `cabinet`, `board of trustees`,
`commencement` — are **focus-only** and are never read out of body prose at all.
That is the Hollybush defect closed at the root rather than damped.

Breadth is read from the audience Rowan published to (`Both` is the broadest
thing the source can say) plus explicit breadth language in the focus text. A
student-only audience is **no longer penalised for being student-facing**: a
parent-relevant student service is exactly what this reader wants, and narrowness
is expressed by the routine signals instead.

Three ceilings keep any one axis from running away: topics ≤ +0.40, routine
penalties ≥ −0.60, breadth ≤ +0.25, over a base of 0.30 against a 0.60 threshold.
The base still sits below the threshold, so an event has to earn its button
rather than merely fail to disqualify itself.

The stored `relevance_reason` now names where each signal came from —
`-purpose:free food` is a different claim from `-free food`, and the audit row
says which one it was.

### Measured against the whole production corpus

Replaying every deterministic decision ever recorded (73 distinct event
candidates):

* **2 decisions change, 0 regress.** Nothing previously offered is now withheld.
* `6783 Wellness Center Open House` 0.20 → 0.87, **offered**.
* `6926 September 11 Memorial Service — 25th Anniversary` 0.10 → 0.73,
  **offered**. A 25th-anniversary memorial explicitly inviting *"the entire
  Rowan University community"* is a major campus institutional event, and this
  is the class the reader asked to keep. Flagged as a deliberate change rather
  than a side effect.
* `6815 Hollybush Tour` 0.85 → 0.70, **still offered**, and the reason is now
  `broad audience, category:Glassboro Campus` — the word `president` appears
  nowhere in it.
* `6846 Student-Led Discussion` 0.50 → 0.00. Its `President Anna Cherian` counts
  for nothing.
* Every negative control holds: SUP general body meeting, Cape May beach day,
  Welcome Week promotions, RCHGHR book club, resume-review drop-in, ESL info
  session, alumni breakfast, and the Late Night @ The Rec lot closure.
* Every positive control holds: Provost's Town Hall 1.00, Provost's Coffee Hours
  0.90, Graduate Open House 0.95.

### What did not change

**Nothing about delivery.** The Outlook compose deep link and the RFC 5545 `.ics`
attachment are untouched, as are travel holds, multi-session sittings, titles,
venue resolution and the `calendar_recommendations` schema. This phase changes
*which* events are offered a control, never how that control works. The user
declined further complexity in calendar dissemination and that decision stands.

**No extra model call.** Relevance still rides on the ranking call that already
runs. `test_calendar_relevance_still_rides_on_the_single_curation_call` counts
invocations and requires exactly one.

**The two paths agree.** The curation system prompt gained the same reasoning:
judge from title, category, audience and purpose; broad student-service events
can matter; wording buried in the body is weak evidence; a historical mention of
a president does not make a tour a leadership event.

---

## 4. Why 9 September used deterministic ordering

Not a defect in curation, and not a mystery. The run log:

```
06:34:10 WARNING curation fell back to deterministic ordering:
         TimeoutExpired: Command '[...claude, --print, --model, sonnet, ...]'
         curation: fallback model=- 240.07s
```

The Claude CLI subprocess hit `curation.timeout_seconds = 240` and was killed.
Not authentication, not quota, not invalid structured output, not permutation
validation — the fallback then did exactly what it is for, and the complete
digest went out on time with all 46 announcements.

**But the budget was wrong.** Successful curation runs across the production
history:

```
15.9  22.8  32.7  33.7  47.6  55.9  67.2  77.4  79.8  82.3  87.1
92.4  136.0  138.6  139.8  143.1  158.5  163.0  211.3  222.8  237.0
```

The maximum successful run is **237.0 s against a 240 s wall** — three seconds of
headroom, on a distribution with a long right tail. 9 September was not an
anomaly; it was the tail arriving. The run killed a call that was working and
the reader lost the curation they were entitled to.

Fixed by raising the default to **420 s**, which covers the observed maximum with
77% headroom. The whole run stays bounded by the unit's `TimeoutStartSec=1800`,
and typical wall clock is unchanged because the timeout is a ceiling, not a wait.
Production `config.toml` was updated to match after a verified backup.

---

## 5. Browser QA

`tools/qa/` gains four fixture cases — the justified regression, its September 11
control, the Wellness and Hollybush calendar callouts — plus a parking callout,
a real Standing card and an UPDATED Standing card, all sanitized committed
production markup with every address removed.

The suite grew from 135 to **477 asserted invariants** across 390×844, 430×932
and 1280×900, plus the dark-mode approximation. New assertions:

* computed `text-align` on every prose block, per card, per viewport;
* no `justify` and no `word-spacing` survives anywhere;
* the regression case now computes identically to its own control;
* metadata prose is left-aligned;
* exactly one transition barrier, spanning the reading column, at least 40 px
  tall, built from ≥ 3 distinct bands, carrying the gold accent;
* New and Standing surfaces are measurably different and each internally
  consistent; Standing keeps New's ink at ≥ 7:1 contrast;
* NEW, STANDING and UPDATED badges remain present and legible;
* the parking callout keeps its own surface, its link colour and its width;
* under the dark-mode approximation: surfaces still differ, ink still matches,
  contrast still holds, bands still distinct, prose still left.

Two tests require the suite to **reject** a page built with a policy switched
off — one for colour, one for alignment. A regression check that cannot fail is
not a regression check.

This is regression QA. Chromium with a CSS inversion filter is not Outlook iOS
dark mode and nothing here claims otherwise; what it checks is that the
properties which broke are still holding.

---

## 6. Tests

1,092 hermetic tests, up from 991. New files:

* `tests/test_render_alignment_policy.py` — 36 tests
* `tests/test_standing_section.py` — 21 tests
* `tests/test_calendar_relevance.py` — 27 tests
* `tests/test_extra_edition.py` — 17 tests

`tests/hermetic_boundary.py` is unchanged. The normal run still reports 0
external connections, 0 SMTP attempts, 0 production credential reads and 0
production state accesses.
