---
version: alpha
name: Helo-BusKL-where-bus-derived-design
description: A design system re-implementing the visual language of ../where-bus (Next.js + Tailwind v4 + Geist), a live RapidKL/MRT-Feeder tracker — floating pill controls over a full-bleed surface, category colour-coding (RapidKL maroon vs MRT Feeder slate), soft rounded-2xl card stacks for live data, and a bottom-sheet-on-mobile / left-sidebar-on-desktop panel that is the system's one structural signature.

colors:
  background: "#FFFFFF"
  foreground: "#2B2926"
  surface: "#F9FAFB"
  surface-card: "#F9FAFB"
  surface-card-hover: "#F3F4F6"
  border-subtle: "#F3F4F6"
  border-default: "#E5E7EB"
  text-primary: "#111827"
  text-secondary: "#6B7280"
  text-muted: "#9CA3AF"
  route-rapidkl: "#880808"
  route-feeder: "#5e6673"
  danger-bg: "#FEF2F2"
  danger-border: "#FEE2E2"
  danger-text: "#DC2626"
  cream: "#F5EDE3"
  terracotta: "#C2805F"
  blush: "#EDD5BE"
  dark-bg-base: "#0D0F10"
  dark-bg-surface: "#141718"
  dark-bg-elevated: "#1C1F21"
  dark-bg-active: "#212527"
  dark-border-subtle: "rgba(255,255,255,0.06)"
  dark-border-card: "rgba(255,255,255,0.07)"
  dark-text-primary: "#F0F2F3"
  dark-text-secondary: "#8A9199"
  dark-text-muted: "#4A5158"
  dark-route-rapidkl: "#9CAF88"
  dark-route-feeder: "#F0F2F3"
  dark-accent-teal: "#4A8B8B"

typography:
  title-lg:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 20px
    fontWeight: 700
    lineHeight: 28px
  title-md:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 16px
    fontWeight: 700
    lineHeight: 22px
  body-md:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 14px
    fontWeight: 500
    lineHeight: 20px
  body-md-regular:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 14px
    fontWeight: 400
    lineHeight: 20px
  caption:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 12px
    fontWeight: 400
    lineHeight: 16px
  eyebrow:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 12px
    fontWeight: 700
    lineHeight: 16px
  eta-number:
    fontFamily: Geist, system-ui, sans-serif
    fontSize: 16px
    fontWeight: 700
    lineHeight: 20px
  mono:
    fontFamily: Geist Mono, ui-monospace, monospace
    fontSize: 12px
    fontWeight: 400
    lineHeight: 18px

rounded:
  none: 0px
  md: 8px
  lg: 12px
  xl: 16px
  2xl: 24px
  pill: 9999px

spacing:
  xxs: 4px
  xs: 6px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 20px
  2xl: 24px

shadow:
  sm: "0 1px 2px rgba(0,0,0,0.05)"
  md: "0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1)"
  xl: "0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1)"
  2xl: "0 25px 50px -12px rgba(0,0,0,0.25)"
  sheet-mobile: "0 -4px 20px rgba(0,0,0,0.1)"
  sheet-desktop: "4px 0 20px rgba(0,0,0,0.1)"

