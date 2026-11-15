"""DeepSeek native Chat Completions receipts; protocol data never becomes audit text."""
import codecs
from copy import deepcopy
import json
import time

from .deepseek_native_contract import VERSION
from .provider_compat import response_metadata, usage_fields

FINISHES = frozenset(('stop', 'length', 'content_filter', 'aborted',
    'insufficient_system_resource', 'tool_calls', 'function_call'))


class NativeResponse:
    def __init__(self, *, stream=False, started=None, progress=None):
        self.stream = stream
        self.started = time.monotonic() if started is None else started
        self.progress = progress
        self.decoder = codecs.getincrementaldecoder('utf-8-sig')()
        self.buffer = ''; self.data = []
        self.message = {'role': 'assistant', 'content': ''}
        self.reasoning = []; self.events = []; self.parts = []
        self.finish_reason = None; self.done = False; self.provider_error = False
        self.last_usage = None; self.metadata = {}; self.event_count = 0
        self.first_content_ms = None; self.first_event_ms = None
        self.invalid = False

    def packet(self, outcome):
        message = deepcopy(self.message)
        if self.stream:
            message['content'] = ''.join(self.parts)
            if self.reasoning:
                message['reasoning_content'] = ''.join(self.reasoning)
        return {'version': VERSION, 'outcome': outcome, 'assistant': message,
            'tool_events': deepcopy(self.events), 'finish_reason': self.finish_reason,
            'stream_complete': self.done if self.stream else None,
            'last_observed_usage': deepcopy(self.last_usage)}

    def diagnostics(self, outcome):
        steps = self.message.get('server_tool_calls')
        steps = steps if isinstance(steps, list) else []
        content = self.message.get('content')
        content = content if isinstance(content, str) else ''
        result = {'native_terminal': outcome, 'native_protocol': VERSION,
            'stream': self.stream, 'stream_complete': self.done,
            'finish_reason': self.finish_reason or 'unknown', 'stream_events': self.event_count,
            'reasoning_present': bool(self.reasoning or self.message.get('reasoning_content') or
                any(step.get('reasoning_content') for step in steps
                    if isinstance(step, dict))),
            'output_characters': len(''.join(self.parts)) if self.stream else len(content),
            **self.metadata}
        if self.first_content_ms is not None:
            result['first_content_delta_ms'] = self.first_content_ms
        if self.first_event_ms is not None:
            result['first_event_ms'] = self.first_event_ms
        return result

    def fail(self, code, *, known=False):
        from .chat import ModelError
        outcome = 'failed' if known else 'unknown'
        error = ModelError(code, usage=deepcopy(self.last_usage) if known else None,
            diagnostics=self.diagnostics(outcome))
        error.native = self.packet(outcome)
        raise error

    def consume(self, value):
        if self.done or not isinstance(value, dict):
            self.fail('provider_native_invalid_response')
        self.event_count += 1
        elapsed = round((time.monotonic() - self.started) * 1000, 3)
        if self.first_event_ms is None: self.first_event_ms = elapsed
        if value.get('usage') is not None:
            if not isinstance(value['usage'], dict):
                self.fail('provider_native_invalid_response')
            self.last_usage = usage_fields(value['usage'], deepseek=True) or None
        self.metadata.update(response_metadata(value))
        # The same final chunk may carry both an error and billable usage.
        if value.get('error') is not None:
            self.provider_error = True
            self.fail('provider_native_internal_error', known=True)
        choices = value.get('choices')
        if not isinstance(choices, list): self.fail('provider_native_invalid_response')
        if not choices: return
        if len(choices) != 1 or not isinstance(choices[0], dict):
            self.fail('provider_native_invalid_response')
        choice = choices[0]
        if choice.get('index', 0) != 0: self.fail('provider_native_invalid_response')
        event = choice.get('server_tool_event')
        if event is not None:
            if not isinstance(event, dict): self.fail('provider_native_invalid_response')
            self.events.append(deepcopy(event))
            if event.get('type') == 'step_reasoning_done': self.reasoning = []
            if self.progress and event.get('type') == 'call_started':
                # Only a status derived from an actual event leaves this parser.
                call = event.get('call') if isinstance(event.get('call'), dict) else {}
                action = call.get('action') if isinstance(call.get('action'), dict) else {}
                state = 'reading' if action.get('type') in ('read_link', 'open_page', 'find_in_page') else 'searching'
                self.progress(state)
        item = choice.get('delta') if self.stream else choice.get('message')
        if not isinstance(item, dict): self.fail('provider_native_invalid_response')
        if item.get('tool_calls') or item.get('function_call'):
            self.fail('provider_native_custom_tool_unsupported', known=True)
        if self.stream:
            text = item.get('content')
            if text is not None:
                if not isinstance(text, str) or self.finish_reason is not None:
                    self.fail('provider_native_invalid_response')
                if text:
                    if self.first_content_ms is None:
                        self.first_content_ms = elapsed
                        if self.progress: self.progress('composing')
                    self.parts.append(text)
            thought = item.get('reasoning_content')
            if thought is not None:
                if not isinstance(thought, str): self.fail('provider_native_invalid_response')
                self.reasoning.append(thought)
            for key in ('server_tool_calls', 'annotations', 'refusal', 'title'):
                if key in item:
                    if key in self.message: self.fail('provider_native_invalid_response')
                    self.message[key] = deepcopy(item[key])
        else:
            if item.get('role') != 'assistant': self.fail('provider_native_invalid_response')
            self.message = deepcopy(item)
        finish = choice.get('finish_reason')
        if finish is not None:
            if self.finish_reason is not None or finish not in FINISHES:
                self.fail('provider_native_invalid_response')
            self.finish_reason = finish

    def _event(self):
        if not self.data: return
        raw = '\n'.join(self.data); self.data = []
        if raw == '[DONE]':
            if self.done or self.finish_reason is None: self.fail('provider_native_invalid_response')
            self.done = True
            return
        try: value = json.loads(raw)
        except (ValueError, TypeError): self.fail('provider_native_invalid_response')
        self.consume(value)

    def _line(self, line):
        if not line: self._event(); return
        if line.startswith(':'): return
        field, _, value = line.partition(':')
        if field == 'data': self.data.append(value.removeprefix(' '))

    def feed(self, chunk):
        try: self.buffer += self.decoder.decode(chunk)
        except UnicodeError: self.fail('provider_native_invalid_response')
        while True:
            positions = [p for p in (self.buffer.find('\n'), self.buffer.find('\r')) if p >= 0]
            if not positions: return
            index = min(positions)
            if self.buffer[index] == '\r' and index == len(self.buffer) - 1: return
            width = 2 if self.buffer[index:index+2] == '\r\n' else 1
            line = self.buffer[:index]; self.buffer = self.buffer[index+width:]
            self._line(line)

    def complete(self):
        if self.stream:
            try: self.buffer += self.decoder.decode(b'', final=True)
            except UnicodeError: self.fail('provider_native_invalid_response')
            if self.buffer or self.data or not self.done:
                self.fail('provider_native_unknown')
        if self.finish_reason is None: self.fail('provider_native_unknown')
        if self.finish_reason != 'stop':
            code = {'length': 'output_truncated', 'content_filter': 'content_filtered'}.get(self.finish_reason, self.finish_reason)
            self.fail('provider_native_' + code, known=True)
        message = self.packet('completed')['assistant']
        text = message.get('content')
        if not isinstance(text, str) or not text.strip():
            self.fail('provider_native_empty_response', known=True)
        steps = message.get('server_tool_calls', [])
        if not isinstance(steps, list): self.fail('provider_native_invalid_response')
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get('calls'), list):
                self.fail('provider_native_invalid_response')
            for call in step['calls']:
                if not isinstance(call, dict) or call.get('status') not in ('completed', 'failed'):
                    self.fail('provider_native_invalid_response')
                if call.get('type') not in ('deepseek_web_search','deepseek_read_url'):
                    self.fail('provider_native_custom_tool_unsupported',known=True)
        annotations = message.get('annotations', [])
        if not isinstance(annotations, list): self.fail('provider_native_invalid_response')
        for annotation in annotations:
            if not isinstance(annotation, dict): self.fail('provider_native_invalid_response')
            if annotation.get('type') == 'url_citation':
                citation = annotation.get('url_citation')
                if (not isinstance(citation, dict) or not isinstance(citation.get('url'), str)
                        or type(citation.get('start_index')) is not int or type(citation.get('end_index')) is not int
                        or not 0 <= citation['start_index'] <= citation['end_index'] <= len(text)):
                    self.fail('provider_native_invalid_citation')
        from .chat import ModelCompletion
        return ModelCompletion(text, deepcopy(self.last_usage), self.diagnostics('completed'), native=self.packet('completed'))
