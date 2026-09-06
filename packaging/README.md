# Packaging TuxThrottle (RPM / COPR)

`tuxthrottle.spec` is a **noarch** spec — the project is pure Python + JSON +
shell, so there is no compile step. It installs the runnable tree to
`/opt/tuxthrottle` (matching `install.sh`, `verify-install.sh` and the
`{TOOLKIT_DIR}` substitution in `config/tweaks.json`), a launcher at
`/usr/bin/tuxthrottle`, `tuxthrottlectl` symlinked into `/usr/bin`, the
`.desktop` entry and the hicolor icons.

The in-app **tweaks** (systemd units, sudoers drop-ins, `kwriteconfig6` edits,
GRUB/fstab changes) are deliberately **not** touched by `%post` — they stay
opt-in and are applied from the GUI or `apply_tweak.py` exactly as with the
git-clone install.

## Build an SRPM locally

```bash
rpmdev-setuptree
git archive --format=tar.gz --prefix=tuxthrottle-main/ -o ~/rpmbuild/SOURCES/tuxthrottle-main.tar.gz HEAD
rpmbuild -bs packaging/tuxthrottle.spec \
  --define "_version 0.1.0" --define "_release 1" --define "_gittag main"
```

Then `mock -r fedora-43-x86_64 ~/rpmbuild/SRPMS/tuxthrottle-*.src.rpm` to test
the binary build.

## COPR

`.github/workflows/copr.yml` builds the SRPM on every `v*` tag (and on manual
dispatch) and hands it to COPR. Set two repo secrets:

| Secret | Value |
|---|---|
| `COPR_CONFIG` | the whole API-token block from <https://copr.fedorainfracloud.org/api/> |
| `COPR_PROJECT` | e.g. `beangreen247/tuxthrottle` (defaults to that if unset) |

One-time COPR project setup: create the project, enable the `fedora-43-x86_64`
and `fedora-rawhide-x86_64` chroots, and (optionally) add this repo's GitHub
webhook so pushes trigger builds without the Actions run.

The git-clone path (`sudo ./install.sh`) keeps working unchanged.
