# ONTbarcoder 3 — Linux

## Install

```bash
tar xzf ONTbarcoder3_linux.tar.gz
cd ONTbarcoder3
./install.sh
```

`install.sh` checks that the bundle can actually run on this machine, repairs
file permissions, and adds **ONTbarcoder 3** to the applications menu. It does
not copy the folder anywhere — keep the folder where you want it, and re-run
`./install.sh` if you move it later.

## Run

- From the **Activities / applications grid** → "ONTbarcoder 3"
- From a terminal → `ontbarcoder3`
- Directly → `./launch.sh`

Double-clicking the `ONTbarcoder3` file in the file manager does work, but only
while its executable bit is intact — and that bit is lost whenever the folder
travels through Windows, a zip file or an NTFS/exFAT drive. When it fails it
fails silently, with no message at all, because the binary is built without a
console. Prefer the menu entry or `./launch.sh`: those report what went wrong.

## If it does not start

Every startup is logged to:

```
~/.local/state/ONTbarcoder/launcher.log
```

`launch.sh` also shows a dialog explaining what went wrong. The usual causes:

| Symptom | Cause | Fix |
|---|---|---|
| `version 'GLIBC_2.xx' not found` | Bundle built on a newer Ubuntu than this one | Rebuild on this machine: `./build_linux.sh` |
| `Could not load the Qt platform plugin "xcb"` | Missing system libraries | `./install.sh --install-deps` |
| Nothing happens at all | Executable bit lost, or a `noexec` mount | `./launch.sh` — it repairs and reports |
| Menu entry missing | Desktop database not refreshed | Log out and back in |

Application crashes (as opposed to startup failures) are recorded separately in
`~/ONTbarcoder_crash.log`.

## Uninstall

```bash
./install.sh --uninstall     # removes the menu entry and the `ontbarcoder3` command
rm -rf /path/to/ONTbarcoder3 # removes the application itself
```

## Building

Build **on the oldest Ubuntu you intend to support** — glibc is backward
compatible but never forward compatible, so a bundle built on 24.04 will not
run on 22.04.

```bash
./build_linux.sh              # build for this machine's distribution
./build_linux_docker.sh       # build in an Ubuntu 22.04 container (portable)
./build_linux_docker.sh 20.04 # even wider compatibility
```
