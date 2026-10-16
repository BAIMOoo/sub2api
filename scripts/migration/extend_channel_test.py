"""Offline migration-channel contract tests. No SSH/Docker/firewall is invoked."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import shlex
import stat
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("channel", pathlib.Path(__file__).with_name("extend_channel.py"))
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
KEY = "ssh-ed25519 "+base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20"+b"k"*32).decode()+" "+m.KEY_COMMENT


def fixture():
    unit = m.original_unit("172.18.0.3")
    source = {"machine_id": "source-machine", "unit": unit, "stored_unit": unit, "unit_sha256": m.digest(unit),
              "public_key": KEY, "pg_ip": "172.18.0.3", "channel_active": True,
              "listeners": [["127.0.0.1", 18081]], "api_status": 200,
              "replicas_absent": True, "firewall_marker_absent": True, "bridge_interface": "br-0123456789ab",
              "firewall_rules": m.firewall_rules("br-0123456789ab"), "firewall_sha256": "firewall-before"}
    auth = "# unrelated existing key\nssh-ed25519 OTHER unrelated\n"+m.key_line(KEY)+"\n"
    target = {"machine_id": "target-machine", "authorized_keys": auth, "authorized_keys_sha256": m.digest(auth),
              "listeners": [["127.0.0.1", 15432], ["127.0.0.1", 16379], ["127.0.0.1", 8082]], "api_status": 200,
              "pg_forward_ok": True, "redis_forward_ok": True, "standby_containers_absent": True}
    return source, target


class ChannelExtensionTests(unittest.TestCase):
    def test_default_and_restore_dry_run_do_not_initialize_controller(self):
        for args in [[], ["--restore"]]:
            with self.subTest(args=args), mock.patch.object(m, "Controller", side_effect=AssertionError("dry run initialized SSH")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(m.main(args), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result["remote_commands"], 0)
                self.assertFalse(result["business_services_modified"])

    def test_inspection_cannot_authorize_mutation(self):
        for args in [["--inspect", "--apply"], ["--inspect", "--restore"]]:
            with self.subTest(args=args), mock.patch.object(m, "Controller", side_effect=AssertionError("must not SSH")):
                with self.assertRaisesRegex(m.Refusal, "inspect-cannot"):
                    m.main(args)

    def test_inspect_is_read_only_and_hides_full_authorized_keys_and_unit(self):
        source, target = fixture()
        with mock.patch.object(m.Controller, "remote", side_effect=[target, source]) as remote, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(["--inspect"]), 0)
        self.assertEqual([call.args for call in remote.call_args_list], [("target", "inspect"), ("source", "inspect")])
        result = json.loads(output.getvalue())
        self.assertTrue(result["eligible_for_extension"])
        self.assertNotIn("authorized_keys", result["target"])
        self.assertNotIn("unit", result["source"])
        self.assertNotIn(KEY, output.getvalue())

    def test_extension_preserves_existing_forward_argv_and_unit_protections(self):
        def argv(unit):
            return shlex.split(next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("ExecStart=")))
        old = argv(m.original_unit("172.18.0.3")); new_unit = m.extended_unit("172.18.0.3"); new = argv(new_unit)
        self.assertEqual(new[:-7], old[:-1])
        self.assertEqual(new[-7:], ["-L", m.ADDED[0], "-L", m.ADDED[1], "-L", m.ADDED[2], old[-1]])
        self.assertEqual(new.count("-R"), 2)
        self.assertEqual(new.count("-L"), 4)
        for line in ["User=ubuntu", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only"]:
            self.assertIn(line+"\n", new_unit)
        self.assertLess(new_unit.index("ExecStartPre=+"), new_unit.index("ExecStart="))
        self.assertNotIn("caddy", new_unit.lower())

    def test_key_extension_changes_exactly_one_owned_line_and_preserves_restrictions(self):
        source, target = fixture()
        changed = m.replace_key(target["authorized_keys"], KEY)
        self.assertEqual(changed.splitlines()[:2], target["authorized_keys"].splitlines()[:2])
        line = changed.splitlines()[-1]
        for part in ['from="13.231.143.136"', "restrict", "port-forwarding", 'command="/bin/false"',
                     'permitopen="127.0.0.1:8082"', 'permitlisten="127.0.0.1:15432"', 'permitlisten="127.0.0.1:16379"']:
            self.assertIn(part, line)
        self.assertEqual(line.count("permitopen="), 4)
        self.assertEqual(line.count("permitlisten="), 2)
        self.assertEqual(m.replace_key(changed, KEY), changed)

    def test_restore_preserves_new_unrelated_keys_and_original_newline(self):
        original = m.key_line(KEY)  # No terminal newline is intentional.
        changed = "ssh-ed25519 NEW another-key\n"+m.replace_key(original, KEY)
        expected = "ssh-ed25519 NEW another-key\n"+original
        self.assertEqual(m.replace_key(changed, KEY, restore=True), expected)
        self.assertEqual(m.replace_key(expected, KEY, restore=True), expected)

    def test_drifted_forced_command_duplicate_key_and_invalid_blob_refused(self):
        cases = [m.key_line(KEY).replace('command="/bin/false"', 'command="/bin/sh"'), m.key_line(KEY)+"\n"+m.key_line(KEY), "ssh-ed25519 OTHER missing"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(m.Refusal):
                m.replace_key(text, KEY)
        with self.assertRaises(m.Refusal):
            m.key_line("ssh-ed25519 Zm9v "+m.KEY_COMMENT)

    def test_firewall_has_one_precise_bridge_allow_then_catchall_port_reject(self):
        allow, deny = m.firewall_rules("br-0123456789ab")
        self.assertEqual(allow[allow.index("-i")+1], "br-0123456789ab")
        self.assertEqual(allow[allow.index("-s")+1], "172.18.0.2/32")
        for rule in [allow, deny]:
            self.assertEqual(rule[rule.index("-d")+1], "172.18.0.1/32")
            self.assertEqual(rule[rule.index("--dport")+1], "26379")
            self.assertIn(m.RUN, rule[rule.index("--comment")+1])
        self.assertNotIn("-s", deny)
        self.assertEqual(allow[-1], "ACCEPT")
        self.assertEqual(deny[deny.index("-j")+1], "REJECT")
        with self.assertRaises(m.Refusal):
            m.firewall_rules("br0; touch /tmp/injected")

    def test_boot_guard_compiles_and_does_not_flush_or_rewrite_other_rules(self):
        program = m.firewall_program("br-0123456789ab", "a"*32)
        compile(program, "root-firewall-guard", "exec")
        self.assertIn('probe.bind(("172.18.0.1",26379))', program)
        self.assertIn('for rule in reversed(RULES)', program)
        self.assertNotIn('"-F"', program)
        self.assertNotIn("iptables-restore", program)
        self.assertNotIn("authorized_keys", program)

    def test_eligibility_rejects_active_replication_port_collision_and_old_forward_failure(self):
        changes = [("source", "replicas_absent", False), ("target", "standby_containers_absent", False),
                   ("target", "pg_forward_ok", False), ("target", "redis_forward_ok", False),
                   ("source", "listeners", [["127.0.0.1", 18081], ["0.0.0.0", 26379]]),
                   ("source", "api_status", 502), ("source", "firewall_marker_absent", False)]
        for side, key, value in changes:
            with self.subTest(side=side, key=key):
                source, target = fixture(); (source if side == "source" else target)[key] = value
                with self.assertRaises(m.Refusal):
                    m.validate_before(source, target)

    def test_missing_target_api_is_explicitly_allowed_before_app_preparation(self):
        source, target = fixture()
        source["api_status"] = target["api_status"] = None
        self.assertTrue(m.validate_before(source, target))

    def test_failure_restores_source_before_key_and_retains_private_exact_backups(self):
        source, target = fixture()
        with tempfile.TemporaryDirectory() as directory:
            controller = m.Controller(pathlib.Path(directory).resolve()/"new-local-state")
            controller.inspect = lambda: (source, target)
            calls = []
            def remote(side, action, **payload):
                calls.append((side, action))
                if (side, action) == ("source", "prepare"):
                    raise m.Refusal("CANARY_PRIVATE_FAILURE")
                return {"restored": True}
            controller.remote = remote; controller.verify = lambda *args, **kwargs: {"verified": True}
            with self.assertRaisesRegex(m.Refusal, "original-channel-restored") as raised:
                controller.apply()
            self.assertNotIn("CANARY", str(raised.exception))
            self.assertEqual(calls, [("target", "prepare"), ("source", "prepare"), ("source", "restore"), ("target", "restore")])
            self.assertEqual((controller.directory/"unit.before").read_text(), source["unit"])
            self.assertEqual((controller.directory/"authorized_keys.before").read_text(), target["authorized_keys"])
            self.assertEqual(stat.S_IMODE(controller.directory.stat().st_mode), 0o700)
            for file in controller.directory.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_failed_source_restore_keeps_target_key_permissions_for_recovery(self):
        source, target = fixture()
        with tempfile.TemporaryDirectory() as directory:
            controller = m.Controller(pathlib.Path(directory).resolve()); calls = []
            def remote(side, action, **payload):
                calls.append((side, action)); raise m.Refusal("source-unit-drifted")
            controller.remote = remote
            with self.assertRaises(m.Refusal):
                controller.restore({"owner": "a"*32, "source": source, "target": target})
            self.assertEqual(calls, [("source", "restore")])

    def test_target_management_path_is_independent_of_aws(self):
        self.assertIn("-oProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p", m.TARGET)
        self.assertIn("-oProxyJump=none", m.TARGET)
        self.assertLess(m.TARGET.index("-oProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p"), m.TARGET.index("-oProxyJump=none"))
        self.assertNotIn("sub2api", m.TARGET)

    def test_remote_stderr_is_never_disclosed(self):
        controller = m.Controller(pathlib.Path("/tmp/no-write-needed").resolve())
        response = types.SimpleNamespace(returncode=1, stdout="not-json", stderr="CANARY_PRIVATE_KEY_MATERIAL")
        with mock.patch.object(m.subprocess, "run", return_value=response):
            with self.assertRaises(m.Refusal) as raised:
                controller.remote("target", "inspect")
        self.assertNotIn("CANARY", str(raised.exception))

    def test_generated_remote_program_compiles_without_import_order_conflict(self):
        compile("REMOTE_PAYLOAD="+repr({"side": "source", "action": "inspect"})+"\n"+pathlib.Path(m.__file__).read_text(), "remote-channel-program", "exec")


if __name__ == "__main__":
    unittest.main()
