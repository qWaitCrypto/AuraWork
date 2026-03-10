export function basename(path: string, fallback = "workspace") {
  const value = String(path || "");
  const parts = value.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || value || fallback;
}
