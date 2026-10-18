"""Response completion facts; no model-generated text leaves this module."""
import json


def completion(status, content_type, raw, path, encoding=''):
    result = {'http_status': status, 'complete': False}
    if status // 100 != 2:
        return result
    try:
        if encoding:
            import io
            if encoding == 'gzip':
                import gzip
                reader = gzip.GzipFile(fileobj=io.BytesIO(raw))
            elif encoding == 'zstd':
                import zstandard
                reader = zstandard.ZstdDecompressor(max_window_size=16 << 20).stream_reader(io.BytesIO(raw))
            else:
                result['reason'] = 'unsupported_response_encoding'
                return result
            with reader:
                raw = reader.read((8 << 20) + 1)
            if len(raw) > 8 << 20:
                return result
        is_sse = 'text/event-stream' in content_type or (not content_type and raw.lstrip().startswith((b'event:', b'data:')))
        if not is_sse:
            data = json.loads(raw)
            result['complete'] = (isinstance(data, dict) and not data.get('error') and
                                  data.get('status') not in {'failed', 'incomplete', 'cancelled'} and
                                  isinstance(data.get('output'), list) and bool(data['output']))
            return result
        events, kinds, done = [], [], False
        for line in raw.splitlines():
            if not line.startswith(b'data:'):
                continue
            data = line[5:].strip()
            if data == b'[DONE]':
                done = True
                continue
            event = json.loads(data)
            if not isinstance(event, dict):
                return result
            events.append(event)
            kinds.append(event.get('type', ''))
        failed = any(k in {'response.failed', 'response.incomplete', 'error'} for k in kinds)
        failed = failed or any(e.get('error') for e in events)
        result['compaction_output'] = any(
            e.get('item', {}).get('type') in {'compaction', 'compaction_summary'} or
            any(x.get('type') in {'compaction', 'compaction_summary'}
                for x in e.get('response', {}).get('output', []) if isinstance(x, dict))
            for e in events)
        if path.endswith('/chat/completions'):
            finished = any(c.get('finish_reason') in {'stop', 'tool_calls'}
                           for e in events for c in e.get('choices', []))
            result['complete'] = bool(done and finished and not failed)
        elif path.endswith('/messages'):
            result['complete'] = kinds.count('message_stop') == 1 and not failed
        else:
            terminal = [k for k in kinds if k in {'response.completed', 'response.failed', 'response.incomplete', 'error'}]
            result['terminal_events'] = terminal
            result['complete'] = terminal == ['response.completed'] and not failed
    except (ValueError, TypeError, AttributeError, OSError):
        result['complete'] = False
    return result
