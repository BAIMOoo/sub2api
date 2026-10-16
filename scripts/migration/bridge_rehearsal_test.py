"""Local unit tests only: all remote/system-mutating commands are mocked."""

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("bridge_rehearsal", pathlib.Path(__file__).with_name("bridge_rehearsal.py"))
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
RUN_ID = "0123456789abcdef"


def result(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class BridgeRehearsalTests(unittest.TestCase):
    def test_absent_container_supports_both_docker_error_formats(self):
        for error in ("Error response from daemon: No such container: test-run\n", "error: no such object: test-run\n"):
            with mock.patch.object(bridge, "command", return_value=result("[]\n", 1, error)) as command:
                self.assertIsNone(bridge.container_info("test-run"))
                self.assertEqual(command.call_args.args[0], ["docker", "container", "inspect", "test-run"])
        with mock.patch.object(bridge, "command", return_value=result("", 1, "permission denied")):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect"):
                bridge.container_info("test-run")

    def test_default_dry_run_never_invokes_a_command(self):
        with mock.patch.object(bridge.sys, "argv", ["bridge_rehearsal.py"]), \
             mock.patch.object(bridge, "command", side_effect=AssertionError("remote command in dry-run")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(bridge.main(), 0)
            self.assertFalse(json.loads(output.getvalue())["remote_commands_executed"])

    def test_only_safe_fixed_ports_are_used(self):
        bridge.safety_invariants()
        self.assertTrue(bridge.PROTECTED_PORTS.isdisjoint({bridge.SOURCE_PUBLISHED, bridge.CADDY_LISTENER, bridge.BRIDGE_LISTENER, bridge.TARGET_PUBLISHED, bridge.CONTAINER_LISTENER}))
        config = bridge.caddy_config()
        self.assertIn("admin off", config)
        self.assertIn("http://127.0.0.1:28080", config)
        self.assertIn("reverse_proxy 127.0.0.1:28081", config)
        self.assertIn("keepalive off", config)
        for port in bridge.PROTECTED_PORTS:
            self.assertNotIn(":" + str(port) + " ", config)

    def test_nat_rule_is_uid_destination_new_tcp_and_run_scoped(self):
        rule = bridge.nat_rule(998, RUN_ID)
        self.assertEqual(rule, ["-p", "tcp", "-d", "127.0.0.1/32", "--dport", "28081",
                                "-m", "owner", "--uid-owner", "998", "-m", "conntrack", "--ctstate", "NEW",
                                "-m", "comment", "--comment", "sub2api-bridge-" + RUN_ID,
                                "-j", "REDIRECT", "--to-ports", "18081"])
        with self.assertRaises(ValueError):
            bridge.nat_rule(0, RUN_ID)
        with mock.patch.object(bridge, "command", return_value=result()) as command:
            bridge.iptables("-I", rule)
            self.assertEqual(command.call_args.args[0], ["iptables", "-w", "5", "-t", "nat", "-I", "OUTPUT", "1"] + rule)
            bridge.iptables("-D", rule)
            self.assertEqual(command.call_args.args[0], ["iptables", "-w", "5", "-t", "nat", "-D", "OUTPUT"] + rule)
            with self.assertRaises(ValueError):
                bridge.iptables("-F", rule)

    def test_rejects_arbitrary_resource_identity(self):
        for value in ("", "production", "../caddy", RUN_ID + "; reboot"):
            with self.assertRaises(ValueError):
                bridge.run_names(value, "source")
        self.assertNotEqual(bridge.run_names(RUN_ID, "source")["caddy_unit"], "caddy.service")

    def test_existing_image_and_exact_loopback_container_publication(self):
        for role, port in (("source", 28081), ("target", 8082)):
            state = {"role": role, "run_id": RUN_ID, "image": "approved-existing-image"}
            scope = pathlib.Path("/var/tmp/sub2api-bridge-rehearsal-" + RUN_ID)
            @contextlib.contextmanager
            def resource_context(_scope):
                yield scope, state
            with mock.patch.object(bridge, "resources", resource_context), \
                 mock.patch.object(bridge, "owned_container", return_value=None), \
                 mock.patch.object(bridge, "check_port_free"), mock.patch.object(bridge, "json_write"), \
                 mock.patch.object(bridge, "command", side_effect=[result("sha256:" + "a" * 64), result("b" * 64)]) as command:
                bridge.start_container(scope)
                args = command.call_args.args[0]
                self.assertIn("--pull=never", args)
                self.assertIn("--read-only", args)
                self.assertIn("--restart=no", args)
                self.assertIn("--cap-drop=ALL", args)
                self.assertIn("--memory=64m", args)
                self.assertIn(f"127.0.0.1:{port}:8080", args)
                self.assertIn(bridge.LABEL + "=" + RUN_ID, args)
                self.assertIn("readonly", args[args.index("--mount") + 1])
                self.assertEqual(state["test_container_id"], "b" * 64)

    def test_cleanup_refuses_unowned_container(self):
        state = {"role": "target", "run_id": RUN_ID}
        item = {"Id": "production-id", "Config": {"Labels": {bridge.LABEL: "another-run"}}}
        with mock.patch.object(bridge, "container_info", return_value=item):
            with self.assertRaisesRegex(RuntimeError, "another owner"):
                bridge.owned_container(state)

    def test_source_cleanup_never_stops_production_pid(self):
        baseline = {"caddy_main_pid": "1284574", "caddy_active": "active", "containers": {}}
        state = {"role": "source", "run_id": RUN_ID, "caddy_uid": 998, "baseline": baseline}
        with tempfile.TemporaryDirectory() as directory:
            scope = pathlib.Path(directory)
            @contextlib.contextmanager
            def resource_context(_scope, **_kwargs):
                yield scope, state
            def property_value(_unit, name):
                return "Sub2API isolated bridge Caddy " + RUN_ID if name == "Description" else "1284574"
            with mock.patch.object(bridge, "resources", resource_context), \
                 mock.patch.object(bridge, "iptables", return_value=result(returncode=1)), \
                 mock.patch.object(bridge, "unit_property", side_effect=property_value), \
                 mock.patch.object(bridge, "owned_container", return_value=None), \
                 mock.patch.object(bridge, "production_snapshot", return_value=baseline), \
                 mock.patch.object(bridge, "command") as command:
                report = bridge.cleanup(scope)
                self.assertTrue(any("collides" in error for error in report["errors"]))
                self.assertTrue(report["production_unchanged"])
                command.assert_not_called()

    def test_source_cleanup_deletes_only_exact_rule_and_owned_id(self):
        baseline = {"caddy_main_pid": "1284574", "caddy_active": "active", "containers": {}}
        state = {"role": "source", "run_id": RUN_ID, "caddy_uid": 998, "baseline": baseline}
        with tempfile.TemporaryDirectory() as directory:
            scope = pathlib.Path(directory)
            @contextlib.contextmanager
            def resource_context(_scope, **_kwargs):
                yield scope, state
            def property_value(_unit, name):
                return "Sub2API isolated bridge Caddy " + RUN_ID if name == "Description" else "456789"
            with mock.patch.object(bridge, "resources", resource_context), \
                 mock.patch.object(bridge, "iptables", side_effect=[result(), result(), result(returncode=1)]) as iptables, \
                 mock.patch.object(bridge, "unit_property", side_effect=property_value), \
                 mock.patch.object(bridge, "owned_container", return_value={"Id": "owned-test-id"}), \
                 mock.patch.object(bridge, "production_snapshot", return_value=baseline), \
                 mock.patch.object(bridge, "command", return_value=result()) as command:
                report = bridge.cleanup(scope)
                self.assertEqual(report["errors"], [])
                self.assertEqual(iptables.call_args_list[1].args, ("-D", bridge.nat_rule(998, RUN_ID)))
                self.assertEqual(command.call_args_list[0].args[0], ["systemctl", "stop", bridge.run_names(RUN_ID, "source")["caddy_unit"]])
                self.assertEqual(command.call_args_list[1].args[0], ["docker", "rm", "--force", "owned-test-id"])

    def test_caddy_uses_private_home_and_xdg_directories(self):
        state = {"role": "source", "run_id": RUN_ID, "timeout": 240}
        with tempfile.TemporaryDirectory() as directory:
            scope = pathlib.Path(directory)
            @contextlib.contextmanager
            def resource_context(_scope):
                yield scope, state
            def run(args, **_kwargs):
                return result("v2.6.2 test" if args == ["caddy", "version"] else "")
            with mock.patch.object(bridge, "resources", resource_context), mock.patch.object(bridge, "check_port_free"), \
                 mock.patch.object(bridge.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=998, pw_gid=998)), \
                 mock.patch.object(bridge.os, "chown"), mock.patch.object(bridge, "unit_property", return_value="456789"), \
                 mock.patch.object(bridge, "command", side_effect=run) as command:
                bridge.start_caddy(scope)
                args = next(call.args[0] for call in command.call_args_list if call.args[0][0] == "systemd-run")
                self.assertIn("--property=User=caddy", args)
                self.assertIn("--setenv=XDG_CONFIG_HOME=" + str(scope / "caddy-config"), args)
                self.assertIn("--setenv=XDG_DATA_HOME=" + str(scope / "caddy-data"), args)
                self.assertIn("--setenv=HOME=" + str(scope / "caddy-home"), args)

    def test_production_snapshot_does_not_expose_environment(self):
        item = {"Name": "/sub2api", "Id": "production-id", "Config": {"Labels": None, "Env": ["SECRET=not-for-report"]},
                "State": {"Running": True, "Health": {"Status": "healthy"}}}
        with mock.patch.object(bridge, "command", side_effect=[result("production-id"), result(json.dumps([item]))]), \
             mock.patch.object(bridge, "unit_property", side_effect=["1284574", "active"]):
            snapshot = bridge.production_snapshot(RUN_ID)
            self.assertNotIn("SECRET", json.dumps(snapshot))
            self.assertEqual(snapshot["containers"]["sub2api"]["id"], "production-id")

    def test_watchdog_deadline_always_runs_cleanup(self):
        with mock.patch.object(bridge, "scope_state", return_value=(None, {"expires_at": 0})), \
             mock.patch.object(bridge.signal, "signal"), mock.patch.object(bridge, "cleanup") as cleanup:
            bridge.watchdog("ignored-test-scope")
            cleanup.assert_called_once_with("ignored-test-scope")

    def test_websocket_frames_are_standard_masked_client_frames(self):
        sock = mock.Mock()
        with mock.patch.object(bridge.os, "urandom", return_value=b"abcd"):
            bridge.ws_send(sock, 1, b"echo")
        wire = sock.sendall.call_args.args[0]
        self.assertEqual(wire[:2], b"\x81\x84")
        self.assertEqual(bytes(value ^ wire[2 + i % 4] for i, value in enumerate(wire[6:])), b"echo")
        opcode, payload = bridge.ws_read(sock, io.BytesIO(b"\x89\x01p\x81\x04echo"))
        self.assertEqual((opcode, payload), (1, b"echo"))

    def test_apply_requires_explicit_images_before_any_remote_command(self):
        with mock.patch.object(bridge, "command", side_effect=AssertionError("must not run")):
            with self.assertRaisesRegex(RuntimeError, "explicitly approved"):
                bridge.apply(types.SimpleNamespace(source_image=None, target_image=None))


if __name__ == "__main__":
    unittest.main()
