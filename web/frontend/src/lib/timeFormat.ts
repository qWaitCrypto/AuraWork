export function fmtTime(ms: number) {
  try {
    return new Date(ms).toLocaleTimeString();
  } catch {
    return "";
  }
}

export function fmtElapsed(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hh)}:${pad(mm)}:${pad(ss)}`;
}