components:
  search-pill:
    backgroundColor: "{colors.background}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.pill}"
    padding: "{spacing.sm} {spacing.lg}"
    shadow: "{shadow.md}"
    border: "{colors.border-default}"
  icon-pill-button:
    backgroundColor: "{colors.background}"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.pill}"
    padding: "{spacing.sm}"
    shadow: "{shadow.md}"
    border: "{colors.border-default}"
  icon-chip:
    backgroundColor: "{colors.surface-card-hover}"
    rounded: "{rounded.pill}"
    padding: "{spacing.xs} {spacing.sm}"
  result-card:
    backgroundColor: "rgba(255,255,255,0.7)"
    textColor: "{colors.foreground}"
    rounded: "{rounded.xl}"
    padding: "{spacing.lg}"
    border: "{colors.border-subtle}"
    shadow: "{shadow.sm}"
  eta-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.xl}"
    padding: "{spacing.md}"
    border: "{colors.border-default}"
  route-badge:
    rounded: "{rounded.pill}"
    padding: "2px {spacing.sm}"
    typography: "{typography.caption}"
  direction-pill:
    rounded: "{rounded.pill}"
    padding: "2px {spacing.sm}"
    typography: "{typography.caption}"
  section-eyebrow:
    textColor: "{colors.text-muted}"
    typography: "{typography.eyebrow}"
  panel-overlay:
    backgroundColor: "rgba(255,255,255,0.9)"
    rounded: "{rounded.2xl}"
    padding: "{spacing.xl}"
    shadow: "{shadow.2xl}"
    border: "{colors.border-default}"
  sheet-mobile:
    backgroundColor: "{colors.background}"
    rounded: "{rounded.2xl} {rounded.2xl} 0 0"
    shadow: "{shadow.sheet-mobile}"
  sheet-desktop:
    backgroundColor: "{colors.background}"
    rounded: "{rounded.none}"
    shadow: "{shadow.sheet-desktop}"
  error-banner:
    backgroundColor: "{colors.danger-bg}"
    textColor: "{colors.danger-text}"
    border: "{colors.danger-border}"
    rounded: "{rounded.xl}"
    padding: "{spacing.lg}"

---

## Overview

where-bus is a live transit tracker for Kuala Lumpur's RapidKL bus network and MRT Feeder shuttles — a full-bleed map with floating controls, not a marketing page. Its whole visual grammar exists to keep live, changing data (bus positions, ETAs, route lists) legible while the map stays the star: controls float as white pills over the map, data surfaces as soft rounded-2xl card stacks, and the one thing every screen agrees on is that **RapidKL and MRT Feeder are two different colours, always** — maroon `{colors.route-rapidkl}` `#880808` for RapidKL buses, slate `{colors.route-feeder}` `#5e6673` for MRT Feeder shuttles, on every icon, route chip, and marker in the system.

The base palette is a warm off-white/charcoal pairing (`{colors.background}` `#FFFFFF` / `{colors.foreground}` `#2B2926`) with a declared but sparingly-used warm accent set (cream, terracotta, blush) sitting alongside it — in practice, almost every surface in the shipped UI reaches for plain neutral grays (`gray-50` through `gray-900`) rather than the terracotta/blush pair, so neutral gray is the working "surface and text" palette and the warm tones are closer to a reserved accent than a load-bearing one. Muted text has a second technique worth naming: several captions set the *foreground* colour at reduced opacity (`text-[#2B2926]/50`) instead of switching to a separate gray token — the same ink, quieter, rather than a different ink.

Dark mode is a full second palette, not just an inverted one: near-black surfaces (`{colors.dark-bg-base}` → `{colors.dark-bg-active}`, each a shade lighter, for base/surface/card/hover/active elevation) and — distinctively — RapidKL's icon colour flips from maroon to sage green (`{colors.dark-route-rapidkl}` `#9CAF88`) while MRT Feeder flips from slate to near-white (`{colors.dark-route-feeder}` `#F0F2F3`), because the light-mode maroon reads as an alert colour on black. The category-colour *contract* — two categories, two consistent colours — holds in both themes; the actual hex values don't.

**Key characteristics:**
- Two category colours and nothing else: RapidKL maroon (light) / sage (dark), MRT Feeder slate (light) / near-white (dark). No other status or brand colour competes with them — red/orange/yellow are reserved for genuine errors (`{colors.danger-*}`), never for a route.
- Every floating control is a pill (`{rounded.pill}`): the search bar, the dark-mode toggle, the cancel button, the drag handle, every direction/route badge.
- Every data surface is `{rounded.xl}` (16px): ETA rows, route cards, search-result rows. Larger *containers* of those surfaces — the search overlay panel, the bottom sheet's top corners — step up one size to `{rounded.2xl}` (24px). Nothing in the system uses a small rounded-md/lg corner as a primary shape; small radii only appear on nested inner elements (icon chips, small pills already covered by `{rounded.pill}`).
- One true structural signature: the same panel is a **bottom sheet on mobile** (slides up from the bottom, rounded top corners, drag handle, swipe-to-dismiss) and a **fixed left sidebar on desktop** (slides in from the left, square corners, full height, no dismiss gesture) — one component, two anchor points, switched at the `md` (768px) breakpoint.
- Cards lift on hover/press: `shadow-sm` at rest, `shadow-xl` + `scale-[1.01]` on hover, `scale-[0.99]` on active — a tactile press response on every clickable result row.
- Section labels are small, bold, uppercase, tracked, and muted (`{typography.eyebrow}`) — "ROUTES", "STOPS", a direction group name — never a bordered tab or a colour block.

