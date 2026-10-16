"""Closed-boundary and once-only safety for the control-script upgrade."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import runpy
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("admission_upgrade", pathlib.Path(__file__).with_name("admission_upgrade.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class UpgradeTests(unittest.TestCase):
    def test_default_is_offline_without_loading_helpers_or_contacting_hosts(self):
        output = io.StringIO()
        with mock.patch.object(M.runpy, "run_path", side_effect=AssertionError("helper execution")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("SSH")), \
             contextlib.redirect_stdout(output):
            M.main([])
        report = json.loads(output.getvalue())
        self.assertTrue(report["offline"])
        self.assertFalse(report["main_started"])
        self.assertFalse(report["request_pauses"])
        self.assertTrue(report["cohort_preserved"])

    def test_changed_window_authority_budget_or_cohort_cannot_upgrade(self):
        ns = {"payload": {"owner": "a"*32, "prior_pause": 7.082038551568985, "cohort": M.COHORT}}
        exec(M.VALIDATION, ns)
        state = {"v": 1, "run": "20261016", "owner": "a"*32, "phase": "SOURCE_DATA", "attempt": 1,
                 "route_fenced": True, "pg": "AWS", "redis": "AWS", "window": None,
                 "deadline_breached": False, "pause_seconds": ns["payload"]["prior_pause"], "budget_cohort": M.COHORT}
        ns["previous_state"](state)
        for changes in ({"attempt": 2}, {"window": {}}, {"pg": "COMMIT_INTENT"}, {"redis": "TENCENT"},
                        {"pause_seconds": 0}, {"budget_cohort": {}}, {"deadline_breached": True}, {"route_fenced": False}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "unused-exact-cohort"):
                ns["previous_state"]({**state, **changes})

    def test_remote_programs_compile_using_side_effect_free_helper_load(self):
        prior = runpy.run_path(str(pathlib.Path(M.__file__).with_name("prepare_retry.py")))
        common = prior["COMMON"].replace("retry-upgrade-20261017", "admission-upgrade-20261017")
        for code in (M.SOURCE_PREFLIGHT, M.TARGET_UPGRADE, M.SOURCE_UPGRADE, M.SOURCE_PROBE):
            compile(common + M.VALIDATION + code, "admission-remote", "exec")

    def test_unreviewed_source_program_stops_before_any_remote_or_intent(self):
        original = M.runpy.run_path
        def helpers(path):
            if str(path).endswith("prepare_retry.py"):
                return {"OWNER": "a"*32, "PRIOR_PAUSE": 7}
            if str(path).endswith("prepare_channel.py"):
                return {"SSH_SOURCE": ["source"], "TARGET": ["target"]}
            return original(path)
        with mock.patch.object(M.runpy, "run_path", side_effect=helpers), \
             mock.patch.object(M.pathlib.Path, "read_bytes", return_value=b"unreviewed program"), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("SSH before digest check")):
            with self.assertRaisesRegex(RuntimeError, "reviewed-program-changed"):
                M.main(["--apply"])


if __name__ == "__main__":
    unittest.main()
