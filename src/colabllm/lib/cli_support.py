"""Shared CLI errors, diagnostic output and authenticated HTTP transport."""
import http.client
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

QUIET = False
NO_COLOR = False


class CliError(RuntimeError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class UsageError(ValueError):
    code = 2


def diagnostic(message, color=None):
    if QUIET:
        return
    if color and not NO_COLOR and not os.environ.get('NO_COLOR') and sys.stderr.isatty():
        message = f'\033[{color}m{message}\033[0m'
    print(message, file=sys.stderr, flush=True)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        def origin(url):
            return (url.scheme.lower(), url.hostname, url.port or (443 if url.scheme == 'https' else 80))
        if req.has_header('Authorization') and origin(old) != origin(new):
            raise CliError('拒绝向其他站点转发认证信息；请检查服务 URL 或重定向配置')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(request, *args, **kwargs):
    return urllib.request.build_opener(SameOriginRedirect()).open(request, *args, **kwargs)


def run_cli(action):
    """Handle expected user-facing failures consistently, including early parsing."""
    global QUIET, NO_COLOR
    debug = False
    args = []
    literal = False
    for arg in sys.argv[1:]:
        if arg == '--':
            literal = True
        if not literal and arg in ('--debug', '--quiet', '--no-color'):
            debug |= arg == '--debug'
            QUIET |= arg == '--quiet'
            NO_COLOR |= arg == '--no-color'
        else:
            args.append(arg)
    sys.argv[1:] = args
    try:
        code = action()
        if isinstance(code, int) and code:
            raise SystemExit(code)
    except BrokenPipeError:
        # Avoid a second flush failure at interpreter shutdown.
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        raise SystemExit(141) from None
    except KeyboardInterrupt:
        diagnostic('已中断当前命令；可用 status/job 查看服务或任务状态。')
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, http.client.HTTPException, subprocess.SubprocessError) as error:
        if debug:
            traceback.print_exc()
        else:
            message = str(error)
            if isinstance(error, urllib.error.HTTPError):
                message = f'HTTP {error.code}: {error.reason}'
            # Errors remain visible in quiet mode.
            print(f'colabllm: {message}', file=sys.stderr)
        raise SystemExit(error.code if isinstance(error, (CliError, UsageError)) else 1) from None
