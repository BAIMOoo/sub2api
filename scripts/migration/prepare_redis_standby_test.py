#!/usr/bin/env python3
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import time
import unittest
import uuid
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepare_redis_standby", pathlib.Path(__file__).with_name("prepare_redis_standby.py"))
standby = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standby)


def facts():
    layers = ["sha256:"+str(i)*64 for i in range(7)]
    source = {"server": {"redis_version": "8.6.2", "run_id": "a"*40},
              "replication": {"role": "master", "master_replid": "b"*40, "master_repl_offset": "12", "repl_backlog_active": "0", "master_failover_state": "no-failover", "connected_slaves": "0"},
              "image_config_digest": standby.CONFIG_DIGEST, "rootfs_layers": layers,
              "redis_uid": 999, "redis_gid": 999,
              "persistence": {"loading": "0", "rdb_bgsave_in_progress": "0", "aof_rewrite_in_progress": "0", "rdb_last_bgsave_status": "ok", "aof_last_bgrewrite_status": "ok", "aof_last_write_status": "ok", "aof_enabled": "0"},
              "config": {"dir": "/data", "dbfilename": "dump.rdb", "appendonly": "no", "appendfsync": "everysec", "save": "3600 1 300 100 60 10000", "maxmemory": "0", "maxmemory-policy": "noeviction", "repl-diskless-sync": "yes", "repl-diskless-load": "disabled", "repl-timeout": "60"},
              "disk": {"free_bytes": 7*standby.GIB}, "available_memory_bytes": standby.GIB,
              "docker_network": {"bridge_gateway": "172.18.0.1", "redis_ip": "172.18.0.2"},
              "memory": {"used_memory_peak": str(7*standby.MIB), "used_memory_rss": str(6*standby.MIB)}}
    target = {"source_channel": {"run_id": "a"*40, "role": "master", "master_replid": "b"*40},
              "image_config_digest": standby.CONFIG_DIGEST, "rootfs_layers": layers, "architecture": "amd64",
              "disk": {"free_bytes": 20*standby.GIB}, "available_memory_bytes": standby.GIB,
              "scope_exists": False, "container_exists": False, "volume_exists": False, "standby_port_free": True,
              "machine_id_sha256": "c"*64}
    return source, target


