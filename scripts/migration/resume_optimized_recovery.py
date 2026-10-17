#!/usr/bin/env python3
"""Once-only continuation of recovered attempt 6; historical tools stay intact."""
import argparse
import hashlib
import pathlib
import types

BASE_SHA = "6246e7ee0b776518419868e7ad322867ebbcb652d2756d0995db26e01b9068ff"
OLD_RPC = "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec"
NEW_RPC = "85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a"
OLD_CONFIG = "ca7d126ee925221fce6bf88d42eccd11641116330a1cedfb1cb7323c50c3b032"
NEW_CONFIG = "8f47473b2a430b4f0b2f9b993cc75eed6c4e405abe8beb678a11e25636a2d40d"
SCOPE_NAME = "resume-optimized-attempt6-20261017"


def need(value, code):
    if not value:
        raise RuntimeError(code)


def derive(raw):
    need(hashlib.sha256(raw).hexdigest() == BASE_SHA, "reviewed-recovery-base-changed")
    text = raw.decode()
    replacements = [
        ("18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4", "5419ce229ed41bd98b9b4143d15f89817236c29b94071f7c8fb54dfefa86fb14", 1),
        (OLD_RPC, NEW_RPC, 1), (OLD_CONFIG, NEW_CONFIG, 1),
        ("18.332221914082766", "30.280733209103346", 1),
        ("f3f1df620d449b751e544ebb34111b1b807f8d4cba171b65214b0644fcbf6be8", "866a4d665f45b671d385d67028b0b46ffa881142bd0a6c63c0989fe7a224ff56", 1),
        ("4bb9a93b3b64f8d91b67165f10ec36d7e2c7b61321de25ae1fb2014ed79835bd", "a8c7730e88f85946fc113eb28f82d9382788def742c75e1ed658e90395ba6112", 1),
        ("7e627ac9d821d566a05d6d03123c7e8c659b1e09e395e86a178b3173b216442d", "7e086ba2e50e496381e3c9efd8952847dcefddff71100dcb1480282f5ef9427a", 1),
        ("c631d3ff7aa10cb0fdf306566d77d3fb84dea6b9bb585922e599bf55721d95fb", "5c0e2528b2d135bc61d248ef9fde41f9c064e5dbdfc768ef79c8df8f254b53a8", 1),
        ("10b4ebf48d69833af52b7d9e6c17b4ed", "5b8ac4098bfcb407742dcc2ecb845298", 1),
        ("9f3d68ecaf85192bd9e9456c387f3b1e", "604dbd5e9caf0536f392529591ff876d", 1),
        ('state.get("attempt") == 4', 'state.get("attempt") == 6', 1),
        ('current.get("attempt", 0) > 4', 'current.get("attempt", 0) > 6', 1),
        ("== 17", "== 24", 5),
        ('"ledger_epoch": 17, "public_route_proven"', '"ledger_epoch": 24, "public_route_proven"', 1),
        ('restored["result"]["ingress"]["epoch"] == 2', 'restored["result"]["ingress"]["epoch"] == 6', 1),
        ('"restored_ingress_epoch": 2', '"restored_ingress_epoch": 6', 1),
        ('ingress_epoch=2', 'ingress_epoch=6', 1),
        ('need(not failed.get("instance_id") and not failed.get("substeps"), "attempt4-quiesce-progressed")',
         'need(failed.get("instance_id") == INSTANCE and set(failed.get("substeps", {})) == {"ingress", "writers", "redis", "postgres"} and all(row.get("phase") == "COMPLETE" for row in failed["substeps"].values()), "attempt6-quiesce-not-complete")', 1),
        ('program = pathlib.Path(__file__).read_bytes()', 'program = DERIVED_PROGRAM', 1),
    ]
    for before, after, count in replacements:
        need(text.count(before) == count, "reviewed-continuation-anchor-changed")
        text = text.replace(before, after)
    need(text.count("resume-idle-attempt4-20261017") == 4, "reviewed-scope-anchor-changed")
    text = text.replace("resume-idle-attempt4-20261017", SCOPE_NAME)
    anchor = '    canary = support.load_bytes("resume_target_preflight", LIB / "recovery_generation_canary.py", text.encode())'
    # The original adoption receipt binds the ORIGINAL controller config.
    # Verify its exact predecessor and the append-only upgrade receipt, then
    # continue to validate every resource against the new config unchanged.
    upgrade = '''    upgrade_raw = support.root_read(ROOT / "rpc/read-optimization-20261017/verification.json")
    upgrade = support.strict(upgrade_raw)
    need(upgrade == {"v": 1, "run": RUN, "owner": OWNER, "old_program_sha256": OLD_RPC_PIN,
                    "new_program_sha256": NEW_RPC_PIN, "old_config_sha256": OLD_CONFIG_PIN,
                    "new_config_sha256": TARGET_CONFIG_SHA, "ledger_sha256": TARGET_LEDGER_SHA,
                    "business_processes_unchanged": True}, "rpc-upgrade-receipt-changed")
    previous_config = support.root_read(ROOT / "rpc/read-optimization-20261017/previous-config.json")
    current_config = support.root_read(ROOT / "rpc/config.json")
    old_cfg, new_cfg = support.strict(previous_config), support.strict(current_config)
    need(sha(previous_config) == OLD_CONFIG_PIN and sha(current_config) == TARGET_CONFIG_SHA and
         old_cfg.get("program_sha256") == OLD_RPC_PIN and new_cfg == {**old_cfg, "program_sha256": NEW_RPC_PIN},
         "rpc-upgrade-config-delta-changed")
    need(text.count(OLD_RPC_PIN) == 1, "canary-program-pin-anchor-changed")
    text = text.replace(OLD_RPC_PIN, NEW_RPC_PIN)
    before = 'receipt.get("controller_config_raw_sha256") == sha(root_read(ROOT / "rpc/config.json"))'
    need(text.count(before) == 1, "canary-adoption-pin-anchor-changed")
    text = text.replace(before, 'receipt.get("controller_config_raw_sha256") == "' + OLD_CONFIG_PIN + '"')
    before = '"controller_config_sha256": receipt["controller_config_raw_sha256"]'
    need(text.count(before) == 1, "canary-current-config-anchor-changed")
    text = text.replace(before, '"controller_config_sha256": sha(root_read(ROOT / "rpc/config.json"))')
'''
    need(text.count(anchor) == 1, "target-check-anchor-changed")
    text = text.replace(anchor, upgrade + anchor)
    text = text.replace('SAVED = SCOPE / ', 'OLD_RPC_PIN = ' + repr(OLD_RPC) + '\nNEW_RPC_PIN = ' + repr(NEW_RPC) + '\nOLD_CONFIG_PIN = ' + repr(OLD_CONFIG) + '\nSAVED = SCOPE / ', 1)
    result = text.encode()
    compile(result, "reviewed-attempt6-continuation", "exec")
    return result


def load_derived(raw):
    module = types.ModuleType("optimized_recovery_derived")
    module.__file__ = __file__
    program = derive(raw)
    exec(compile(program, __file__, "exec"), module.__dict__)
    module.DERIVED_PROGRAM = program
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    module = load_derived(pathlib.Path(__file__).with_name("resume_idle_recovery.py").read_bytes())
    return module.main(["--apply"] if args.apply else ["--inspect"] if args.inspect else [])


if __name__ == "__main__":
    raise SystemExit(main())
