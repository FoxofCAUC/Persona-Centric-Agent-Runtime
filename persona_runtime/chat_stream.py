"""Chat Completions SSE folding. No partial answer is exposed for delivery."""
import codecs
import json
import time

import aiohttp

from .provider_compat import is_grok47, response_metadata, usage_fields


class ChatStream:
    def __init__(self, model, *, started=None):
        self.model=model
        self.started=time.monotonic() if started is None else started
        self.decoder=codecs.getincrementaldecoder('utf-8-sig')()
        self.buffer='';self.data=[];self.parts=[]
        self.finished=None;self.done=False;self.reasoning=False
        self.usage=None;self.metadata={};self.events=0
        self.first_event_ms=None;self.first_content_delta_ms=None

    def error(self, code):
        from .chat import ModelError
        # A stream without terminal evidence has no final usage receipt.
        usage=self.usage if self.done else None
        raise ModelError(code,usage=usage,diagnostics=self.diagnostics())

    def diagnostics(self):
        result={'stream':True,'stream_complete':self.done,
                'stream_events':self.events,'reasoning_present':self.reasoning,
                'output_characters':sum(map(len,self.parts)),
                'finish_reason':self.finished or 'unknown',**self.metadata}
        if self.first_event_ms is not None:result['first_event_ms']=self.first_event_ms
        if self.first_content_delta_ms is not None:result['first_content_delta_ms']=self.first_content_delta_ms
        return result

    def _event(self):
        if not self.data:return
        raw='\n'.join(self.data);self.data=[]
        if self.done:self.error('provider_invalid_response')
        self.events+=1
        elapsed=round((time.monotonic()-self.started)*1000,3)
        if self.first_event_ms is None:self.first_event_ms=elapsed
        if raw=='[DONE]':
            if self.finished is None:self.error('provider_invalid_response')
            self.done=True;return
        try:value=json.loads(raw)
        except (ValueError,TypeError):self.error('provider_invalid_response')
        if not isinstance(value,dict):self.error('provider_invalid_response')
        if value.get('error') is not None:self.error('provider_aborted')
        self.metadata.update(response_metadata(value,grok=is_grok47(self.model)))
        supplied=value.get('usage')
        if supplied is not None:
            if not isinstance(supplied,dict):self.error('provider_invalid_response')
            # Usage events are cumulative snapshots, never additive deltas.
            self.usage=usage_fields(supplied,deepseek=True,grok=True) or None
        choices=value.get('choices')
        if not isinstance(choices,list):self.error('provider_invalid_response')
        if not choices:return
        if len(choices)!=1 or not isinstance(choices[0],dict):self.error('provider_invalid_response')
        choice=choices[0]
        if choice.get('index',0)!=0:self.error('provider_invalid_response')
        delta=choice.get('delta')
        if not isinstance(delta,dict):self.error('provider_invalid_response')
        if delta.get('tool_calls') or delta.get('function_call'):
            self.error('provider_tool_call_not_supported')
        text=delta.get('content')
        if text is not None:
            if not isinstance(text,str):self.error('provider_invalid_response')
            if text:
                if self.finished is not None:self.error('provider_invalid_response')
                if self.first_content_delta_ms is None:self.first_content_delta_ms=elapsed
                self.parts.append(text)
        if delta.get('reasoning_content') or delta.get('reasoning'):self.reasoning=True
        finish=choice.get('finish_reason')
        if finish is not None:
            if self.finished is not None or finish not in ('stop','length','content_filter','tool_calls','function_call','insufficient_system_resource','aborted'):
                self.error('provider_invalid_response')
            self.finished=finish

    def _line(self,line):
        if not line:self._event();return
        if line.startswith(':'):return
        field,_,value=line.partition(':')
        if value.startswith(' '):value=value[1:]
        if field=='data':self.data.append(value)

    def feed(self,chunk):
        try:self.buffer+=self.decoder.decode(chunk)
        except UnicodeError:self.error('provider_invalid_response')
        # CRLF can straddle chunks; leave the final CR buffered until known.
        while True:
            lf=self.buffer.find('\n');cr=self.buffer.find('\r')
            positions=[p for p in (lf,cr) if p>=0]
            if not positions:break
            index=min(positions)
            if self.buffer[index]=='\r' and index==len(self.buffer)-1:break
            width=2 if self.buffer[index:index+2]=='\r\n' else 1
            line=self.buffer[:index];self.buffer=self.buffer[index+width:]
            self._line(line)

    def finish(self):
        try:self.buffer+=self.decoder.decode(b'',final=True)
        except UnicodeError:self.error('provider_invalid_response')
        if self.buffer or self.data:self.error('provider_invalid_response')
        if not self.done:self.error('provider_network_error')
        if self.finished=='content_filter':self.error('provider_content_filtered')
        if self.finished in ('tool_calls','function_call'):self.error('provider_tool_call_not_supported')
        if self.finished in ('length','insufficient_system_resource','aborted'):
            self.error('provider_output_truncated' if self.finished=='length' else 'provider_'+self.finished)
        text=''.join(self.parts)
        if not text.strip():self.error('provider_empty_response')
        return text,self.usage,self.diagnostics()


async def read_stream(response,model,diagnostic,*,started=None):
    """Read full SSE under the caller's transport policy; never accept JSON fallback."""
    from .chat import ModelCompletion, ModelError
    content_type=response.headers.get('Content-Type','').split(';',1)[0].strip().lower()
    if content_type!='text/event-stream':
        raise ModelError('provider_stream_expected',diagnostics=diagnostic())
    stream=ChatStream(model,started=started)
    try:
        async for chunk in response.content.iter_chunked(65536):
            stream.feed(chunk)
            if stream.done:break
        text,usage,details=stream.finish()
        return ModelCompletion(text,usage,{**diagnostic(),**details,'http_status':response.status})
    except ModelError as error:
        error.diagnostics={**diagnostic(),**stream.diagnostics(),'http_status':response.status}
        raise
    except aiohttp.ClientError:
        raise ModelError('provider_network_error',diagnostics={**diagnostic(),**stream.diagnostics(),
                         'http_status':response.status}) from None
