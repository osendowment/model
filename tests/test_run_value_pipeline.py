"""Guard: the value runner runs only steps that feed value.csv.

Mirrors the risk runner's score-forming discipline (test_run_risk_pipeline).
`audit_ecosystems` (eco-audit) writes only data/sources/ecosystems/audit.csv,
which is read by NOTHING downstream and never touches a value.csv column, so
it must stay OUT of the default STEPS — it lives in AUDIT_STEPS, runnable by
hand but never by the pipeline.
"""


def test_default_steps_exclude_audit_only_step():
    from src.value.run_value_pipeline import STEPS, AUDIT_STEPS
    step_modules = {s.module for s in STEPS}
    audit_modules = {s.module for s in AUDIT_STEPS}
    assert "src.value.audit_ecosystems" not in step_modules
    assert "src.value.audit_ecosystems" in audit_modules
    assert not (step_modules & audit_modules)   # disjoint


def test_default_steps_cover_every_value_csv_producing_stage():
    """Every step that originates a value.csv column stays in the default run:
    the eco sub-pipelines (results.csv), resolve (identity), unify (the table),
    validation (git_valid), criticality (openssf_crit/eco_crit/value_score),
    plus stats (feeds the stats.py --check gate)."""
    from src.value.run_value_pipeline import STEPS
    modules = {s.module for s in STEPS}
    for m in (
        "src.value.npm_pipeline", "src.value.crates_pipeline",
        "src.value.pypi_pipeline", "src.value.cpp_pipeline",
        "src.value.build_stats", "src.value.build_git_urls",
        "src.sources.ecosystems.candidates",
        "src.value.apply_ecosystems_authority", "src.value.unify_value_data",
        "src.value.build_validation", "src.value.apply_criticality",
    ):
        assert m in modules, m


def test_rollup_labels_are_a_subset_of_default_steps():
    """--rollup selects a subset of STEPS (no dangling audit label)."""
    from src.value.run_value_pipeline import STEPS, ROLLUP_LABELS
    labels = {s.label for s in STEPS}
    assert set(ROLLUP_LABELS) <= labels
