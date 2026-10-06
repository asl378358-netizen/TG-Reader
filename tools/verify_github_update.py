"""CI-only round trip through the same downloader used by the Windows app."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_client as updates


def main():
    expected = os.environ['GITHUB_SHA']
    # The tested commit has not been promoted to stable yet.
    updates.CHANNEL = 'main'
    requirements = (Path(__file__).resolve().parents[1] / 'requirements.txt').read_bytes()
    with tempfile.TemporaryDirectory(prefix='TG Reader real update ') as temp:
        root = Path(temp)
        old = root / 'versions' / ('a' * 40)
        old.mkdir(parents=True)
        (old / 'app.py').write_text('# previous version\n', encoding='utf-8')
        (root / 'start.py').write_text('# previous launcher\n', encoding='utf-8')
        (root / 'config.json').write_bytes(b'keep config')
        updates.save_json(root / 'current.json', {'sha': 'a' * 40, 'version': 'test',
            'python': sys.executable, 'requirements_sha256': hashlib.sha256(requirements).hexdigest()})
        record, source, changed = updates.check_update(root, print)
        assert changed and record['sha'] == expected
        assert (root / 'config.json').read_bytes() == b'keep config'
        assert (root / 'start.py').read_bytes() == (source / 'launcher.py').read_bytes()
        _, _, changed = updates.check_update(root, print)
        assert not changed
    print('GitHub archive download, runtime verification and activation: OK')


if __name__ == '__main__':
    main()
