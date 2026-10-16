#!/usr/bin/env python3
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import types
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("nat_rehearsal", pathlib.Path(__file__).with_name("rolling_app_nat_rehearsal.py"))
N = importlib.util.module_from_spec(spec)
spec.loader.exec_module(N)


def network():
    return {"addresses": [{"ifname": "lo", "link_type": "loopback", "addr_info": [{"local": "127.0.0.1"}]}], "routes": [], "routes6": []}


def evidence():
    return {"ok": True, "isolated": True, "network": network(), "production_rules_touched": False,
            "scenario": "warm-current", "conntrack_activated_before_clients": True,
            "conntrack_before_route": tracked(41000) + tracked(41001), "stream_local_port": 41000,
            "production_application_tested": False, "new_uid1000": {"backend": "new", "uid": 1000, "first_byte_seconds": 0.1},
            "new_uid0": {"backend": "old"}, "other_destination": {"backend": "old"}, "other_port": {"backend": "old"},
            "stream": {"done": True, "values": ["old:" + str(i) for i in range(N.FRAMES)], "max_frame_gap": 0.04},
            "frames_after_rule": 20, "idle_before": {"backend": "old", "local_port": 41001},
            "idle_after": {"backend": "old", "same_local_port": True}, "old_tcp_during_stream": 2,
            "old_tcp_after_stream_before_idle_close": 1, "old_tcp_final": 0,
            "old_sigterm_count": 1, "old_exit_code": 0, "nat_other_rules_unchanged": True,
            "quiet_seconds": 2.1, "quiet_samples": 43}


def tracked(port):
    return f"ipv4 2 tcp 6 431999 ESTABLISHED src=127.0.0.1 dst=127.0.0.1 sport={port} dport=8082 src=127.0.0.1 dst=127.0.0.1 sport=8082 dport={port} [ASSURED] mark=0 use=1\n"


