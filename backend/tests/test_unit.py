"""Fast tests that need neither R nor a finished run."""

from pathlib import Path

import pytest
import yaml

from app import codebank, results
from app.params import AnalysisSettings, validate
from app.setup_proposal import propose


def table(text: str) -> tuple[list[str], list[list[str]]]:
    rows = [line.split(",") for line in text.strip().splitlines()]
    return rows[0], rows[1:]


def test_proposal_paired_design_with_control_name():
    header, rows = table("""
sample,treatment,donor,run
s1,vehicle,D1,R1
s2,vehicle,D2,R2
s3,vehicle,D3,R3
s4,drug,D1,R4
s5,drug,D2,R5
s6,drug,D3,R6
""")
    _, p = propose(header, rows)
    assert (p.condition, p.reference_level, p.test_level) == ("treatment", "vehicle", "drug")
    assert not p.reference_guessed
    assert p.covariates == ["donor"]          # pairs samples across groups
    assert "run" not in p.covariates          # unique per sample


def test_proposal_flags_confounded_batch_and_guesses_reference():
    header, rows = table("""
id,cell_type,status,lane
a,basal,virgin,L2
b,basal,virgin,L2
c,basal,pregnant,L2
d,basal,pregnant,L2
e,luminal,virgin,L1
f,luminal,virgin,L1
g,luminal,pregnant,L1
h,luminal,pregnant,L1
""")
    _, p = propose(header, rows)
    assert p.condition == "cell_type"
    assert p.reference_guessed and p.reference_level == "basal"
    assert p.covariates == ["status"]
    assert any("'lane' lines up exactly" in w for w in p.warnings)


def test_proposal_low_replicates_warning_and_chosen_condition():
    header, rows = table("""
id,genotype,sex
a,WT,M
b,WT,F
c,KO,M
d,KO,F
""")
    _, p = propose(header, rows, condition="genotype")
    assert p.reference_level == "WT"
    assert any("at least 3 per group" in w for w in p.warnings)


def test_proposal_without_group_column():
    header, rows = table("""
id,run
a,1
b,2
c,3
d,4
""")
    _, p = propose(header, rows)
    assert p.condition is None and p.warnings


@pytest.mark.parametrize("value, expected", [
    ("batch", "batch"), ("yes", "'yes'"), ("1", "'1'"), ("cell line", "cell line"),
    ("a,b", "'a,b'"), (0.05, "0.05"), (True, "true"), (["H", "GO:BP"], "[H, 'GO:BP']"),
])
def test_yaml_scalar_round_trips(value, expected):
    assert codebank._yaml_scalar(value) == expected
    if isinstance(value, str):
        assert yaml.safe_load(codebank._yaml_scalar(value)) == value


def test_yaml_scalar_rejects_semicolon():
    with pytest.raises(ValueError):
        codebank._yaml_scalar("a;b")


def base(**kw) -> AnalysisSettings:
    return AnalysisSettings(**{"condition": "c", "reference_level": "ctrl", "test_level": "trt", **kw})


def test_validate_against_manifest():
    s, problems = validate(base(advanced={"padj_cutoff": 0.01, "gene_set_collections": ["H"]}))
    assert problems == [] and s.advanced["padj_cutoff"] == 0.01
    _, problems = validate(base(organism="zebrafish"))
    assert problems
    _, problems = validate(base(advanced={"padj_cutoff": "small"}))
    assert any("number" in p for p in problems)
    _, problems = validate(base(advanced={"gene_set_collections": ["NOT_A_SET"]}))
    assert problems
    _, problems = validate(base(advanced={"rm_rf": True}))
    assert any("Unknown setting" in p for p in problems)
    _, problems = validate(base(test_level="ctrl"))
    assert problems
    _, problems = validate(base(covariates=["c"]))
    assert problems


def test_run_name_is_cleaned():
    assert base(run_name="KO vs WT / day 3").run_name == "KO_vs_WT___day_3"


def test_safe_path_blocks_escapes(tmp_path: Path):
    run = tmp_path / "run"
    (run / "05_de_plots").mkdir(parents=True)
    (run / "05_de_plots" / "volcano_plot.png").write_bytes(b"x")
    (run / "dds.rds").write_bytes(b"x")
    (tmp_path / "secret.csv").write_text("x")
    assert results.safe_path(run, "05_de_plots/volcano_plot.png").name == "volcano_plot.png"
    for bad in ("../secret.csv", "dds.rds", "missing.png", "/etc/passwd"):
        with pytest.raises(KeyError):
            results.safe_path(run, bad)


def test_manifest_steps_are_readable():
    names = [s["name"] for s in codebank.steps()]
    assert names[0] == "01_validate_inputs" and names[-1] == "07_report"
