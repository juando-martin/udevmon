#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Guided real-hardware test for udevmon. Run it from the source tree:

    python3 tools/usb-test.py

It tells you when to plug and unplug a USB stick and does everything else:
throwaway config and runtime folder (your real config is never touched), a
daemon, the checks, and the clean-up. Only one udevmon daemon can run per user,
so stop the service first if you use it: systemctl --user stop udevmon

Developer option: --simulate-loop [--image FILE] uses a loop-mounted disk image
instead of a stick (no USB device, so the USB-only checks are skipped).
"""
import argparse
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PY = sys.executable
SCHEMAS = ("org.cinnamon.desktop.media-handling", "org.gnome.desktop.media-handling",
           "org.mate.media-handling")
results: list[tuple[str, bool, str]] = []
state: dict = {}


def say(msg=""):
    print(msg, flush=True)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    say(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({detail})" if detail and not ok else ""))
    return ok


# -- talking to udevmon ------------------------------------------------------------
def env():
    e = dict(os.environ, PYTHONPATH=str(ROOT / "src"), XDG_RUNTIME_DIR=state["runtime"])
    return e


def u(*args, ok=True):
    """Run a udevmon command on the throwaway config; returns the CompletedProcess."""
    p = subprocess.run([PY, "-m", "udevmon", "--config", state["cfg"], *args],
                       env=env(), capture_output=True, text=True)
    if ok and p.returncode:
        raise RuntimeError(f"udevmon {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p


def log_from(mark):
    try:
        return state["log"].read_text().splitlines()[mark:]
    except FileNotFoundError:
        return []


def wait_for(mark, wanted, timeout=10):
    """Wait until every line in `wanted` is in the log after `mark`.

    Long waits (a person is doing something) show what has arrived so far."""
    end = time.time() + timeout
    shown, last = set(), time.time()
    while time.time() < end:
        lines = log_from(mark)
        if timeout >= 30:
            for w in wanted:
                if w in lines and w not in shown:
                    shown.add(w)
                    say(f"    seen: {w}")
            if time.time() - last >= 10:
                last = time.time()
                say(f"    ... still waiting ({int(end - last)}s left)")
        if all(w in lines for w in wanted):
            return True, lines
        time.sleep(0.2)
    return False, log_from(mark)


def quiet(mark, unwanted, settle=2.0):
    """After a pause, none of `unwanted` may be in the log after `mark`."""
    time.sleep(settle)
    lines = log_from(mark)
    return not any(x in lines for x in unwanted), lines


def expect(name, mark, wanted=(), unwanted=(), timeout=10):
    ok, lines = wait_for(mark, wanted, timeout) if wanted else (True, log_from(mark))
    if ok and unwanted:
        ok, lines = quiet(mark, unwanted)
    return check(name, ok, f"log: {lines}, wanted {list(wanted)}, unwanted {list(unwanted)}")


def logger(text):
    """An action command that appends a line to the test log."""
    return ["--", "sh", "-c", f'echo "{text}" >> "{state["log"]}"']


# -- the stick ----------------------------------------------------------------------
def snap():
    from udevmon.sources.udev import UdevSource
    from udevmon.sources.udisks import UdisksSource
    usb = {e.key: e for e in UdevSource().snapshot()}
    fs = {e.key: e for e in UdisksSource().snapshot(include_system=True)}
    return usb, fs


def fs_now(uuid):
    return next((e for e in snap()[1].values() if e.props.get("fs_uuid") == uuid), None)


def block_dev(fs):
    return "/dev/" + fs.key.rsplit("/", 1)[-1]


def udisks(*args):
    return subprocess.run(["udisksctl", args[0], "--no-user-interaction", *args[1:]], capture_output=True, text=True)


def ensure_mounted(timeout=15):
    end = time.time() + timeout
    fs = None
    while time.time() < end:
        fs = fs_now(state["uuid"])
        if fs and fs.props.get("mountpoint"):
            return True
        if fs:
            udisks("mount", "-b", block_dev(fs))
        time.sleep(0.5)
    return False


def cycle():
    """Unmount and mount the stick's filesystem the way a desktop would."""
    fs = fs_now(state["uuid"])
    udisks("unmount", "-b", block_dev(fs))
    time.sleep(1.5)
    udisks("mount", "-b", block_dev(fs))
    time.sleep(1.5)


