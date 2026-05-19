export function formatCurrency(n: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n);
}

export function formatCurrencyPrecise(n: number): string {
  const abs = Math.abs(n);
  if (abs === 0) return "$0.00";
  if (abs < 0.0001) {
    return n < 0 ? "-<$0.0001" : "<$0.0001";
  }
  let decimals: number;
  if (abs >= 1) {
    decimals = 2;
  } else if (abs >= 0.01) {
    decimals = 4;
  } else {
    decimals = 6;
  }
  const formatted = abs.toFixed(decimals);
  return n < 0 ? `-$${formatted}` : `$${formatted}`;
}

export function formatQty(n: number): string {
  // 8 decimals max, strip trailing zeros after the 4th
  const s = n.toFixed(8);
  // Keep at least 4 decimal places, strip trailing zeros beyond that
  const [intPart, fracPart] = s.split(".");
  // fracPart is always 8 chars due to toFixed(8)
  const keepMin4 = fracPart.slice(0, 4);
  const rest = fracPart.slice(4).replace(/0+$/, "");
  return `${intPart}.${keepMin4}${rest}`;
}

export function formatRelative(iso: string, now: number = Date.now()): string {
  const diffMs = now - new Date(iso).getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 10) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export function formatNumber(n: number, decimals: number): string {
  return n.toFixed(decimals);
}
