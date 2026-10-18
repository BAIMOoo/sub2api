"""A single latest-generation queue; a live request is never cancelled by pushes."""
import datetime
import fcntl
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from .evidence import atomic_json


class Schedule:
    def __init__(self, path, quiet_seconds=300):
        self.path = Path(path)
        self.quiet = quiet_seconds
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'seen': None, 'pending': None, 'changed_at': 0, 'daily': None, 'completed': None}

    def observe(self, generation, now):
        if generation != self.state['seen']:
            self.state.update(seen=generation, pending=generation, changed_at=now)
            self.save()

    def due(self, now):
        local = datetime.datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        day = local.date().isoformat()
        daily = local.hour >= 9 and self.state['daily'] != day
        changed = self.state['pending'] is not None and now - self.state['changed_at'] >= self.quiet
        return daily or changed

    def started(self, generation, now):
        # Claim only the generation actually being tested. Later observations
        # stay pending and become the next serial run.
        local = datetime.datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        if local.hour >= 9:
            self.state['daily'] = local.date().isoformat()
        if self.state['pending'] == generation:
            self.state['pending'] = None
        self.state['running'] = {'generation': generation, 'started': now}
        self.save()

    def finished(self, generation):
        self.state['completed'] = generation
        self.state.pop('running', None)
        self.save()

    def save(self):
        atomic_json(self.path, self.state)


def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = path.open('a')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd
