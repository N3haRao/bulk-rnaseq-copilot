// Number formatting for biologists: plain where possible, scientific only for tiny p-values.

export function fmtNum(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return x.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtP(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  if (x === 0) return "< 1e-300";
  if (x < 0.001) return x.toExponential(1);
  return x.toFixed(3);
}

export function fmtInt(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  return Math.round(x).toLocaleString();
}

export function geneLabel(g: { gene_symbol?: string | null; gene_id: string }): string {
  return g.gene_symbol && g.gene_symbol !== "NA" ? g.gene_symbol : g.gene_id;
}

export function fmtTime(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
