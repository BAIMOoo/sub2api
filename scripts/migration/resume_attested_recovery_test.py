"""Attempt-5 boundary tests reuse the original recovery fault harness."""
import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


W = load("attested_wrapper_tested", "resume_attested_recovery.py")
T = load("reused_recovery_faults", "resume_idle_recovery_test.py")
RAW = pathlib.Path(__file__).with_name("resume_idle_recovery.py").read_bytes()
R = W.load_derived(RAW)


class Host(T.Host):
    def __init__(self, failure=None):
        super().__init__(failure)
        record = {"v": 1, "run": R.RUN, "owner": R.OWNER, "id": "eeceb7d8975c689d59705aaeacb073e0",
                  "previous_state": self.initial, "previous_state_sha256": T.E.digest(self.initial), "proof_sha256": "c" * 64}
        raw = T.A.canonical(record)
        self.records[record["id"]] = raw
        self.initial = {**self.initial, "attempt": 5, "pause_seconds": R.LIFETIME, "quiet_instance_id": R.INSTANCE,
                        "budget_cohort": {"id": record["id"], "record_sha256": R.sha(raw)}}
        self.raw = T.A.canonical(self.initial)
        self.frozen["state.json"] = self.raw
        self.target.update(ledger_epoch=20, restored_ingress_epoch=4)

    def observe(self, name):
        result = super().observe(name)
        if name == "runtime.status":
            result["ingress"]["epoch"] = 4
        else:
            result["epoch"] = 4
        return result

    def start_main(self):
        super().start_main()
        if self.failure not in ("monitor_timeout", "main_failed"):
            value = T.A.strict(self.raw)
            value.update(attempt=6, pause_seconds=R.LIFETIME + 4)
            self.raw = T.A.canonical(value)


