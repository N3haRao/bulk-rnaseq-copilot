"""Validate analysis settings against the code bank manifest before anything runs.

Every value that reaches `run_pipeline.R` passes through here. The same check
will guard the executor agent in Phase 2: a model can propose settings, but
only values that match the manifest's types and allowed values are run.
"""

from pydantic import BaseModel, Field, field_validator

from . import codebank

import re

# Advanced settings the UI and the assistants may change (all other parameters keep
# code-bank defaults). A setting is only offered when the code bank's manifest has it.
ANALYSIS_PARAMS = ("padj_cutoff", "log2fc_cutoff", "lfc_shrinkage", "min_count", "run_enrichment",
                   "gene_set_collections", "exclude_samples")
PLOT_PARAMS = ("n_label_volcano", "n_heatmap_genes", "volcano_label_genes", "plot_palette", "group_colours",
               "up_colour", "down_colour", "pca_colour_by", "pca_shape_by", "pca_label_samples")
ADVANCED_PARAMS = ANALYSIS_PARAMS + PLOT_PARAMS
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
NAMED_COLOURS = {"black", "white", "grey", "gray", "red", "blue", "green", "orange", "purple", "pink", "brown",
                 "yellow", "darkblue", "darkred", "darkgreen", "navy", "firebrick", "steelblue", "tomato", "gold"}


def is_colour(v: str) -> bool:
    return isinstance(v, str) and (bool(HEX.match(v)) or v.lower() in NAMED_COLOURS)


class AnalysisSettings(BaseModel):
    condition: str = Field(min_length=1)
    reference_level: str = Field(min_length=1)
    test_level: str = Field(min_length=1)
    covariates: list[str] = []
    organism: str = "auto"
    run_name: str = "analysis"
    advanced: dict = {}

    @field_validator("run_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in v.strip())[:40]
        return cleaned or "analysis"


def manifest_params() -> dict[str, dict]:
    """parameter name -> manifest spec (type, values, default), merged over all steps."""
    out: dict[str, dict] = {}
    for step in codebank.manifest()["steps"]:
        for name, spec in (step.get("parameters") or {}).items():
            merged = {**out.get(name, {}), **(spec or {})}
            out[name] = merged
    return out


def available_advanced() -> list[str]:
    """The advanced settings this code bank version supports."""
    specs = manifest_params()
    return [k for k in ADVANCED_PARAMS if k in specs]


def check_value(name: str, value, spec: dict, columns: list[str] | None = None):
    """Return the value converted to its manifest type, or raise ValueError with a plain message."""
    t = spec.get("type")
    allowed = spec.get("values")
    if t == "colour":
        if not is_colour(value):
            raise ValueError(f"{name} must be a colour such as #D55E00.")
        return value
    if t == "column":
        if value in (None, "", "none"):
            return value or None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a column of the sample table.")
        if columns is not None and value not in columns:
            raise ValueError(f"{name}: '{value}' is not a column of the sample table.")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number.")
        return float(value)
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number.")
        return value
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false.")
        return value
    if t == "enum":
        if value not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(map(str, allowed))}.")
        return value
    if t == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{name} must be a list of text values.")
        if allowed:
            bad = [x for x in value if x not in allowed]
            if bad:
                raise ValueError(f"{name}: {', '.join(bad)} not allowed (choose from {', '.join(allowed)}).")
        if name == "group_colours":
            for x in value:
                level, _, colour = x.partition("=")
                if not level.strip() or not is_colour(colour.strip()):
                    raise ValueError(f"group_colours entries look like WT=#000000; got '{x}'.")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    return value


def validate(settings: AnalysisSettings, columns: list[str] | None = None) -> tuple[AnalysisSettings, list[str]]:
    """Checked copy of the settings and a list of problems (empty when valid).

    `columns` (the sample table's columns, when known) lets column settings be checked too.
    """
    specs = manifest_params()
    problems: list[str] = []
    try:
        check_value("organism", settings.organism, specs["organism"])
    except ValueError as e:
        problems.append(str(e))
    if settings.reference_level == settings.test_level:
        problems.append("The two groups being compared must be different.")
    if settings.condition in settings.covariates:
        problems.append(f"'{settings.condition}' is the comparison column; it cannot also be a covariate.")
    if len(set(settings.covariates)) != len(settings.covariates):
        problems.append("A covariate is listed twice.")
    clean_adv = {}
    for k, v in settings.advanced.items():
        if k not in ADVANCED_PARAMS or k not in specs:
            problems.append(f"Unknown setting '{k}'.")
            continue
        try:
            clean_adv[k] = check_value(k, v, specs[k], columns)
        except ValueError as e:
            problems.append(str(e))
    for k in ("padj_cutoff",):
        if k in clean_adv and not (0 < clean_adv[k] <= 1):
            problems.append("padj_cutoff must be between 0 and 1.")
    for k in ("log2fc_cutoff", "min_count", "n_label_volcano", "n_heatmap_genes"):
        if k in clean_adv and clean_adv[k] < 0:
            problems.append(f"{k} cannot be negative.")
    return settings.model_copy(update={"advanced": clean_adv}), problems
