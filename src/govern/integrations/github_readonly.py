"""Read-only PR credentials: write-only settings and ephemeral Git authorization."""
import base64
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from .github import GitHubClient


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'GitHub redirect refused', headers, fp)


class ReadOnlyGitHubClient(GitHubClient):
    def __init__(self, token):
        super().__init__(token, timeout=20, max_attempts=2)
        self._open=urllib.request.build_opener(NoRedirect()).open

    def post_comment(self, api_url, markdown):
        # Legacy post_comment bypasses _request, unlike the other write helpers.
        raise PermissionError('Govern GitHub connection is read-only')

    def _request(self, method, url, payload=None, **kwargs):
        parsed=urllib.parse.urlsplit(url)
        if method!='GET' or payload is not None or parsed.scheme!='https' or parsed.netloc!='api.github.com' or parsed.username:
            raise PermissionError('Govern GitHub connection only permits HTTPS read requests to api.github.com')
        try:
            return super()._request(method,url,payload,**kwargs)
        except Exception as exc:
            # Never forward provider bodies, headers, subprocess output or exception chains.
            match=re.search(r'returned HTTP (\d{3})',str(exc))
            status=match.group(1) if match else 'unavailable'
            raise ValueError('GitHub read failed (%s); check repository access, token expiry and read permissions'%status) from None


def client_for(workbench):
    config=workbench.config
    token=config['github'].get('token','') if 'github' in config else getattr(workbench.service.settings,'github_token','')
    return ReadOnlyGitHubClient(token)


def fetch_snapshot(root, repository, base, head, token):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repository) or any(not re.fullmatch(r'[0-9a-f]{40,64}',s) for s in (base,head)):
        raise ValueError('Invalid GitHub repository or commit SHA')
    # No token in argv/URL/config file. Disable inherited trace and credential helpers.
    env={k:v for k,v in os.environ.items() if not k.upper().startswith('GIT_') and k.upper()!='GCM_TRACE'}
    settings=[('credential.helper',''),('http.followRedirects','false'),('protocol.ext.allow','never')]
    if token:
        encoded=base64.b64encode(('x-access-token:'+token).encode()).decode()
        settings.append(('http.https://github.com/.extraHeader','Authorization: Basic '+encoded))
    env.update(GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_COUNT=str(len(settings)))
    for i,(key,value) in enumerate(settings):env['GIT_CONFIG_KEY_'+str(i)]=key;env['GIT_CONFIG_VALUE_'+str(i)]=value
    command=['git','-C',str(root),'fetch','--no-tags','https://github.com/'+repository+'.git',base,head]
    try:
        result=subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,check=False)
        if result.returncode:raise ValueError('GitHub source fetch failed; check Contents read access and network availability')
    except (OSError,subprocess.TimeoutExpired):
        raise ValueError('GitHub source fetch unavailable or timed out; no interactive login was attempted') from None
