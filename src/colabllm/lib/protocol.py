"""Incremental, line-delimited notebook protocol; arbitrary log text is not readiness."""
import codecs
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CELL = re.compile(r'\[colab\] Executing cell (\d+)/(\d+)')
URL = re.compile(r'CT_PUBLIC_URL=(https?://\S+)')
CREDENTIALS = re.compile(r'CT_CREDENTIALS user=(\S+) pass=(\S+)')
QUICK = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')


class NotebookProtocol:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.discarding = False

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
            if match := URL.fullmatch(line):
                updates['url'] = match[1]
            elif match := QUICK.search(line):
                updates['url'] = match[0]
            if match := CREDENTIALS.fullmatch(line):
                updates['login_user'], updates['login_pass'] = match.groups()
            if match := CELL.search(line):
                if match[1] == match[2]:
                    updates['last_cell_started'] = True
        if len(self.pending) > 65536:
            self.pending = ''
            self.discarding = True
        return updates