class RedisStandbyTests(unittest.TestCase):
    def test_default_is_offline_dry_run(self):
        output = io.StringIO()
        with patch.object(standby, "remote", side_effect=AssertionError("dry run called SSH")), contextlib.redirect_stdout(output):
            self.assertEqual(standby.main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["remote_operations"], 0)
        self.assertFalse(plan["promotion_included"])
        self.assertFalse(plan["source_configuration_changes"])

    def test_apply_requires_source_identity_before_any_ssh(self):
        with patch.object(standby, "remote", side_effect=AssertionError("unguarded apply called SSH")):
            with self.assertRaisesRegex(standby.Refusal, "reviewed-source-run-id"):
                standby.main(["--apply"])

    def test_inspect_only_reads_source_and_target(self):
        source, target = facts()
        with patch.object(standby, "remote", side_effect=[source, target]) as remote, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(standby.main(["--inspect"]), 0)
        self.assertEqual(remote.call_count, 2)
        for call in remote.call_args_list:
            script = call.args[1]
            self.assertNotIn('"docker","create"', script)
            self.assertNotIn('"docker","start"', script)
            self.assertNotIn('"CONFIG","SET"', script)
            self.assertNotIn('"REPLICAOF"', script)

    def test_healthy_inspection_and_policy_preserving_config(self):
        source, target = facts()
        self.assertTrue(standby.validate_inspection(source, target)["inspection_passed"])
        config = standby.render_config(source)
        for line in ["bind 127.0.0.1", "port 26379", "replicaof 127.0.0.1 16379", "replica-read-only yes", "replica-serve-stale-data no", "appendonly no", "save 3600 1 300 100 60 10000", "maxmemory 0", "maxmemory-policy noeviction", "replica-announce-ip 172.18.0.1", "replica-announce-port 26379"]:
            self.assertIn(line+"\n", config)
        self.assertNotIn("requirepass", config)
        self.assertNotIn("masterauth", config)

    def test_wrong_identity_image_policy_or_resources_refused(self):
        changes = [("source", ["replication", "role"], "slave"),
                   ("source", ["replication", "master_failover_state"], "failover-in-progress"),
                   ("source", ["replication", "connected_slaves"], "1"),
                   ("source", ["replication", "repl_backlog_active"], "unknown"),
                   ("target", ["source_channel", "run_id"], "d"*40),
                   ("target", ["image_config_digest"], "sha256:"+"d"*64),
                   ("target", ["rootfs_layers"], []),
                   ("source", ["docker_network", "bridge_gateway"], "172.19.0.1"),
                   ("source", ["redis_uid"], 0),
                   ("source", ["persistence", "rdb_last_bgsave_status"], "err"),
                   ("source", ["persistence", "rdb_bgsave_in_progress"], "1"),
                   ("source", ["config", "appendonly"], "yes"),
                   ("source", ["disk", "free_bytes"], standby.GIB),
                   ("target", ["available_memory_bytes"], standby.MIB),
                   ("source", ["memory", "used_memory_peak"], str(128*standby.MIB)),
                   ("target", ["volume_exists"], True),
                   ("target", ["standby_port_free"], False)]
        for side, path, value in changes:
            with self.subTest(side=side, path=path):
                source, target = facts()
                node = source if side == "source" else target
                for part in path[:-1]: node = node[part]
                node[path[-1]] = value
                with self.assertRaises(standby.Refusal): standby.validate_inspection(source, target)

    def test_config_line_injection_refused(self):
        source, _ = facts()
        source["config"]["save"] += "\nreplicaof attacker.invalid 6379"
        with self.assertRaises(standby.Refusal): standby.render_config(source)

    def test_first_backlog_replid_rotation_requires_same_primary_process_and_no_failover(self):
        server = {"run_id": "a"*40}
        replication = {"role": "master", "master_failover_state": "no-failover", "master_replid": "c"*40, "repl_backlog_active": "1"}
        self.assertTrue(standby.source_epoch_allowed(server, replication, "a"*40, "b"*40, "0"))
        self.assertFalse(standby.source_epoch_allowed(server, replication, "a"*40, "b"*40, "1"))
        for key, value in [("role", "slave"), ("master_failover_state", "failover-in-progress"), ("repl_backlog_active", "0"), ("master_replid", "invalid")]:
            changed = dict(replication); changed[key] = value
            self.assertFalse(standby.source_epoch_allowed(server, changed, "a"*40, "b"*40, "0"))
        self.assertFalse(standby.source_epoch_allowed({"run_id": "d"*40}, replication, "a"*40, "b"*40, "0"))

    def test_target_command_owns_separate_volume_and_no_public_port(self):
        command = standby.container_command("f"*32)
        self.assertIn("host", command)
        self.assertNotIn("-p", command)
        self.assertNotIn("--publish", command)
        self.assertIn("type=volume,src="+standby.VOLUME+",dst=/data,volume-nocopy", command)
        self.assertIn(standby.IMAGE, command)
        self.assertNotIn("deploy_redis_data", " ".join(command))
        self.assertEqual(command[-3:], ["sh", "-ceu", standby.startup_shell()])
        self.assertIn("config=/data/migration.conf", command[-1])
        self.assertNotIn("--entrypoint", command)
        self.assertFalse(any("type=bind" in part for part in command))

    def test_startup_shell_preserves_official_entrypoint_and_rejects_custom_module_arguments(self):
        shell = standby.startup_shell()
        subprocess.run(["sh", "-n"], input=shell, text=True, check=True, capture_output=True)
        self.assertIn("NF != 2", shell)
        self.assertIn("exec /usr/local/bin/docker-entrypoint.sh redis-server", shell)
        self.assertNotIn("replicaof", shell)

    def test_apply_is_target_only_and_has_no_promotion_or_cleanup(self):
        source, target = facts()
        inspection = standby.validate_inspection(source, target)
        script = standby.apply_script(inspection, "f"*32)
        compile(script, "target-apply-script", "exec")
        self.assertNotIn("REPLICAOF", script)
        self.assertNotIn("FAILOVER", script)
        self.assertNotIn("FORCE", script)
        self.assertNotIn('"docker","rm"', script)
        self.assertNotIn('"volume","rm"', script)
        self.assertIn('"source_remains_primary":True', script)
        self.assertIn('"replica-read-only":"yes"', script)
        self.assertIn('"target-scope-already-exists"', script)
        self.assertIn('"target-memory-reserve-lost"', script)
        self.assertIn('"target-volume-ownership-mismatch"', script)
        self.assertIn('"target-volume-not-empty"', script)
        self.assertIn('"CONFIG","REWRITE"', script)
        self.assertIn('runtime_config_rewrite_verified=True', script)

    def test_final_failover_requires_verified_source_bridge_tunnel(self):
        prerequisite = standby.dry_run_plan()["role_swap_network_prerequisite"]
        self.assertEqual(prerequisite["announced_address"], "172.18.0.1:26379")
        self.assertIn("FAILOVER is forbidden", prerequisite["before_final_failover"])
        self.assertFalse(prerequisite["prepared_here"])

    def test_generated_inspection_scripts_compile(self):
        for script in [standby.source_inspection_script(), standby.target_inspection_script()]:
            compile(script, "read-only-inspection", "exec")

    def test_reviewed_identity_change_refuses_before_target_mutation(self):
        source, target = facts()
        with patch.object(standby, "remote", side_effect=[source, target]) as remote:
            with self.assertRaisesRegex(standby.Refusal, "reviewed-source-run-id-changed"):
                standby.main(["--apply", "--expected-source-run-id", "d"*40])
        self.assertEqual(remote.call_count, 2)

    def test_remote_stderr_cannot_leak_credentials(self):
        completed = type("Completed", (), {"returncode": 1, "stderr": "TOKEN_CANARY_SECRET", "stdout": ""})()
        with patch.object(standby.subprocess, "run", return_value=completed):
            with self.assertRaises(standby.Refusal) as refused:
                standby.remote(["ssh", "example"], "pass")
        self.assertNotIn("TOKEN_CANARY_SECRET", str(refused.exception))