def wait_until(pred, timeout, what):
    end = time.time() + timeout
    last = time.time()
    while time.time() < end:
        r = pred()
        if r:
            return r
        if time.time() - last >= 10:
            last = time.time()
            say(f"    ... still waiting for {what} ({int(end - last)}s left)")
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {what}")


# -- desktop pop-ups ------------------------------------------------------------------
def windows():
    p = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True) if shutil.which("wmctrl") else None
    return {l.split()[0]: l for l in p.stdout.splitlines()} if p and p.returncode == 0 else {}


def stop_popups():
    """Turn off "open a window when a volume is mounted" until we exit."""
    if not shutil.which("gsettings"):
        return
    for schema in SCHEMAS:
        p = subprocess.run(["gsettings", "get", schema, "automount-open"], capture_output=True, text=True)
        if p.returncode == 0:
            old = p.stdout.strip()
            subprocess.run(["gsettings", "set", schema, "automount-open", "false"])
            def restore(schema=schema, old=old):
                subprocess.run(["gsettings", "set", schema, "automount-open", old])
            atexit.register(restore)
            state["restore"] = restore
            return


# -- the run ---------------------------------------------------------------------------
def sticks_present():
    """USB storage devices plugged in now, leaving out the disk the system runs from."""
    usb, fs = snap()
    system = {e.props.get("drive_serial") for e in fs.values()
              if (e.props.get("mountpoint") or "").startswith(("/boot", "/home", "/usr", "/var"))
              or e.props.get("mountpoint") == "/"}
    serials = {e.props.get("drive_serial") for e in fs.values()} - system - {None}
    return [e for e in usb.values() if e.props.get("serial") in serials]


def find_stick(sim, image):
    if not sim:
        present = sticks_present()
        if present:
            names = ", ".join(f"{e.props.get('model') or '?'} ({e.props.get('vendor_id')}:"
                              f"{e.props.get('product_id')})" for e in present)
            say(f"A USB stick is already plugged in: {names}")
            say("UNPLUG it now (no need to eject); the test starts when it is gone.")
            wait_until(lambda: not sticks_present(), 180, "the stick to be unplugged")
            time.sleep(2)
            say("Thank you.")
    before_usb, before_fs = snap()
    if sim:
        say("Simulating a stick with a loop-mounted disk image.")
        p = udisks("loop-setup", "-f", str(image))
        m = re.search(r"(/dev/loop\d+)", p.stdout)
        if not m:
            raise RuntimeError(f"loop-setup failed: {p.stderr.strip()}")
        state["loop"] = m.group(1)
    else:
        say("PLUG IN the USB stick now (wait for it to be recognised).")
    def new():
        usb, fs = snap()
        nu = [e for k, e in usb.items() if k not in before_usb]
        nf = [e for k, e in fs.items() if k not in before_fs and e.props.get("fs_uuid")]
        if sim:
            return (None, nf[0]) if nf else None
        return (nu[0], nf[0]) if nu and nf else None
    found = wait_until(new, 180, "a NEW USB stick with a filesystem to appear (was it "
                                 "already plugged in when you started?)")
    usb_dev, fs = found
    state["usb"], state["uuid"] = usb_dev, fs.props["fs_uuid"]
    state["label"] = fs.props.get("fs_label", "")
    say(f"Found: {fs.props.get('fs_label') or ''} {fs.props.get('fs_type', '')} "
        f"uuid={state['uuid']}" + (f" on USB {usb_dev.props.get('vendor_id')}:"
                                  f"{usb_dev.props.get('product_id')}" if usb_dev else ""))
    ensure_mounted()


def build_config():
    usb = state["usb"]
    Path(state["cfg"]).write_text("[daemon]\ndebounce = 0\n")
    os.chmod(state["cfg"], 0o600)
    fs_match = ["--match", f"fs_uuid={state['uuid']}"]
    if usb:
        m = []
        for k in ("vendor_id", "product_id", "serial"):
            if usb.props.get(k):
                m += ["--match", f"{k}={usb.props[k]}"]
        u("devices", "add", "stick", *m, "--tag", "usbtest")
    u("devices", "add", "stick-fs", *fs_match, "--tag", "usbtest")
    u("actions", "add", "--tag", "usbtest", "connected,disconnected,mounted,unmounted,removed",
      "--name", "log", *logger("$UDEVMON_EVENT dev=$UDEVMON_DEVICE surprise=$UDEVMON_SURPRISE"))
    u("actions", "add", "--device", "stick-fs", "mounted,unmounted", "--name", "own",
      *logger("own:$UDEVMON_EVENT"))
    if usb:
        u("actions", "add", "--device", "stick", "startup,shutdown", "--name", "ss",
          *logger("$UDEVMON_EVENT dev=$UDEVMON_DEVICE"))
    p = u("check", ok=False)
    check("the config is valid (`check`)", p.returncode == 0, p.stdout + p.stderr)


