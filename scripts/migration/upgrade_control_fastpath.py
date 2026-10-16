#!/usr/bin/env python3
"""CAS-install the reviewed single-window controller before its first pause."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

OLD = {
    "target_rpc.py": "dffaf4274a6d92f7d745a7ae5c1be268a7cb4e890820cbdbf4c80029073ec947",
    "runner_backend.py": "7904f16e12ef9893b910961c2d51d5156207a7463c8c82950ceb40f64b46638d",
    "durable_runner.py": "604052d8dd144fc156e94e55e6af6557e331263afa70db5b5c09a25329b87ef9",
    "runner_service.py": "407a0b02d40b1416c9aaace631d53480d789a792fb46b9ef5e9f3519fec54267",
}
NEW = {
    "target_rpc.py": "30faa6c7d50cf72800bbc102af2550d1e01b187b4b5894d2b18ab2d9dd0406b1",
    "runner_backend.py": "afb43d190c7e80ed2e9759c88ce5d0b6cefbbf7755eeb1b04f53a0f7de131dd1",
    "durable_runner.py": "b721cb9a9dc7de8ada54c0795b94c08b7cd9fac4ac37f5bbf0f9a01ced5bba7a",
    "runner_service.py": "f84d86611bae5baec488f857d47283b5c7443095fd2798ecadb0706f5096bfa5",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "new": NEW, "customer_route_changes": False}))
        return
    root = pathlib.Path(__file__).parent
    prior = runpy.run_path(str(root / "upgrade_control_reads.py"))
    common = prior["COMMON"].replace("read-upgrade-20261017", "fastpath-upgrade-20261017")
    target = prior["TARGET"].replace(
        "98f37d9476e8317d06b85893593222d7a1808c2c8a7a481d372b463a6e056ce7",
        "99b9d4bd51bbc9c46035ddb9ae0b782c9ebc221a2e26f457352e7ab04a5c1bcb")
    source = prior["SOURCE"]
    programs = {name: (root / name).read_bytes() for name in NEW}
    assert all(hashlib.sha256(raw).hexdigest() == NEW[name] for name, raw in programs.items())
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    payload = json.dumps({"old": OLD, "new": NEW,
                          "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()}}).encode()
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    for label, remote, argv in (("target", target, channel["TARGET"]), ("source", source, channel["SSH_SOURCE"])):
        path = folder / (label + "-fastpath-upgrade-20261017.json")
        assert not path.exists(), "existing-upgrade-evidence-inspect-before-resuming"
        result = subprocess.run(argv + ["sudo python3 -I -c " + shlex.quote(common + remote)],
                                input=payload, capture_output=True, timeout=40)
        evidence = result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
            "stdout": result.stdout.decode(errors="replace")[:4096], "stderr": result.stderr.decode(errors="replace")[:4096]}).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(evidence); out.flush(); os.fsync(out.fileno())
        assert result.returncode == 0, "upgrade-incomplete-inspect-private-evidence"
        print(result.stdout.decode())


if __name__ == "__main__":
    main()