@unittest.skipUnless(os.environ.get("SUB2API_REDIS_STANDBY_DOCKER_IMAGE"), "set an existing local official Redis image to exercise writable config and restart")
class RedisStandbyDockerTests(unittest.TestCase):
    def test_official_entrypoint_rewrites_owned_volume_config_and_role_survives_restart(self):
        # Only disposable local resources are used. Network=none prevents this
        # test from ever contacting the configured source replication channel.
        image = os.environ["SUB2API_REDIS_STANDBY_DOCKER_IMAGE"]
        suffix = uuid.uuid4().hex
        container, volume = "sub2api-redis-config-test-"+suffix, "sub2api-redis-config-test-"+suffix
        def docker(*args, input=None):
            return subprocess.run(["docker", *args], input=input, text=True, check=True, capture_output=True, timeout=25).stdout.strip()
        def redis(*args):
            return docker("exec", container, "redis-cli", "-p", str(standby.PORT), "--raw", *args)
        try:
            docker("image", "inspect", image)  # Existing image only; no pull.
            docker("volume", "create", volume)
            source, _ = facts()
            docker("run", "--rm", "-i", "--network", "none", "--mount", "type=volume,src="+volume+",dst=/data,volume-nocopy",
                   image, "sh", "-ceu", "umask 022; test ! -e /data/migration.conf; cat > /data/migration.conf; chown redis:redis /data /data/migration.conf", input=standby.render_config(source))
            command = standby.container_command("f"*32)[1:]
            command = [part.replace(standby.CONTAINER, container).replace(standby.VOLUME, volume) for part in command]
            command[command.index("--network")+1] = "none"
            command[command.index(standby.IMAGE)] = image
            docker(*command)
            docker("start", container)
            def wait_started():
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    try:
                        if "redis_version:" in redis("INFO", "server"):
                            return
                    except subprocess.CalledProcessError:
                        pass
                    time.sleep(.1)
                self.fail("isolated Redis did not start: "+docker("logs", container)[-4000:])
            wait_started()
            docker("exec", "--user", "redis", container, "sh", "-ceu", "test -w /data; test -w /data/migration.conf")
            self.assertIn("config_file:/data/migration.conf", redis("INFO", "server"))
            self.assertEqual(redis("CONFIG", "REWRITE"), "OK")
            self.assertIn("replicaof 127.0.0.1 16379", docker("exec", container, "cat", "/data/migration.conf"))
            self.assertEqual(redis("REPLICAOF", "NO", "ONE"), "OK")
            self.assertEqual(redis("CONFIG", "REWRITE"), "OK")
            self.assertEqual(redis("SET", "isolated-role-persistence-probe", suffix), "OK")
            self.assertEqual(redis("SAVE"), "OK")
            docker("restart", "--time", "5", container)
            wait_started()
            self.assertIn("role:master", redis("INFO", "replication"))
            self.assertEqual(redis("GET", "isolated-role-persistence-probe"), suffix)
            self.assertNotIn("replicaof ", docker("exec", container, "cat", "/data/migration.conf"))
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=25)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=25)


if __name__ == "__main__":
    unittest.main()
