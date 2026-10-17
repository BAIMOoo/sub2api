#!/usr/bin/env python3
"""Offline allowlist, stable-read, encryption and failure tests; no SSH."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import tarfile
import tempfile
import types
import unittest
from unittest import mock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

s=importlib.util.spec_from_file_location('backup_runtime_bootstrap',pathlib.Path(__file__).with_name('backup_runtime_bootstrap.py'))
M=importlib.util.module_from_spec(s);s.loader.exec_module(M);B=M.helper()


def fixture(change=None,extra=None,duplicate=None):
    rows=[];contents={}
    files=list(M.FILES)
    for kind,names in M.VOLUME_CONFIGS.items():
        files.extend((M.VOLUME_PATHS[kind]+'/'+name,70 if kind=='pg' else 999,0o600,1024) for name in names)
    for path,uid,mode,limit in files:
        raw=('protected-'+M.sha(path.encode())[:8]).encode()
        rows.append({'path':path,'uid':uid,'gid':uid,'mode':mode,'bytes':len(raw),'sha256':M.sha(raw)})
        contents['files/'+path.lstrip('/')]=raw
    manifest={'v':1,'files':rows,'explicit_systemd_masks':[{'path':p,'target':'/dev/null','uid':0} for p in M.MASKS],
              'cross_service_atomic':False,'no_images_or_database_relations':True}
    for kind in ('pg','redis','app'):
        contents['metadata/docker-'+kind+'.inspect.json']=json.dumps({'Id':'a'*64,'State':{'Running':True}}).encode()
    contents['metadata/systemd.show.txt']=b'Id=fixed\nUnitFileState=enabled\n'
    if change:change(manifest,contents)
    contents['manifest.json']=json.dumps(manifest,sort_keys=True).encode()
    if extra:contents[extra]=b'extra'
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as archive:
        for name,raw in contents.items():
            member=tarfile.TarInfo(name);member.mode=0o600;member.size=len(raw)
            archive.addfile(member,io.BytesIO(raw))
        if duplicate:
            member=tarfile.TarInfo(duplicate);member.mode=0o600;archive.addfile(member,io.BytesIO())
    return out.getvalue()


def result(stdout=b'',stderr=b'',code=0,failure=None):
    return {'stdout':stdout,'stderr':stderr,'returncode':code,'failure':failure}


class ArchiveTest(unittest.TestCase):
    def test_fixed_allowlist_archive_validates(self):
        proof=M.validate(fixture())
        self.assertEqual(proof['files'],50)
        self.assertGreater(proof['uncompressed_bytes'],0)

    def test_missing_required_file_rejected(self):
        def change(manifest,contents):
            row=manifest['files'].pop();contents.pop('files/'+row['path'].lstrip('/'))
        with self.assertRaisesRegex(RuntimeError,'ALLOWLIST'):M.validate(fixture(change))

    def test_unexpected_extra_file_rejected(self):
        for name in ('files/etc/passwd','../outside','/absolute'):
            with self.subTest(name=name),self.assertRaises(RuntimeError):M.validate(fixture(extra=name))

    def test_database_relation_or_image_cannot_replace_allowlisted_config(self):
        def change(manifest,contents):
            row=manifest['files'][-1];old=row['path'];row['path']=M.VOLUME_PATHS['redis']+'/dump.rdb'
            contents['files/'+row['path'].lstrip('/')]=contents.pop('files/'+old.lstrip('/'))
        with self.assertRaisesRegex(RuntimeError,'ALLOWLIST'):M.validate(fixture(change))

    def test_digest_corruption_rejected(self):
        def change(manifest,contents):contents['files/'+manifest['files'][0]['path'].lstrip('/')]=b'corrupt'
        with self.assertRaisesRegex(RuntimeError,'DIGEST'):M.validate(fixture(change))

    def test_duplicate_member_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'DUPLICATE'):M.validate(fixture(duplicate='manifest.json'))

    def test_wrong_mask_target_is_not_silently_followed(self):
        def change(manifest,contents):manifest['explicit_systemd_masks'][0]['target']='/etc/shadow'
        with self.assertRaisesRegex(RuntimeError,'MASK_PROOF'):M.validate(fixture(change))

    def test_stopped_container_metadata_rejected(self):
        def change(manifest,contents):contents['metadata/docker-pg.inspect.json']=b'{"Id":"a","State":{"Running":false}}'
        with self.assertRaisesRegex(RuntimeError,'CONTAINER_METADATA'):M.validate(fixture(change))

    def test_remote_program_compiles_without_recursive_capture_or_mutations(self):
        for check in (True,False):compile(M.program(B,check),'bootstrap-remote','exec')
        code=M.program(B)
        self.assertNotIn('rglob',code)
        self.assertNotIn("'start'",code)
        self.assertNotIn("'restart'",code)
        self.assertNotIn("'reload'",code)
        self.assertNotIn("'pg_dump'",code)
        self.assertNotIn("'--rdb'",code)
        self.assertNotIn('assert ',code)
        self.assertIn('BOOTSTRAP_MASK_LINK_CHANGED',code)
        self.assertIn("'BOOTSTRAP_VOLUME_PATH_CHANGED'",code)


class MountComparisonTest(unittest.TestCase):
    def setUp(self):
        namespace={'require':M.require};exec(M.MOUNT_COMPARE,namespace)
        self.normalize=namespace['normalized_mounts']

    def test_reorder_preserves_full_mount_identity(self):
        first={'Destination':'/app/data','Source':'/fixed/data','RW':True,'Type':'bind'}
        second={'Destination':'/migration-state','Source':'/fixed/state','RW':False,'Type':'bind'}
        self.assertEqual(self.normalize([first,second]),self.normalize([second,first]))
        changed=dict(first,RW=False)
        self.assertNotEqual(self.normalize([first,second]),self.normalize([changed,second]))
        self.assertNotEqual(self.normalize([first]),self.normalize([first,second]))

    def test_duplicate_missing_destination_and_nonlist_rejected(self):
        for mounts in ([{'Destination':'/same'},{'Destination':'/same'}],[{}],
                       [{'Destination':None}],[{'Destination':''}],{}):
            with self.subTest(mounts=mounts),self.assertRaises(RuntimeError):self.normalize(mounts)


class StableReadTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=pathlib.Path(self.temp.name).resolve();self.path=self.folder/'config'
        self.path.write_bytes(b'private-value');self.path.chmod(0o600)
        namespace={'r':types.SimpleNamespace(trusted_directory=lambda *a,**k:None),'require':M.require,
                   'pathlib':pathlib,'os':os,'stat':stat,'hashlib':__import__('hashlib')}
        exec(M.STABLE_READ,namespace);self.read=namespace['stable_read']

    def test_exact_stable_file_read(self):
        raw,proof=self.read(self.path,os.getuid(),0o600,100)
        self.assertEqual(raw,b'private-value');self.assertEqual(proof['sha256'],M.sha(raw))
        self.assertEqual(proof['uid'],os.getuid())

    def test_symlink_hardlink_owner_mode_and_size_are_rejected(self):
        link=self.folder/'link';link.symlink_to(self.path)
        with self.assertRaises(OSError):self.read(link,os.getuid(),0o600,100)
        for uid,mode,limit in ((os.getuid()+1,0o600,100),(os.getuid(),0o644,100),(os.getuid(),0o600,1)):
            with self.assertRaisesRegex(RuntimeError,'UNTRUSTED'):self.read(self.path,uid,mode,limit)
        hard=self.folder/'hard';os.link(self.path,hard)
        with self.assertRaisesRegex(RuntimeError,'UNTRUSTED'):self.read(self.path,os.getuid(),0o600,100)

    def test_replaced_path_after_fd_read_rejected(self):
        original=self.path.lstat()
        attributes={name:getattr(original,name) for name in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_gid','st_mode','st_nlink')}
        attributes['st_ino']+=1
        with mock.patch.object(pathlib.Path,'lstat',return_value=types.SimpleNamespace(**attributes)):
            with self.assertRaisesRegex(RuntimeError,'CHANGED'):self.read(self.path,os.getuid(),0o600,100)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=pathlib.Path(self.temp.name).resolve()/'bootstrap'

    def test_default_is_offline(self):
        with mock.patch.object(M,'helper',side_effect=AssertionError('offline')),contextlib.redirect_stdout(io.StringIO()) as output:M.main([])
        self.assertTrue(json.loads(output.getvalue())['offline'])

    def test_valid_backup_encrypted_and_decrypts_from_disk(self):
        raw=fixture();remote=mock.Mock(return_value=result(raw))
        report=M.apply(self.folder,[],B,remote=remote)
        self.assertEqual(report['status'],'complete');self.assertFalse(report['remote_mutations'])
        self.assertFalse(report['cross_service_atomic']);remote.assert_called_once()
        blob=(self.folder/'bootstrap.tar.gz.aesgcm').read_bytes();key=(self.folder/'backup.key').read_bytes()
        self.assertEqual(AESGCM(key).decrypt(blob[:12],blob[12:],report['artifact']['aad'].encode()),raw)
        for path in self.folder.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads((self.folder/'verification.json').read_bytes()),report)

    def test_existing_scope_cannot_overwrite_evidence(self):
        self.folder.mkdir(mode=0o700);remote=mock.Mock()
        with self.assertRaises(FileExistsError):M.apply(self.folder,[],B,remote=remote)
        remote.assert_not_called()

    def test_remote_failure_preserves_encrypted_partial_and_diagnostics(self):
        remote=mock.Mock(return_value=result(b'partial-private',b'secret-diagnostic',1))
        with self.assertRaisesRegex(RuntimeError,'inspect-private-receipt'):M.apply(self.folder,[],B,remote=remote)
        self.assertTrue((self.folder/'bootstrap.tar.gz.aesgcm').exists())
        self.assertTrue((self.folder/'capture.stderr.aesgcm').exists())
        self.assertTrue((self.folder/'failure.json').exists());self.assertFalse((self.folder/'verification.json').exists())
        for path in self.folder.iterdir():
            self.assertNotIn(b'partial-private',path.read_bytes());self.assertNotIn(b'secret-diagnostic',path.read_bytes())

    def test_invalid_archive_kept_encrypted_but_not_declared_complete(self):
        with self.assertRaisesRegex(RuntimeError,'inspect-private-receipt'):M.apply(self.folder,[],B,remote=mock.Mock(return_value=result(b'wrong archive')))
        self.assertTrue((self.folder/'bootstrap.tar.gz.aesgcm').exists());self.assertFalse((self.folder/'verification.json').exists())


if __name__=='__main__':unittest.main()
