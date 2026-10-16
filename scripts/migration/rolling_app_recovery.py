#!/usr/bin/env python3
"""Offline, injectable core for replacing the one fixed migration application.

There is deliberately no production adapter, shell execution, SSH, or apply CLI.
Backend observations must be fresh. App ``business_env_sha256`` is the canonical
SHA256 of *all* actual environment entries except ENV_DIFFERENCES; credentials
remain in the backend and are never returned here. ``inbound_tcp`` counts every
accepted TCP socket on old app port 8082, including idle HTTP keep-alives and
closing accepted sockets; exclude the independent control listener.
``business_active`` and ``business_pending`` cover accepted work, excluding
idle worker goroutines.

Backend methods: observe(), inspect_history(), archive_history(spec),
prepare_app(spec), start_app(container_id), install_redirect(rule, nat_before),
signal_old_once(container_id). Mutations may raise on uncertain outcomes. Such
an intent is reconciled by inspection only, never by calling its mutation again.
The adapter must use exact pinned identities and compare immediately before
each effect. It must not implement docker stop, timeout kills, conntrack flush,
DNS/Caddy changes, or controller/authority ledger updates.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import time

ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = ROOT / "rolling-recovery"
DATA = SCOPE / "app" / "data"
ENDPOINT_DIRECTORY = ROOT / "app"
TOKEN_FILE = SCOPE / "app-control.token"
HISTORY_DIRECTORY = SCOPE / "history"
OLD_ID = "cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82"
IMAGE = "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a"
NEW_NAME = "sub2api-migration-recovery-app-20261017"
FILES = ("config.yaml", ".installed", "model_pricing.json", "model_pricing.sha256")
ENV_DIFFERENCES = ("SERVER_PORT", "MIGRATION_RUNTIME_ADDR", "MIGRATION_RUNTIME_TOKEN")
ENDPOINTS = {"v": 1, "postgres": "source", "redis": "source"}
SETTINGS = {
    "SERVER_HOST": "127.0.0.1", "SERVER_PORT": "8083", "AUTO_SETUP": "false",
    "DATABASE_HOST": "127.0.0.1", "DATABASE_PORT": "15432",
    "DATABASE_USER": "sub2api", "DATABASE_DBNAME": "sub2api",
    "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "16379",
    "MIGRATION_HANDOVER_MODE": "quiet", "MIGRATION_SHARED_REDIS": "true",
    "MIGRATION_BACKGROUND_WORKERS": "false", "MIGRATION_USAGE_ENABLED": "false",
    "MIGRATION_POSTGRES_ENABLED": "true", "MIGRATION_REDIS_ENABLED": "true",
    "MIGRATION_REDIS_TARGET_ADDR": "127.0.0.1:26379",
    "MIGRATION_RUNTIME_ADDR": "127.0.0.1:18093",
    "MIGRATION_ENDPOINT_STATE_FILE": "/migration-state/endpoints.json",
}
MOUNTS = [
    {"source": str(DATA), "destination": "/app/data", "rw": True},
    {"source": str(ENDPOINT_DIRECTORY), "destination": "/migration-state", "rw": False},
]
# Canonical iptables -S token representation. The adapter must preserve every
# preexisting rule, including order and other NAT chains/policies.
REDIRECT = ["-A", "OUTPUT", "-d", "127.0.0.1/32", "-p", "tcp", "-m", "conntrack", "--ctstate", "NEW",
            "-m", "tcp", "--dport", "8082", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN",
            "-m", "owner", "--uid-owner", "1000",
            "-m", "comment", "--comment", "sub2api-recovery-app-20261017",
            "-j", "REDIRECT", "--to-ports", "8083"]
QUIET_FIELDS = ("inbound_tcp", "http", "hijacked", "business_active", "business_pending",
                "ops_active", "ops_pending")
APP_IDENTITY_FIELDS = ("container_id", "image_id", "config_sha256", "rootfs_diffids",
                       "environment_sha256", "business_env_sha256", "files", "control_token_sha256")


class Refusal(RuntimeError):
    """Only fixed error codes, never backend exceptions or credential values."""


def need(condition, code):
    if not condition:
        raise Refusal(code)


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_KEY")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refusal("NONFINITE_JSON")))
    except (ValueError, UnicodeError):
        raise Refusal("INVALID_STORE") from None


class FileStore:
    """0600 atomic checkpoints; intent file and containing directory are fsynced.

    The production installer creates the private root-owned parent first. No
    file/directory is automatically removed, and a corrupt state fails closed.
    """
    def __init__(self, path, uid=None):
        self.path = pathlib.Path(path)
        self.uid = os.geteuid() if uid is None else uid

    def _parent(self):
        info = self.path.parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid and
             stat.S_IMODE(info.st_mode) == 0o700, "PRIVATE_STORE_DIRECTORY_REQUIRED")
        for ancestor in self.path.parent.parents:
            need(not ancestor.is_symlink(), "STORE_SYMLINK")

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_file(self, fd):
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_STORE_FILE")

    @contextlib.contextmanager
    def locked(self):
        self._parent()
        lock = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            self._check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.fsync(fd)
            self._sync_dir(self.path.parent)
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            self._check_file(fd)
            raw = os.read(fd, 2 * 1024 * 1024 + 1)
            need(len(raw) <= 2 * 1024 * 1024, "STORE_SIZE")
            return _strict_json(raw)
        finally:
            os.close(fd)

    def write(self, state):
        raw = canonical(state) + b"\n"
        need(len(raw) <= 2 * 1024 * 1024, "STORE_SIZE")
        pending = self.path.with_name(".rolling-pending-" + secrets.token_hex(16))
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(pending, self.path)
            self._sync_dir(self.path.parent)
        finally:
            if pending.exists():
                pending.unlink()


class Recovery:
    def __init__(self, backend, store, owner, *, clock=time.monotonic,
                 quiet_seconds=2.0, quiet_samples=3):
        need(isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{32}", owner), "OWNER_REQUIRED")
        need(quiet_seconds >= 2.0 and quiet_samples >= 3, "DRAIN_POLICY_TOO_WEAK")
        self.b, self.store, self.owner, self.clock = backend, store, owner, clock
        self.quiet_seconds, self.quiet_samples = quiet_seconds, quiet_samples

    def _save(self, s):
        self.store.write(copy.deepcopy(s))

    @staticmethod
    def _identity(app):
        need(type(app) is dict and all(key in app for key in APP_IDENTITY_FIELDS), "APP_IDENTITY_MISSING")
        need(digest(app["container_id"]) and app["image_id"] == IMAGE and
             all(digest(app[key]) for key in ("config_sha256", "environment_sha256",
                                             "business_env_sha256", "control_token_sha256")), "APP_IDENTITY_INVALID")
        layers = app["rootfs_diffids"]
        need(type(layers) is list and len(layers) > 0 and
             all(isinstance(x, str) and x.startswith("sha256:") and digest(x[7:]) for x in layers), "APP_LAYERS_INVALID")
        need(type(app["files"]) is dict and set(app["files"]) == set(FILES) and
             all(digest(x) for x in app["files"].values()), "APP_FILES_INVALID")
        return {key: copy.deepcopy(app[key]) for key in APP_IDENTITY_FIELDS}

    def _observe(self, s=None):
        o = self.b.observe()
        need(type(o) is dict and o.get("window") is None and o.get("quiesce_cancelled") is True and
             o.get("pg_authority") == o.get("redis_authority") == "AWS" and o.get("endpoints") == ENDPOINTS,
             "SOURCE_AUTHORITY_AND_CANCELLED_WINDOW_REQUIRED")
        need(isinstance(o.get("sample_id"), str) and 0 < len(o["sample_id"]) <= 128 and
             isinstance(o.get("boot_id"), str) and 0 < len(o["boot_id"]) <= 128, "FRESH_SAMPLE_ID_REQUIRED")
        old = self._identity(o.get("old"))
        need(old["container_id"] == OLD_ID, "OLD_IDENTITY_CHANGED")
        need(o["old"].get("ops_failed") == 1 and o["old"].get("ops_dropped") == 3, "HISTORICAL_COUNTERS_CHANGED")
        need(o["old"].get("endpoints") == ENDPOINTS, "OLD_ENDPOINTS_CHANGED")
        if s is not None:
            need(old == s["old_identity"], "OLD_IDENTITY_CHANGED")
            if "history" in s:
                need(self._history(self.b.inspect_history()) == s["history"], "HISTORY_ARCHIVE_CHANGED")
            expected_nat = copy.deepcopy(s["nat_before"])
            if s["phase"] not in ("ARCHIVE_INTENT", "ARCHIVED", "PREPARE_INTENT", "PREPARED", "START_INTENT", "HEALTHY"):
                # ROUTE_INTENT may be observed before or after the single insert.
                routed = copy.deepcopy(expected_nat)
                routed["output"] = [REDIRECT] + routed["output"]
                need(o.get("nat") == routed or
                     (s["phase"] == "ROUTE_INTENT" and o.get("nat") == expected_nat), "NAT_CHANGED")
            else:
                need(o.get("nat") == expected_nat, "NAT_CHANGED")
        return o

    def _new(self, s, app, *, running=False):
        ident = self._identity(app)
        need(ident["container_id"] != OLD_ID and app.get("name") == NEW_NAME and
             ident["rootfs_diffids"] == s["old_identity"]["rootfs_diffids"] and
             ident["business_env_sha256"] == s["old_identity"]["business_env_sha256"] and
             ident["files"] == s["old_identity"]["files"] and
             ident["control_token_sha256"] != s["old_identity"]["control_token_sha256"], "NEW_INHERITANCE_INVALID")
        need(app.get("settings") == SETTINGS and app.get("mounts") == MOUNTS and
             app.get("restart") == "no" and app.get("network") == "host" and
             app.get("entrypoint") == ["/app/docker-entrypoint.sh"] and app.get("command") == ["/app/sub2api"] and
             app.get("control_token_file") == str(TOKEN_FILE) and app.get("endpoints") == ENDPOINTS,
             "NEW_CONFIGURATION_INVALID")
        need(app.get("ops_failed") == 0 and app.get("ops_dropped") == 0, "NEW_WRITER_FAILURE")
        if "new_identity" in s:
            need(ident == s["new_identity"], "NEW_IDENTITY_CHANGED")
        if running:
            need(app.get("state") == "running" and app.get("ready") is True and
                 app.get("serving") is True and app.get("draining") is False and
                 app.get("all_writer_failures") == 0 and app.get("all_writer_drops") == 0,
                 "NEW_APP_NOT_HEALTHY")
        return ident

    @staticmethod
    def _history(receipt):
        need(type(receipt) is dict and set(receipt) == {"failed", "dropped", "logs_sha256",
             "recovery_receipt_sha256", "archive_directory", "durable"}, "HISTORY_RECEIPT_REQUIRED")
        need(receipt["failed"] == 1 and receipt["dropped"] == 3 and receipt["durable"] is True and
             digest(receipt["logs_sha256"]) and digest(receipt["recovery_receipt_sha256"]) and
             receipt["archive_directory"] == str(HISTORY_DIRECTORY), "HISTORY_NOT_ARCHIVED")
        return copy.deepcopy(receipt)

    def _spec(self, s):
        return {"old_id": OLD_ID, "image_id": IMAGE, "name": NEW_NAME, "owner": self.owner,
                "settings": copy.deepcopy(SETTINGS), "mounts": copy.deepcopy(MOUNTS),
                "files": copy.deepcopy(s["old_identity"]["files"]),
                "inherit_all_environment_except": list(ENV_DIFFERENCES),
                "independent_token_file": str(TOKEN_FILE), "endpoints": copy.deepcopy(ENDPOINTS),
                "history_directory": str(HISTORY_DIRECTORY), "historical_failed": 1, "historical_dropped": 3}

    def _inspect_effect(self, s):
        original = s
        s = copy.deepcopy(s)
        o = self._observe(s)
        kind = s["intent"]["kind"]
        if kind != "term":
            need(o["old"].get("state") == "running", "OLD_EXITED_BEFORE_DRAIN")
        if kind == "archive":
            s["history"] = self._history(self.b.inspect_history())
            s["phase"] = "ARCHIVED"
        elif kind == "prepare":
            s["new_identity"] = self._new(s, o.get("new"))
            need(o["new"].get("state") == "created", "NEW_APP_NOT_INERT")
            s["phase"] = "PREPARED"
        elif kind == "start":
            self._new(s, o.get("new"), running=True)
            s["phase"] = "HEALTHY"
        elif kind == "route":
            self._new(s, o.get("new"), running=True)
            routed = copy.deepcopy(s["nat_before"])
            routed["output"] = [REDIRECT] + routed["output"]
            need(o["nat"] == routed, "ROUTE_NOT_INSTALLED")
            s.update(phase="DRAINING", quiet=None)
        elif kind == "term":
            self._new(s, o.get("new"), running=True)
            need(o["old"].get("state") == "exited" and o["old"].get("exit_code") == 0 and
                 o["old"].get("oom_killed") is False, "OLD_EXIT_PENDING")
            need(self._history(self.b.inspect_history()) == s["history"], "HISTORY_ARCHIVE_CHANGED")
            s["phase"] = "COMPLETE"
            s["adoption_receipt"] = {"v": 1, "owner": self.owner, "old_container_id": OLD_ID,
                "new_app": copy.deepcopy(s["new_identity"]), "control_port": 18093,
                "control_token_file": str(TOKEN_FILE), "data_directory": str(DATA),
                "endpoint_directory": str(ENDPOINT_DIRECTORY), "endpoints": copy.deepcopy(ENDPOINTS),
                "postgres_authority": "AWS", "redis_authority": "AWS", "history": copy.deepcopy(s["history"]),
                "new_process_failed": 0, "new_process_dropped": 0, "redirect": copy.deepcopy(REDIRECT),
                "old_exited_cleanly": True, "controller_config_changed": False}
        else:
            raise Refusal("UNKNOWN_INTENT_KIND")
        s["completed_intents"].append(copy.deepcopy(s["intent"]))
        s["intent"] = None
        self._save(s)
        original.clear()
        original.update(s)

    def _effect(self, s, kind, call):
        s.update(phase=kind.upper() + "_INTENT", intent={"id": secrets.token_hex(16), "kind": kind, "status": "INTENT"})
        self._save(s)  # Durable intent precedes the only mutation call.
        try:
            call()
        except Exception:
            s["intent"]["status"] = "UNKNOWN"
            self._save(s)
            return self._result(s)
        try:
            self._inspect_effect(s)
        except Exception:
            # The durable intent already prevents replay. A checkpoint/fsync
            # failure must not overwrite a possibly committed newer checkpoint.
            s["intent"]["status"] = "UNKNOWN"
        return self._result(s)

    @staticmethod
    def _result(s):
        result = {"phase": s["phase"], "status": "complete" if s["phase"] == "COMPLETE" else
                  ("unknown_inspect_only" if s.get("intent") else "pending"),
                  "historical_failed": 1, "historical_dropped": 3}
        if s["phase"] == "COMPLETE":
            result["adoption_receipt"] = copy.deepcopy(s["adoption_receipt"])
        return result

    def advance(self):
        """At most one effect per call. Pending old connections never get killed."""
        with self.store.locked():
            s = self.store.read()
            if s is None:
                o = self._observe()
                need(o["old"].get("state") == "running" and o.get("new") is None, "INITIAL_APPS_INVALID")
                nat = o.get("nat")
                need(type(nat) is dict and set(nat) == {"output", "other"} and type(nat["output"]) is list and
                     REDIRECT not in nat["output"], "INITIAL_NAT_INVALID")
                s = {"v": 1, "owner": self.owner, "phase": "INITIAL", "intent": None,
                     "completed_intents": [], "old_identity": self._identity(o["old"]),
                     "nat_before": copy.deepcopy(nat)}
                return self._effect(s, "archive", lambda: self.b.archive_history(self._spec(s)))
            need(type(s) is dict and s.get("v") == 1 and s.get("owner") == self.owner, "STATE_OWNER_CHANGED")
            if s.get("intent"):
                try:
                    self._inspect_effect(s)
                except Exception:
                    # A lost prepare/start/route/signal reply cannot authorize a
                    # repeat. Leave the original intent for independent review.
                    pass
                return self._result(s)
            o = self._observe(s)
            if s["phase"] == "COMPLETE":
                self._new(s, o.get("new"), running=True)
                need(o["old"].get("state") == "exited", "OLD_RESTARTED")
                return self._result(s)
            need(o["old"].get("state") == "running", "OLD_EXITED_BEFORE_DRAIN")
            if s["phase"] == "ARCHIVED":
                need(self._history(self.b.inspect_history()) == s["history"], "HISTORY_ARCHIVE_CHANGED")
                return self._effect(s, "prepare", lambda: self.b.prepare_app(self._spec(s)))
            if s["phase"] == "PREPARED":
                self._new(s, o.get("new"))
                need(o["new"].get("state") == "created", "NEW_APP_NOT_INERT")
                return self._effect(s, "start", lambda: self.b.start_app(s["new_identity"]["container_id"]))
            self._new(s, o.get("new"), running=True)
            if s["phase"] == "HEALTHY":
                return self._effect(s, "route", lambda: self.b.install_redirect(copy.deepcopy(REDIRECT), copy.deepcopy(s["nat_before"])))
            need(s["phase"] == "DRAINING", "UNKNOWN_PHASE")
            work = o["old"].get("work", {})
            need(o["old"].get("tcp_scope") == "all-accepted-including-idle" and
                 set(work) == set(QUIET_FIELDS) and all(type(work[k]) is int and work[k] >= 0 for k in QUIET_FIELDS),
                 "COMPLETE_DRAIN_OBSERVATION_REQUIRED")
            now = self.clock()
            quiet = s.get("quiet")
            if any(work.values()):
                s["quiet"] = None
            elif quiet is None or quiet["boot_id"] != o["boot_id"] or now < quiet["since"]:
                s["quiet"] = {"since": now, "samples": 1, "last_sample": o["sample_id"], "boot_id": o["boot_id"]}
            elif quiet["last_sample"] != o["sample_id"]:
                quiet["samples"] += 1
                quiet["last_sample"] = o["sample_id"]
                if now - quiet["since"] >= self.quiet_seconds and quiet["samples"] >= self.quiet_samples:
                    return self._effect(s, "term", lambda: self.b.signal_old_once(OLD_ID))
            self._save(s)
            return self._result(s)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true", help="show the fixed offline contract")
    parser.parse_args(argv)
    print(json.dumps({"offline": True, "production_adapter_required": True, "old_container_id": OLD_ID,
                      "new_name": NEW_NAME, "image_id": IMAGE, "new_api_port": 8083, "new_control_port": 18093}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
