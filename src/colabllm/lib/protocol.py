"""Incremental, line-delimited notebook protocol; arbitrary log text is not readiness."""
import codecs
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CELL = re.compile(r'\[colab\] Executing cell (\d+)/(\d+)')
URL = re.compile(r'CT_PUBLIC_URL=(https?://\S+)')
CREDENTIALS = re.compile(r'CT_CREDENTIALS user=(\S+) pass=(\S+)')
QUICK = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
PREFLIGHT_FATAL = re.compile(
    r'^(?:'
    r'RuntimeError: Connection was lost\.'
    r'|TimeoutError: Timeout waiting for '
    r'|RuntimeError: Kernel (?:dead|restarting) before the code finished\.'
    r'|\[colab\] Session .+ (?:appears to be lost|not found\.)'
    r')'
)
EXC_SUMMARY = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception): .+')


class NotebookProtocol:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.discarding = False
        self.cell_started = False
        self.preflight_error: str | None = None
        self._in_exec_traceback = False

    def feed(self, chunk, *, final=False):
        text = self.decoder.decode(chunk, final=final).replace('\r', '\n')
        lines = (self.pending + text).split('\n')
        self.pending = '' if final else lines.pop()
        updates = {}
        for line in lines:
            if self.discarding:
                self.discarding = False
                continue
            line = ANSI.sub('', line).strip()
            if line == 'CT_READY':
                updates['ready'] = True
                self.cell_started = True
                self._in_exec_traceback = False
            if match := URL.fullmatch(line):
                updates['url'] = match[1]
                self.cell_started = True
                self._in_exec_traceback = False
            elif match := QUICK.search(line):
                updates['url'] = match[0]
                self.cell_started = True
                self._in_exec_traceback = False
            if match := CREDENTIALS.fullmatch(line):
                updates['login_user'], updates['login_pass'] = match.groups()
            if match := CELL.search(line):
                self.cell_started = True
                self._in_exec_traceback = False
                if match[1] == match[2]:
                    updates['last_cell_started'] = True
            if not self.cell_started and self.preflight_error is None and line:
                if 'Traceback (most recent call last)' in line or 'colab_cli/' in line:
                    self._in_exec_traceback = True
                if PREFLIGHT_FATAL.match(line) or (self._in_exec_traceback and EXC_SUMMARY.match(line)):
                    self.preflight_error = line
        if len(self.pending) > 65536:
            self.pending = ''
            self.discarding = True
        return updates
