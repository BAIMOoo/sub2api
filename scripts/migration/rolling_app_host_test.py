#!/usr/bin/env python3
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = load("host", "rolling_app_host.py")
M = load("core", "rolling_app_recovery.py")
R = load("rpc", "target_rpc.py")
OWNER = "30bda011defad8149621c2630a2bd89c"


def proc_stat(pid, start):
    return str(pid) + " (name with ) spaces) " + " ".join(["S"] + ["0"] * 18 + [str(start)] + ["0"] * 6)


def tcp_row(local, remote, state, inode, uid=1000):
    return "0: " + local + " " + remote + " " + state + " 00000000:00000000 00:00000000 00000000 " + str(uid) + " 0 " + str(inode)


def fake_proc(directory):
    proc = pathlib.Path(directory)
    (proc / "net").mkdir()
    (proc / "101" / "fd").mkdir(parents=True)
    (proc / "101" / "stat").write_text(proc_stat(101, 777))
    (proc / "202").mkdir()
    (proc / "202" / "status").write_text("Name:\tcaddy\nUid:\t1000\t1000\t1000\t1000\n")
    (proc / "101" / "fd" / "1").symlink_to("socket:[10001]")
    (proc / "101" / "fd" / "2").symlink_to("socket:[10002]")
    (proc / "101" / "fd" / "3").symlink_to("socket:[10003]")
    (proc / "net" / "tcp").write_text("header\n" + "\n".join([
        tcp_row("0100007F:1F92", "00000000:0000", "0A", 10001),
        tcp_row("0100007F:1F92", "0100007F:AABB", "01", 10002),
        tcp_row("0100007F:1F92", "0100007F:AABC", "08", 10003),
        tcp_row("0100007F:AABB", "0100007F:1F92", "01", 20002),
        tcp_row("0100007F:1F92", "0100007F:AAAA", "01", 30000),
    ]) + "\n")
    (proc / "net" / "tcp6").write_text("header\n")
    return proc


def runtime():
    tasks = {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0} for name in R.REQUIRED_WRITERS}
    tasks["content_moderation"]["active"] = 32
    return {"tasks": tasks, "active_requests": 0, "active_hijacked_connections": 0}


