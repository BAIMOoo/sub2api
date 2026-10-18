"""Adopt a reviewed application replacement without rewriting migration history.

The original keeper still verifies and manages PostgreSQL, Redis, their completed
migration proof, and the existing entry route. This adapter substitutes only the
application identity recorded by the explicit deployment receipt. It does not
build images, create containers, change routes, or select a new deployment.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time


def require(value, code):
    if not value:
        raise ValueError(code)


def trusted(path):
    path = Path(path)
    for parent in path.parents:
        s = parent.lstat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
                'untrusted_keeper_parent')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o077 and s.st_size < 4 << 20,
                'untrusted_keeper_file')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            return f.read()
    finally:
        os.close(fd)


def module(path, expected, name):
    raw = trusted(path)
    require(hashlib.sha256(raw).hexdigest() == expected, 'keeper_dependency_changed')
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    exec(compile(raw, path, 'exec'), result.__dict__)
    return result


def adapter(base, rpc, receipt):
    class Backend(base):
        def inspect(self, kind):
            if kind != 'app':
                return super().inspect(kind)
            expected = receipt['app']
            actual = self.docker_json('container', expected['container_id'])
            require(actual['Id'] == expected['container_id'] and actual['Image'] == expected['image_id'] and
                    rpc.sha(actual['Config']) == expected['config_sha256'] and
                    rpc.sha(actual['HostConfig']) == expected['host_config_sha256'] and
                    rpc.sha(sorted(actual['Mounts'], key=lambda m: m['Destination'])) == expected['mounts_sha256'],
                    'adopted_application_changed')
            image = self.docker_json('image', expected['image_id'])
            require(image['RootFS']['Layers'] == expected['rootfs_diffids'], 'adopted_image_changed')
            state = actual['State']
            require(state['Status'] in ('running', 'exited') and not state.get('OOMKilled') and
                    not state.get('Paused') and not state.get('Restarting') and not state.get('Dead'),
                    'adopted_application_state_uncertain')
            # The unchanged entry rule must still reach this exact listener.
            env = dict(v.split('=', 1) for v in actual['Config']['Env'] if '=' in v)
            require(env.get('SERVER_HOST') == '127.0.0.1' and env.get('SERVER_PORT') == '8083' and
                    env.get('MIGRATION_RUNTIME_ADDR') == '127.0.0.1:18093', 'adopted_listener_changed')
            return actual

        def control(self, name, epoch=0):
            self.inspect('app')
            response = self._app_request(name, self._app_token(), epoch)
            require(response.get('http_status') == 200 and isinstance(response.get('body'), dict),
                    'adopted_control_unavailable')
            return response['body']

    return Backend


def run(receipt_path):
    require(os.geteuid() == 0, 'root_required')
    receipt = json.loads(trusted(receipt_path))
    require(receipt['schema'] == 1 and receipt['route_unchanged'] is True, 'invalid_adoption_receipt')
    rpc = module(receipt['rpc_program'], receipt['rpc_sha256'], 'alignment_original_rpc')
    original = module(receipt['keeper_program'], receipt['keeper_sha256'], 'alignment_original_keeper')
    cfg = rpc.load_config()
    require(rpc.sha(cfg) == receipt['original_config_sha256'], 'original_migration_config_changed')
    base = original.backend_class(rpc)
    backend_cfg = copy.deepcopy(cfg)
    backend_cfg['app'] = {k: receipt['app'][k] for k in (
        'container_id', 'image_id', 'config_sha256', 'rootfs_diffids', 'control_port')}
    backend = adapter(base, rpc, receipt)(backend_cfg)
    store = original.Store(rpc, cfg)
    with rpc.file_lock(original.SCOPE / 'keeper.lock', nonblocking=True):
        keeper = original.Keeper(rpc, cfg, backend, store, receipt['keeper_sha256'])
        previous = None
        while True:
            try:
                keeper.step()
                status = 'READY'
            except Exception as exc:
                status = 'ADOPTED_KEEPER_UNVERIFIED_' + type(exc).__name__
            if status != previous:
                print(json.dumps({'keeper': status}), flush=True)
                previous = status
            time.sleep(2)


if __name__ == '__main__':
    require(len(sys.argv) == 2, 'receipt_path_required')
    run(sys.argv[1])
