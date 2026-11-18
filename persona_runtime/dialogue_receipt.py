"""Decode a retained first HTTP body. Both frozen v8 SSE and plain completions are supported."""
import json

from .provider_compat import usage_fields


def parse_receipt(raw, streaming):
    model = usage = finish = None
    complete = False
    pieces = []
    try:
        if streaming:
            for line in raw.decode('utf-8').splitlines():
                if not line.startswith('data:'):
                    continue
                value = line[5:].strip()
                if complete:
                    raise ValueError()
                if value == '[DONE]':
                    complete = True
                    continue
                chunk = json.loads(value)
                incoming = chunk.get('model')
                if incoming:
                    if model and incoming != model:
                        raise ValueError()
                    model = incoming
                if chunk.get('usage') is not None:
                    usage = usage_fields(chunk['usage'], deepseek=True)
                choices = chunk['choices']
                if choices:
                    choice, = choices
                    delta = choice['delta']
                    if choice.get('index', 0) != 0 or delta.get('tool_calls') or delta.get('function_call'):
                        raise ValueError()
                    if delta.get('content') is not None:
                        pieces.append(delta['content'])
                    if choice.get('finish_reason'):
                        if finish is not None:
                            raise ValueError()
                        finish = choice['finish_reason']
        else:
            packet = json.loads(raw)
            model = packet['model']
            usage = usage_fields(packet.get('usage'), deepseek=True)
            choice, = packet['choices']
            message = choice['message']
            if message.get('tool_calls') or message.get('function_call'):
                raise ValueError()
            pieces.append(message['content'])
            finish = choice['finish_reason']
            complete = True
        body = ''.join(pieces)
        body.encode('utf-8')
        valid = complete and finish == 'stop' and bool(body.strip()) and len(body) <= 60000
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError):
        body, valid = '', False
    if not isinstance(model, str) or not model.isascii() or len(model) > 128:
        model, valid = None, False
    if not isinstance(finish, str) or not finish.isascii() or len(finish) > 128:
        finish, valid = None, False
    return {'body': body if valid else None, 'model': model, 'usage': usage, 'finish_reason': finish,
            'valid': valid, 'incomplete_stream': streaming and not complete}
