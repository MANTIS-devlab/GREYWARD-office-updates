#!/usr/bin/env bash
# Run inside the bounded unprivileged container, never on the installed OS.
set -euo pipefail
test "$(id -u)" != 0
slug="$1"
case "$slug" in writer|sheets|pdf|slides) ;; *) exit 2 ;; esac
mkdir -p /build/repo /build/cache /build/result
cp -a /source/packaging /source/branding /source/tests /build/repo/
cp /candidate/source.json /build/repo/packaging/greyward-office/source.json
cd /build/repo
python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, 'packaging/greyward-office')
from prepare import verified_file
m=json.loads(Path('packaging/greyward-office/source.json').read_text())
p=verified_file(Path('/build/cache'),m['toolchain'],m['toolchain']['file'])
Path('/build/toolchain-path').write_text(str(p))
PY
mkdir /build/rust
tar -xJf "$(cat /build/toolchain-path)" -C /build/rust --strip-components=1
/build/rust/install.sh --prefix=/build/toolchain --disable-ldconfig --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu
rm -rf /build/rust
export PATH="/build/toolchain/bin:$PATH"
python3 packaging/greyward-office/build.py --repo /build/repo --cache /build/cache \
    --output /build/output --target /build/target "$slug"
python3 tests/fixtures/office-roundtrip.py --binaries /build/target/release --output /build/roundtrip --only "$slug"
cp /build/output/RPMS/x86_64/greyward-"$slug"-*.rpm /build/output/SRPMS/greyward-"$slug"-*.src.rpm /build/result/
cp /build/roundtrip/results.json /build/result/"$slug"-engine.json
rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort > /build/result/"$slug"-builder-packages.txt
# The host-owned bind directory is writable but its metadata is not ours.
# Copy the regular output files, without trying to preserve that directory.
cp /build/result/* /result/
python3 - <<'PY'
import json, shutil
from pathlib import Path
result = {name: (Path('/sys/fs/cgroup') / name).read_text().strip()
          for name in ('memory.peak', 'memory.max') if (Path('/sys/fs/cgroup') / name).is_file()}
result['diskFreeBytes'] = shutil.disk_usage('/build').free
print('OFFICE_RESOURCE_READBACK=' + json.dumps(result))
PY
echo "OFFICE_BUILD_ELAPSED_SECONDS=$SECONDS"