## Colors

### Route / Category
- **RapidKL** (`{colors.route-rapidkl}` `#880808` light / `{colors.dark-route-rapidkl}` `#9CAF88` dark): every RapidKL bus icon, route badge, and marker. The system's only "loud" colour in light mode.
- **MRT Feeder** (`{colors.route-feeder}` `#5e6673` light / `{colors.dark-route-feeder}` `#F0F2F3` dark): every feeder shuttle icon, badge, and marker. Quiet and desaturated in both themes — feeder service is the secondary network.
- **Teal** (`{colors.dark-accent-teal}` `#4A8B8B`, dark mode only): the "inbound" direction pill when it needs to read differently from the sage "outbound" — the one place a third hue appears, and only in dark mode, only as a low-opacity tint.

### Surface (light / default)
- **Background** (`{colors.background}` `#FFFFFF`): the map canvas and page base.
- **Surface** (`{colors.surface}` `#F9FAFB`, Tailwind gray-50): card and row fill — ETA rows, route-card headers, empty states.
- **Surface hover** (`{colors.surface-card-hover}` `#F3F4F6`, gray-100): hover fill for cards and icon chips.
- **Border subtle** (`{colors.border-subtle}` `#F3F4F6`) / **border default** (`{colors.border-default}` `#E5E7EB`): hairline dividers throughout — never more than 1px, never a heavy stroke.

### Surface (dark)
- **Base → Active** (`{colors.dark-bg-base}` `#0D0F10` → `{colors.dark-bg-surface}` `#141718` → `{colors.dark-bg-elevated}` `#1C1F21` → `{colors.dark-bg-active}` `#212527`): four steps of elevation, each a little lighter — map/page, sidebar/sheet, card, hover/pressed.
- **Border subtle/card** (`rgba(255,255,255,0.06–0.07)`): hairlines, barely visible — dark mode leans on elevation steps for separation, not borders.

### Text
- **Primary** (`{colors.text-primary}` `#111827` light / `{colors.dark-text-primary}` `#F0F2F3` dark): titles, primary labels, the ETA number itself.
- **Secondary** (`{colors.text-secondary}` `#6B7280` light / `{colors.dark-text-secondary}` `#8A9199` dark): captions, "stops away", route long names.
- **Muted** (`{colors.text-muted}` `#9CA3AF` light / `{colors.dark-text-muted}` `#4A5158` dark): placeholders, section eyebrows, empty-state copy. Light mode sometimes reaches for `foreground` at 50% opacity instead of this token (see Overview) — both read the same, pick whichever the surrounding markup already uses.

### Semantic
- **Danger** (`{colors.danger-bg}` `#FEF2F2` / `{colors.danger-border}` `#FEE2E2` / `{colors.danger-text}` `#DC2626`): the *only* other colour role in the system, reserved for a genuine failure ("Failed to load ETAs", "Failed to load stops"). Never used for a route, a badge, or a warning that isn't an actual error.
- **Reserved, underused**: `{colors.cream}` `#F5EDE3`, `{colors.terracotta}` `#C2805F`, `{colors.blush}` `#EDD5BE` are declared as CSS variables but rarely reached for over plain gray in the components that ship — documented for completeness, not a licence to sprinkle them in.

## Typography

### Font Family
**Geist** for everything — one face, no separate display/text split the way a marketing system needs. Weights 400 (regular captions), 500 (body/labels), 700 (titles, eyebrows, the ETA number) cover the whole system. **Geist Mono** ships alongside it but isn't visibly used in any component read for this system — available, not load-bearing.

### Hierarchy