def start_daemon():
    out = open(state["daemon_log"], "w")
    state["daemon"] = subprocess.Popen([PY, "-m", "udevmon", "--config", state["cfg"], "-v", "run"],
                                       env=env(), stdout=out, stderr=subprocess.STDOUT,
                                       start_new_session=True)
    time.sleep(2.5)
    return state["daemon"].poll() is None


def stop_daemon():
    d = state.get("daemon")
    if d and d.poll() is None:
        d.send_signal(signal.SIGINT)
        try:
            d.wait(8)
        except subprocess.TimeoutExpired:
            d.kill()


def steps(sim):
    usb = state["usb"]
    L = state["log"]
    mark = lambda: len(L.read_text().splitlines()) if L.exists() else 0

    say("\n[1/7] Start the daemon with the stick plugged in")
    if not check("the daemon starts", start_daemon(), Path(state["daemon_log"]).read_text()):
        return
    if usb:
        expect("`startup` fires for the plugged-in stick", 0, ["startup dev=stick"])

    say("\n[2/7] Clean unmount, then mount (done for you)")
    m = mark()
    fs = fs_now(state["uuid"])
    udisks("unmount", "-b", block_dev(fs))
    expect("`unmounted` with surprise=0, and no `removed`", m, ["unmounted dev=stick-fs surprise=0"],
           ["removed dev=stick-fs surprise=0", "removed dev=stick-fs surprise=1"])
    m = mark()
    udisks("mount", "-b", block_dev(fs))
    expect("`mounted` fires", m, ["mounted dev=stick-fs surprise="])

    say("\n[3/7] Device status")
    for status, own_runs in (("disable", False), ("ignore", False), ("enable", True)):
        u("devices", status, "stick-fs")
        m = mark()
        cycle()
        ok, lines = wait_for(m, ["own:mounted"] if own_runs else [], 4)
        tag_runs = "mounted dev=stick-fs surprise=" in lines
        wanted_tag = status != "ignore"
        check(f"`devices {status}`: own action {'runs' if own_runs else 'skipped'}, "
              f"tag action {'runs' if wanted_tag else 'skipped'}",
              ("own:mounted" in lines) == own_runs and tag_runs == wanted_tag, str(lines))

    say("\n[4/7] Pause and action switch")
    u("pause")
    m = mark()
    cycle()
    expect("nothing runs while paused", m, unwanted=["own:mounted", "own:unmounted"])
    u("resume")
    m = mark()
    cycle()
    expect("actions run again after `resume`", m, ["own:mounted"])
    u("actions", "disable", "own")
    m = mark()
    cycle()
    expect("a disabled action is skipped, others run", m, ["mounted dev=stick-fs surprise="], ["own:mounted"])
    u("actions", "enable", "own")

    say("\n[5/7] The unnamed gate")
    u("actions", "remove", "own")
    u("devices", "remove", "stick-fs")
    u("tags", "add", "usbrule", "--match", f"fs_uuid={state['uuid']}")
    u("actions", "add", "--tag", "usbrule", "mounted", "--allow-unnamed", "--name", "open",
      *logger("open dev=[$UDEVMON_DEVICE]"))
    u("actions", "add", "--tag", "usbrule", "mounted", "--named-only", "--name", "closed",
      *logger("closed dev=[$UDEVMON_DEVICE]"))
    m = mark()
    cycle()
    expect("unnamed: only the allow_unnamed action runs", m, ["open dev=[]"], ["closed dev=[]"])
    u("devices", "add", "stick-fs", "--match", f"fs_uuid={state['uuid']}", "--tag", "usbtest")
    m = mark()
    cycle()
    expect("named again: both run", m, ["open dev=[stick-fs]", "closed dev=[stick-fs]"])

    say("\n[6/7] The real pull")
    if not sim:
        say("  PULL THE STICK OUT NOW, without ejecting it. (Nothing should be writing to it.)")
    ensure_mounted()
    m = mark()
    if sim:
        say("  (skipped: a loop device cannot be pulled while mounted)")
        udisks("unmount", "-b", state["loop"])
        udisks("loop-delete", "-b", state["loop"])
        return_to_plug = True
    else:
        return_to_plug = False
    wanted = ["unmounted dev=stick-fs surprise=1", "removed dev=stick-fs surprise=1"]
    if usb:
        wanted.append("disconnected dev=stick surprise=")
    if not sim:
        expect("`unmounted`, `removed` (surprise=1) and `disconnected`", m, wanted, timeout=90)

    say("\n[7/7] Plug it back in, then stop the daemon")
    m = mark()
    if sim:
        p = udisks("loop-setup", "-f", str(state["image"]))
        mm = re.search(r"(/dev/loop\d+)", p.stdout)
        state["loop"] = mm.group(1) if mm else None
    else:
        say("  PLUG THE STICK IN AGAIN now.")
    wait_until(lambda: fs_now(state["uuid"]), 90, "the stick to come back")
    ensure_mounted()
    wanted = ["mounted dev=stick-fs surprise="]
    if usb:
        wanted.append("connected dev=stick surprise=")
    expect("`connected` and `mounted` fire again", m, wanted)
    m = mark()
    stop_daemon()
    if usb:
        expect("`shutdown` fires when the daemon stops", m, ["shutdown dev=stick"], timeout=3)
    else:
        check("the daemon stops", state["daemon"].poll() is not None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--simulate-loop", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--image", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--keep-popups", action="store_true",
                    help="don't switch off the desktop's open-a-window-on-mount setting")
    args = ap.parse_args()
    sim = args.simulate_loop
    state["image"] = args.image

    if not sim and not shutil.which("udisksctl"):
        sys.exit("udisksctl not found (install the udisks2 package).")
    if subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", r"(-m udevmon|bin/udevmon)( .*)? run$"],
                      capture_output=True).returncode == 0:
        sys.exit("A udevmon daemon seems to be running. Stop it first: "
                 "systemctl --user stop udevmon")

    tmp = Path(tempfile.mkdtemp(prefix="udevmon-usbtest-"))
    state.update(tmp=tmp, cfg=str(tmp / "config.toml"), log=tmp / "test.log",
                 daemon_log=str(tmp / "daemon.log"), runtime=str(tmp / "run"))
    os.mkdir(state["runtime"], 0o700)
    state["log"].touch()
    before_windows = windows()
    if not args.keep_popups:
        stop_popups()
    failed_hard = None
    try:
        find_stick(sim, args.image)
        build_config()
        steps(sim)
    except (RuntimeError, KeyboardInterrupt) as e:
        failed_hard = e
        say(f"\nStopped: {e}")
    finally:
        stop_daemon()
        if state.get("loop"):
            udisks("unmount", "-b", state["loop"])
            udisks("loop-delete", "-b", state["loop"])
        # close a file-manager window our mounts opened, if the desktop still made one
        label = state.get("label")
        if label:
            for wid, line in windows().items():
                if wid not in before_windows and label in line:
                    subprocess.run(["wmctrl", "-ic", wid])
        if "restore" in state:
            state["restore"]()
        bad = [r for r in results if not r[1]] or failed_hard
        if bad:
            report = Path.cwd() / "usb-test-report.txt"
            parts = [f"failures: {[r[0] for r in results if not r[1]]}", f"stopped: {failed_hard}"]
            for label_, p in (("config", state["cfg"]), ("action log", state["log"]),
                              ("daemon -v output", state["daemon_log"])):
                try:
                    parts.append(f"--- {label_} ---\n{Path(p).read_text()}")
                except OSError:
                    pass
            parts.append("--- details ---\n" + "\n".join(f"{n}: {d}" for n, ok, d in results if not ok))
            report.write_text("\n".join(parts))
            say(f"\nDetails saved in {report}. Send me that file.")
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for r in results if r[1])
    say(f"\n{passed} of {len(results)} checks passed." + ("" if not failed_hard else " (run incomplete)"))
    sys.exit(1 if (len(results) - passed or failed_hard) else 0)


if __name__ == "__main__":
    main()
