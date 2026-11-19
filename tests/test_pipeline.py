"""End to end: every fault the generator injects must be caught."""
from data.generate_data import generate
from src.anomaly_lambda import run_all
from src.schemas import validate_dataframe


def test_pipeline_catches_every_injected_fault(tmp_path):
    result = generate(out_dir=str(tmp_path), seed=7, offline=True)
    valid, bad = validate_dataframe(result["telemetry"])
    # Corrupt rows are quarantined with a reason; the rest is clean.
    assert 0 < len(bad) < 0.02 * len(result["telemetry"])
    assert bad["quarantine_reason"].str.len().min() > 0
    alerts = run_all(valid)
    found = {(a["rule"], a["site"]) for a in alerts}
    assert ("silent_meter", "Goma") in found
    assert ("silent_meter", "Beni") in found
    assert ("distribution_loss", "Tadu") in found
    assert ("voltage_sag", "Faradje") in found
    assert ("diesel_spike", "Beni") in found

def test_generator_is_reproducible(tmp_path):
    a = generate(out_dir=str(tmp_path / "a"), seed=3, offline=True)["telemetry"]
    b = generate(out_dir=str(tmp_path / "b"), seed=3, offline=True)["telemetry"]
    assert a.equals(b)

def test_fallback_is_labelled_never_nasa(tmp_path):
    result = generate(out_dir=str(tmp_path), offline=True)
    assert set(result["irradiance_sources"].values()) == {"synthetic-fallback"}
