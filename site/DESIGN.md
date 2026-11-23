# Duckish Site Design Brief

This is the rulebook for the Duckish about page. Read this before touching any file in `site/`. Every design decision must trace back to the anchors below. If a change cannot be justified by this document, do not make it. Update this document first if the direction changes.

## Anchors

**Emotional anchors:** playful and defiant.

**Mental image:** golden afternoon light on a pond. A duck sits on a floating chessboard. Unbothered. Slightly smug. It does not care that you are rated higher.

**What this is:** a character with a point of view, not a spec sheet or a landing funnel.

## Goals

- Feel like a creature with an attitude, not a brochure about an engine
- Prove the attitude with something Duckish actually did, not adjectives
- Keep the challenge flow working and honest

## Non-goals

- A dashboard, a rating chart, a spec table as the lead
- Explaining NNUE, threads, or hash sizes before the personality lands
- Any phrase beginning with Empower, Unlock, Transform, Seamless

## Anti-goals (would make this a failure even if it works)

- Generic template structure centered on a wordmark and a paragraph
- Purple, indigo, blue, gradients, glows, blurred blobs, purple-adjacent accents
- Inter, Roboto, Space Grotesk, or a system font as the primary face
- Three-card grids, hero CTA, FAQ accordion, testimonials, logo walls
- Emoji used as icons, clip-art rubber ducks, yellow everything, quack jokes
- Box-shadows, layout-shifting hover, motion that loops fast or never rests

## Design language

Editorial typography with a playful undercurrent. Flat. Hairline borders. Generous space. One accent. Motion is slow, low-amplitude, and optional.

### Tokens (locked)

```
--bg: #14110b
--surface: #201b12
--raised: #2b2418
--border: #4a4030
--text: #ebe2d0
--text-dim: #b3a68d
--heading: #fff8e7
--accent: #f0c24e
--accent-hover: #f7d577
--bill: #e2913f
--water: #6aa89b
--dot-offline: #6f6858
```

Type: Fraunces for display, Newsreader for body, IBM Plex Mono for labels and metadata. These are frozen. Do not swap fonts without updating this brief.

### The duck

The mascot is a single elegant line silhouette with three tones: body in `currentColor` (accent yellow), bill in `--bill`, waterline in `--water`. It is decorative and `aria-hidden`. It bobs slowly on animejs, tilts on hover via motion, and never becomes a cartoon. One duck only.

## Page structure (in order)

1. **Masthead** — wordmark + duck, tagline `chess engine / duck on lichess`, status row. Small.
2. **Statement** — the defiant line in large Fraunces. This is the hero, not the wordmark. Supporting subline in mono or body. Suggested copy: `Duckish does not beg for draws.` Followed by one quiet line about what it does instead. Keep it short and in voice.
3. **Proof** — one real game. The final position of `9dYMwyZN` (Duckish as White, `Bh1#` on move 106 vs eigenmann). Small static mini-board, accurate pieces, no interactivity needed. Caption with the facts: 106 moves, draw was on the table, mate instead. This is proof of life and must be a real game, never decorative chess art.
4. **Bio** — four paragraphs. Frozen verbatim. Do not edit, re-punctuate, or reorder.
5. **Facts** — compact `<dl>` definition list. Rating, openings, engine. No cards.
6. **Challenge** — form with exact Lichess `#friend` params, defaults casual / 10 / 0 / white / standard, live URL preview. Honest notes about rated needing sign-in and standard-only.
7. **Footer** — Lichess + GitHub links. No colophon.

## Behavioral rules (frozen)

- The bot never offers a draw. This site must never imply otherwise.
- Bio paragraphs are byte-for-byte identical to the approved text.
- Status text is always present; color is never the only indicator.
- Reduced motion disables all JS and CSS animation. Page works with zero JS animation.
- Vendor bundles are local in `site/vendor/`. No CDN dependency at runtime.
- `node_modules` is ignored. Vendor files are committed.
- No outgoing draw offers, no changes to bot logic from site work.

## Motion

animejs: duck idle bob, slow and low amplitude. motion: duck hover spring, button press spring, status dot pulse, preview fade. All optional and gated behind `prefers-reduced-motion`. Transform and opacity only. No layout shift.

## Agent rules

1. Read this file first.
2. Reuse the tokens and components defined here. Do not invent new ones.
3. If a change needs a new color, font, or section, update this brief first and get approval.
4. Verify in a real browser at 375 / 768 / 1024 / 1440 before claiming done.
5. Kill any local server you start.