class RehearsalTests(unittest.TestCase):
    def test_default_is_offline_without_process_or_docker(self):
        with mock.patch.object(N.subprocess, "run", side_effect=AssertionError("offline")), \
             mock.patch.object(N.subprocess, "Popen", side_effect=AssertionError("offline")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(N.main([]), 0)
        self.assertTrue(json.loads(out.getvalue())["offline"])

    def test_run_requires_opt_in_and_immutable_image(self):
        with self.assertRaisesRegex(N.Refusal, "RUN_FLAG_REQUIRED"):
            N.main(["--image", "latest"])
        with self.assertRaisesRegex(N.Refusal, "IMMUTABLE_IMAGE_REQUIRED"):
            N.launch_argv("docker", "latest", "/tmp", "sub2api-nat-rehearsal-" + "0" * 16)

    def test_launcher_has_private_namespace_minimal_caps_and_readonly_mount(self):
        name = "sub2api-nat-rehearsal-" + "0" * 16
        argv = N.launch_argv("docker", "sha256:" + "1" * 64, "/tmp/scripts", name)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--cap-add"], ["NET_ADMIN", "SETUID", "SETGID"])
        self.assertIn("type=bind,src=/tmp/scripts,dst=/rehearsal,readonly", argv)
        self.assertIn(N.LABEL + "=" + name, argv)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("--publish", argv)
        self.assertNotIn("-p", argv)
        self.assertNotIn("--pid", argv)
        self.assertIn("--read-only", argv)

    def test_guard_refuses_host_and_non_loopback_namespace(self):
        with mock.patch.object(N.sys, "platform", "darwin"), self.assertRaises(N.Refusal):
            N.guard(coordinator=True)
        state = network()
        state["addresses"].append({"ifname": "eth0", "link_type": "ether", "operstate": "DOWN", "flags": [], "addr_info": []})
        with self.assertRaisesRegex(N.Refusal, "LOOPBACK_ONLY_NAMESPACE_REQUIRED"):
            N.validate_network(state)

    def test_dormant_kernel_fallback_allowed_only_without_address_or_route(self):
        link = {"ifname": "gre0", "link_type": "gre", "operstate": "DOWN", "flags": ["NOARP"], "addr_info": []}
        state = network()
        state["addresses"].append(link)
        N.validate_network(state)
        for key, value in [("operstate", "UP"), ("flags", ["UP"]), ("addr_info", [{"local": "1.2.3.4"}]), ("link_type", "ether")]:
            bad = copy.deepcopy(state)
            bad["addresses"][1][key] = value
            with self.subTest(key=key), self.assertRaises(N.Refusal):
                N.validate_network(bad)
        state["routes"].append({"dev": "gre0", "type": "unicast", "table": "main", "dst": "default"})
        with self.assertRaises(N.Refusal):
            N.validate_network(state)

    def test_coordinator_requires_root_pid1(self):
        with mock.patch.object(N.sys, "platform", "linux"), mock.patch.dict(N.os.environ, {N.MARKER: "1"}), \
             mock.patch.object(N.pathlib.Path, "is_file", return_value=True), \
             mock.patch.object(N.pathlib.Path, "iterdir", return_value=[pathlib.Path("lo")]), \
             mock.patch.object(N.os, "getpid", return_value=2), mock.patch.object(N.os, "getuid", return_value=0), \
             self.assertRaisesRegex(N.Refusal, "ISOLATED_ROOT_PID1_REQUIRED"):
            N.guard(coordinator=True)

    def test_complete_proof_passes(self):
        N.validate_evidence(evidence())

    def test_conntrack_requires_assured_established_exact_origin_tuple(self):
        self.assertEqual(N.tracked_client_ports(tracked(41000) + tracked(41001)), {41000, 41001})
        for raw in (tracked(41000).replace("ESTABLISHED", "SYN_SENT"), tracked(41000).replace("[ASSURED]", ""),
                    tracked(41000).replace("dport=8082", "dport=8083"), tracked(41000).replace("src=127.0.0.1", "src=127.0.0.2", 1)):
            self.assertEqual(N.tracked_client_ports(raw), set())
        candidate = evidence()
        candidate["conntrack_before_route"] = tracked(41000)
        with self.assertRaisesRegex(N.Refusal, "EXISTING_CONNECTIONS_NOT_TRACKED"):
            N.validate_evidence(candidate)

    def test_cold_syn_proof_requires_no_preexisting_tracking_and_exact_flags(self):
        candidate = evidence()
        candidate.update(scenario="cold-syn", conntrack_activated_before_clients=False, conntrack_before_route="",
                         rule=N.scenario_rule(N.load("rolling_app_recovery").REDIRECT, "cold-syn"))
        N.validate_evidence(candidate)
        for field, value in [("conntrack_before_route", tracked(41000)), ("conntrack_activated_before_clients", True), ("rule", [])]:
            bad = copy.deepcopy(candidate)
            bad[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(N.Refusal, "UNTRACKED_SYN_PROOF_MISSING"):
                N.validate_evidence(bad)

    def test_incomplete_or_overbroad_proof_fails(self):
        mutations = [("production_rules_touched", True), ("production_application_tested", True),
                     ("quiet_seconds", 1.9), ("quiet_samples", 2), ("frames_after_rule", 0),
                     ("old_tcp_during_stream", 1), ("old_tcp_after_stream_before_idle_close", 0),
                     ("old_tcp_final", 1), ("old_sigterm_count", 2), ("nat_other_rules_unchanged", False)]
        for field, value in mutations:
            candidate = evidence()
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(N.Refusal):
                N.validate_evidence(candidate)
        for field, subfield, value in [("new_uid0", "backend", "new"), ("other_destination", "backend", "new"),
                                        ("other_port", "backend", "new"), ("idle_after", "same_local_port", False),
                                        ("stream", "done", False), ("stream", "max_frame_gap", 1.1)]:
            candidate = evidence()
            candidate[field][subfield] = value
            with self.subTest(field=field, subfield=subfield), self.assertRaises(N.Refusal):
                N.validate_evidence(candidate)

    def test_cleanup_can_only_remove_exact_owned_container_id(self):
        name, cid = "sub2api-nat-rehearsal-" + "0" * 16, "a" * 64
        metadata = [{"Id": cid, "Name": "/" + name, "Config": {"Labels": {N.LABEL: name}}}]
        responses = [types.SimpleNamespace(returncode=0, stdout=json.dumps(metadata).encode()), types.SimpleNamespace(returncode=0)]
        with mock.patch.object(N.subprocess, "run", side_effect=responses) as run:
            N.cleanup_own_container("docker", name)
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", cid])
        for field in ("Name", "Config"):
            wrong = copy.deepcopy(metadata)
            wrong[0][field] = "/different" if field == "Name" else {"Labels": {N.LABEL: "other"}}
            with mock.patch.object(N.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(wrong).encode())) as run, \
                 self.assertRaisesRegex(N.Refusal, "REHEARSAL_CLEANUP_IDENTITY_MISMATCH"):
                N.cleanup_own_container("docker", name)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
