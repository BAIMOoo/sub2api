#!/usr/bin/env python3
"""New durable canary after an exact, proven unsent postcutover lane.

The original failed journal and the original canary program remain unchanged.
Only RPC OPERATION_BUSY from read-only preflight/SQL may be read again. The
reviewed model POST, TLS, SSE, billing and durable no-repeat logic are reused.
Default execution is offline; --apply sends at most one billable request.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import time
import types
import uuid

ORIGINAL_SHA256 = "4ad1856dc17f1de1818a80c3100f950ad9e02c309e7ba737464838db8c541725"
FAILED_REPORT_SHA256 = "0dc9baa14e3968ee667711ae1f6565dcbf019d27034a23392a9fa7ad8d9a8b5a"
FAILED_LOCAL_REPORT = ".codex/backups/sub2api-postcutover-canary-9bc24f91088a4ab9/report.json"
FAILED_REMOTE_REPORT = pathlib.Path("/var/lib/sub2api-migration-20261016/postcutover-canary/target.json")
SCOPE = FAILED_REMOTE_REPORT.parent.with_name("postcutover-confirmed-canary-20261017")
LANE = "postcutover-confirmed"
MAX_READ_ATTEMPTS, READ_RETRY_SECONDS, READ_RETRY_DELAY = 8, 3.0, 0.15


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def reused_postcutover(raw):
    need(hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA256, "reviewed-postcutover-canary-changed")
    module = types.ModuleType("confirmed_reused_postcutover_canary")
    module.__file__ = str(pathlib.Path(__file__).with_name("postcutover_canary.py"))
    exec(compile(raw, "reviewed-postcutover-canary", "exec"), module.__dict__)
    need(module.SCOPE / module.JOURNAL == FAILED_REMOTE_REPORT,
         "original-postcutover-journal-path-changed")
    return module


def load_original():
    return reused_postcutover(pathlib.Path(__file__).with_name("postcutover_canary.py").read_bytes())


def validate_unsent_report(raw):
    try:
        report = json.loads(raw)
    except (ValueError, TypeError):
        raise Refusal("original-unsent-report-invalid") from None
    need(type(report) is dict and hashlib.sha256(canonical(report)).hexdigest() == FAILED_REPORT_SHA256,
         "original-unsent-report-sha-changed")
    need(report.get("ok") is False and report.get("request_attempted") is False and
         report.get("lane") == "postcutover" and report.get("stream") == {} and
         not report.get("post_intent_durable") and report.get("journal_path") == str(FAILED_REMOTE_REPORT),
         "original-model-request-not-proven-unsent")
    return report


def local_unsent_report():
    path = pathlib.Path.home() / FAILED_LOCAL_REPORT
    need(not any(p.is_symlink() for p in [path, *path.parents]), "original-local-report-symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 2 * 1024**2,
             "original-local-report-not-private")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return validate_unsent_report(handle.read(2 * 1024**2 + 1))
    finally:
        os.close(fd)


class ReadOnlyBusyAdapter:
    """Retry exact RPC busy refusals only on the two enumerated read paths."""
    def __init__(self, backend, rpc, refusal=Refusal):
        self.backend, self.rpc, self.refusal = backend, rpc, refusal
        self.events = []

    def _read(self, name, *args):
        need(name in {"preflight", "sql"}, "only-read-only-methods-may-retry")
        deadline = time.monotonic() + READ_RETRY_SECONDS
        for attempt in range(1, MAX_READ_ATTEMPTS + 1):
            try:
                return getattr(self.backend, name)(*args)
            except Exception as error:
                if not isinstance(error, self.rpc.Refusal) or error.code != "OPERATION_BUSY":
                    raise
                self.events.append({"operation": name, "busy_attempt": attempt})
                if attempt == MAX_READ_ATTEMPTS or time.monotonic() + READ_RETRY_DELAY > deadline:
                    raise self.refusal("read-only-operation-busy-exhausted") from None
                time.sleep(READ_RETRY_DELAY)

    def preflight(self):
        return self._read("preflight")

    def sql(self, query):
        return self._read("sql", query)


def remote_apply(payload, original, base, backend=None, rpc=None):
    need(os.geteuid() == 0 and sys.platform == "linux", "tencent-linux-root-required")
    # Both copies must prove the prior lane never reached its durable POST intent.
    validate_unsent_report(original.root_read(FAILED_REMOTE_REPORT))
    if backend is None:
        rpc, cfg = original.load_target()
        backend = original.TargetBackend(rpc, cfg)
    need(rpc is not None and isinstance(rpc.Refusal, type), "original-rpc-refusal-type-required")
    adapter = ReadOnlyBusyAdapter(backend, rpc, original.Refusal)
    original.SCOPE = SCOPE
    private_write = base.private_write

    def journal(path, report, **kwargs):
        need(pathlib.Path(path) == SCOPE / original.JOURNAL, "new-journal-scope-required")
        report.update(lane=LANE, original_unsent_report_sha256=FAILED_REPORT_SHA256,
                      original_unsent_journal=str(FAILED_REMOTE_REPORT),
                      read_only_busy_retries=list(adapter.events),
                      read_only_busy_policy={"max_attempts": MAX_READ_ATTEMPTS,
                                             "retry_window_seconds": READ_RETRY_SECONDS})
        return private_write(path, report, **kwargs)

    base.private_write = journal
    try:
        return original.remote_apply(payload, base, adapter)
    finally:
        base.private_write = private_write


def remote_inspect(original):
    original.SCOPE = SCOPE
    report = original.remote_inspect()
    need(report.get("lane") == LANE and report.get("original_unsent_report_sha256") == FAILED_REPORT_SHA256 and
         report.get("original_unsent_journal") == str(FAILED_REMOTE_REPORT), "new-journal-binding-invalid")
    return report


def remote_source(payload=None, inspect=False):
    # The original remote_source reads its own __file__; embed its exact bytes
    # explicitly instead of rebinding __file__ to this continuation's filename.
    raw = pathlib.Path(__file__).with_name("postcutover_canary.py").read_bytes()
    original = reused_postcutover(raw)
    embedded = "original=reused_postcutover(base64.b64decode(" + repr(base64.b64encode(raw).decode()) + "))\n"
    if inspect:
        call = "remote_inspect(original)"
    else:
        model_raw = pathlib.Path(__file__).with_name("model_canary.py").read_bytes()
        original.reused_base(model_raw)  # Validate the independent model-program pin before any SSH.
        call = "remote_apply(" + repr(payload) + ",original,original.reused_base(base64.b64decode(" + repr(base64.b64encode(model_raw).decode()) + ")))"
    source = pathlib.Path(__file__).read_text()
    return ("__name__='confirmed_canary_remote'\n__file__='postcutover_confirmed_canary.py'\n" + source +
            "\n" + embedded + "try:\n result=" + call +
            "\n print(json.dumps(result,sort_keys=True),flush=True)\nexcept Exception:\n" +
            " print(json.dumps({'ok':False,'error':'confirmed-canary-remote-refused-inspect-journal-no-repeat'}),flush=True)\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "network_access": False, "billable_request_sent": False,
                          "lane": LANE, "original_unsent_report_sha256": FAILED_REPORT_SHA256,
                          "durable_no_repeat_journal": str(SCOPE / "target.json")}))
        return 0
    original = load_original()
    base = original.load_base()
    payload = {}
    if args.apply:
        local_unsent_report()  # Fail before credentials or network if the original receipt changed.
        payload = {"token": base.load_token(), "request_id": "migration-canary-postcutover-" + uuid.uuid4().hex,
                   "client_request_id": str(uuid.uuid4())}
    local = pathlib.Path.home() / ".codex/backups" / ("sub2api-postcutover-confirmed-canary-" + uuid.uuid4().hex[:16])
    base.no_symlinks(local)
    local.mkdir(mode=0o700, parents=True)
    base.private_write(local / "request.json", {k: v for k, v in payload.items() if k != "token"})
    try:
        result = subprocess.run(original.target_ssh() + ["sudo -n python3 -I -"],
            input=remote_source(payload, inspect=args.inspect).encode(), capture_output=True, timeout=220)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("confirmed-canary-ssh-outcome-unknown-inspect-journal-no-repeat") from None
    need(result.returncode == 0, "confirmed-canary-ssh-unavailable-inspect-journal-no-repeat")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise Refusal("confirmed-canary-ssh-json-invalid-inspect-journal-no-repeat") from None
    need(type(report) is dict and (not payload.get("token") or payload["token"] not in canonical(report).decode()),
         "secret-or-invalid-evidence-refused")
    base.private_write(local / "report.json", report)
    print(json.dumps({**report, "local_report": str(local / "report.json"),
                      "report_sha256": hashlib.sha256(canonical(report)).hexdigest()}, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "confirmed-canary-outcome-unknown-inspect-journal-no-repeat"}), file=sys.stderr)
        raise SystemExit(1)
