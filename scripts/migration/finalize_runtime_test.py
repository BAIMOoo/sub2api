"""The final source proof accepts only bounded, verifiable request cohorts."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


F, E = load("finalize_runtime"), load("durable_runner")
OWNER = "ab"*16


class FinalBudgetProofTests(unittest.TestCase):
    def source_proof(self, total=15, current=8, breach=False, unknown=False, bad_record=False, legacy=False):
        previous = {"v": 1, "run": E.RUN, "owner": OWNER, "phase": "SOURCE_DATA", "pg": "AWS", "redis": "AWS",
                    "attempt": 1, "pause_seconds": total-current, "window": None, "route_fenced": True}
        record = {"v": 1, "run": E.RUN, "owner": OWNER, "id": "01"*16, "previous_state": previous,
                  "previous_state_sha256": E.digest(previous), "proof_sha256": "cd"*32}
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()+b"\n"
        state = {**copy.deepcopy(previous), "phase": "COMPLETE", "pg": "TENCENT", "redis": "TENCENT", "attempt": 2,
                 "pause_seconds": total, "deadline_breached": breach, "budget_accounting_unknown": unknown,
                 "budget_cohort": {"id": record["id"], "record_sha256": hashlib.sha256(raw).hexdigest()}}
        if legacy:
            del state["budget_cohort"]
        observed = {"resources": {k: {"id": k, "running": False, "status": "exited", "restart": "no",
                                     "exit_code": 0, "oom_killed": False} for k in ("app", "postgres", "redis")}}
        ops = {"operations": {action: {"request": {"action": action}, "phase": "complete", "steps": {signal: "accepted"}}
                              for action, signal in (("app_stop", "sigterm"), ("pg_stop", "sigint"), ("redis_role_persist", "sigterm"))}}
        backend = types.SimpleNamespace(config={"owner": OWNER, "machine_id_sha256": "ef"*32},
            store=types.SimpleNamespace(read=lambda kind: state), source=lambda label, method: observed,
            source_ops=types.SimpleNamespace(_ledger=lambda: ops, m={"containers": {k: {"id": k} for k in observed["resources"]}}))
        written = []
        source_module = types.SimpleNamespace(SCOPE=pathlib.Path("/fixed/runner"), private_bytes=lambda p: b"deployment-fence",
            canonical=lambda obj: json.dumps(obj, sort_keys=True).encode(), atomic_private=lambda p, raw: written.append((p, raw)))
        module = types.SimpleNamespace(B=types.SimpleNamespace(RunnerBackend=lambda: backend, sha=lambda raw: hashlib.sha256(raw).hexdigest()),
            E=E, S=source_module, Service=lambda b, s: types.SimpleNamespace(verify_complete=lambda: {"phase": "COMPLETE"}))
        fake_spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda m: None))
        with mock.patch.object(importlib.util, "spec_from_file_location", return_value=fake_spec), \
             mock.patch.object(importlib.util, "module_from_spec", return_value=module), \
             mock.patch.object(E, "read_budget_record", return_value=raw+b" " if bad_record else raw), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            exec(compile(F.SOURCE, "finalize-source-proof-test", "exec"), {})
        self.assertEqual(len(written), 1)
        return json.loads(stdout.getvalue())

    def test_complete_accepts_global_total_over_ten_when_each_cohort_is_bounded(self):
        proof = self.source_proof(total=15, current=8)
        self.assertEqual(proof["budget"]["total_pause_seconds"], 15)
        self.assertEqual(proof["budget"]["max_cohort_pause_seconds"], 8)
        self.assertEqual(proof["state"]["pause_seconds"], 15)

    def test_complete_rejects_unknown_record_over_limit_cohort_and_sticky_breach(self):
        for kwargs in ({"bad_record": True}, {"total": 17.2, "current": 10.2}, {"breach": True}, {"unknown": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(E.Refusal):
                self.source_proof(**kwargs)

    def test_legacy_complete_still_requires_total_at_most_ten(self):
        self.assertEqual(self.source_proof(total=9, current=9, legacy=True)["budget"]["max_cohort_pause_seconds"], 9)
        with self.assertRaises(E.Refusal): self.source_proof(total=15, current=8, legacy=True)

    def test_both_generated_finalize_workers_still_compile(self):
        compile(F.SOURCE, "finalize-source", "exec")
        compile(F.TARGET, "finalize-target", "exec")


if __name__ == "__main__":
    unittest.main()
