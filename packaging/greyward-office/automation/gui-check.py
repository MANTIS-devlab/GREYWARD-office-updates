#!/usr/bin/env python3
"""Private X11 startup/listener check, not physical desktop acceptance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


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
                time.sleep(8)
                if app.poll() is not None:
                    raise ValueError('Ordinary GUI startup failed: ' + (root / 'app.log').read_text()[-2000:])
                windows = subprocess.check_output(['xwininfo', '-root', '-tree'], env=env, text=True)
                if '0 children' in windows or 'has no children' in windows:
                    raise ValueError('No X11 application window appeared')
                if subprocess.check_output(['ss', '-H', '-lntu'], text=True) != before:
                    raise ValueError('Ordinary startup opened a network listener')
        finally:
            if app:
                app.terminate()
                try: app.wait(timeout=10)
                except subprocess.TimeoutExpired: app.kill(); app.wait()
            x.terminate(); x.wait(timeout=10)
        output.write_text(json.dumps({'private_x11_startup': True, 'no_tcp_udp_listener': True,
                                      'physical_input_verified': False}) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); check(a.binary, a.output)
