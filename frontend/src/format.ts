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
