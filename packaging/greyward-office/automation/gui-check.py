#!/usr/bin/env python3
"""Private X11 startup/listener check, not physical desktop acceptance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import re
import importlib.util
import sys


def check(binary, output):
    with tempfile.TemporaryDirectory(prefix='office-gui-') as temporary:
        root = Path(temporary)
        env = dict(os.environ, HOME=str(root), DISPLAY=':99', XDG_RUNTIME_DIR=str(root), LIBGL_ALWAYS_SOFTWARE='1')
        for key in list(env):
            if key.endswith('_CONTROL_PORT') or key in ('GH_TOKEN', 'GITHUB_TOKEN'):
                env.pop(key)
        x = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        app = None
        try:
            time.sleep(2)
            before = subprocess.check_output(['ss', '-H', '-lntu'], text=True)
            with (root / 'app.log').open('w') as log:
                app = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=log)
                start = time.monotonic()
                while time.monotonic() - start < 30:
                    if app.poll() is not None:
                        raise ValueError('Ordinary GUI startup failed: ' + (root / 'app.log').read_text()[-2000:])
                    if subprocess.check_output(['ss', '-H', '-lntu'], text=True) != before:
                        raise ValueError('Ordinary startup opened a network listener')
                    windows = subprocess.check_output(['xwininfo', '-root', '-tree'], env=env, text=True)
                    if time.monotonic() - start >= 8 and re.search(r'\b[1-9][0-9]* (?:child|children)\b', windows):
                        break
                    time.sleep(.5)
                else:
                    raise ValueError('No X11 application window appeared within 30 seconds')
        finally:
            if app:
                app.terminate()
                try: app.wait(timeout=10)
                except subprocess.TimeoutExpired: app.kill(); app.wait()
            x.terminate(); x.wait(timeout=10)
        output.write_text(json.dumps({'private_x11_startup': True, 'no_tcp_udp_listener': True,
                                      'physical_input_verified': False}) + '\n')


def check_rpm(repo, rpm, slug, output):
    sys.path.insert(0, str(repo / 'packaging/greyward-office'))
    spec = importlib.util.spec_from_file_location('gui_office_inputs', repo / 'packaging/greyward-office/verify-inputs.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    app = next(a for a in json.loads((repo / 'packaging/greyward-office/source.json').read_text())['applications'] if a['slug'] == slug)
    module.verify_header(rpm, app)
    with tempfile.TemporaryDirectory(prefix='office-packaged-gui-') as temporary:
        source = subprocess.Popen(['rpm2cpio', str(rpm)], stdout=subprocess.PIPE)
        try:
            subprocess.run(['cpio', '-id', '--quiet', '--no-absolute-filenames', '--no-preserve-owner'],
                           cwd=temporary, stdin=source.stdout, check=True)
        finally:
            source.stdout.close()
            if source.wait(): raise ValueError('Cannot read packaged GUI payload')
        check(Path(temporary) / 'usr/libexec/greyward-office' / app['upstream'], output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', type=Path)
    p.add_argument('--rpm', type=Path);p.add_argument('--repo', type=Path);p.add_argument('--slug')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.rpm:
        if not a.repo or a.slug not in ('writer','sheets','pdf','slides'):p.error('RPM GUI check needs trusted recipe and app')
        check_rpm(a.repo.resolve(),a.rpm.resolve(),a.slug,a.output)
    elif a.binary:check(a.binary,a.output)
    else:p.error('Select --rpm or --binary')
