# TrayTrim design rules

Read this before touching any UI. Every UI change is reviewed against it.

## Direction

- Our own neutral look, the same on Windows, macOS and Linux. No imitation of a single OS,
  and no imitation of Sims tools (no plumbob green).
- Follows the system light/dark setting. Neither theme is "the real one"; both get equal care.
- UI text is English.
- One window: tray gallery on the left (thumbnails, search), the selected household or Sim on the
  right.
- Each screen has one thing that matters most. On the household view that is the size saving
  ("5.1 GB with whole packages, 38 MB with TrayTrim"). Everything else is visibly quieter.
- The imagery is the user's own content: tray portraits and CAS thumbnails. No illustrations,
  no decorative graphics.

## Tokens

All colours, sizes and fonts come from `src/traytrim/gui/theme.py`. Never hard-code a colour in a
widget.

- **Palette "Ink": colour has meaning or it's neutral.** The accent is ink (near-black in light,
  near-white in dark) so the user's CC images carry the colour. Green was dropped on purpose: it
  made the app look like a Sims mod manager.
  - `accent` (ink): the primary action, selection, focus.
  - `success_text` (green): "verified", nothing else.
  - `warning` (amber): conflicts and approximate results.
  - `danger` (red): missing resources and failures.
  - Everything else is neutral greys tuned for contrast.
- **Contrast:** body text at least 4.5:1, icons, borders of inputs and focus rings at least 3:1, in
  both themes.
- **Type:** IBM Plex Sans for UI, IBM Plex Mono for resource keys and file sizes in tables. Sizes
  come from the type scale in `theme.py`; weights Regular, Medium, SemiBold only.
- **Radius:** small scale (controls 4, cards 8, window panels 0). Not one value everywhere.
- **Spacing:** 4-pt grid. Spacing shows grouping: tighter inside a group than between groups.

## Icons

- No emoji anywhere. No Unicode symbols used as icons (warning signs, check marks, arrows, stars).
- Only our own SVG icons in `src/traytrim/gui/assets/icons/`: 20-px grid, one stroke width, same
  caps and joins, coloured from theme tokens at runtime.
- No stock icon sets (Lucide, Phosphor, Material, ...). No sparkle icon.
- Icons support labels, they don't replace them, except for universally obvious controls
  (close, search) which still get a tooltip and an accessible name.
- New icons are shown on an icon sheet in the pull request before they go into the app.

## Banned

Visual
- Gradients of any kind used as decoration, gradient text, purple/indigo accents.
- Glass / frosted blur, coloured glows, large soft shadows.
- A coloured stripe on the left or top edge of a card.
- Icons inside tinted rounded squares.
- Cards inside cards. A single line of text inside a bordered box.
- Rows of equal-weight stat cards.
- ALL-CAPS letter-spaced labels. Decorative numbering (01 / 02 / 03).
- "A · B · C" meta strings. Write a sentence or use separate labels.
- Fake window controls, fake device frames.
- Default "AI fonts": Inter, Space Grotesk, Geist, Instrument Serif, Fraunces.

Motion
- Only functional motion: progress, expanding and collapsing, moving between states. About
  150 ms, ease-out. No bounce, no count-up numbers, no fade-in on everything.
- Respect the OS "reduce motion" setting.
- Hover and press feedback only on things that do something when clicked.

Copy
- Buttons say what they do: "Export 2 Sims", "Re-run check", "Choose folder". No bare "OK",
  "Submit", "Get started".
- Errors say what happened and what to do: "Couldn't read Foo.package: the file is locked by the
  game. Close The Sims 4 and try again." Never "Something went wrong" or "Oops".
- Empty states say why it's empty and what to do next.
- No marketing words (seamless, powerful, effortless, elevate), no exclamation marks, no em
  dashes, no lists of three for rhythm, no arrows glued onto button labels.
- Don't over-explain. If a label needs a paragraph, the control is wrong.

## States every view needs

Empty, loading, slow (still working after a few seconds: say what is happening), partial result,
error, and the normal state. Approximate results are marked as approximate.

## Real-data checks before a UI change is merged

- Longest Sim, household and package names in your own Tray and Mods folders.
- A household with 8 Sims, a Sim with hundreds of CC items, a 1.7 GB merged package.
- Display scaling at 100 %, 150 % and 200 %; the smallest window size we allow.
- Keyboard only: every action reachable, focus always visible.
- Both themes.
