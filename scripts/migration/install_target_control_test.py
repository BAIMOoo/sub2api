"""Offline inert-installer boundary, replay, SSH-policy and adoption tests."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("install_target_control", pathlib.Path(__file__).with_name("install_target_control.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
WIRE = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + b"A" * 32
KEY = {"algorithm": "ssh-ed25519", "base64": base64.b64encode(WIRE).decode(), "sha256": m.sha(WIRE)}
OWNER = "a" * 32
PG_OWNER = "b" * 32
REDIS_OWNER = "c" * 32
PROGRAM_BYTES = b"# approved test program\n"


def approval():
    return {"v": 1, "run": m.RUN, "programs": {name: m.sha(PROGRAM_BYTES) for name in m.PROGRAMS},
            "source_runtime_sha256": m.RUNTIME_SHA256, "initial_manifest_sha256": "b" * 64,
            "redis_ownership_sha256": "c" * 64, "public_key": copy.deepcopy(KEY)}


class FakeBackend:
    def __init__(self):
        self.files = {m.PROGRAM_STAGE / name: PROGRAM_BYTES for name in m.PROGRAMS}
        self.files[m.AUTHORIZED] = b"# existing root access\nssh-ed25519 unrelated-key administrator\n"
        self.events = []
        self.fail_after = None
        self.override = False
        self.self_bytes = PROGRAM_BYTES
        self.policy = {"permitrootlogin": "prohibit-password", "pubkeyauthentication": "yes", "authenticationmethods": "any", "authorizedkeysfile": ".ssh/authorized_keys"}
        self.resources = {}

    def root(self):
        self.events.append(("root-check",))

    def machine(self):
        return "d" * 64

    def ssh_policy(self):
        self.events.append(("policy-check",))
        return self.policy

    def self_source(self):
        return self.self_bytes

    def read(self, path, *, staged=False, mode=0o600, missing=False):
        self.events.append(("read", path))
        if path not in self.files:
            m.need(missing, "FILE_MISSING")
            return None
        return self.files[path]

    def write(self, path, raw, *, mode=0o600, replace=False, expected=None):
        previous = self.files.get(path)
        if expected is not None:
            m.need((previous or b"") == expected, "CONCURRENT_FILE_CHANGE")
        if not replace and previous is not None:
            m.need(previous == raw, "EXISTING_FILE_DIFFERS")
            return
        self.files[path] = raw
        self.events.append(("write", path, mode))
        if self.fail_after == path:
            self.fail_after = None
            raise m.Unknown("injected-write-response-loss")

    def directory(self, path, **fields):
        self.events.append(("directory", path))

    @contextlib.contextmanager
    def lock(self):
        yield

    def command(self, argv, data=None):
        self.events.append(tuple(argv))
        if argv[:3] == ["/usr/bin/systemctl", "show", m.GUARD + ".timer"] and "ActiveState" in argv[-1]:
            return ("ActiveState=active\nUnitFileState=enabled\nJob=\nDropInPaths=\nFragmentPath=/etc/systemd/system/" + m.GUARD + ".timer\n").encode()
        if argv[:2] == ["/usr/bin/systemctl", "show"]:
            return ("LoadState=loaded\nDropInPaths=" + ("/etc/evil.conf" if self.override else "") + "\nFragmentPath=/etc/systemd/system/" + argv[2] + "\n").encode()
        if argv[:2] == ["/usr/bin/systemctl", "daemon-reload"] or argv[:3] == ["/usr/bin/systemctl", "enable", "--now"]:
            return b""
        if argv[:2] == ["/usr/bin/docker", "exec"]:
            return b"run_id=invalid"
        raise AssertionError("Unexpected mutation command: " + repr(argv))

    def inspect(self, kind, name):
        return copy.deepcopy(self.resources[(kind, name)])


class MinimalInstaller(m.Installer):
    """Real install lifecycle, replace only read-only Docker adoption details."""
    def _rpc_config(self, state, adoption, rpc):
        self.b.events.append(("resource-adoption",))
        state["adopted"] = {"app_container_id": "1" * 64, "pg_container_id": "2" * 64,
                            "redis_container_id": "3" * 64, "rpc_config_sha256": "4" * 64}
        return {"v": 1, "run": m.RUN, "owner": state["owner"]}


def fake_prep(backend):
    def load_inputs():
        return m.strict_json(backend.files[m.PREP / "config.json"]), {}, {}
    def prepare(config, runtime, payload, docker):
        backend.events.append(("create-inert",))
        backend.files[m.PREP / "app-control.token"] = b"a" * 48 + b"\n"
        return {"owner": config["owner"]}
    return types.SimpleNamespace(load_inputs=load_inputs, prepare=prepare, Docker=lambda: None)


class InstallerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.a = approval()
        self.installer = MinimalInstaller(self.a, self.b)
        self.loader = mock.patch.object(m, "load_reviewed", side_effect=lambda raw, name: fake_prep(self.b) if name == "prepare_live_app.py" else object())
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def assert_refuses(self, code, function):
        with self.assertRaises(m.Refusal) as error:
            function()
        self.assertEqual(str(error.exception), code)

    def test_default_is_offline_and_does_not_consume_stdin(self):
        stream = mock.Mock()
        with mock.patch.object(m, "Installer", side_effect=AssertionError("must be offline")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main([], stream), 0)
        stream.read.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["production_actions"])

    def test_module_subprocess_is_offline(self):
        output = subprocess.check_output([sys.executable, str(pathlib.Path(m.__file__))])
        self.assertFalse(json.loads(output)["starts_application"])

    def test_install_creates_inert_only_and_preserves_existing_root_keys(self):
        original = self.b.files[m.AUTHORIZED]
        result = self.installer.install()
        self.assertEqual(result["phase"], "INSTALLED_INERT")
        self.assertFalse(result["application_started"])
        self.assertEqual(result["initial_epoch"], 0)
        self.assertTrue(self.b.files[m.AUTHORIZED].startswith(original))
        self.assertIn(m.key_line(KEY), self.b.files[m.AUTHORIZED])
        self.assertFalse(any("start" in event or "restart" in event or "promote" in event for event in self.b.events))
        self.assertTrue(any(event[:3] == ("/usr/bin/systemctl", "enable", "--now") for event in self.b.events))
        self.assertFalse(any("/home/ubuntu/.ssh/authorized_keys" in str(event) for event in self.b.events))

    def test_shared_owner_is_persisted_before_application_creation(self):
        self.installer.install()
        writes = [event for event in self.b.events if event[0] == "write"]
        self.assertEqual(writes[0][1], m.SCOPE / "state.json")
        install = m.strict_json(self.b.files[m.SCOPE / "state.json"])
        prep = m.strict_json(self.b.files[m.PREP / "config.json"])
        rpc = m.strict_json(self.b.files[m.RPC / "config.json"])
        ledger = m.strict_json(self.b.files[m.RPC / "state.json"])
        self.assertEqual({install["owner"], prep["owner"], rpc["owner"], ledger["owner"]}, {install["owner"]})

    def test_retry_reuses_owner_and_does_not_duplicate_authorized_key(self):
        first = self.installer.install()
        before = self.b.files[m.AUTHORIZED]
        second = MinimalInstaller(self.a, self.b).install()
        self.assertEqual(first["owner"], second["owner"])
        self.assertEqual(self.b.files[m.AUTHORIZED], before)
        self.assertEqual(before.count(m.KEY_TAG.encode()), 1)

    def test_lost_installer_state_write_response_reconciles_existing_owner(self):
        self.b.fail_after = m.SCOPE / "state.json"
        self.assert_refuses("injected-write-response-loss", self.installer.install)
        owner = m.strict_json(self.b.files[m.SCOPE / "state.json"])["owner"]
        self.assertEqual(self.installer.install()["owner"], owner)

    def test_root_installer_source_must_match_reviewed_hash(self):
        self.b.self_bytes = b"unexpected code"
        self.assert_refuses("INSTALLER_SELF_CHANGED", self.installer.install)
        self.assertNotIn(m.SCOPE / "state.json", self.b.files)

    def test_staging_program_byte_change_rejected_before_execution(self):
        self.b.files[m.PROGRAM_STAGE / "target_rpc.py"] += b"changed"
        self.assert_refuses("APPROVED_PROGRAM_CHANGED", self.installer.install)
        self.assertFalse(any(event[0] == "write" for event in self.b.events))

    def test_existing_root_program_not_silently_overwritten(self):
        self.b.files[m.LIBEXEC / "target_rpc.py"] = b"foreign root code"
        self.assert_refuses("EXISTING_FILE_DIFFERS", self.installer.install)
        self.assertNotIn(m.AUTHORIZED, [event[1] for event in self.b.events if event[0] == "write"])

    def test_refuse_to_reset_live_rpc_epoch_or_operations(self):
        self.installer.install()
        ledger = m.strict_json(self.b.files[m.RPC / "state.json"])
        ledger["epoch"] = 1
        self.b.files[m.RPC / "state.json"] = m.canonical(ledger)
        self.assert_refuses("RPC_ALREADY_ACTIVE", self.installer.install)

    def test_noninitial_ledger_never_reaches_app_preparation(self):
        self.installer.install()
        initial = m.strict_json(self.b.files[m.RPC / "state.json"])
        variants = ({"v": 2}, {"v": True}, {"epoch": False}, {"extra": None},
                    {"operations": {"a" * 32: {"phase": "ACCEPTED"}}},
                    {"pg": {"authority": "COMMIT_INTENT", "arm": None, "promotion_intent": {}}},
                    {"redis": {"authority": "TENCENT"}})
        for changed in variants:
            with self.subTest(changed=changed):
                raw = m.canonical({**initial, **changed})
                self.b.files[m.RPC / "state.json"] = raw
                self.b.events = []
                self.assert_refuses("RPC_ALREADY_ACTIVE", self.installer.install)
                self.assertEqual(self.b.files[m.RPC / "state.json"], raw)
                self.assertNotIn(("create-inert",), self.b.events)

    def test_ledger_changed_during_preparation_is_never_overwritten(self):
        self.installer.install()
        active = m.strict_json(self.b.files[m.RPC / "state.json"])
        active["epoch"] = 1
        active_bytes = m.canonical(active)
        original = self.installer._rpc_config
        def accept_request(*args):
            self.b.files[m.RPC / "state.json"] = active_bytes
            return original(*args)
        with mock.patch.object(self.installer, "_rpc_config", side_effect=accept_request):
            self.assert_refuses("EXISTING_FILE_DIFFERS", self.installer.install)
        self.assertEqual(self.b.files[m.RPC / "state.json"], active_bytes)

    def test_lost_timer_enable_response_reuses_inert_state_on_retry(self):
        original = self.b.command
        def lose_enable(argv, data=None):
            if argv[:3] == ["/usr/bin/systemctl", "enable", "--now"]:
                raise m.Unknown("injected-enable-response-loss")
            return original(argv, data)
        with mock.patch.object(self.b, "command", side_effect=lose_enable):
            self.assert_refuses("injected-enable-response-loss", self.installer.install)
        state = m.strict_json(self.b.files[m.SCOPE / "state.json"])
        self.assertEqual(state["phase"], "GUARD_ENABLE_INTENT")
        owner = state["owner"]
        self.assertEqual(self.installer.install()["owner"], owner)
        self.assertEqual(self.b.files[m.AUTHORIZED].count(m.KEY_TAG.encode()), 1)

    def test_ssh_policy_is_rechecked_before_root_key_write(self):
        calls = 0
        def deny_second_check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise m.Refusal("ROOT_FORCED_PUBLIC_KEY_NOT_PERMITTED")
            return self.b.policy
        original = self.b.files[m.AUTHORIZED]
        with mock.patch.object(self.b, "ssh_policy", side_effect=deny_second_check):
            self.assert_refuses("ROOT_FORCED_PUBLIC_KEY_NOT_PERMITTED", self.installer.install)
        self.assertEqual(calls, 2)
        self.assertEqual(self.b.files[m.AUTHORIZED], original)
        self.assertFalse(any(event[:3] == ("/usr/bin/systemctl", "enable", "--now") for event in self.b.events))

    def test_concurrent_administrator_key_edit_is_not_overwritten(self):
        original_write = self.b.write
        edited = self.b.files[m.AUTHORIZED] + b"# concurrent admin access\n"
        def admin_edit(path, raw, **fields):
            if path == m.AUTHORIZED:
                self.b.files[path] = edited
            return original_write(path, raw, **fields)
        with mock.patch.object(self.b, "write", side_effect=admin_edit):
            self.assert_refuses("CONCURRENT_FILE_CHANGE", self.installer.install)
        self.assertEqual(self.b.files[m.AUTHORIZED], edited)

    def test_changed_approval_cannot_reuse_existing_installation(self):
        self.installer.install()
        self.a["initial_manifest_sha256"] = "e" * 64
        self.assert_refuses("INSTALL_STATE_CHANGED", lambda: MinimalInstaller(self.a, self.b).install())

    def test_guard_override_is_rejected_before_enabling_timer(self):
        self.b.override = True
        self.assert_refuses("GUARD_UNIT_OVERRIDDEN", self.installer.install)
        self.assertFalse(any(event[:3] == ("/usr/bin/systemctl", "enable", "--now") for event in self.b.events))

    def test_private_control_token_copied_without_public_output(self):
        result = self.installer.install()
        self.assertEqual(self.b.files[m.RPC / "app-control.token"], self.b.files[m.PREP / "app-control.token"])
        self.assertNotIn("a" * 48, json.dumps(result))

    def test_program_and_config_modes_are_root_private(self):
        self.installer.install()
        writes = {event[1]: event[2] for event in self.b.events if event[0] == "write"}
        self.assertEqual(writes[m.LIBEXEC / "target_rpc.py"], 0o700)
        self.assertEqual(writes[m.RPC / "config.json"], 0o600)
        self.assertEqual(writes[m.AUTHORIZED], 0o600)
        self.assertEqual(writes[pathlib.Path("/etc/systemd/system") / (m.GUARD + ".service")], 0o644)


class InputAndPolicyTests(unittest.TestCase):
    def assert_refuses(self, code, function):
        with self.assertRaises(m.Refusal) as error:
            function()
        self.assertEqual(str(error.exception), code)

    def test_public_key_wire_algorithm_length_and_hash(self):
        self.assertTrue(m.public_key(KEY).startswith("ssh-ed25519 "))
        for field, bad, code in (("sha256", "a" * 64, "PUBLIC_KEY_IDENTITY"), ("algorithm", "ssh-rsa", "PUBLIC_KEY_FIELDS"), ("base64", KEY["base64"] + "$(id)", "PUBLIC_KEY_FIELDS")):
            self.assert_refuses(code, lambda: m.public_key({**KEY, field: bad}))

    def test_key_wire_trailing_bytes_rejected(self):
        wire = WIRE + b"extra"
        bad = {**KEY, "base64": base64.b64encode(wire).decode(), "sha256": m.sha(wire)}
        self.assert_refuses("PUBLIC_KEY_IDENTITY", lambda: m.public_key(bad))

    def test_input_does_not_accept_paths_units_or_shell_text(self):
        for field in ("host", "command", "path", "owner", "unit"):
            self.assert_refuses("APPROVAL_FIELDS", lambda: m.validate_input({**approval(), field: "anything"}))

    def test_exact_program_set_only(self):
        value = approval()
        value["programs"]["other.py"] = "a" * 64
        self.assert_refuses("APPROVAL_IDENTITY", lambda: m.validate_input(value))

    def test_runtime_digest_is_fixed(self):
        self.assert_refuses("UNREVIEWED_RUNTIME", lambda: m.validate_input({**approval(), "source_runtime_sha256": "f" * 64}))

    def test_managed_key_has_fixed_constraints(self):
        line = m.key_line(KEY).decode()
        self.assertTrue(line.startswith('from="13.231.143.136",restrict,command="/usr/bin/python3 -I '))
        self.assertIn("target_rpc.py serve\" ssh-ed25519 ", line)
        self.assertTrue(line.endswith(m.KEY_TAG))

    def test_existing_authorizations_preserved_byte_for_byte(self):
        original = b"# root admin\nssh-rsa AAAA admin-with-comment"
        merged = m.merge_key(original, KEY)
        self.assertEqual(merged, original + b"\n" + m.key_line(KEY) + b"\n")
        self.assertEqual(m.merge_key(merged, KEY), merged)

    def test_duplicate_same_key_broader_authorization_is_rejected(self):
        original = m.public_key(KEY).encode() + b" unbounded-root-key\n"
        self.assert_refuses("SAME_KEY_HAS_OTHER_ROOT_AUTHORIZATION", lambda: m.merge_key(original, KEY))

    def test_same_tag_changed_command_is_rejected(self):
        original = m.key_line(KEY).replace(b" serve\"", b" shell\"") + b"\n"
        self.assert_refuses("MANAGED_KEY_CHANGED", lambda: m.merge_key(original, KEY))

    def test_duplicate_managed_entries_rejected(self):
        original = (m.key_line(KEY) + b"\n") * 2
        self.assert_refuses("DUPLICATE_MANAGED_KEY", lambda: m.merge_key(original, KEY))

    def test_guard_has_fixed_reconcile_command_and_no_business_start(self):
        units = m.guard_units()
        service = units[m.GUARD + ".service"].decode()
        commands = [line for line in service.splitlines() if line.startswith("Exec")]
        self.assertEqual(commands, ["ExecStart=/usr/bin/python3 -I " + str(m.LIBEXEC / "target_rpc.py") + " guard-reconcile"])
        self.assertNotIn("/usr/bin/docker", service)
        self.assertNotIn("promote", service)
        self.assertIn("User=root", service)

    def ssh_policy(self, permit="prohibit-password", pubkey="yes", auth="any", keys=".ssh/authorized_keys"):
        backend = m.LocalBackend()
        route = m.canonical([{"prefsrc": "10.0.0.5"}])
        policy = ("permitrootlogin " + permit + "\npubkeyauthentication " + pubkey + "\nauthenticationmethods " + auth + "\nauthorizedkeysfile " + keys + "\n").encode()
        return backend, route, policy

    def test_effective_root_policy_uses_aws_and_actual_local_address(self):
        backend, route, policy = self.ssh_policy()
        with mock.patch.object(backend, "command", side_effect=[route, policy]) as command:
            self.assertEqual(backend.ssh_policy()["permitrootlogin"], "prohibit-password")
        argv = command.call_args_list[1].args[0]
        self.assertEqual(argv[:3], ["/usr/sbin/sshd", "-T", "-C"])
        self.assertIn("addr=13.231.143.136", argv[-1])
        self.assertIn("laddr=10.0.0.5", argv[-1])

    def test_effective_policy_accepts_repeatable_unrelated_sshd_directives(self):
        backend, route, policy = self.ssh_policy()
        repeated = (b"hostkey /etc/ssh/ssh_host_rsa_key\nhostkey /etc/ssh/ssh_host_ecdsa_key\n"
                    b"hostkey /etc/ssh/ssh_host_ed25519_key\nlistenaddress 0.0.0.0:22\nlistenaddress [::]:22\n"
                    b"acceptenv LANG\nacceptenv LC_*\n")
        with mock.patch.object(backend, "command", side_effect=[route, repeated + policy]):
            actual = backend.ssh_policy()
        self.assertEqual(set(actual), {"permitrootlogin", "pubkeyauthentication", "authenticationmethods", "authorizedkeysfile"})
        self.assertEqual(actual["permitrootlogin"], "prohibit-password")

    def test_effective_policy_rejects_repeated_used_directive(self):
        for directive in ("permitrootlogin yes", "pubkeyauthentication yes", "authenticationmethods any", "authorizedkeysfile .ssh/authorized_keys"):
            with self.subTest(directive=directive):
                backend, route, policy = self.ssh_policy()
                with mock.patch.object(backend, "command", side_effect=[route, policy + directive.encode() + b"\n"]):
                    self.assert_refuses("DUPLICATE_SSH_POLICY", backend.ssh_policy)

    def test_root_denied_policy_never_triggers_config_change(self):
        for field, value in (("permit", "no"), ("pubkey", "no"), ("auth", "publickey,password"), ("keys", "none")):
            backend, route, policy = self.ssh_policy(**{field: value})
            with mock.patch.object(backend, "command", side_effect=[route, policy]) as command:
                self.assert_refuses("ROOT_FORCED_PUBLIC_KEY_NOT_PERMITTED", backend.ssh_policy)
            self.assertEqual(command.call_count, 2)

    def test_request_duplicate_nonfinite_and_trailing_json_rejected(self):
        for raw in (b'{"v":1,"v":1}', b'{"x":NaN}', b'{"x":1e999}', b'{}{}'):
            with self.assertRaises(m.Refusal):
                m.strict_json(raw)

    def test_unapproved_library_name_not_executable(self):
        self.assert_refuses("UNAPPROVED_PROGRAM_NAME", lambda: m.load_reviewed(b"raise RuntimeError()", "shell.py"))

    def test_process_timeout_does_not_echo_private_outputs(self):
        with mock.patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired(["docker"], 8, stderr=b"dsn-secret")):
            self.assert_refuses("LOCAL_COMMAND_UNKNOWN", lambda: m.LocalBackend().command(["/usr/bin/docker", "inspect"]))


class ResourceAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.a = approval()
        self.installer = m.Installer(self.a, self.b)
        self.state = {"owner": OWNER, "machine_id_sha256": "d" * 64}
        self.rpc = types.SimpleNamespace(APP_CONTAINER="app", PG_CONTAINER="pg", REDIS_CONTAINER="redis", REDIS_VOLUME="redisvolume")
        self.calls = []
        def image(config):
            return {"Id": config, "Architecture": "amd64", "Os": "linux", "RootFS": {"Layers": ["sha256:" + "e" * 64]}}
        def container(cid, image_id):
            return {"Id": cid, "Image": image_id, "Config": {"Labels": {"owner": "value"}},
                    "State": {"Running": True, "Status": "running", "Pid": 55}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        app_id = "sha256:" + "4" * 64
        app = container("1" * 64, app_id)
        app["State"].update(Running=False, Status="created", Pid=0)
        self.b.resources = {("container", "app"): app, ("container", "pg"): container("2" * 64, m.PG_CONFIG),
                            ("container", "redis"): container("3" * 64, m.REDIS_CONFIG), ("volume", "redisvolume"): {"Mountpoint": "/var/lib/docker/volumes/redis/_data"},
                            ("image", app_id): image(app_id), ("image", m.PG_CONFIG): image(m.PG_CONFIG), ("image", m.REDIS_CONFIG): image(m.REDIS_CONFIG)}
        authority = {"run": "sub2api-pg-authority-20261016", "phase": "standby-role-aware-ready", "authority_mode": "STANDBY", "promotion_intent": None,
                     "owner": PG_OWNER, "container_id": "2" * 64, "image_id": m.PG_CONFIG, "volume_path": "/var/lib/docker/volumes/pg/_data", "system_identifier": "12345", "source_timeline": 1}
        self.b.files[m.AUTHORITY] = m.canonical(authority)
        redis_owner = {"run": "sub2api-redis-standby-20261016", "owner": REDIS_OWNER, "source_run_id": "a" * 40, "source_replid": "b" * 40, "container": "redis", "volume": "redisvolume"}
        self.b.files[m.REDIS_OWNERSHIP] = m.canonical(redis_owner)
        self.a["redis_ownership_sha256"] = m.sha(self.b.files[m.REDIS_OWNERSHIP])
        self.installer = m.Installer(self.a, self.b)
        self.adoption = {"container_id": app["Id"], "image_id": app_id, "owner": OWNER, "control_port": 18092,
                         "config_sha256": m.sha(app["Config"]), "rootfs_diffids": ["sha256:" + "e" * 64]}
        self.rpc.validate_config = lambda config: self.calls.append("validate-config")
        self.rpc.LocalBackend = lambda config: types.SimpleNamespace(resource=lambda kind: self.calls.append("resource:" + kind),
            pg_manifest=lambda: self.calls.append("pg-manifest"), pg_sample=lambda: {"standby": True},
            redis_inspect=lambda: {"role": "slave", "failover_state": "no-failover", "run_id": "c" * 40, "master_replid": "b" * 40},
            redis_command=lambda *parts: "master_host:127.0.0.1\nmaster_port:16379\nmaster_link_status:up\nmaster_sync_in_progress:0\n")
        self.rpc.TargetRPC = lambda *args: types.SimpleNamespace(_pg_validate=lambda sample: self.calls.append("pg-role-validate"))
        self.command = mock.patch.object(self.b, "command", return_value=b"run_id:" + b"a" * 40 + b"\nrole:master\nmaster_replid:" + b"b" * 40 + b"\nmaster_failover_state:no-failover\n")
        self.command.start()
        self.addCleanup(self.command.stop)

    def assert_refuses(self, code, function):
        with self.assertRaises(m.Refusal) as error:
            function()
        self.assertEqual(str(error.exception), code)

    def test_actual_identity_adoption_keeps_pg_and_redis_owners(self):
        config = self.installer._rpc_config(self.state, self.adoption, self.rpc)
        self.assertEqual(config["owner"], OWNER)
        self.assertEqual(config["pg"]["owner"], PG_OWNER)
        self.assertEqual(config["redis"]["owner"], REDIS_OWNER)
        self.assertEqual(config["app"]["config_sha256"], self.adoption["config_sha256"])
        self.assertEqual(config["pg"]["rootfs_diffids"], ["sha256:" + "e" * 64])
        self.assertTrue({"resource:pg", "resource:redis", "pg-manifest", "pg-role-validate"} <= set(self.calls))

    def test_running_application_cannot_be_adopted_as_inert(self):
        self.b.resources[("container", "app")]["State"]["Running"] = True
        self.assert_refuses("APP_NOT_OWNED_INERT", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_pg_already_promoted_cannot_be_given_fresh_aws_authority(self):
        state = m.strict_json(self.b.files[m.AUTHORITY])
        state["authority_mode"] = "PRIMARY"
        self.b.files[m.AUTHORITY] = m.canonical(state)
        self.assert_refuses("PG_AUTHORITY_NOT_STANDBY", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_pg_container_replaced_refused(self):
        self.b.resources[("container", "pg")]["Id"] = "9" * 64
        self.assert_refuses("PG_AUTHORITY_CONTAINER_CHANGED", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_redis_owner_snapshot_must_match_approved_bytes(self):
        self.b.files[m.REDIS_OWNERSHIP] += b"\n"
        self.assert_refuses("REDIS_OWNERSHIP_CHANGED", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_source_redis_lineage_change_refuses_target_adoption(self):
        self.b.command.return_value = b"run_id:" + b"f" * 40 + b"\nrole:master\nmaster_replid:" + b"b" * 40 + b"\n"
        self.assert_refuses("REDIS_SOURCE_LINEAGE_CHANGED", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_app_image_ordered_diffids_match_adoption(self):
        self.adoption["rootfs_diffids"] = ["sha256:" + "0" * 64]
        self.assert_refuses("APP_IMAGE_CHANGED", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))

    def test_image_architecture_change_refused(self):
        self.b.resources[("image", m.PG_CONFIG)]["Architecture"] = "arm64"
        self.assert_refuses("IMAGE_IDENTITY", lambda: self.installer._rpc_config(self.state, self.adoption, self.rpc))


if __name__ == "__main__":
    unittest.main()