class ContinuationTests(unittest.TestCase):
    def test_original_tool_is_hash_bound_and_unmodified(self):
        self.assertEqual(hashlib.sha256(RAW).hexdigest(), W.BASE_SHA)
        with self.assertRaisesRegex(RuntimeError, "base-changed"):
            W.derive(RAW + b"\n")

    def test_only_new_boundary_is_accepted(self):
        host = Host()
        R.source_state(T.A, host.raw, host.engine)
        for key, bad in (("attempt", 4), ("pg", "TENCENT"), ("pause_seconds", T.R.LIFETIME)):
            value = {**host.initial, key: bad}
            with self.assertRaisesRegex(RuntimeError, "boundary-required"):
                R.source_state(T.A, T.A.canonical(value), host.engine)

    def test_restore_epoch_four_required(self):
        host = Host()
        args = [host.observe("ingress.status"), host.observe("runtime.status"), host.observe("ingress.status")]
        self.assertTrue(R.drained(*args, host.required))
        for row in (args[0], args[1]["ingress"], args[2]):
            row["epoch"] = 2
        self.assertFalse(R.drained(*args, host.required))

    def test_new_cohort_preserves_full_attempt_five_history_and_single_start(self):
        host = Host()
        original = copy.deepcopy(host.initial)
        result = R.resume(host)
        self.assertTrue(result["ok"])
        self.assertEqual(host.events.count("start"), 1)
        self.assertEqual(T.A.strict(host.records[host.cohort_id])["previous_state"], original)
        self.assertLess(host.events.index("unlock"), host.events.index("start"))

    def test_uncertain_start_stays_once_only(self):
        host = Host("start")
        result = R.resume(host)
        self.assertEqual(result["start_outcome"], "unknown-inspect-only")
        self.assertEqual(host.events.count("start"), 1)

    def test_remote_program_and_installer_compile_with_safe_argument_size(self):
        compile(W.INSTALL, "reviewed-install", "exec")
        support = pathlib.Path(__file__).with_name("accept_recovery_generation.py").read_bytes()
        canary = pathlib.Path(__file__).with_name("recovery_generation_canary.py").read_bytes()
        import base64
        payload = {"support": base64.b64encode(support).decode(), "canary": base64.b64encode(canary).decode()}
        for action in ("target", "source", "inspect"):
            if action == "source":
                payload.pop("canary")
                payload.update(target={"padding": "x" * 8192}, cohort_id="a" * 32)
            code = R.remote_code(action, payload, R.DERIVED_PROGRAM)
            compile(code, "derived-remote", "exec")
            import shlex
            self.assertLess(len(("sudo -n python3 -I -c " + shlex.quote(code)).encode()), 131072)
        self.assertEqual(R.PROGRAMS["durable_runner.py"], W.NEW_ENGINE)

    def test_target_recovery_requires_complete_cancelled_quiesce_and_exact_restore(self):
        def fixture(change=None):
            quiesce = {"request": {"action": "app.quiesce"}, "phase": "COMPLETE", "cancelled": True,
                        "instance_id": R.INSTANCE, "substeps": {k: {"phase": "COMPLETE"}
                            for k in ("ingress", "writers", "redis", "postgres")}}
            restore = {"request": {"action": "app.restore", "op_id": "b" * 32}, "phase": "COMPLETE", "accepted_at": 10,
                       "response": {"code": "OK", "epoch": 20, "result": {
                           "app": {"body": {"instance_id": R.INSTANCE}}, "ingress": {"epoch": 4}}}}
            if change:
                change(quiesce, restore)
            raw = T.A.canonical({"epoch": 20, "operations": {R.FAILED_OP: quiesce, "b" * 32: restore}})
            config = b"{}"
            proof = {"instance_id": R.INSTANCE, "ledger_epoch": 20,
                     "target_ledger_sha256": R.sha(raw), "controller_config_sha256": R.sha(config)}
            support = types.SimpleNamespace(strict=T.A.strict,
                root_read=lambda path: raw if path.name == "state.json" else config,
                load_bytes=lambda *args: types.SimpleNamespace(target_preflight=lambda: proof))
            with mock.patch.object(R, "TARGET_LEDGER_SHA", R.sha(raw)), mock.patch.object(R, "TARGET_CONFIG_SHA", R.sha(config)):
                canary = pathlib.Path(__file__).with_name("recovery_generation_canary.py").read_bytes()
                return R.target_check(support, canary)
        self.assertEqual(fixture()["restored_ingress_epoch"], 4)
        changes = [lambda q, r: q["substeps"].pop("redis"),
                   lambda q, r: q["substeps"]["redis"].update(phase="INTENT"),
                   lambda q, r: q.update(cancelled=False), lambda q, r: q.update(instance_id="wrong"),
                   lambda q, r: r["response"]["result"]["ingress"].update(epoch=2),
                   lambda q, r: r["response"]["result"]["app"]["body"].update(instance_id="wrong")]
        for change in changes:
            with self.subTest(change=changes.index(change)), self.assertRaises(RuntimeError):
                fixture(change)

    def test_installer_timeout_preserves_unknown_receipt_and_never_retries(self):
        written = []
        with tempfile.TemporaryDirectory() as folder:
            support = types.SimpleNamespace(FOLDER=pathlib.Path(folder), canonical=T.A.canonical,
                load_bytes=lambda *args: types.SimpleNamespace(SSH_SOURCE=["ssh", "fixed"]),
                write_new=lambda path, raw: written.append((path.name, json.loads(raw))))
            error = subprocess.TimeoutExpired("fixed", 35, output=b"partial", stderr=b"detail")
            with mock.patch.object(W, "load_derived", return_value=R), \
                 mock.patch.object(R, "load_support", return_value=support), \
                 mock.patch.object(W.subprocess, "run", side_effect=error) as remote:
                with self.assertRaisesRegex(RuntimeError, "uncertain-inspect"):
                    W.main(["--install"])
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(len(written), 2)
        self.assertEqual(written[-1][1]["outcome"], "unknown-inspect-only")
        self.assertEqual(written[-1][1]["stdout_b64"], "cGFydGlhbA==")
        self.assertTrue(written[-1][1]["no_repeat"])


if __name__ == "__main__":
    unittest.main()
