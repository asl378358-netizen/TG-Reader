"""Promote this passing CI commit only if main has not advanced."""
import json
import os
import urllib.error
import urllib.request


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    sha = os.environ['GITHUB_SHA']
    token = os.environ['GITHUB_TOKEN']
    def request(path, method='GET', payload=None):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        req = urllib.request.Request('https://api.github.com/repos/' + repo + '/' + path,
            data=data, method=method, headers={'Authorization': 'Bearer ' + token,
                'User-Agent': 'TG-Reader-CI', 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    if request('git/ref/heads/main')['object']['sha'] != sha:
        print('Main advanced; newer CI will publish stable.')
        return
    try:
        request('git/refs/heads/stable', 'PATCH', {'sha': sha, 'force': False})
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        request('git/refs', 'POST', {'ref': 'refs/heads/stable', 'sha': sha})
    print('Stable published:', sha)


if __name__ == '__main__':
    main()
