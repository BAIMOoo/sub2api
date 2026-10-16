"""Offline final-preflight interface and generated read-program checks."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import stat
import tempfile
import types
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("final_live_preflight", pathlib.Path(__file__).with_name("final_live_preflight.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class FinalLivePreflightTests(unittest.TestCase):
    def test_default_offline_never_reads_reference_or_runs_ssh(self):
        output = io.StringIO()
        with mock.patch.object(M.subprocess, "run", side_effect=AssertionError("remote call")), \
             mock.patch.object(M.runpy, "run_path", side_effect=AssertionError("not offline")), \
             mock.patch.object(M.pathlib.Path, "read_bytes", side_effect=AssertionError("reference read")), \
             contextlib.redirect_stdout(output):
            M.main([])
        result = json.loads(output.getvalue())
        self.assertTrue(result["offline"])
        self.assertFalse(result["remote_mutations"])
        self.assertFalse(result["model_request_sent"])

    def test_generated_remote_programs_compile_and_disable_bytecode(self):
        source, target = M.remote_scripts({"source": {}, "target": {}}, {"runner_guard.py": "a"*64})
        for name, script in (("source", source), ("target", target)):
            with self.subTest(host=name):
                compile(script, name+"-preflight", "exec")
                self.assertLess(script.index("sys.dont_write_bytecode=True"), script.index("=load("))
                tree = ast.parse(script)
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
                writes = {"write", "write_bytes", "write_text", "atomic_private", "save_state", "persist_endpoints"}
                self.assertFalse([node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr in writes])
                self.assertNotIn("POST", script)
                self.assertNotIn("'start'", script)
                self.assertNotIn("'stop'", script)

    def test_program_modes_match_source_0644_and_target_0700(self):
        source, target = M.remote_scripts({}, {})
        self.assertIn("root_read(lib/name,0o644)", source)
        self.assertNotIn("root_read(lib/name,0o700)", source)
        self.assertIn("root_read(lib/'target_rpc.py',0o700)", target)

    def test_lsn_checks_reject_missing_malformed_and_nonhex_values(self):
        # Exercise the generated helper itself without any privileged code.
        tree = ast.parse(M.COMMON)
        selected = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                   and node.name in ("need", "lsn")], type_ignores=[])
        namespace = {}
        exec(compile(selected, "preflight-lsn-helpers", "exec"), namespace)
        self.assertEqual(namespace["lsn"]("8/CDD2A798"), (8 << 32)+0xCDD2A798)
        for value in (None, "", "8/", "8/ZZ", "-1/10", "100000000/10"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                namespace["lsn"](value)

    def test_check_writes_only_private_local_success_or_failure_evidence(self):
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                folder = pathlib.Path(directory)
                base = {"source": {key: {} for key in ("source_resources", "deployment_fences",
                        "deployment_verification_raw_sha256", "tls")}, "target": {"target_resources": {}}}
                raw = json.dumps(base).encode()
                (folder / "final-preflight-20261017.json").write_bytes(raw)
                response = json.dumps({"ok": True, "business_file_sha256": {"config.yaml": "ab"}}).encode()
                answer = types.SimpleNamespace(returncode=1 if fails else 0, stdout=response, stderr=b"fixed-error")
                routes = {"TARGET": ["mock-target"], "SSH_SOURCE": ["mock-source"]}
                backend_hash = hashlib.sha256(pathlib.Path(M.__file__).with_name("runner_backend.py").read_bytes()).hexdigest()
                with mock.patch.object(M, "FOLDER", folder), \
                     mock.patch.object(M, "REFERENCE_SHA", hashlib.sha256(raw).hexdigest()), \
                     mock.patch.object(M, "BACKEND_SHA", backend_hash), \
                     mock.patch.object(M.runpy, "run_path", side_effect=[{"NEW": {}}, routes]), \
                     mock.patch.object(M.subprocess, "run", return_value=answer) as remote, \
                     contextlib.redirect_stdout(io.StringIO()):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "target-preflight-failed"):
                            M.main(["--check"])
                    else:
                        M.main(["--check"])
                paths = list(folder.glob("final-live-preflight-*.json"))
                self.assertEqual(len(paths), 1)
                self.assertEqual(stat.S_IMODE(paths[0].stat().st_mode), 0o600)
                evidence = json.loads(paths[0].read_bytes())
                self.assertEqual(evidence["ok"], not fails)
                self.assertFalse(evidence["remote_mutations"])
                self.assertEqual(remote.call_count, 1 if fails else 2)


if __name__ == "__main__":
    unittest.main()