| Token | Size | Weight | Use |
|---|---|---|---|
| `{typography.title-lg}` | 20px | 700 | Stop/route panel title ("KL Sentral", route number + name). |
| `{typography.title-md}` | 16px | 700 | Search-result row title, route-card header name. |
| `{typography.body-md}` | 14px | 500 | Stop-row labels, selected-state emphasis. |
| `{typography.body-md-regular}` | 14px | 400 | Default body copy. |
| `{typography.eta-number}` | 16px | 700 | The live ETA readout — always the heaviest, right-aligned number in its row. |
| `{typography.caption}` | 12px | 400 | "Stop ID: …", route long names, direction captions. |
| `{typography.eyebrow}` | 12px | 700 | Section labels — uppercase, tracked wide. |

### Principles
- **One face, weight does the work.** No secondary display font; hierarchy comes from size + the 400/500/700 weight steps, not a typeface switch.
- **The ETA number is always the loudest thing in its row** — bold, primary text colour, right-aligned, nothing competes with it for attention inside a card.
- **Eyebrows are the only uppercase text in the system.** Titles and body copy are always sentence case.

### Note on Font Substitutes
Geist is open-source (SIL license) and free to use directly — no substitution needed. Load via a Google Fonts / self-hosted `@font-face` equivalent (`Geist` isn't on Google Fonts; use the official `geist` npm/CDN distribution or fall back to `system-ui` if unavailable, which is close in x-height and neutrality).

## Layout

### Spacing System
- **Base unit**: 4px. Tokens: `{spacing.xxs}` 4px · `{spacing.xs}` 6px · `{spacing.sm}` 8px · `{spacing.md}` 12px · `{spacing.lg}` 16px · `{spacing.xl}` 20px · `{spacing.2xl}` 24px.
- **Card padding**: `{spacing.lg}` 16px is the standard card/row inset (`p-4`); the search overlay panel steps up to `{spacing.xl}` 20px (`p-5`).
- **List rhythm**: `{spacing.sm}`–`{spacing.md}` (8–12px) between stacked rows (`space-y-2`/`space-y-3`); `{spacing.2xl}` 24px between distinct sections (routes vs. stops).
- **Icon-to-label gap**: `{spacing.sm}`–`{spacing.md}` (a `mr-2`/`mr-3`/`mr-4` icon chip sits left of every title).

### Grid & Container
- No marketing grid — this is an app shell. Content width is capped only where it floats over the map: the search bar and its results panel cap at `max-w-md` (~448px), centred, with `{spacing.md}`–`{spacing.lg}` side gutters.
- The sidebar/sheet panel is full-width on mobile, a fixed 400px column on desktop.

### Whitespace Philosophy
Cards touch their neighbours closely (8–12px) inside one list, but a full `{spacing.2xl}` 24px separates one semantic group from the next (a route's stop list vs. its own header, "Routes" results vs. "Stops" results). The map itself carries no internal whitespace rules — it's the one element in the system that's allowed to be edge-to-edge with nothing floating "in" it, only "over" it.

### Responsive Strategy

#### Breakpoints
| Name | Width | Key change |
|---|---|---|
| Mobile | < 768px | Panel is a bottom sheet: slides up, rounded top corners, drag handle, swipe down (>100px) to dismiss. |
| Desktop | ≥ 768px | Panel is a fixed left sidebar: slides in from the left, square corners, full viewport height, no drag/dismiss gesture — a visible close (×) button instead. |

#### Touch Targets
Icon-pill buttons (dark-mode toggle, cancel, close) run ~40–44px including padding — comfortable for a thumb over a map. The drag handle itself is a generous 48×6px bar with a large invisible touch-catch area around it (`pt-4 pb-2` on the whole handle row), not just the visible pill.

#### Collapsing Strategy
- **Search**: same floating pill at every width; only the results panel width caps out at `max-w-md`.
- **Panel (the signature)**: see Breakpoints above — this is the one component that doesn't just reflow, it changes anchor edge and animation axis (`y` on mobile, `x` on desktop) entirely.
- **Route/stop cards**: identical markup at every width; they simply have more room to breathe inside the wider desktop sidebar.

#### Marker / Icon Behavior
- **Route & stop icons**: `lucide-react` line icons (Bus, MapPin, RouteIcon, Search, X, Moon/Sun), always inside a `{rounded.pill}` chip, coloured by category where the icon *is* the category signal (route/stop markers), neutral gray where it's chrome (search icon, close button).
- **No photography or illustration system** — this app has none, live data and the map itself carry all the visual interest.

## Elevation & Depth

| Level | Treatment | Use |
|---|---|---|
| Flat | No shadow | Default row/card background — rely on the gray-50-on-white contrast, not a shadow, for separation. |
| `{shadow.sm}` | Subtle | Resting state of interactive result cards. |
| `{shadow.md}` | Floating chrome | The search pill and its icon-pill siblings, floating over the map. |
| `{shadow.xl}` | Hover lift | Interactive cards on hover, paired with `scale-[1.01]`. |
| `{shadow.2xl}` | Overlay | The full-screen blurred search-results panel. |
| `{shadow.sheet-mobile}` / `{shadow.sheet-desktop}` | Directional | The sheet/sidebar's one hand-tuned shadow — cast upward off the top edge on mobile, cast sideways off the right edge on desktop, matching whichever edge it's anchored to. |

### Decorative Depth
- **Press feedback as depth**: `scale-[1.01]` on hover, `scale-[0.99]` on active — the card physically responds to touch, which reads as "depth" more than any shadow does.
- **Blur as a scrim, not a card effect**: the full-screen search overlay uses `backdrop-blur-md`/`-2xl` over a darkened map, not a drop shadow, to separate the overlay from the content beneath it.

## Shapes

### Border Radius Scale
| Token | Value | Use |
|---|---|---|
| `{rounded.none}` | 0px | Desktop sidebar (deliberately square against the map edge). |
| `{rounded.md}` | 8px | Not used as a primary shape — reserved, avoid introducing it as a new "medium" card radius. |
| `{rounded.lg}` | 12px | Not used as a primary shape either; same note. |
| `{rounded.xl}` | 16px | **Canonical data-surface radius** — every ETA row, route card, search-result row. |
| `{rounded.2xl}` | 24px | **Canonical container radius** — the search overlay panel, the mobile sheet's top corners. |
| `{rounded.pill}` | 9999px | **Canonical control radius** — search bar, icon buttons, all badges, the drag handle. |

### Icon & Marker Geometry
- Icons sit inside a round chip (`{rounded.pill}`), never a squared or rounded-square container.
- A route/stop's category colour is carried by the *icon glyph colour* inside a neutral chip, not by the chip's background — the chip is always the same neutral gray at every level; only the glyph changes hue.

## Components

### Controls
**`search-pill`** — the persistent search input.
- Background `{colors.background}`, text `{colors.text-primary}`, `{typography.body-md}`, rounded `{rounded.pill}`, padding `{spacing.sm} {spacing.lg}`, `{shadow.md}`, 1px `{colors.border-default}`.

**`icon-pill-button`** — dark-mode toggle, cancel, and similar single-purpose floating buttons.
- Background `{colors.background}`, text `{colors.text-secondary}`, rounded `{rounded.pill}`, padding `{spacing.sm}`, `{shadow.md}`, 1px `{colors.border-default}`. Hover fills `{colors.surface-card-hover}`.

**`icon-chip`** — the round neutral container that every category/action icon sits inside.
- Background `{colors.surface-card-hover}`, rounded `{rounded.pill}`, padding `{spacing.xs} {spacing.sm}`. The icon glyph itself carries the category colour, not the chip.

### Data Surfaces
**`result-card`** — a search-result row (route or stop).
- Background `rgba(255,255,255,0.7)` over the panel's own translucent surface, text `{colors.foreground}`, rounded `{rounded.xl}`, padding `{spacing.lg}`, 1px `{colors.border-subtle}`, `{shadow.sm}` resting → `{shadow.xl}` + `scale-[1.01]` on hover, `scale-[0.99]` on active.

**`eta-row`** — one live bus entry inside an ETA list.
- Background `{colors.surface}`, text `{colors.text-primary}`, rounded `{rounded.xl}`, padding `{spacing.md}`, 1px `{colors.border-default}`. Contents: icon chip → primary label (flex-1) → `{typography.eta-number}` right-aligned, never wrapped.

**`route-badge`** — a route's short code/name, coloured by category.
- Rounded `{rounded.pill}`, padding `2px {spacing.sm}`, `{typography.caption}`. Icon or text tinted `{colors.route-rapidkl}`/`{colors.route-feeder}` per category (or the dark-mode pair).

**`direction-pill`** — "Out" / "In" tags on a route-serving-a-stop card.
- Rounded `{rounded.pill}`, padding `2px {spacing.sm}`, `{typography.caption}`, background a ~12–15% tint of the route colour (or, dark mode, sage for outbound / teal `{colors.dark-accent-teal}` for inbound).

**`panel-overlay`** — the full-screen search-results container.
- Background `rgba(255,255,255,0.9)` + backdrop blur, rounded `{rounded.2xl}`, padding `{spacing.xl}`, `{shadow.2xl}`, 1px `{colors.border-default}`.

**`error-banner`** — a failed-fetch state.
- Background `{colors.danger-bg}`, text `{colors.danger-text}`, 1px `{colors.danger-border}`, rounded `{rounded.xl}`, padding `{spacing.lg}`, centred caption text. The only place danger red appears.

### Signature Component
**`sheet-mobile`** / **`sheet-desktop`** — one panel, two forms, switched at 768px.
- Mobile: background `{colors.background}`, rounded `{rounded.2xl} {rounded.2xl} 0 0` (top corners only), `{shadow.sheet-mobile}`, slides up from `y: 100%`, drag handle, swipe down >100px to dismiss.
- Desktop: background `{colors.background}`, rounded `{rounded.none}`, `{shadow.sheet-desktop}`, fixed 400px left column, slides in from `x: -100%`, explicit close button instead of a dismiss gesture.
- Both: spring transition (damping 25, stiffness 200), same content markup inside either shell.

### Navigation / Labels
**`section-eyebrow`** — "ROUTES", "STOPS", a direction-group heading.
- Text `{colors.text-muted}`, `{typography.eyebrow}` (uppercase, wide tracking), no background, no border.

## Do's and Don'ts

### Do
- **In this build, red is reserved for the live-call status badge — full stop.** Every other surface (header icon, call button, route badges, the alert banner) is black/white/grey. Where-bus's own reference system colours route/stop icons by category (RapidKL maroon, MRT Feeder slate); this build keeps the *category distinction* — black for RapidKL, grey for MRT Feeder — but drops the colour itself, so red stays legible as the one "something is live" signal instead of competing with a decorative brand colour.
- Use `{rounded.pill}` for every control and badge, `{rounded.xl}` for every data card, `{rounded.2xl}` only for a *container* of cards (the overlay panel, the sheet's outer corners).
- Right-align the ETA number in `{typography.eta-number}`, bold, primary text colour, as the loudest element in its row.
- Give interactive cards a hover lift (`{shadow.xl}` + `scale-[1.01]`) and a press-down (`scale-[0.99]`) — the tactile response *is* the depth cue, not a heavier shadow.
- Keep the sheet/sidebar as one component with two anchor states, not two separately-designed panels that happen to show the same data.

### Don't
- **Don't use red anywhere except the live-call status badge — no exceptions.** Not the header icon, not the call button, not a route badge, not the alert banner, and not a "not found" or "did you mean" recovery card either — those are the agent asking a normal follow-up question, not a failure, and get a neutral grey treatment. `{colors.danger-*}` is where-bus's own reference token for a genuine error state; this build doesn't have a surfaced hard-failure state distinct enough to earn it, so it stays unused.
- Don't introduce a third "status" colour beyond live-red and error-red — a warning state reuses neutral gray + copy, not a new hue.
- Don't put a route's category colour on a chip's *background* — the chip is always neutral; only the icon glyph or badge text carries the colour.
- Don't reach for `{rounded.md}`/`{rounded.lg}` (8–12px) as a primary card shape — those sizes aren't part of this system's working vocabulary even though the token exists; every real card is `{rounded.xl}` or bigger.
- Don't give the desktop sidebar rounded corners or a top/bottom shadow — square, full-height, side-shadow only, deliberately flush against the map.
- Don't reach for the cream/terracotta/blush trio by default — they're declared, not the working palette; plain neutral gray is what actually ships.
