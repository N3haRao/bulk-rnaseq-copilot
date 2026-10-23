"""Rule-based proposal of the analysis setup from the sample table.

Given the sample table, propose which column holds the groups to compare,
which group is the control, and which other columns to adjust for. This is
the no-LLM baseline: in Phase 2 the executor agent proposes the setup, and
this module is both its fallback and the source of the facts it is shown
(levels, group sizes, which columns are confounded or pair samples).
"""

import re
from dataclasses import dataclass, field

CONDITION_NAME_HINTS = re.compile(
    r"condition|group|treat|genotype|cell.?type|status|disease|phenotype|strain|diet|infection|stimul|drug|knock|tissue",
    re.I)
CONTROL_LEVEL_HINTS = re.compile(
    r"^(control|ctrl|ctl|con|wt|wild.?type|untreated|untr|vehicle|veh|mock|baseline|naive|sham|dmso|pbs|"
    r"normal|healthy|unstim\w*|nt|non.?target\w*|scramble\w*|sc|neg\w*|placebo|untransfected|uninfected|day0|d0|t0|0h|"
    r"(si|sh|sg)[_-]?(ctrl|ctl|control|nt|scr\w*|neg\w*|luc|gfp)|empty([_ -]?vector)?|ev|mock[_-]?\w*)$",
    re.I)
COVARIATE_NAME_HINTS = re.compile(
    r"batch|sex|gender|donor|patient|subject|individual|mouse|animal|pair|replicate|litter|cell.?line|lane|run|plate|age|seq",
    re.I)


@dataclass
class Column:
    name: str
    levels: dict[str, int]          # level -> number of samples
    numeric: bool
    unique_per_sample: bool
    n_missing: int

    def as_dict(self) -> dict:
        return {"name": self.name, "levels": self.levels, "n_levels": len(self.levels),
                "numeric": self.numeric, "unique_per_sample": self.unique_per_sample,
                "n_missing": self.n_missing}


@dataclass
class Proposal:
    sample_id_column: str
    condition: str | None = None
    reference_level: str | None = None
    test_level: str | None = None
    covariates: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reference_guessed: bool = True

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _is_number(x: str) -> bool:
    try:
        float(x)
        return True
    except ValueError:
        return False


def describe_columns(header: list[str], rows: list[list[str]]) -> tuple[str, list[Column]]:
    """The sample ID column (first) and a description of every other column."""
    if not header:
        raise ValueError("The sample table is empty.")
    cols = []
    n = len(rows)
    for j, name in enumerate(header[1:], start=1):
        values = [r[j] if j < len(r) else "" for r in rows]
        present = [v for v in values if v not in ("", "NA", "na", "NaN")]
        levels: dict[str, int] = {}
        for v in present:
            levels[v] = levels.get(v, 0) + 1
        numeric = bool(present) and all(_is_number(v) for v in present) and len(levels) > 6
        cols.append(Column(name=name, levels=levels, numeric=numeric,
                           unique_per_sample=n > 2 and len(levels) == n, n_missing=n - len(present)))
    return header[0], cols


def _pick_reference(levels: list[str]) -> tuple[str, bool]:
    """(reference level, guessed) - a control-like name, otherwise the first alphabetically."""
    for lv in levels:
        if CONTROL_LEVEL_HINTS.match(lv.strip()):
            return lv, False
    for lv in levels:  # weaker: control word inside a longer name ("WT_mouse", "vehicle_24h")
        if re.search(r"(^|[^a-z])(control|ctrl|wt|vehicle|untreated|mock|sham)([^a-z]|$)", lv, re.I):
            return lv, False
    return sorted(levels)[0], True


def _confounded(a: list[str], b: list[str]) -> bool:
    """True when knowing one column's value tells you the other's (they cannot be separated)."""
    ab, ba = {}, {}
    for x, y in zip(a, b):
        ab.setdefault(x, set()).add(y)
        ba.setdefault(y, set()).add(x)
    return all(len(v) == 1 for v in ab.values()) or all(len(v) == 1 for v in ba.values())


def _design_rank_ok(rows_cond: list[str], covariates: list[list[str]]) -> bool:
    """Whether ~ covariates + condition can be estimated (full column rank, at least 1 residual df)."""
    n = len(rows_cond)
    cols: list[list[float]] = [[1.0] * n]
    for values in [*covariates, rows_cond]:
        levels = sorted(set(values))
        for lv in levels[1:]:
            cols.append([1.0 if v == lv else 0.0 for v in values])
    p = len(cols)
    if p >= n:
        return False
    # Gaussian elimination on the n x p matrix
    m = [[cols[j][i] for j in range(p)] for i in range(n)]
    rank, row = 0, 0
    for c in range(p):
        pivot = next((r for r in range(row, n) if abs(m[r][c]) > 1e-9), None)
        if pivot is None:
            continue
        m[row], m[pivot] = m[pivot], m[row]
        for r in range(n):
            if r != row and abs(m[r][c]) > 1e-9:
                f = m[r][c] / m[row][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[row])]
        row += 1
        rank += 1
    return rank == p


