"""Keep the existing security verifier's rejection checks in every CI run."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('caddy_security', Path(__file__).parents[1] / 'scripts/verify-caddy-security.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
EvidenceTests = verifier.EvidenceTests
