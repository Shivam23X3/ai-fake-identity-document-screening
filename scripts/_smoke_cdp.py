"""Browser smoke test for the SIH demo mode (Step 14) — internal tool.

Drives headless Chrome over the DevTools Protocol (CDP) using the venv's
``websockets`` package: login -> SIH Demo tab -> run all four scripted cases
-> open the full report from the demo panel. Prints PASS/FAIL per step.

Usage:
    .venv/Scripts/python scripts/_smoke_cdp.py
(Requires: uvicorn on :8000, vite on :5173, and Chrome launched with
 --remote-debugging-port=9222 --user-data-dir=<tmp profile>.)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles default to cp1252 and crash on emoji in page text.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from websockets.sync.client import connect  # noqa: E402

FRONTEND = "http://localhost:5173/"
CDP_HTTP = "http://127.0.0.1:9222"
USERNAME, PASSWORD = "smoke.admin", "Smoke#Demo#2026!"

_results: list[tuple[str, bool, str]] = []


def record(step: str, ok: bool, note: str = "") -> None:
    _results.append((step, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" — {note}" if note else ""))


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = connect(ws_url, max_size=50 * 1024 * 1024)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error for {method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP timeout for {method}")

    def eval_js(self, expr: str, timeout: float = 30):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        val = res.get("result", {})
        if val.get("subtype") == "error":
            raise RuntimeError(f"JS error: {val.get('description')}")
        return val.get("value")


def wait_js(cdp: CDP, expr: str, timeout: float, poll: float = 0.7, desc: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = cdp.eval_js(expr)
        except Exception as exc:  # transient eval errors are fine while polling
            last = f"eval-error: {exc}"
        if last is True:
            return True
        time.sleep(poll)
    record(f"wait: {desc or expr[:60]}", False, f"timed out after {timeout:.0f}s (last={last})")
    return False


def launch_chrome() -> None:
    import os
    import shutil

    candidates = [
        shutil.which("chrome"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("msedge"),
    ]
    exe = next((c for c in candidates if c and Path(c).is_file()), None)
    if exe is None:
        raise RuntimeError("Neither Chrome nor Edge was found on this machine")
    profile = Path("data/_smoke_chrome_profile").resolve()
    subprocess.Popen(
        [
            exe,
            "--headless=new",
            "--remote-debugging-port=9222",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--window-size=1440,1000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{CDP_HTTP}/json/version", timeout=1)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Chrome CDP endpoint did not come up on :9222")


def main() -> int:
    print("== SIH demo mode browser smoke test ==")
    launch_chrome()
    targets = json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json/list", timeout=5).read())
    page = next(t for t in targets if t.get("type") == "page")
    cdp = CDP(page["webSocketDebuggerUrl"])

    # 1. Load the app
    cdp.call("Page.enable")
    cdp.call("Page.navigate", {"url": FRONTEND})
    wait_js(cdp, "!!document.querySelector('#username')", 20, desc="login form renders")

    # 2. Login (React controlled inputs need the native value setter)
    cdp.eval_js(
        """
        (() => {
          const setNative = (el, v) => {
            const desc = Object.getOwnPropertyDescriptor(
              Object.getPrototypeOf(el), 'value');
            desc.set.call(el, v);
            el.dispatchEvent(new Event('input', { bubbles: true }));
          };
          setNative(document.querySelector('#username'), %r);
          setNative(document.querySelector('#password'), %r);
          document.querySelector('form button[type=submit]').click();
          return true;
        })()
        """
        % (USERNAME, PASSWORD)
    )
    wait_js(cdp, "!!document.querySelector('.tabs')", 20, desc="dashboard after login")

    # 3. Open the SIH Demo tab
    cdp.eval_js(
        "[...document.querySelectorAll('button.tab')]"
        ".find(b => b.textContent.includes('SIH Demo')).click()"
    )
    wait_js(cdp, "document.querySelectorAll('.demo-case-card').length === 4", 20,
            desc="4 demo case cards render")
    titles = cdp.eval_js(
        "[...document.querySelectorAll('.demo-case-card h3')].map(h => h.textContent.trim())"
    )
    record("case catalog shows all four cases", isinstance(titles, list) and len(titles) == 4,
           str(titles))
    notice = cdp.eval_js("document.querySelector('.demo-notice')?.textContent ?? ''")
    record("DEMONSTRATION notice visible", "DEMONSTRATION" in str(notice).upper())

    # 4. Run each scripted case from the UI
    order = ["case1_valid", "case2_tampered", "case3_identity_mismatch", "case4_low_quality"]
    for i, case_id in enumerate(order):
        cdp.eval_js(
            f"[...document.querySelectorAll('.demo-case-card')][{i}]"
            ".querySelector('button.btn-primary').click()"
        )
        ok = wait_js(
            cdp,
            "!!document.querySelector('.demo-run-panel') && "
            "![...document.querySelectorAll('.spinner')].length",
            120,
            desc=f"{case_id} pipeline run completes",
        )
        if not ok:
            continue
        panel = cdp.eval_js(
            "document.querySelector('.demo-run-panel').innerText.slice(0, 500)"
        )
        met = cdp.eval_js("document.querySelectorAll('.demo-checks .check-met').length")
        unmet = cdp.eval_js("document.querySelectorAll('.demo-checks .check-unmet').length")
        record(
            f"{case_id}: run panel shows checkpoints",
            isinstance(met, int) and met >= 4 and unmet == 0,
            f"met={met} unmet={unmet}",
        )
        record(
            f"{case_id}: panel carries DEMONSTRATION label",
            "DEMONSTRATION" in str(panel).upper(),
        )

    # 5. Deep-link from the demo panel into the full report
    cdp.eval_js(
        "[...document.querySelectorAll('.demo-run-panel button')]"
        ".find(b => b.textContent.includes('Open full report')).click()"
    )
    wait_js(cdp, "!!document.querySelector('.grid-detail')", 20, desc="full report view opens")
    detail_run = cdp.eval_js(
        "document.querySelector('.grid-detail')?.innerText.includes('demo_') ?? false"
    )
    record("full report view shows the demo run", bool(detail_run))

    # 6. Backend honesty check: the stored report has no demo markers.
    # NOTE: raw fetch() from page context does NOT carry the JWT (the app's
    # api client attaches it manually) — read the token from sessionStorage
    # and send it explicitly.
    import re

    detail_text = str(cdp.eval_js("document.querySelector('.grid-detail')?.innerText ?? ''"))
    m = re.search(r"demo_[a-z0-9_]+", detail_text)
    run_id = m.group(0) if m else ""
    if run_id:
        stored = cdp.eval_js(
            f"(() => {{ const t = sessionStorage.getItem('screening.token'); "
            f"return fetch('/api/screening/{run_id}', "
            f"{{ headers: {{ Authorization: 'Bearer ' + t }} }}) "
            f".then(r => r.json()).then(d => JSON.stringify(d.report ?? {{}})); }})()"
        )
        s = str(stored).upper()
        record(
            "stored report stays canonical (no demo markers)",
            "DEMONSTRATION" not in s and '"demo"' not in s,
            f"checked {run_id}",
        )
    else:
        record("stored report stays canonical", False, "no demo run id in detail view")

    print("\n== Summary ==")
    failed = [r for r in _results if not r[1]]
    for step, ok, note in _results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {step}")
    print(f"\n{len(_results) - len(failed)}/{len(_results)} steps passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    raise SystemExit(code)