def propose(header: list[str], rows: list[list[str]], *, condition: str | None = None) -> tuple[list[Column], Proposal]:
    sample_col, cols = describe_columns(header, rows)
    prop = Proposal(sample_id_column=sample_col)
    by_name = {c.name: c for c in cols}
    n = len(rows)

    # ---- condition --------------------------------------------------------------
    def score(c: Column) -> float:
        if c.numeric or c.unique_per_sample or len(c.levels) < 2 or c.n_missing:
            return -1
        s = 0.0
        if CONDITION_NAME_HINTS.search(c.name):
            s += 3
        if COVARIATE_NAME_HINTS.search(c.name):
            s -= 2
        if len(c.levels) == 2:
            s += 2
        if min(c.levels.values()) >= 2:
            s += 1
        if any(CONTROL_LEVEL_HINTS.match(lv) for lv in c.levels):
            s += 2
        return s

    if condition and condition in by_name:
        cond_col = by_name[condition]
        prop.reasons.append(f"Comparing groups in '{condition}', as chosen.")
    else:
        ranked = sorted((c for c in cols if score(c) >= 0), key=score, reverse=True)
        cond_col = ranked[0] if ranked else None
        if cond_col:
            prop.reasons.append(f"'{cond_col.name}' looks like the groups being compared "
                                f"({len(cond_col.levels)} groups: {', '.join(cond_col.levels)}).")
        else:
            prop.warnings.append("No column in the sample table looks like groups to compare. "
                                 "Add a column such as 'condition' with values like control / treated.")
            return cols, prop
    prop.condition = cond_col.name

    levels = list(cond_col.levels)
    ref, guessed = _pick_reference(levels)
    prop.reference_level, prop.reference_guessed = ref, guessed
    others = [lv for lv in levels if lv != ref]
    prop.test_level = max(others, key=lambda lv: cond_col.levels[lv]) if others else None
    if guessed:
        prop.reasons.append(f"No group is named like a control, so '{ref}' was picked as the reference "
                            "(the baseline). Please check this.")
    else:
        prop.reasons.append(f"'{ref}' looks like the control group.")
    if len(levels) > 2:
        prop.reasons.append(f"'{cond_col.name}' has {len(levels)} groups; one pair is compared per run.")

    # ---- covariates -------------------------------------------------------------
    cond_values = _column_values(header, rows, cond_col.name)
    chosen: list[str] = []
    for c in cols:
        if c.name == cond_col.name or c.unique_per_sample or c.n_missing or len(c.levels) < 2 or c.numeric:
            continue
        values = _column_values(header, rows, c.name)
        if _confounded(values, cond_values):
            if COVARIATE_NAME_HINTS.search(c.name):
                prop.warnings.append(
                    f"'{c.name}' lines up exactly with '{cond_col.name}', so its effect cannot be separated "
                    f"from the comparison. It is not used as a covariate; keep this in mind when reading the results.")
            continue
        pairs = _pairs_samples(values, cond_values)
        hinted = bool(COVARIATE_NAME_HINTS.search(c.name))
        if not (pairs or hinted):
            continue
        trial = [_column_values(header, rows, x) for x in [*chosen, c.name]]
        if not _design_rank_ok(cond_values, trial):
            prop.warnings.append(f"'{c.name}' was not added: with it, the design has too few samples to estimate.")
            continue
        chosen.append(c.name)
        if pairs and max(c.levels.values()) <= len(cond_col.levels):
            prop.reasons.append(f"'{c.name}' pairs samples across groups (each {c.name} has a sample in "
                                "more than one group), so it is adjusted for (paired design).")
        elif pairs:
            prop.reasons.append(f"'{c.name}' varies within each group ({', '.join(c.levels)}), "
                                "so it is adjusted for.")
        else:
            prop.reasons.append(f"'{c.name}' looks like a batch or sample characteristic, so it is adjusted for.")
    prop.covariates = chosen

    # ---- replicate warnings -------------------------------------------------------
    for lv in (prop.reference_level, prop.test_level):
        if lv and cond_col.levels.get(lv, 0) < 3:
            prop.warnings.append(f"Group '{lv}' has {cond_col.levels.get(lv, 0)} samples; "
                                 "at least 3 per group are recommended.")
    if n < 4:
        prop.warnings.append("Fewer than 4 samples in total: differential expression cannot be estimated reliably.")
    return cols, prop


def _column_values(header: list[str], rows: list[list[str]], name: str) -> list[str]:
    j = header.index(name)
    return [r[j] if j < len(r) else "" for r in rows]


def _pairs_samples(values: list[str], cond_values: list[str]) -> bool:
    """True when most levels of `values` occur in more than one condition group (donor, mouse, ...)."""
    groups: dict[str, set[str]] = {}
    for v, c in zip(values, cond_values):
        groups.setdefault(v, set()).add(c)
    multi = sum(1 for g in groups.values() if len(g) > 1)
    return len(groups) > 1 and multi >= max(2, len(groups) // 2) and len(groups) < len(values)
