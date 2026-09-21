# QuantDesk Design

## Direction

Night risk-control console: a quiet, high-density workstation built for repeated scanning. Selected symbols carry the typographic scale of a market board while each metric behaves like one trusted instrument in a flight cross-check.

## Visual System

- Near-black matte ground with hairline panel rules.
- Cool cyan identifies connected data, active controls, and constructive signals.
- Amber marks degraded data or caution. Red is reserved for downside and risk.
- Display symbols are large and tightly set. Measurement, timestamps, and prices use tabular monospace numerals; prose remains sans serif.
- Panels join through shared rules instead of repeated floating cards.
- The user-provided animated beams remain a restrained background signal and never cross the chart at full contrast.
- The floating dock is the primary module switcher; the fixed universe remains continuously visible.

## Interaction

- Instrument and timeframe selection update the main chart directly.
- Live API failure falls back to an explicitly labelled demo state with a recovery action.
- File upload accepts drag, click, keyboard focus, success, and rejection states.
- Motion is limited to the background signal, dock magnification, and upload transition, with reduced-motion support.

## Responsive Behavior

- Desktop keeps a fixed universe rail, chart, and resonance panel.
- Tablet converts the universe into a horizontal strip and stacks analysis below the chart.
- Mobile uses the compact dock, two-column derivatives grid, and single-column workspaces.
