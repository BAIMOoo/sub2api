#!/usr/bin/env python3
"""Offline tests only; no Docker, SSH, or privileged filesystem operations."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("prepare_live_app", pathlib.Path(__file__).with_name("prepare_live_app.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def cfg():
    return {"v": 1, "run": mod.RUN, "owner": "a" * 32, "machine_id_sha256": "b" * 64,
            "source_runtime_sha256": mod.RUNTIME_SHA256, "initial_manifest_sha256": "c" * 64}


def source():
    return {"container_id": "1" * 64, "image_id": mod.SOURCE_IMAGE, "entrypoint": ["/app/docker-entrypoint.sh"],
            "command": ["/app/sub2api"], "environment": ["DATABASE_USER=sub2api", "DATABASE_DBNAME=sub2api",
            "DATABASE_PASSWORD=private-password", "JWT_SECRET=private-jwt", "TOTP_ENCRYPTION_KEY=private-totp",
            "SERVER_MODE=release", "REDIS_DB=0", "CUSTOM_OPTION=literal $() ` & #", "HTTP_PROXY=http://proxy.invalid:1234"]}


def image():
    return {"Id": mod.IMAGE_ID, "Architecture": "amd64", "Os": "linux", "RootFS": {"Layers": ["sha256:" + "d" * 64]},
            "Config": {"Entrypoint": ["/app/docker-entrypoint.sh"], "Cmd": ["/app/sub2api"], "Env": ["PATH=/bin:/usr/bin"],
                       "Labels": {"io.sub2api.migration.binary": mod.BINARY_SHA256, "io.sub2api.migration.baseline": mod.BASELINE}}}


def container(values):
    return {"Id": "2" * 64, "Image": mod.IMAGE_ID, "State": {"Status": "created", "Running": False, "Pid": 0},
            "HostConfig": {"NetworkMode": "host", "PortBindings": {}, "RestartPolicy": {"Name": "no"},
                           "Memory": 512*mod.MIB, "MemorySwap": 512*mod.MIB, "NanoCpus": 1000000000, "PidsLimit": 256},
            "Config": {"Entrypoint": ["/app/docker-entrypoint.sh"], "Cmd": ["/app/sub2api"],
                       "Labels": {mod.LABEL: cfg()["owner"], "io.sub2api.migration.run": "sub2api-live-app-20261016"},
                       "Env": [key + "=" + value for key, value in values.items()]},
            "Mounts": [{"Destination": "/app/data", "Type": "bind", "Source": str(mod.APP / "data"), "RW": True},
                       {"Destination": "/migration-state", "Type": "bind", "Source": str(mod.APP), "RW": False}]}


class FakeDocker:
    def __init__(self):
        self.actual = None
        self.commands = []
        self.lose_reply = False

    def inspect(self, kind, _name, missing=False):
        return image() if kind == "image" else copy.deepcopy(self.actual)

    def command(self, argv):
        self.commands.append(argv)
        values = dict(row.split("=", 1) for row in (mod.SCOPE / "app.env").read_text().splitlines())
        self.actual = container({"PATH": "/bin:/usr/bin", **values})
        if self.lose_reply:
            self.lose_reply = False
            raise mod.Unknown("DOCKER_OUTCOME_UNKNOWN")
        return (self.actual["Id"] + "\n").encode()


class Assertions:
    def refusal(self, function, code=None):
        with self.assertRaises(mod.Refusal) as caught:
            function()
        if code:
            self.assertEqual(str(caught.exception), code)


class Tests(Assertions, unittest.TestCase):
    def test_default_offline(self):
        with mock.patch.object(mod, "load_inputs", side_effect=AssertionError("offline")), \
             mock.patch.object(mod.subprocess, "run", side_effect=AssertionError("no commands")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mod.main([]), 0)
        self.assertTrue(json.loads(output.getvalue())["offline"])

    def test_closed_config_and_exact_source_snapshot(self):
        self.assertEqual(mod.validate_config(cfg()), cfg())
        for changes in [{"v": True}, {"run": "different"}, {"owner": "../../bad"},
                        {"source_runtime_sha256": "f" * 64}, {"extra": "command"}]:
            self.refusal(lambda changes=changes: mod.validate_config({**cfg(), **changes}))
        self.assertEqual(mod.verify_runtime(source()), source())
        bad = source(); bad["command"] = ["/bin/sh"]
        self.refusal(lambda: mod.verify_runtime(bad), "SOURCE_RUNTIME_IDENTITY")

    def test_environment_preserves_business_options_and_only_selected_overrides(self):
        actual = mod.environment(source()["environment"], "t" * 64)
        original = dict(row.split("=", 1) for row in source()["environment"])
        for key in ("JWT_SECRET", "TOTP_ENCRYPTION_KEY", "CUSTOM_OPTION", "HTTP_PROXY", "DATABASE_PASSWORD", "REDIS_DB"):
            self.assertEqual(actual[key], original[key])
        self.assertEqual((actual["SERVER_HOST"], actual["SERVER_PORT"]), ("127.0.0.1", "8082"))
        self.assertEqual((actual["DATABASE_HOST"], actual["DATABASE_PORT"]), ("127.0.0.1", "15432"))
        self.assertEqual((actual["REDIS_HOST"], actual["REDIS_PORT"], actual["REDIS_PASSWORD"]), ("127.0.0.1", "16379", ""))
        self.assertEqual(actual["MIGRATION_BACKGROUND_WORKERS"], "false")
        self.assertEqual(actual["MIGRATION_RUNTIME_ADDR"], "127.0.0.1:18092")
        self.assertEqual(actual["MIGRATION_ENDPOINT_STATE_FILE"], "/migration-state/endpoints.json")
        self.assertEqual(actual["GOMEMLIMIT"], "400MiB")
        with_old = source()["environment"] + ["MIGRATION_UNKNOWN_DANGEROUS=true", "MIGRATION_USAGE_ENABLED=true"]
        replaced = mod.environment(with_old, "t" * 64)
        self.assertNotIn("MIGRATION_UNKNOWN_DANGEROUS", replaced)
        self.assertEqual(replaced["MIGRATION_USAGE_ENABLED"], "false")

    def test_environment_injection_duplicate_or_invalid_token_refused(self):
        for rows in [source()["environment"] + ["JWT_SECRET=duplicate"], source()["environment"] + ["BAD=one\ntwo"],
                     source()["environment"] + ["BAD-NAME=value"], source()["environment"] + ["NUL=bad\x00"]]:
            self.refusal(lambda rows=rows: mod.environment(rows, "t" * 64))
        self.refusal(lambda: mod.environment(source()["environment"], "short"), "INVALID_CONTROL_TOKEN")
        values = [row if not row.startswith("DATABASE_PASSWORD=") else "DATABASE_PASSWORD=a'\\b" for row in source()["environment"]]
        self.assertIn("password='a\\'\\\\b'", mod.environment(values, "t" * 64)["MIGRATION_POSTGRES_TARGET_DSN"])

    def test_create_argv_is_fixed_inert_and_contains_no_secrets(self):
        argv = mod.create_argv(cfg()["owner"])
        self.assertEqual(argv[:2], ["/usr/bin/docker", "create"])
        self.assertEqual(argv[-1], mod.IMAGE_ID)
        self.assertIn("never", argv)
        self.assertNotIn("start", argv)
        self.assertNotIn("run", argv)
        self.assertFalse(any(value in str(argv) for value in ("private-password", "private-jwt", "Bearer")))
        self.assertIn("type=bind,src=" + str(mod.APP) + ",dst=/migration-state,readonly", argv)
        self.assertIn(str(512*mod.MIB), argv)

    def test_image_identity_and_provenance(self):
        mod.verify_image(image())
        for field, value in [("Id", "sha256:" + "e" * 64), ("Os", "windows"), ("Architecture", "arm64")]:
            bad = image(); bad[field] = value
            self.refusal(lambda bad=bad: mod.verify_image(bad), "IMAGE_IDENTITY")
        bad = image(); bad["Config"]["Labels"]["io.sub2api.migration.binary"] = "0" * 64
        self.refusal(lambda: mod.verify_image(bad), "IMAGE_PROVENANCE")

    def test_only_created_container_with_exact_env_mounts_and_runtime_is_adopted(self):
        values = mod.environment(source()["environment"], "t" * 64)
        mod.verify_created(container(values), cfg(), values)
        cases = [lambda c: c["State"].update(Status="exited"), lambda c: c["State"].update(Running=True),
                 lambda c: c["HostConfig"].update(NetworkMode="bridge"),
                 lambda c: c["HostConfig"]["RestartPolicy"].update(Name="always"),
                 lambda c: c["Config"]["Env"].append("JWT_SECRET=changed"),
                 lambda c: c["Mounts"][1].update(RW=True)]
        for mutate in cases:
            actual = container(values); mutate(actual)
            self.refusal(lambda actual=actual: mod.verify_created(actual, cfg(), values))

    def test_docker_timeout_is_unknown_and_failed_inspect_is_not_absence(self):
        with mock.patch.object(mod.subprocess, "run", side_effect=__import__("subprocess").TimeoutExpired("fixed", 20)):
            self.refusal(lambda: mod.Docker().command(["/usr/bin/docker", "create"]), "DOCKER_OUTCOME_UNKNOWN")
        docker = mod.Docker()
        docker.command = mock.Mock(side_effect=[mod.Unknown("DOCKER_OUTCOME_UNKNOWN"), (mod.CONTAINER + "\n").encode()])
        self.refusal(lambda: docker.inspect("container", mod.CONTAINER, missing=True), "CONTAINER_EXISTENCE_UNKNOWN")
        docker.command = mock.Mock(side_effect=[mod.Unknown("DOCKER_OUTCOME_UNKNOWN"), b"unrelated-container\n"])
        self.assertIsNone(docker.inspect("container", mod.CONTAINER, missing=True))

    def test_real_private_reads_reject_symlink_and_public_secret_modes(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary)
            path = root / "secret"; path.write_bytes(b"secret"); path.chmod(0o600)
            self.assertEqual(mod.read_bytes(path, uid=os.geteuid()), b"secret")
            path.chmod(0o644)
            self.refusal(lambda: mod.read_bytes(path, uid=os.geteuid()), "UNTRUSTED_FILE")
            link = root / "link"; link.symlink_to(path)
            with self.assertRaises((OSError, mod.Refusal)):
                mod.read_bytes(link, uid=os.geteuid())


class PreparationTests(Assertions, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=pathlib.Path.home())
        self.root = pathlib.Path(self.temporary.name)
        self.scope = self.root / "app-prepare"; self.scope.mkdir(mode=0o700)
        self.patches = [mock.patch.object(mod, "SCOPE", self.scope), mock.patch.object(mod, "APP", self.root / "app"),
                        mock.patch.object(mod, "directory"),
                        mock.patch.object(mod, "app_directory", side_effect=lambda: mod.APP.mkdir(mode=0o755, exist_ok=True)),
                        mock.patch.object(mod, "read_bytes", side_effect=lambda path, **_kw: pathlib.Path(path).read_bytes()),
                        mock.patch.object(mod.os, "chown"), mock.patch.object(mod.os, "fchown")]
        for patch in self.patches:
            patch.start()
        self.payload = {name: (name + " private file").encode() for name in mod.FILES}
        self.docker = FakeDocker()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def test_inert_creation_pins_adoption_without_disclosing_secrets(self):
        metadata = mod._prepare_locked(cfg(), source(), self.payload, self.docker)
        self.assertTrue(metadata["created_inert"])
        self.assertFalse(metadata["application_started"])
        self.assertNotIn("private-password", json.dumps(metadata))
        self.assertNotIn("private-jwt", json.dumps(metadata))
        self.assertEqual(len(self.docker.commands), 1)
        self.assertEqual(self.docker.commands[0][1], "create")
        self.assertEqual(stat.S_IMODE((self.scope / "app.env").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.scope / "app-control.token").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((mod.APP / "endpoints.json").stat().st_mode), 0o644)
        self.assertEqual((mod.APP / "data" / "config.yaml").read_bytes(), self.payload["config.yaml"])
        again = mod._prepare_locked(cfg(), source(), self.payload, self.docker)
        self.assertEqual(again, metadata)
        self.assertEqual(len(self.docker.commands), 1)

    def test_lost_create_reply_is_reconciled_without_second_create(self):
        self.docker.lose_reply = True
        self.refusal(lambda: mod._prepare_locked(cfg(), source(), self.payload, self.docker), "DOCKER_OUTCOME_UNKNOWN")
        self.assertEqual(json.loads((self.scope / "state.json").read_text())["phase"], "CREATING")
        metadata = mod._prepare_locked(cfg(), source(), self.payload, self.docker)
        self.assertTrue(metadata["created_inert"])
        self.assertEqual(len(self.docker.commands), 1)

    def test_existing_unowned_or_later_started_container_refused(self):
        self.docker.actual = container(mod.environment(source()["environment"], "t" * 64))
        self.refusal(lambda: mod._prepare_locked(cfg(), source(), self.payload, self.docker), "UNOWNED_CONTAINER_EXISTS")
        self.docker.actual = None
        mod._prepare_locked(cfg(), source(), self.payload, self.docker)
        self.docker.actual["State"].update(Status="exited")
        self.refusal(lambda: mod._prepare_locked(cfg(), source(), self.payload, self.docker), "CONTAINER_NOT_INERT")
        self.assertEqual(len(self.docker.commands), 1)


if __name__ == "__main__":
    unittest.main()
