import codecs
import re
from pathlib import Path

class LogTail:
    def __init__(self): self.states = {}; self.ansi_pending = {}
    def read(self, path, clear=False):
        path = Path(path)
        if not path.exists(): return ""
        stat = path.stat(); key = str(path)
        state = self.states.get(key)
        if clear:
            self.states[key] = (stat.st_ino, stat.st_size, codecs.getincrementaldecoder("utf-8")("replace")); self.ansi_pending[key] = ""; return ""
        if not state or state[0] != stat.st_ino or stat.st_size < state[1]:
            state = (stat.st_ino, max(0, stat.st_size - 65536), codecs.getincrementaldecoder("utf-8")("replace")); self.ansi_pending[key] = ""
        with path.open("rb") as stream:
            stream.seek(state[1]); data = stream.read(65536); offset = stream.tell()
        self.states[key] = (stat.st_ino, offset, state[2])
        text = self.ansi_pending.get(key, "") + state[2].decode(data)
        match = re.search(r"\x1b(?:\[[0-?]*[ -/]*)?$", text)
        self.ansi_pending[key] = match.group(0) if match else ""
        if match: text = text[:match.start()]
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")
