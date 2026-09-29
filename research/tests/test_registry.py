from pathlib import Path

from registry import Trial, read, record, trial_count


def test_failed_trials_are_counted(tmp_path: Path) -> None:
    log = tmp_path / "registry" / "trials.jsonl"
    assert trial_count(log, "mm-btc") == 0
    record(log, Trial("mm-btc", "abc", passed=False, metrics={"sharpe": -0.2}))
    record(log, Trial("mm-btc", "def", passed=True, metrics={"sharpe": 1.1}))
    record(log, Trial("other", "abc", passed=False, metrics={}))
    assert trial_count(log, "mm-btc") == 2
    assert len(log.read_text().splitlines()) == 3


# The committed ledger itself, not QC_REGISTRY_DIR: this guards the file the repo ships.
LOG = Path(__file__).resolve().parents[1] / "registry" / "log"


def test_committed_ledger_is_the_only_trial_log() -> None:
    logs = sorted(LOG.parent.rglob("trials.jsonl"))
    assert logs == [LOG / "trials.jsonl"]


def test_committed_ledger_has_no_duplicate_trials() -> None:
    rows = [(r["experiment"], r["config_hash"]) for r in read(LOG / "trials.jsonl")]
    assert len(rows) == len(set(rows))
    assert trial_count(LOG / "trials.jsonl", "study-0001-favorite-longshot") == 1
