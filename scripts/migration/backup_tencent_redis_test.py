#!/usr/bin/env python3
"""Offline guard, framing, encryption, fault, and one-shot tests; no SSH."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

spec = importlib.util.spec_from_file_location('backup_tencent_redis',pathlib.Path(__file__).with_name('backup_tencent_redis.py'))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
B = R.helper()


def info():
    return {'redis_version':'8.6.2','run_id':R.EXPECTED_RUN_ID,'role':'master',
            'loading':'0','async_loading':'0','rdb_bgsave_in_progress':'0',
            'aof_rewrite_in_progress':'0','aof_rewrite_scheduled':'0',
            'module_fork_in_progress':'0','current_cow_size':'0',
            'rdb_last_bgsave_status':'ok','used_memory':'20000000','used_memory_rss':'42000000'}


def settings():
    return {'repl-diskless-sync':'yes','repl-diskless-sync-delay':'5','rdbchecksum':'yes'}


def response(stdout=b'',stderr=b'',code=0,failure=None):
    return {'stdout':stdout,'stderr':stderr,'returncode':code,'failure':failure}


# Empty RDB with a genuine CRC64, independently accepted by the target's
# redis-check-rdb /dev/stdin during read-only capability verification.
PAYLOAD = bytes.fromhex('524544495330303132ff10d0db4120bef4d3')
WIRE = PAYLOAD + b'abcdef0123456789abcdef0123456789abcdef01'
DIAGNOSTICS = b"SYNC sent to master, writing bytes of bulk transfer until EOF marker to '-'\nTransfer finished with success after " + str(len(PAYLOAD)).encode() + b' bytes\n'


class GuardTest(unittest.TestCase):
    def test_available_memory_and_identity_pass(self):
        result = R.snapshot_safety(info(),1024**3,settings(),0)
        self.assertEqual(result['required_available_bytes'],512*1024**2)
        self.assertEqual(result['container_memory_limit_bytes'],0)

    def test_rejects_identity_loading_fork_and_failed_previous_save(self):
        changes = [('redis_version','8.0.0'),('run_id','0'*40),('role','slave'),
                   ('rdb_last_bgsave_status','err')]
        changes += [(key,'1') for key in ('loading','async_loading','rdb_bgsave_in_progress',
                    'aof_rewrite_in_progress','aof_rewrite_scheduled','module_fork_in_progress','current_cow_size')]
        for key,value in changes:
            altered=info();altered[key]=value
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                R.snapshot_safety(altered,1024**3,settings(),0)

    def test_rejects_missing_info_fields(self):
        for key in info():
            altered=info();del altered[key]
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                R.snapshot_safety(altered,1024**3,settings(),0)

    def test_rejects_changed_diskless_checksum_or_excessive_delay(self):
        for key,value in [('repl-diskless-sync','no'),('rdbchecksum','no'),
                          ('repl-diskless-sync-delay','11'),('repl-diskless-sync-delay','unknown')]:
            changed=settings();changed[key]=value
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                R.snapshot_safety(info(),1024**3,changed,0)

    def test_rejects_memory_pressure_capture_limits_and_unknown_numbers(self):
        cases = [(info(),100*1024**2,0),(info(),1024**3,100*1024**2),
                 (info(),True,0),(info(),1024**3,-1)]
        for key,value in [('used_memory',str(65*1024**2)),('used_memory_rss',str(257*1024**2)),
                          ('used_memory','NaN'),('used_memory','0')]:
            changed=info();changed[key]=value;cases.append((changed,1024**3,0))
        for values in cases:
            with self.subTest(values=values),self.assertRaises(RuntimeError):
                R.snapshot_safety(values[0],values[1],settings(),values[2])


class FramingTest(unittest.TestCase):
    def test_removes_exactly_protocol_marker(self):
        self.assertEqual(R.canonical_rdb(WIRE,DIAGNOSTICS),PAYLOAD)
        self.assertEqual(R.transfer_length(DIAGNOSTICS),len(PAYLOAD))

    def test_refuses_truncation_extra_bytes_nonhex_or_wrong_length(self):
        for raw in (WIRE[:-1],WIRE+b'x',PAYLOAD+b'X'*40,PAYLOAD):
            with self.subTest(raw=raw),self.assertRaisesRegex(RuntimeError,'TRAILER_INVALID'):
                R.canonical_rdb(raw,DIAGNOSTICS)

    def test_requires_single_success_and_eof_mode(self):
        for text in (b'',DIAGNOSTICS+DIAGNOSTICS,
                     DIAGNOSTICS.replace(b'until EOF marker to',b'definite payload to'),
                     DIAGNOSTICS.replace(b'success after',b'failed after')):
            with self.subTest(text=text),self.assertRaises(RuntimeError):
                R.transfer_length(text)

    def test_rejects_disabled_checksum_invalid_header_or_missing_eof_opcode(self):
        for raw in (b'WRONG0012'+PAYLOAD[9:],PAYLOAD[:-8]+b'\0'*8,
                    PAYLOAD[:-9]+b'X'+PAYLOAD[-8:]):
            with self.subTest(raw=raw),self.assertRaisesRegex(RuntimeError,'HEADER_OR_CHECKSUM'):
                R.canonical_rdb(raw+WIRE[-40:],DIAGNOSTICS)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=pathlib.Path(self.temp.name).resolve()/'redis-backup'
        self.operation='a'*32

    def good(self,hash_override=None):
        proof={'checker_verified':True,'rdb_sha256':hash_override or R.sha(PAYLOAD)}
        return mock.Mock(side_effect=[response(b'{}'),response(WIRE,DIAGNOSTICS),
                                     response(json.dumps(proof).encode())])

    def execute(self,remote):
        return R.apply(self.folder,['no-ssh'],B,remote=remote,operation=self.operation)

    def test_default_offline_never_loads_helper_or_channel(self):
        with mock.patch.object(R,'helper',side_effect=AssertionError('no helper')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            R.main([])
        self.assertTrue(json.loads(output.getvalue())['offline'])

    def test_success_encrypts_framed_wire_and_canonical_rdb_only_to_disk(self):
        remote=self.good();report=self.execute(remote)
        self.assertEqual(report['status'],'complete')
        self.assertFalse(report['cross_service_atomic'])
        self.assertEqual(remote.call_count,3)
        self.assertEqual(remote.call_args_list[2].kwargs['input'],PAYLOAD)
        self.assertIn('--rdb',remote.call_args_list[1].args[1])
        self.assertNotIn('--rdb',remote.call_args_list[0].args[1])
        self.assertNotIn('--rdb',remote.call_args_list[2].args[1])
        key=(self.folder/'backup.key').read_bytes()
        for name,expected in [('snapshot-wire',WIRE),('redis.rdb',PAYLOAD)]:
            blob=(self.folder/(name+'.aesgcm')).read_bytes()
            recovered=AESGCM(key).decrypt(blob[:12],blob[12:],(R.RUN+'/'+name).encode())
            self.assertEqual(recovered,expected)
            self.assertEqual(report['files'][name]['plaintext_sha256'],hashlib.sha256(expected).hexdigest())
        for path in self.folder.iterdir():
            self.assertNotIn(PAYLOAD,path.read_bytes())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertTrue((self.folder/'snapshot.intent.json').exists())
        self.assertEqual(json.loads((self.folder/'verification.json').read_bytes()),report)

    def test_existing_local_intent_scope_blocks_any_second_remote_operation(self):
        remote=self.good();self.execute(remote)
        other=self.good()
        with self.assertRaises(FileExistsError):self.execute(other)
        other.assert_not_called()

    def test_unknown_snapshot_is_never_retried_and_partial_output_encrypted(self):
        remote=mock.Mock(side_effect=[response(b'{}'),response(b'partial-secret',b'private stderr',255,'COMMAND_TIMEOUT')])
        with self.assertRaisesRegex(RuntimeError,'do-not-retry'):self.execute(remote)
        self.assertEqual(remote.call_count,2)
        failure=json.loads((self.folder/'failure.json').read_bytes())
        self.assertTrue(failure['retry_forbidden'])
        self.assertEqual(failure['failed_stage'],'snapshot')
        self.assertIn('snapshot.unverified-partial',failure['files'])
        for path in self.folder.iterdir():
            self.assertNotIn(b'partial-secret',path.read_bytes())
            self.assertNotIn(b'private stderr',path.read_bytes())
        self.assertFalse((self.folder/'verification.json').exists())

    def test_bad_framing_keeps_encrypted_wire_and_stops_before_checker(self):
        remote=mock.Mock(side_effect=[response(b'{}'),response(WIRE[:-1],DIAGNOSTICS)])
        with self.assertRaisesRegex(RuntimeError,'do-not-retry'):self.execute(remote)
        self.assertEqual(remote.call_count,2)
        self.assertTrue((self.folder/'snapshot-wire.aesgcm').exists())
        self.assertFalse((self.folder/'redis.rdb.aesgcm').exists())

    def test_checker_failure_keeps_snapshot_but_not_success_verification(self):
        remote=mock.Mock(side_effect=[response(b'{}'),response(WIRE,DIAGNOSTICS),response(b'',b'CRC failed',1)])
        with self.assertRaisesRegex(RuntimeError,'do-not-retry'):self.execute(remote)
        self.assertTrue((self.folder/'redis.rdb.aesgcm').exists())
        self.assertFalse((self.folder/'verification.json').exists())
        self.assertEqual(json.loads((self.folder/'failure.json').read_bytes())['failed_stage'],'verify')

    def test_verification_hash_mismatch_cannot_pass(self):
        with self.assertRaisesRegex(RuntimeError,'do-not-retry'):self.execute(self.good('f'*64))
        self.assertFalse((self.folder/'verification.json').exists())

    def test_preflight_failure_stops_before_durable_snapshot_intent(self):
        remote=mock.Mock(return_value=response(b'',b'insufficient memory',1))
        with self.assertRaisesRegex(RuntimeError,'do-not-retry'):self.execute(remote)
        remote.assert_called_once()
        self.assertFalse((self.folder/'snapshot.intent.json').exists())

    def test_remote_programs_compile_single_snapshot_and_use_independent_checker(self):
        preflight,snapshot,verify=R.programs(B,self.operation)
        for name,program in [('preflight',preflight),('snapshot',snapshot),('verify',verify)]:
            compile(program,name,'exec')
            self.assertNotIn('assert ',program)
        self.assertEqual(snapshot.count("'--rdb','-'"),1)
        self.assertIn("scope.mkdir(mode=0o700)",snapshot)
        self.assertLess(snapshot.index("r.atomic_json(scope/'intent.json',intent)"),snapshot.index("'--rdb','-'"))
        self.assertIn("'redis-check-rdb','/dev/stdin'",verify)
        self.assertIn("b'Checksum OK'",verify)
        self.assertNotIn('BGSAVE',snapshot)
        self.assertNotIn('CLIENT PAUSE',snapshot)
        self.assertNotIn('CONFIG SET',snapshot)
        self.assertNotIn('CONFIG REWRITE',snapshot)

    def test_invalid_operation_rejected_before_any_snapshot(self):
        with self.assertRaisesRegex(RuntimeError,'OPERATION_INVALID'):
            R.programs(B,'bad;command')


if __name__=='__main__':unittest.main()
