/**
 * Render a backend UTC timestamp ("YYYY-MM-DD HH:MM:SS") in the viewer's local
 * time. Falls back to the raw string if it cannot be parsed.
 */
export function formatTimestamp(value: string): string {
  const parsed = new Date(value.replace(" ", "T") + "Z");
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

/** Render an estimated USD cost compactly, or null if unknown. */
export function formatCost(cost: number | null | undefined): string | null {
  if (cost == null) return null;
  if (cost === 0) return "$0";
  if (cost < 0.0001) return "<$0.0001";
  return "$" + cost.toFixed(4);
}
