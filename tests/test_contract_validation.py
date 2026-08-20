import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_contract_validator_passes():
    completed = subprocess.run(
        [sys.executable, "scripts/validate_contracts.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "contracts valid:" in completed.stdout
