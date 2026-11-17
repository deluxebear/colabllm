"""Incremental, line-delimited notebook protocol; arbitrary log text is not readiness."""
import codecs
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CELL = re.compile(r'\[colab\] Executing cell (\d+)/(\d+)')
CT_CELL = re.compile(r'CT_CELL=(\d+)/(\d+)(?::(.*))?')
CT_STAGE = re.compile(r'CT_STAGE=(\S+)')
CT_CACHE_EVENT = re.compile(r'CT_CACHE_EVENT\s+artifact=(\S+)\s+status=(\S+)')
CT_DISK_STAT = re.compile(r'CT_DISK_STAT\s+path=(\S+)\s+free_mb=(\d+)')
URL = re.compile(r'CT_PUBLIC_URL=(https?://\S+)')
CREDENTIALS = re.compile(r'CT_CREDENTIALS user=(\S+) pass=(\S+)')
QUICK = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
PREFLIGHT_FATAL = re.compile(
    r'^(?:'
    r'RuntimeError: Connection was lost\.'
    r'|TimeoutError: Timeout waiting for '
    r'|RuntimeError: Kernel (?:dead|restarting) before the code finished\.'
    r'|\[colab\] Session .+ (?:appears to be lost|not found\.)'
    r'|\[(?:session|kaggle-ext|error)\]\s+.+'
    r')'
)
EXC_SUMMARY = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception): .+')


class NotebookProtocol:
    def __init__(self, *, provider: str = 'colab'):
        self.provider = provider
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.discarding = False
        self.cell_started = False
        self.preflight_error: str | None = None
        self._in_exec_traceback = False
        self.stage: str | None = None
        self.cell: str | None = None
        self.cache_events: list[dict[str, str]] = []
        self.disk_stats: list[dict[str, int | str]] = []

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
            if match := CT_STAGE.fullmatch(line):
                self.stage = match[1]
                updates['stage'] = match[1]
                self.cell_started = True
                self._in_exec_traceback = False
            if match := CT_CELL.fullmatch(line):
                self.cell = line[len('CT_CELL='):]
                updates['cell'] = self.cell
                self.cell_started = True
                self._in_exec_traceback = False
                if match[1] == match[2]:
                    updates['last_cell_started'] = True
            if match := CT_CACHE_EVENT.fullmatch(line):
                ev = {'artifact': match[1], 'status': match[2]}
                self.cache_events.append(ev)
                updates.setdefault('cache_events', []).append(ev)
            if match := CT_DISK_STAT.fullmatch(line):
                stat_ev = {'path': match[1], 'free_mb': int(match[2])}
                self.disk_stats.append(stat_ev)
                updates.setdefault('disk_stats', []).append(stat_ev)
            if match := CELL.search(line):
                self.cell_started = True
                self._in_exec_traceback = False
                if match[1] == match[2]:
                    updates['last_cell_started'] = True
            if not self.cell_started and self.preflight_error is None and line:
                if 'Traceback (most recent call last)' in line or 'colab_cli/' in line or 'kaggle_ext/' in line:
                    self._in_exec_traceback = True
                if PREFLIGHT_FATAL.match(line) or (self._in_exec_traceback and EXC_SUMMARY.match(line)):
                    self.preflight_error = line
        if len(self.pending) > 65536:
            self.pending = ''
            self.discarding = True
        return updates
