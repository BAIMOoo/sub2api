#!/usr/bin/env python3
"""Offline safety/protocol tests; these do not start Docker, SSH or an app."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).with_name("rehearse_app_handover.py")
SPEC = importlib.util.spec_from_file_location("app_rehearsal", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
RUN = "0123456789abcdef"
PAYLOAD = {"run_id": RUN, "app_image": "sub2api:migration-compat-20261016-976be8f157f7",
    "app_image_id": "sha256:29ec0798495691abb66989d377e2425df914beb7bae5ea94ff03608e4364604c",
    "binary_sha256": "976be8f157f7688f9de834c9ab885b88485bb49dcb16a7711e2e46b3b95fcb1c"}


def snapshot():
    return {"background_workers_enabled": False, "active_requests": 0,
        "active_hijacked_connections": 0, "ready": True,
        "ingress": {"paused": True, "quiet": True},
        "tasks": {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0}
                  for name in M.REQUIRED_WRITERS},
        "readiness": {name: True for name in ["postgres", "redis", "quiet_controls", "writer_coverage"]}}


def consumers(epoch=1, paused=False):
    return {"epoch": epoch, "paused": paused, "pubsub_cache_ready": True,
            "address": "127.0.0.1:"+str(M.PORTS["redis2"]),
            "consumers": [{"name": name, "generation": epoch} for name in M.REQUIRED_CONSUMERS]}


class ProtocolRunner(M.Rehearsal):
    """Script-order model, not a substitute for the real app control tests."""
    def __init__(self):
        super().__init__(PAYLOAD)
        self.calls = []
        self.state = {"ingress": False, "writers": False, "redis": False, "postgres": False}
        self.pg_primary = "source"
        self.redis_primary = "source"
        self.pg_endpoint = "source"
        self.redis_endpoint = "source"
        self.redis_epoch = 1
        self.pg_epoch = 1
        self.rebuilt_epoch = 1
        self.writes = 1
        self.target_writes = 0

    def start_scenario(self, name):
        self.current = {"name": name, "start": M.time.monotonic()}
        self.scenarios.append(self.current)
        self.state["ingress"] = True

    def finish_scenario(self):
        self.post("writers", "resume")
        self.post("ingress", "resume", expected=204)
        self.current["ok"] = True
        self.current = None

    def write_report(self):
        pass

    def checkpoint_endpoints(self, component, endpoint):
        self.calls.append("checkpoint_"+component+"_"+endpoint)
        self.endpoint_state[component] = endpoint

    def assert_no_warnings(self):
        self.calls.append("warning_gate")

    def get(self, component):
        if component == "runtime":
            return snapshot()
        if component == "postgres":
            return {"epoch": self.pg_epoch, "endpoint": self.pg_endpoint, "active_operations": 0}
        if component == "redis":
            result = consumers(self.redis_epoch, self.state["redis"])
            result["address"] = "127.0.0.1:"+str(M.PORTS["redis2"] if self.redis_endpoint == "target" else M.PORTS["redis1"])
            return result
        return {"epoch": 1}

    def post(self, component, operation, *, expected=200, epoch=None, endpoint=None):
        self.calls.append(component+"/"+operation+("/"+endpoint if endpoint else ""))
        status = 200
        if component == "ingress":
            if operation == "resume" and self.state["writers"]:
                status = 409
            else:
                self.state["ingress"] = operation == "pause"
                status = 204 if operation == "resume" else 200
        elif component == "writers":
            if not self.state["ingress"] or (operation == "resume" and (self.state["redis"] or self.state["postgres"])):
                status = 409
            else:
                self.state["writers"] = operation == "pause"
        elif component == "postgres":
            if not (self.state["ingress"] and self.state["writers"] and self.state["redis"]):
                status = 409
            elif operation == "switch" and (not self.state["postgres"] or self.pg_primary != endpoint):
                status = 409
            elif operation == "switch":
                self.pg_endpoint = endpoint
                self.pg_epoch += 1
            else:
                self.state["postgres"] = operation == "pause"
        elif component == "redis":
            if not (self.state["ingress"] and self.state["writers"]):
                status = 409
            elif epoch is not None and epoch != self.redis_epoch:
                status = 409
            elif operation == "switch" and (not self.state["redis"] or self.redis_primary != endpoint):
                status = 409
            elif operation == "switch":
                self.redis_endpoint = endpoint
                self.redis_epoch += 1
                self.rebuilt_epoch = 0
            elif operation == "rebuild":
                if not self.state["redis"] or self.state["postgres"]:
                    status = 409
                else:
                    self.rebuilt_epoch = self.redis_epoch
            elif operation == "resume":
                if self.rebuilt_epoch != self.redis_epoch:
                    status = 409
                else:
                    self.state["redis"] = False
            else:
                self.state["redis"] = True
                self.rebuilt_epoch = 0
        M.need(status == expected, "protocol-model-order-failed")
        return self.get(component)

    def sql(self, role, text, **kwargs):
        if "INSERT" in text:
            self.writes += 1
            if role == "pg2":
                M.need(self.pg_primary == "target", "write-before-promotion")
                self.target_writes += 1
        if "count(*)" in text:
            if role == "pg2":
                M.need(self.pg_primary == "target" and self.target_writes >= 2, "unreplicated-target-read")
            return str(self.writes)
        return ""

    def synchronized_pg_lsn(self):
        M.need(self.state["postgres"], "wal-sync-before-db-pause")
        self.target_writes = self.writes
        self.calls.append("final_wal_replay")

    def fence_pg1(self):
        M.need(self.state["postgres"] and self.target_writes == self.writes, "source-fence-before-replay")
        self.pg_primary = None
        self.calls.append("fence_pg1")

    def promote_pg2(self):
        M.need(self.pg_primary is None, "promotion-before-old-primary-fence")
        self.pg_primary = "target"
        self.calls.append("promote_pg2")

    def failover_redis(self):
        M.need(self.state["redis"] and not self.state["postgres"], "redis-failover-order")
        self.redis_primary = "target"
        self.calls.append("coordinated_redis_failover")

    def redis(self, *args, **kwargs):
        return "OK" if args[1] == "SET" else "confirmed-after-handover"

    def wait(self, label, condition, timeout=20):
        M.need(condition(), "condition-never-ready")


class OfflineTests(unittest.TestCase):
    def test_default_is_offline_even_when_process_tools_unavailable(self):
        with mock.patch.object(M.subprocess, "run", side_effect=AssertionError("network/process forbidden")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        value = json.loads(out.getvalue())
        self.assertTrue(value["dry_run"])
        self.assertFalse(value["network_access"])
        self.assertFalse(value["real_model_first_token_latency_proven"])

    def test_apply_requires_reviewed_image_before_network_or_files(self):
        with mock.patch.object(M.subprocess, "run", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(M.Refusal, "image-inputs-required"):
                M.main(["--apply"])

    def test_run_identity_cannot_escape_owned_prefix(self):
        for bad in ["../sub2api", "sub2api", "", "A"*16, "0123456789abcdef/pg1"]:
            with self.assertRaises(M.Refusal):
                M.names(bad)
        self.assertTrue(all(v.startswith("sub2api-app-rehearsal-"+RUN+"-") for v in M.names(RUN).values()))

    def test_cold_input_has_only_readonly_mount_and_owned_destination(self):
        mounts = M.cold_clone_mounts(M.names(RUN)["pg1"]+"-data")
        self.assertIn("src="+M.COLD_VOLUME+",dst=/input,readonly", mounts[1])
        self.assertNotIn(M.COLD_VOLUME, mounts[3])
        for bad in [M.COLD_VOLUME, "sub2api-postgres-data", "../data"]:
            with self.assertRaises(M.Refusal):
                M.cold_clone_mounts(bad)

    def test_runtime_secret_and_proxy_handling(self):
        password = "secret'\\$(not_a_shell)`no`"
        env = M.app_environment(["DATABASE_USER=sub2api", "DATABASE_DBNAME=sub2api", "DATABASE_PASSWORD="+password,
            "http_proxy=http://production:8888", "UPDATE_PROXY_URL=http://production", "MIGRATION_BAD_DRIFT=true"], "t"*48)
        self.assertFalse(any("proxy" in key.lower() for key in env))
        self.assertNotIn("MIGRATION_BAD_DRIFT", env)
        self.assertEqual(env["DATABASE_PASSWORD"], password)
        self.assertIn("password='secret\\'\\\\", env["MIGRATION_POSTGRES_TARGET_DSN"])
        self.assertEqual(env["MIGRATION_BACKGROUND_WORKERS"], "false")
        self.assertEqual(env["MIGRATION_SHARED_REDIS"], "true")
        self.assertEqual(env["SERVER_HOST"], "127.0.0.1")
        self.assertEqual(env["REDIS_PASSWORD"], "")

    def test_multiline_environment_refused(self):
        with self.assertRaisesRegex(M.Refusal, "environment-value"):
            M.env_text({"DATABASE_PASSWORD": "bad\nSECOND=value"})

    def test_endpoint_checkpoint_atomic_replace_and_nonsecret_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary).resolve()/"endpoint-state"
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            initial = {"v": 1, "postgres": "source", "redis": "source"}
            M.atomic_endpoints(directory, initial)
            path = directory/"endpoints.json"
            inode = path.stat().st_ino
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            switched = {**initial, "postgres": "target"}
            M.atomic_endpoints(directory, switched, expected=initial)
            self.assertEqual(json.loads(path.read_text()), switched)
            self.assertNotEqual(path.stat().st_ino, inode)
            with self.assertRaisesRegex(M.Refusal, "phase-mismatch"):
                M.atomic_endpoints(directory, initial, expected=initial)

    def test_failed_atomic_replace_retains_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary).resolve()/"endpoint-state"
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            initial = {"v": 1, "postgres": "source", "redis": "source"}
            M.atomic_endpoints(directory, initial)
            with mock.patch.object(M.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    M.atomic_endpoints(directory, {**initial, "redis": "target"}, expected=initial)
            self.assertEqual(json.loads((directory/"endpoints.json").read_text()), initial)

    def test_checkpoint_is_persisted_before_runtime_switch_and_survives_rejection(self):
        runner = ProtocolRunner()
        with self.assertRaises(M.Refusal):
            runner.switch_app_endpoint("postgres")
        self.assertEqual(runner.endpoint_state["postgres"], "target")
        self.assertEqual(runner.calls[:2], ["checkpoint_postgres_target", "postgres/switch/target"])

    def test_private_files_stay_0600_and_refuse_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory).resolve()/"app.env"
            M.private_file(path, "secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                M.private_file(path, "replace")
            link = pathlib.Path(directory).resolve()/"link"
            link.symlink_to(path)
            with self.assertRaises(M.Refusal):
                M.private_file(link, "bad", replace=True)
            self.assertEqual(path.read_text(), "secret")

    def test_chunked_private_status_is_parsed(self):
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n{"a":1}\r\n0\r\n\r\n'
        self.assertEqual(M.parse_http(raw), (200, {"a": 1}))

    def test_truncated_control_response_fails_without_echoing_body(self):
        with self.assertRaisesRegex(M.Refusal, "incomplete-http-response") as exc:
            M.parse_http(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nsensitive")
        self.assertNotIn("sensitive", str(exc.exception))

    def test_control_token_only_travels_in_stdin(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.token = "private-control-token-"+"a"*40
        runner.ids["app"] = "a"*64
        runner.inspect = lambda kind, identifier: {"State": {"Running": True, "Pid": 4242},
            "Config": {"Labels": {M.LABEL: runner.run_id}}}
        seen = {}
        def execute(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", b"")
        runner.run = execute
        self.assertEqual(runner.http("/status")[0], 200)
        self.assertNotIn(runner.token, repr(seen["argv"]))
        self.assertIn(("Authorization: Bearer "+runner.token).encode(), seen["data"])
        self.assertEqual(seen["argv"][:6], ["nsenter", "--target", "4242", "--net", "--", "/usr/bin/python3"])
        self.assertIn("('127.0.0.1',38092)", seen["argv"][-1])

    def test_all_six_consumers_must_ack_current_epoch(self):
        M.validate_consumers(consumers(), resumed=True)
        missing = consumers()
        missing["consumers"].pop()
        with self.assertRaisesRegex(M.Refusal, "manifest-incomplete"):
            M.validate_consumers(missing, resumed=True)
        stale = consumers()
        stale["consumers"][0]["generation"] = 0
        with self.assertRaisesRegex(M.Refusal, "epoch-not-acknowledged"):
            M.validate_consumers(stale, resumed=True)

    def test_writer_loss_and_incomplete_manifests_block(self):
        M.validate_snapshot(snapshot(), quiet=True, ready=True)
        for key in ["failed", "dropped"]:
            value = snapshot()
            value["tasks"]["billing_cache"][key] = 1
            with self.assertRaisesRegex(M.Refusal, "warning-or-loss"):
                M.validate_snapshot(value)
        value = snapshot()
        value["tasks"].pop("detached_writers")
        with self.assertRaisesRegex(M.Refusal, "manifest-incomplete"):
            M.validate_snapshot(value)

    def test_active_hijacked_websocket_blocks_quiet(self):
        value = snapshot()
        value["active_hijacked_connections"] = 1
        with self.assertRaisesRegex(M.Refusal, "not-drained"):
            M.validate_snapshot(value, quiet=True)

    def test_warning_filter_is_narrow(self):
        self.assertEqual(M.warning_count(b'{"level":"warn","msg":"startup issue"}\n'), 1)
        self.assertEqual(M.warning_count(b'{"level":"info","component":"error_passthrough"}\n'), 0)
        shutdown = b"2026-10-16 13:00:00 [77] FATAL:  terminating connection due to administrator command\n"
        self.assertEqual(M.warning_count(shutdown), 1)
        self.assertEqual(M.warning_count(shutdown, expected_pg_shutdown=True, isolated_role="pg1"), 0)
        self.assertEqual(M.warning_count(b"FATAL:  database is corrupt\n", expected_pg_shutdown=True), 1)
        self.assertEqual(M.warning_count(b"WARNING:  terminating connection due to administrator command\n", expected_pg_shutdown=True), 1)

    def test_pg_shutdown_exceptions_require_verified_fence_correct_role_port_and_message(self):
        cases = [("pg1", b"FATAL:  the database system is shutting down\n"),
                 ("pg2", b"FATAL:  could not send end-of-streaming message to primary: server closed the connection unexpectedly\n"),
                 ("pg2", b'FATAL:  streaming replication receiver "walreceiver" could not connect to the primary server: connection to server at "127.0.0.1", port 35432 failed: FATAL:  the database system is shutting down\n'),
                 ("pg2", b'FATAL:  streaming replication receiver "walreceiver" could not connect to the primary server: connection to server at "127.0.0.1", port 35432 failed: Connection refused\n')]
        for role, raw in cases:
            self.assertEqual(M.warning_count(raw, isolated_role=role), 1)
            self.assertEqual(M.warning_count(raw, expected_pg_shutdown=True, isolated_role=role), 0)
            self.assertEqual(M.warning_count(raw, expected_pg_shutdown=True, isolated_role="app"), 1)
        self.assertEqual(M.warning_count(cases[-1][1].replace(b"35432", b"5432"),
                                        expected_pg_shutdown=True, isolated_role="pg2"), 1)
        self.assertEqual(M.warning_count(cases[1][1], expected_pg_shutdown=True, isolated_role="pg1"), 1)

    def test_six_source_baseline_warnings_are_exact_app_warn_only_exceptions(self):
        self.assertEqual(len(M.BASELINE_APP_WARNINGS), 6)
        for message in M.BASELINE_APP_WARNINGS:
            raw = b"timestamp\tWARN\tapp\t" + message + b"\t{}\n"
            self.assertEqual(M.warning_count(raw, isolated_role="app"), 0)
            self.assertEqual(M.warning_count(raw, isolated_role="pg1"), 1)
            self.assertEqual(M.warning_count(raw.replace(b"\tWARN\t", b"\tERROR\t"), isolated_role="app"), 1)
            self.assertEqual(M.warning_count(raw.replace(message, message+b" extra"), isolated_role="app"), 1)

    def test_clean_owned_postgres_exit_proves_fence_despite_exec_137_or_timeout(self):
        for lost in (False, True):
            runner = M.Rehearsal(PAYLOAD)
            runner.ids["pg1"] = "a"*64
            runner.pg_user = "999:999"
            before = {"Id": "a"*64, "Config": {"Labels": {M.LABEL: RUN}},
                      "HostConfig": {"RestartPolicy": {"Name": "no"}}, "State": {"Running": True}}
            after = json.loads(json.dumps(before))
            after["State"] = {"Running": False, "Status": "exited", "ExitCode": 0, "OOMKilled": False, "FinishedAt": "2026-10-16T15:24:44Z"}
            runner.inspect = mock.Mock(side_effect=[before, after])
            runner.exec = mock.Mock(side_effect=subprocess.TimeoutExpired("fixed", 3) if lost else None,
                                    return_value=subprocess.CompletedProcess([], 137, b"", b""))
            runner.write_report = mock.Mock()
            runner.fence_pg1()
            self.assertTrue(runner.pg_fence_verified)
            self.assertTrue(runner.report["postgres_source_fence"]["verified"])
            self.assertEqual(runner.report["postgres_source_fence"]["pg_ctl_exec_returncode"], None if lost else 137)
            self.assertFalse(runner.exec.call_args.kwargs["check"])
            self.assertEqual(runner.write_report.call_count, 2)

    def test_source_fence_refuses_wrong_owner_running_unclean_or_oom_exit(self):
        for change in [{"Running": True}, {"ExitCode": 137}, {"OOMKilled": True}, {"Status": "dead"}]:
            runner = M.Rehearsal(PAYLOAD); runner.ids["pg1"] = "a"*64; runner.pg_user = "999:999"
            before = {"Id": "a"*64, "Config": {"Labels": {M.LABEL: RUN}},
                      "HostConfig": {"RestartPolicy": {"Name": "no"}}, "State": {"Running": True}}
            after = json.loads(json.dumps(before))
            after["State"] = {"Running": False, "Status": "exited", "ExitCode": 0, "OOMKilled": False, **change}
            runner.inspect = mock.Mock(side_effect=[before, after]); runner.write_report = mock.Mock()
            runner.exec = mock.Mock(return_value=subprocess.CompletedProcess([], 137, b"", b""))
            with self.assertRaisesRegex(M.Refusal, "not-cleanly-fenced"):
                runner.fence_pg1()
            self.assertFalse(runner.pg_fence_verified)
        before["Config"]["Labels"][M.LABEL] = "someone-else"
        runner.inspect = mock.Mock(return_value=before); runner.exec.reset_mock()
        with self.assertRaisesRegex(M.Refusal, "refuse-fence-nonowned"):
            runner.fence_pg1()
        runner.exec.assert_not_called()

    def test_create_network_and_limits_are_bound_to_owned_container_ids(self):
        self.assertEqual(M.RUNNING_MEMORY_LIMIT, M.GIB)
        for role in M.LIMITS:
            runner = M.Rehearsal(PAYLOAD)
            parent = "a"*64
            if role not in {"anchor", "clone"}:
                runner.ids["anchor"] = parent
            runner.image_ids["test"] = "sha256:"+"b"*64
            seen = []
            runner.run = lambda argv: (seen.append(argv) or subprocess.CompletedProcess(argv, 0, ("c"*64+"\n").encode(), b""))
            network = "none" if role in {"anchor", "clone"} else "container:"+parent
            runner.inspect = lambda *args: {"Config": {"Labels": {M.LABEL: RUN}}, "Image": runner.image_ids["test"],
                "HostConfig": {"NetworkMode": network, "PortBindings": {}, "Memory": M.LIMITS[role], "MemorySwap": M.LIMITS[role]}}
            runner.write_report = lambda: None
            runner.create(role, "test", [], ["test"])
            argv = seen[0]
            self.assertEqual(argv[argv.index("--network")+1], network)
            self.assertEqual(argv[argv.index("--restart")+1], "no")
            self.assertEqual(argv[argv.index("--memory")+1], str(M.LIMITS[role]))
            self.assertNotIn("--publish", argv)
            self.assertNotIn("--privileged", argv)

    def test_namespace_anchor_must_be_owned_running_and_is_stopped_last(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.ids = {role: role+"_id" for role in M.LIMITS}
        def info(kind, identity):
            return {"Id": identity, "Config": {"Labels": {M.LABEL: RUN}},
                "State": {"Running": True},
                "HostConfig": {"NetworkMode": "none", "RestartPolicy": {"Name": "no"}}}
        runner.inspect = mock.Mock(side_effect=info)
        runner.verify_anchor()
        for change in [{"Id": "someone-else"}, {"State": {"Running": False}},
                       {"Config": {"Labels": {M.LABEL: "another-run"}}},
                       {"HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}}}]:
            runner.inspect = mock.Mock(return_value={**info("container", "anchor_id"), **change})
            with self.assertRaisesRegex(M.Refusal, "anchor-not-owned-running"):
                runner.verify_anchor()
        seen = set(); stopped = []
        def stopped_info(kind, identity):
            return {"Config": {"Labels": {M.LABEL: RUN}}, "State": {"Running": identity not in seen}}
        def stop(argv, **kwargs):
            stopped.append(argv[-1]);seen.add(argv[-1]);return subprocess.CompletedProcess(argv, 0, b"", b"")
        runner.inspect, runner.run = stopped_info, stop
        self.assertTrue(runner.stop_owned())
        self.assertEqual(stopped[-1], "anchor_id")

    def test_old_source_probe_requires_connection_refusal_in_owned_namespace(self):
        runner = M.Rehearsal(PAYLOAD);runner.ids = {"app": "a"*64}
        runner.verify_anchor = mock.Mock()
        runner.inspect = mock.Mock(return_value={"Id": "a"*64, "Config": {"Labels": {M.LABEL: RUN}},
                                                "State": {"Running": True, "Pid": 123}})
        evidence = {"pg1": "connection-refused", "redis1": "connection-refused"}
        runner.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(evidence).encode(), b""))
        self.assertEqual(runner.source_endpoints_unreachable(), evidence)
        argv = runner.run.call_args.args[0]
        self.assertEqual(argv[:6], ["nsenter", "--target", "123", "--net", "--", "/usr/bin/python3"])
        self.assertEqual(json.loads(runner.run.call_args.kwargs["data"]), {"pg1": 35432, "redis1": 36379})
        compile(argv[-1], "source-probe", "exec")
        runner.run.return_value.stdout = b'{"pg1":"timeout","redis1":"connection-refused"}'
        with self.assertRaisesRegex(M.Refusal, "source-endpoint-still-reachable"):
            runner.source_endpoints_unreachable()

    def test_no_volume_deletion_or_production_mutator_exists(self):
        source = PATH.read_text()
        for bad in ['"volume", "rm"', '"volume", "prune"', '"systemctl", "restart"', '"iptables"', '"REPLICAOF"']:
            self.assertNotIn(bad, source)
        self.assertNotIn("13.231.143.136", source)
        self.assertNotIn('["docker", "exec", "sub2api"', source)

    def test_only_immutable_owned_ids_are_stopped(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.ids = {"app": "d"*64}
        runner.inspect = lambda *args: {"Config": {"Labels": {M.LABEL: "someone-else"}}, "State": {"Running": True}}
        runner.run = mock.Mock(side_effect=AssertionError("must never stop other owner"))
        self.assertFalse(runner.stop_owned())
        runner.run.assert_not_called()

    def test_real_handover_script_order_and_confirmed_writes(self):
        runner = ProtocolRunner()
        runner.handover()
        order = runner.calls
        for before, after in [("redis/pause", "postgres/pause"), ("postgres/pause", "final_wal_replay"),
            ("final_wal_replay", "fence_pg1"), ("fence_pg1", "promote_pg2"),
            ("promote_pg2", "postgres/switch/target"), ("postgres/resume", "coordinated_redis_failover"),
            ("coordinated_redis_failover", "redis/switch/target"), ("redis/switch/target", "redis/rebuild"),
            ("redis/rebuild", "redis/resume"), ("redis/resume", "writers/resume"), ("writers/resume", "ingress/resume")]:
            # A target switch before promotion is an intentional negative check.
            first = order.index(before)
            later = max(i for i, value in enumerate(order) if value == after)
            self.assertLess(first, later, (before, after))
        for component in ["postgres", "redis"]:
            self.assertLess(order.index("checkpoint_"+component+"_target"),
                            max(i for i, v in enumerate(order) if v == component+"/switch/target"))
        self.assertEqual(runner.report["synthetic_committed_postgres_writes_retained"], 3)
        self.assertTrue(runner.report["synthetic_confirmed_redis_writes_retained"])
        self.assertFalse(runner.report["business_billing_semantics_proven"])

    def test_same_epoch_abort_requires_rebuild_before_resume(self):
        runner = ProtocolRunner()
        runner.same_epoch_abort()
        self.assertEqual(runner.redis_epoch, 1)
        self.assertEqual(runner.endpoint_state, {"v": 1, "postgres": "source", "redis": "source"})
        self.assertFalse(any(runner.state.values()))
        self.assertLess(runner.calls.index("redis/rebuild"), max(i for i, v in enumerate(runner.calls) if v == "redis/resume"))

    def test_warning_before_role_change_prevents_promotion(self):
        runner = ProtocolRunner()
        runner.assert_no_warnings = mock.Mock(side_effect=M.Refusal("isolated-runtime-warning-resources-retained"))
        with self.assertRaisesRegex(M.Refusal, "warning"):
            runner.handover()
        self.assertNotIn("promote_pg2", runner.calls)
        self.assertEqual(runner.pg_primary, "source")

    def test_10_second_requirement_cannot_be_reported_passed_when_exceeded(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.current = {"name": "too_slow", "start": 0}
        runner.queue_result = {"status": 200, "seconds": 10.5}
        runner.queue_thread = mock.Mock()
        runner.queue_thread.is_alive.return_value = False
        runner.post = mock.Mock()
        runner.step = lambda name, fn: fn()
        runner.check_ready = lambda: None
        runner.assert_no_warnings = lambda: None
        runner.get = lambda *args: {"queued_requests": 0}
        runner.write_report = lambda: None
        with mock.patch.object(M.time, "monotonic", return_value=11):
            with self.assertRaisesRegex(M.Refusal, "budget-exceeded"):
                runner.finish_scenario()
        self.assertFalse(runner.current["within_10_seconds"])
        self.assertNotIn("ok", runner.current)

    def test_failure_stops_owned_resources_and_preserves_evidence(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.prepare = mock.Mock(side_effect=M.Refusal("test-failure"))
        runner.stop_owned = mock.Mock(return_value=True)
        runner.write_report = mock.Mock()
        result = runner.execute()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "test-failure")
        runner.stop_owned.assert_called_once()
        runner.write_report.assert_called_once()
        self.assertFalse(result["real_model_first_token_latency_proven"])

    def test_restart_proves_target_selection_with_both_sources_stopped(self):
        runner = M.Rehearsal(PAYLOAD)
        runner.endpoint_state = {"v": 1, "postgres": "target", "redis": "target"}
        runner.ids = {role: role+"_id" for role in ["pg1", "redis1", "app", "anchor"]}
        runner.inspect = lambda *args: {"State": {"Running": False}}
        calls = []
        runner.run = lambda argv, **kwargs: (calls.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b""))
        runner.start = lambda role: calls.append(["start", role])
        runner.wait = lambda label, fn, **kwargs: self.assertTrue(fn())
        runner.http = lambda *args, **kwargs: (200, {})
        runner.check_ready = lambda: None
        runner.assert_no_warnings = lambda: None
        runner.verify_anchor = mock.Mock()
        runner.source_endpoints_unreachable = mock.Mock(return_value={"pg1": "connection-refused", "redis1": "connection-refused"})
        runner.get = lambda component: {"endpoint": "target"} if component == "postgres" else consumers()
        runner.sql = lambda *args: "3"
        runner.restart_on_persisted_targets()
        self.assertEqual(calls[0][-1], "redis1_id")
        self.assertEqual(calls[1][-1], "app_id")
        self.assertEqual(calls[2], ["start", "app"])
        self.assertTrue(runner.report["restart_on_persisted_targets"]["both_sources_stopped"])
        self.assertEqual(runner.source_endpoints_unreachable.call_count, 2)
        self.assertEqual(len(runner.report["restart_on_persisted_targets"]["redis_consumers"]), 6)
        self.assertEqual(runner.report["restart_on_persisted_targets"]["postgres_endpoint"], "target")
        self.assertFalse(runner.report["restart_on_persisted_targets"]["counts_toward_cutover_10_seconds"])

    def test_remote_worker_source_compiles_and_skips_local_cli(self):
        source = M.remote_source(PAYLOAD)
        compile(source, "remote-worker", "exec")
        self.assertTrue(source.startswith("__name__ = 'isolated_remote_worker'"))
        self.assertTrue(source.rstrip().endswith("remote_main("+repr(PAYLOAD)+")"))


if __name__ == "__main__":
    unittest.main()