class PureTests(unittest.TestCase):
    def test_owned_syn_rule_normalizes_two_argument_flags_without_broadening(self):
        rule = ["-A", "OUTPUT", "-d", "127.0.0.1/32", "-p", "tcp", "-m", "conntrack", "--ctstate", "NEW",
                "-m", "tcp", "--dport", "8082", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN", "-m", "owner", "--uid-owner", "1000",
                "-m", "comment", "--comment", "sub2api-recovery-app-20261017", "-j", "REDIRECT", "--to-ports", "8083"]
        reordered = ["-A", "OUTPUT", "-p", "tcp", "-d", "127.0.0.1", "-m", "owner", "--uid-owner", "1000",
                     "-m", "tcp", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN", "--dport", "8082", "-m", "conntrack", "--ctstate", "NEW",
                     "-m", "comment", "--comment", "sub2api-recovery-app-20261017", "-j", "REDIRECT", "--to-ports", "8083"]
        self.assertEqual(H.normalize_owned_rule(reordered, rule), rule)
        for candidate in (reordered[:12] + reordered[15:], reordered + ["--tcp-flags", "FIN,SYN,RST,ACK", "SYN"],
                          ["ACK" if token == "SYN" else token for token in reordered], reordered + ["--tcp-flags"]):
            with self.subTest(candidate=candidate):
                self.assertNotEqual(H.normalize_owned_rule(candidate, rule), rule)

    def test_tcp_counts_idle_and_close_wait_but_not_other_pid_or_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = fake_proc(directory)
            self.assertEqual(H.tcp_inbound_count(101, 777, proc=proc), 2)

    def test_ipv6_accepted_socket_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = fake_proc(directory)
            (proc / "net" / "tcp6").write_text("header\n" + tcp_row("0" * 32 + ":1F92", "0" * 32 + ":AABC", "09", 10001) + "\n")
            self.assertEqual(H.tcp_inbound_count(101, 777, proc=proc), 3)

    def test_pid_reuse_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = fake_proc(directory)
            with self.assertRaisesRegex(H.Refusal, "PROCESS_ID_REUSED"):
                H.tcp_inbound_count(101, 778, proc=proc)

    def test_moderation_idle_32_requires_zero_detached_work(self):
        body = runtime()
        work, discounted = H.work_summary(body, R.REQUIRED_WRITERS)
        self.assertTrue(discounted)
        self.assertEqual(work["business_active"], 0)
        body["tasks"]["detached_writers"]["active"] = 1
        work, discounted = H.work_summary(body, R.REQUIRED_WRITERS)
        self.assertFalse(discounted)
        self.assertEqual(work["business_active"], 33)

    def test_moderation_queue_deferred_or_unexpected_worker_count_blocks_idle(self):
        for key, value in [("pending", 1), ("deferred", 1), ("active", 31), ("active", 33)]:
            body = runtime()
            body["tasks"]["content_moderation"][key] = value
            work, discounted = H.work_summary(body, R.REQUIRED_WRITERS)
            self.assertFalse(discounted)
            self.assertGreater(work["business_active"] + work["business_pending"], 0)

    def test_writer_coverage_and_nonnegative_counters_required(self):
        body = runtime()
        del body["tasks"]["usage_records"]
        with self.assertRaises(H.Refusal):
            H.work_summary(body, R.REQUIRED_WRITERS)
        body = runtime()
        body["tasks"]["usage_records"]["pending"] = -1
        with self.assertRaises(H.Refusal):
            H.work_summary(body, R.REQUIRED_WRITERS)

    def test_nat_ignores_only_counters_and_comments(self):
        before = b"# stamp\n*nat\n:PREROUTING ACCEPT [1:2]\n:OUTPUT ACCEPT [3:4]\n-A OUTPUT -p tcp --dport 1 -j RETURN\n-A PREROUTING -j RETURN\nCOMMIT\n# done\n"
        after = before.replace(b"[1:2]", b"[9:900]").replace(b"# stamp", b"# later")
        self.assertEqual(H.parse_nat(before), H.parse_nat(after))
        self.assertNotEqual(H.parse_nat(before), H.parse_nat(before.replace(b"--dport 1", b"--dport 2")))

    def test_owned_nat_order_is_normalized_but_narrowing_must_match(self):
        rule = M.REDIRECT
        pairs, i = [], 0
        while i < len(rule):
            width = 3 if rule[i] == "--tcp-flags" else 2
            pairs.append(rule[i:i + width])
            i += width
        reordered = pairs[0] + [part for pair in reversed(pairs[1:]) for part in pair]
        self.assertEqual(H.normalize_owned_rule(reordered, rule), rule)
        changed = list(reordered)
        changed[changed.index("--uid-owner") + 1] = "0"
        self.assertNotEqual(H.normalize_owned_rule(changed, rule), rule)
        raw = ("*nat\n:OUTPUT ACCEPT [0:0]\n" + shlex_join(reordered) + "\nCOMMIT\n").encode()
        self.assertEqual(H.parse_nat(raw, rule)["output"], [rule])

    def test_default_cli_does_not_load_or_execute_backend(self):
        out = io.StringIO()
        with mock.patch.object(H, "load_host", side_effect=AssertionError("must stay offline")), contextlib.redirect_stdout(out):
            self.assertEqual(H.main([]), 0)
        self.assertTrue(json.loads(out.getvalue())["offline"])

    def test_cli_exception_never_prints_credentials(self):
        out = io.StringIO()
        with mock.patch.object(H, "load_host", side_effect=RuntimeError("super-secret-password")), contextlib.redirect_stdout(out):
            self.assertEqual(H.main(["step"]), 1)
        self.assertNotIn("super-secret", out.getvalue())

    def test_inspect_cli_never_advances_or_writes_core_store(self):
        out = io.StringIO()
        backend = types.SimpleNamespace(observe=lambda: {"safe": True})
        with mock.patch.object(H, "load_host", return_value=(None, backend, {})), contextlib.redirect_stdout(out):
            self.assertEqual(H.main(["inspect"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"safe": True})


def shlex_join(parts):
    import shlex
    return " ".join(shlex.quote(x) for x in parts)


class BackendTests(unittest.TestCase):
    def backend(self):
        backend = H.HostBackend.__new__(H.HostBackend)
        backend.m, backend.r = M, R
        backend.config = {"owner": OWNER, "source_recovery_sha256": "e" * 64}
        backend.proc = pathlib.Path("/proc")
        backend._capacity = lambda: None
        return backend

    def mounted_new_app(self, mounts):
        backend = self.backend()
        environment = {**M.SETTINGS, "MIGRATION_RUNTIME_TOKEN": "n" * 64}
        container = {"Id": "a" * 64, "Image": M.IMAGE, "Name": "/" + M.NEW_NAME,
                     "Config": {"Env": [key + "=" + value for key, value in environment.items()],
                                "Labels": {"io.sub2api.migration.owner": OWNER,
                                           "io.sub2api.migration.run": "sub2api-rolling-recovery-20261017"},
                                "Entrypoint": ["/app/docker-entrypoint.sh"], "Cmd": ["/app/sub2api"]},
                     "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"},
                                    "Memory": 512 * H.MIB, "MemorySwap": 512 * H.MIB,
                                    "NanoCpus": 1000000000, "PidsLimit": 256},
                     "State": {"Running": False, "Pid": 0, "StartedAt": "zero", "Status": "created",
                               "ExitCode": 0, "OOMKilled": False},
                     "Mounts": [{"Source": item["source"], "Destination": item["destination"], "RW": item["rw"]}
                                for item in mounts]}
        backend._docker = lambda *args: {"Id": M.IMAGE, "RootFS": {"Layers": ["sha256:" + "e" * 64]}}
        with mock.patch.object(H, "private_read", side_effect=lambda path, **kwargs:
                b"n" * 64 if path == M.TOKEN_FILE else b"fixed-original-file"):
            return backend._app(container, new=True)

    def inherited_state(self, app):
        old = M.Recovery._identity(app)
        old.update(container_id=M.OLD_ID, control_token_sha256="f" * 64)
        return {"old_identity": old}

    def test_app_mount_order_is_canonical_without_changing_core_contract(self):
        for mounts in (M.MOUNTS, list(reversed(M.MOUNTS))):
            with self.subTest(mounts=mounts):
                app = self.mounted_new_app(mounts)
                self.assertEqual(app["mounts"], M.MOUNTS)
                M.Recovery(None, None, OWNER)._new(self.inherited_state(app), app)

    def test_app_mount_normalization_keeps_wrong_values_missing_extra_and_duplicates_invalid(self):
        original = self.mounted_new_app(M.MOUNTS)
        invalid = []
        for key, value in (("source", "/wrong-endpoints"), ("rw", True), ("destination", "/other")):
            mounts = copy.deepcopy(M.MOUNTS)
            mounts[1][key] = value
            invalid.append(mounts)
        invalid.extend([M.MOUNTS[:1], M.MOUNTS + [M.MOUNTS[1]], [M.MOUNTS[0], M.MOUNTS[0]]])
        for mounts in invalid:
            with self.subTest(mounts=mounts), self.assertRaises((H.Refusal, M.Refusal)):
                app = self.mounted_new_app(mounts)
                M.Recovery(None, None, OWNER)._new(self.inherited_state(original), app)

    def test_entry_uid_and_pinned_listener_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend()
            backend.proc = fake_proc(directory)
            backend._command = lambda argv: types.SimpleNamespace(stdout=b"202\n")
            proof = backend._ingress_owner_check({"pid": 101, "start_ticks": 777, "started_at": "start"})
            self.assertEqual(proof["entry_uid"], 1000)
            self.assertEqual(proof["old_listener_inodes"], ["10001"])
            (backend.proc / "202" / "status").write_text("Uid:\t0\t0\t0\t0\n")
            with self.assertRaisesRegex(H.Refusal, "ENTRY_UID_NOT_1000"):
                backend._ingress_owner_check({"pid": 101, "start_ticks": 777})

    def test_actual_forward_client_uid_mismatch_blocks_route(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend()
            backend.proc = fake_proc(directory)
            backend._command = lambda argv: types.SimpleNamespace(stdout=b"202\n")
            path = backend.proc / "net" / "tcp"
            path.write_text(path.read_text().replace("1000 0 20002", "0 0 20002"))
            with self.assertRaisesRegex(H.Refusal, "EXISTING_FORWARD_CLIENT_UID_NOT_1000"):
                backend._ingress_owner_check({"pid": 101, "start_ticks": 777})

    def test_prepare_copies_files_and_preserves_secrets_only_in_private_env_file(self):
        backend = self.backend()
        source_files = {name: ("original-" + name).encode() for name in M.FILES}
        files = {name: H.raw_sha(raw) for name, raw in source_files.items()}
        state = {"old_identity": {"files": files}, "history": {"durable": True}}
        backend._intent = lambda kind: (state, {"new": None})
        backend.inspect_history = lambda: state["history"]
        environment = {**M.SETTINGS, "SERVER_PORT": "8082", "MIGRATION_RUNTIME_ADDR": "127.0.0.1:18092",
                       "MIGRATION_RUNTIME_TOKEN": "old-token", "DATABASE_PASSWORD": "never-print-this",
                       "MIGRATION_POSTGRES_TARGET_DSN": "password=never-print-this", "BUSINESS_OPTION": "same"}
        backend._docker = lambda *args: {"Config": {"Env": [k + "=" + v for k, v in environment.items()]}}
        commands, writes = [], {}
        backend._command = lambda argv: commands.append(argv)
        def read(path, **kwargs):
            return source_files[pathlib.Path(path).name]
        with mock.patch.object(H, "directory_new"), mock.patch.object(H, "private_read", side_effect=read), \
             mock.patch.object(H, "write_new", side_effect=lambda path, raw, **kw: writes.update({str(path): raw})), \
             mock.patch.object(H.secrets, "token_urlsafe", return_value="n" * 64):
            spec = M.Recovery(backend, None, OWNER)._spec(state)
            backend.prepare_app(spec)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:3], ["/usr/bin/docker", "create", "--pull"])
        self.assertNotIn("never-print-this", json.dumps(commands))
        self.assertIn(b"DATABASE_PASSWORD=never-print-this\n", writes[str(H.SCOPE / "app.env")])
        self.assertIn(b"BUSINESS_OPTION=same\n", writes[str(H.SCOPE / "app.env")])
        for name, raw in source_files.items():
            self.assertEqual(writes[str(M.DATA / name)], raw)
        self.assertIn("type=bind,src=" + str(M.ENDPOINT_DIRECTORY) + ",dst=/migration-state,readonly", commands[0])
        self.assertNotIn("start", commands[0])

    def test_start_is_single_fixed_start_and_durable_process_receipt(self):
        backend = self.backend()
        backend._intent = lambda kind: ({}, {"new": {"container_id": "a" * 64, "state": "created"}})
        backend._docker = lambda *args: {"fixed": True}
        backend._process = lambda container: {"pid": 123, "start_ticks": 99, "started_at": "same"}
        commands, writes = [], []
        backend._command = lambda argv: commands.append(argv)
        with mock.patch.object(H, "write_new", side_effect=lambda *args, **kwargs: writes.append(args)):
            backend.start_app("a" * 64)
        self.assertEqual(commands, [["/usr/bin/docker", "start", "a" * 64]])
        self.assertEqual(writes[0][0], H.SCOPE / "new-process.json")

    def test_route_is_only_precise_insert_and_requires_owner_proof(self):
        backend = self.backend()
        nat = {"output": [["-A", "OUTPUT", "-j", "RETURN"]], "other": []}
        state = {"nat_before": copy.deepcopy(nat)}
        backend._intent = lambda kind: (state, {"nat": copy.deepcopy(nat), "old": {"process": {"pid": 1}}})
        backend._ingress_owner_check = lambda process: {"verified": True}
        routed = copy.deepcopy(nat)
        routed["output"].insert(0, M.REDIRECT)
        backend._nat = lambda: routed
        commands = []
        backend._command = lambda argv: commands.append(argv)
        with mock.patch.object(H, "write_new"):
            backend.install_redirect(M.REDIRECT, nat)
        self.assertEqual(commands, [["/usr/sbin/iptables", "-w", "2", "-t", "nat", "-I", "OUTPUT", "1"] + M.REDIRECT[2:]])
        self.assertNotIn("conntrack", commands[0][:8])

    def test_signal_only_term_and_busy_old_is_never_signalled(self):
        backend = self.backend()
        nat = {"output": [], "other": []}
        state = {"nat_before": nat, "history": {"durable": True}}
        routed = {"output": [M.REDIRECT], "other": []}
        work = {key: 0 for key in M.QUIET_FIELDS}
        backend._intent = lambda kind: (state, {"old": {"work": work}, "nat": routed})
        backend.inspect_history = lambda: state["history"]
        commands = []
        backend._command = lambda argv: commands.append(argv)
        backend.signal_old_once(M.OLD_ID)
        self.assertEqual(commands, [["/usr/bin/docker", "kill", "--signal", "TERM", M.OLD_ID]])
        work["inbound_tcp"] = 1
        with self.assertRaises(H.Refusal):
            backend.signal_old_once(M.OLD_ID)
        self.assertEqual(len(commands), 1)

    def test_capacity_thresholds_are_strict(self):
        backend = self.backend()
        with tempfile.TemporaryDirectory() as directory:
            backend.proc = pathlib.Path(directory)
            (backend.proc / "meminfo").write_text("MemAvailable: 716800 kB\n")
            with self.assertRaisesRegex(H.Refusal, "MEMAVAILABLE_UNDER_700MIB"):
                H.HostBackend._capacity(backend)
            (backend.proc / "meminfo").write_text("MemAvailable: 800000 kB\n")
            with mock.patch.object(H.os, "statvfs", return_value=types.SimpleNamespace(f_bavail=1024, f_frsize=1024 * 1024)):
                with self.assertRaisesRegex(H.Refusal, "DISK_UNDER_1GIB"):
                    H.HostBackend._capacity(backend)

    def test_safety_requires_fixed_closure_cancelled_quiesce_and_actual_standbys(self):
        backend = self.backend()
        ledger = {"owner": OWNER, "run": "20261016", "pg": {"authority": "AWS", "arm": None, "promotion_intent": None},
                  "redis": {"authority": "AWS"}, "operations": {"q": {"request": {"action": "app.quiesce"}, "cancelled": True}}}
        raw = H.canonical(ledger)
        closure = {"owner": OWNER, "run": "20261016", "window": None, "controller_stopped": True,
                   "pg": "AWS", "redis": "AWS", "target_ledger_sha256": H.raw_sha(raw)}
        closure_raw = H.canonical(closure)
        backend.config["source_controller_sha256"] = H.raw_sha(closure_raw)
        backend.rpc_config = {"pg": {"system_identifier": "123", "source_timeline": 1}}
        backend.r = types.SimpleNamespace(SCOPE=pathlib.Path("/rpc"), private_read=lambda *a, **k: raw,
                                          strict_json=lambda data, limit: json.loads(data), REDIS_TARGET_RUN_ID="fixed-run")
        pg = {"in_recovery": True, "system_identifier": "123", "source_timeline": 1}
        redis = {"role": "slave", "run_id": "fixed-run", "failover_state": "no-failover"}
        backend.read_backend = types.SimpleNamespace(endpoint_state=lambda: M.ENDPOINTS, resource=lambda name: {},
            pg_manifest=lambda: {"authority_mode": "STANDBY", "promotion_intent": None},
            pg_sample=lambda: pg, redis_inspect=lambda: redis)
        with mock.patch.object(H, "private_read", return_value=closure_raw):
            self.assertEqual(backend._safety(), ledger)
        # Cancellation is retained in the real ledger as evidence; checking
        # source authority must neither delete it nor require it to be None.
        for arm in [{"status": "CANCELLED", "arm_id": "archived-arm", "epoch": 14},
                    {"status": "ARMED"}, {"status": "COMMITTED"}, {}, "CANCELLED"]:
            ledger["pg"]["arm"] = arm
            raw = H.canonical(ledger)
            closure["target_ledger_sha256"] = H.raw_sha(raw)
            closure_raw = H.canonical(closure)
            backend.config["source_controller_sha256"] = H.raw_sha(closure_raw)
            with mock.patch.object(H, "private_read", return_value=closure_raw):
                if isinstance(arm, dict) and arm.get("status") == "CANCELLED":
                    self.assertEqual(backend._safety(), ledger)
                    self.assertEqual(ledger["pg"]["arm"], arm)
                else:
                    with self.assertRaisesRegex(H.Refusal, "SOURCE_AUTHORITY_REQUIRED"):
                        backend._safety()
        ledger["pg"]["arm"] = {"status": "CANCELLED", "arm_id": "archived-arm", "epoch": 14}
        raw = H.canonical(ledger)
        closure["target_ledger_sha256"] = H.raw_sha(raw)
        closure_raw = H.canonical(closure)
        backend.config["source_controller_sha256"] = H.raw_sha(closure_raw)
        with mock.patch.object(H, "private_read", return_value=closure_raw):
            pg["in_recovery"] = False
            with self.assertRaisesRegex(H.Refusal, "ACTUAL_TARGET_PG_NOT_STANDBY"):
                backend._safety()
            pg["in_recovery"] = True
            redis["role"] = "master"
            with self.assertRaisesRegex(H.Refusal, "ACTUAL_TARGET_REDIS_NOT_STANDBY"):
                backend._safety()
            redis["role"] = "slave"
            ledger["operations"]["q"]["cancelled"] = False
            raw = H.canonical(ledger)
            closure["target_ledger_sha256"] = H.raw_sha(raw)
            closure_raw = H.canonical(closure)
            backend.config["source_controller_sha256"] = H.raw_sha(closure_raw)
        with mock.patch.object(H, "private_read", return_value=closure_raw):
            with self.assertRaisesRegex(H.Refusal, "QUIESCE_NOT_CANCELLED"):
                backend._safety()


if __name__ == "__main__":
    unittest.main()
