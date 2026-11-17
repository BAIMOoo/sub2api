#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_pnpm_audit_exceptions.py"


class AuditExceptionCheckerTest(unittest.TestCase):
    def run_checker(self, audit: dict, exceptions: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audit_path = root / "audit.json"
            exceptions_path = root / "exceptions.yml"
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            exceptions_path.write_text(exceptions, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--audit",
                    str(audit_path),
                    "--exceptions",
                    str(exceptions_path),
                ],
                text=True,
                capture_output=True,
            )

    @staticmethod
    def exception(advisory: str) -> str:
        return f"""version: 1
exceptions:
  - package: xlsx
    advisory: "{advisory}"
    severity: high
    reason: write-only
    mitigation: never parse files
    expires_on: "2099-01-01"
    owner: test
"""

    def test_matching_exception_passes(self):
        audit = {
            "advisories": {
                "1": {
                    "module_name": "xlsx",
                    "severity": "high",
                    "github_advisory_id": "GHSA-test",
                    "title": "test",
                }
            },
            "metadata": {"vulnerabilities": {"high": 1}},
        }
        proc = self.run_checker(audit, self.exception("GHSA-test"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_audit_endpoint_error_fails(self):
        proc = self.run_checker(
            {"error": {"code": "ERR_PNPM_AUDIT_ENDPOINT_NOT_EXISTS", "message": "missing"}},
            self.exception("GHSA-test"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pnpm audit failed", proc.stderr)

    def test_unused_exception_fails(self):
        proc = self.run_checker(
            {"advisories": {}, "metadata": {"vulnerabilities": {}}},
            self.exception("GHSA-test"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no longer match", proc.stderr)


if __name__ == "__main__":
    unittest.main()
