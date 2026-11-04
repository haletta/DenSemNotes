"""Keep DenSemNotes up to date from its GitHub repository.

update.json (in the repository) names the repository and branch. version.json (written when the app is
installed or updated) records which commit is installed. The app compares that with the newest commit on
the branch, shows what changed, and on request downloads that commit, keeps the current files as
"previous" (so you can go back), runs the installer in update mode, and restarts.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from ai import SSL_CTX

HERE = os.path.dirname(os.path.abspath(__file__))
MAC = sys.platform == "darwin"
CHECK_EVERY = 6 * 3600


def _read(name):
    try:
        with open(os.path.join(HERE, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def source():
    """(owner/repo, branch) the app updates from."""
    u = _read("update.json")
    return str(u.get("repo") or ""), str(u.get("branch") or "main")


def installed():
    return _read("version.json")


class Updater:
    def __init__(self, hub):
        self.hub = hub
        self.state = {"state": "idle", "available": False, "current": installed().get("sha", ""), "latest": "",
                      "notes": [], "error": "", "checked": 0, "repo": source()[0], "branch": source()[1],
                      "can_restore": os.path.isdir(os.path.join(HERE, "previous"))}
        self.lock = threading.Lock()

    # -- GitHub
    def _get(self, url, accept="application/vnd.github+json", timeout=20):
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "DenSemNotes-updater",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
        token = str(self.hub.cfg.get("github_token") or "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise RuntimeError("Couldn't see the repository. If it's private, add a GitHub token in "
                                   "Preferences › General › Updates.") from None
            raise RuntimeError(f"GitHub answered {e.code}.") from None
        except urllib.error.URLError:
            raise RuntimeError("Couldn't reach GitHub. Check the internet connection.") from None

    def _emit(self):
        self.hub.emit("update", self.public())

    def public(self):
        return dict(self.state, auto=self.hub.cfg.get("auto_update_check", True) is not False)

    def check(self, quiet=False):
        repo, branch = source()
        self.state.update(repo=repo, branch=branch)
        if not repo:
            self.state.update(state="idle", error="This copy wasn't installed from GitHub, so there's nothing to "
                                                  "check. Install it from your repository once to get updates.")
            return self.public()
        with self.lock:
            self.state.update(state="checking", error="")
            if not quiet:
                self._emit()
            try:
                head = json.loads(self._get(f"https://api.github.com/repos/{repo}/commits/{branch}"))
                latest = head["sha"]
                cur = installed().get("sha", "")
                notes = []
                if cur and cur != latest:
                    try:
                        cmp = json.loads(self._get(f"https://api.github.com/repos/{repo}/compare/{cur}...{latest}"))
                        notes = [c["commit"]["message"].split("\n")[0][:140] for c in cmp.get("commits", [])][-15:][::-1]
                    except Exception:
                        notes = [head["commit"]["message"].split("\n")[0][:140]]
                elif not cur:
                    notes = [head["commit"]["message"].split("\n")[0][:140]]
                self.state.update(state="idle", latest=latest, current=cur, available=bool(latest and latest != cur),
                                  notes=notes, checked=time.time(),
                                  date=head.get("commit", {}).get("committer", {}).get("date", ""))
            except Exception as e:
                self.state.update(state="idle", error=str(e), checked=time.time())
        self._emit()
        return self.public()

    def loop(self):
        time.sleep(15)
        while True:
            if self.hub.cfg.get("auto_update_check", True) is not False and source()[0]:
                try:
                    self.check(quiet=True)
                except Exception:
                    pass
            time.sleep(CHECK_EVERY)

    # -- installing
    def update(self):
        if not self.state.get("latest"):
            self.check(quiet=True)
        if not self.state.get("available"):
            return {"ok": False, "error": self.state.get("error") or "You already have the newest version."}
        if self.hub.recording:
            return {"ok": False, "error": "Pause recording first, then update."}
        if self.state["state"] == "installing":
            return {"ok": False, "error": "Already updating."}
        self.state.update(state="installing", error="", step="Downloading the update…")
        self._emit()
        threading.Thread(target=self._install, daemon=True).start()
        return {"ok": True}

    def _step(self, text):
        self.state["step"] = text
        self._emit()

    def _install(self):
        repo, _ = source()
        sha = self.state["latest"]
        tmp = tempfile.mkdtemp(prefix="densem-update-")
        try:
            blob = self._get(f"https://api.github.com/repos/{repo}/zipball/{sha}", accept="application/vnd.github+json",
                             timeout=120)
            zpath = os.path.join(tmp, "update.zip")
            with open(zpath, "wb") as f:
                f.write(blob)
            self._step("Unpacking…")
            out = os.path.join(tmp, "src")
            with zipfile.ZipFile(zpath) as z:
                for n in z.namelist():  # never write outside the folder
                    if n.startswith("/") or ".." in n.split("/"):
                        raise RuntimeError("The update file looks wrong; nothing was changed.")
                z.extractall(out)
            roots = [os.path.join(out, d) for d in os.listdir(out)]
            src = next((r for r in roots if os.path.exists(os.path.join(r, "app.py"))), None)
            if not src or not os.path.exists(os.path.join(src, "ui.html")) or not os.path.exists(
                    os.path.join(src, "Install.command")):
                raise RuntimeError("The update is missing app files; nothing was changed.")
            with open(os.path.join(src, "version.json"), "w") as f:
                json.dump({"repo": repo, "sha": sha, "installed": time.time()}, f)
            self._step("Keeping the current version so you can go back…")
            self._backup()
            self._step("Installing (a minute or two if new libraries are needed)…")
            if MAC:
                r = subprocess.run(["bash", os.path.join(src, "Install.command"), "--update"], capture_output=True,
                                   text=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError("The installer stopped: " + (r.stderr or r.stdout)[-400:])
            else:  # development copy on another system: just copy the files over
                for name in os.listdir(src):
                    p = os.path.join(src, name)
                    if os.path.isfile(p) and (name.endswith(".py") or name in ("ui.html", "template.md", "update.json",
                                                                                "version.json", "requirements.txt")):
                        shutil.copy(p, os.path.join(HERE, name))
            self.state.update(state="ready", available=False, current=sha, step="", can_restore=True,
                              notes=self.state.get("notes", []))
            self._emit()
        except Exception as e:
            self.state.update(state="idle", error=str(e)[:400], step="")
            self._emit()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _backup(self):
        prev = os.path.join(HERE, "previous")
        shutil.rmtree(prev, ignore_errors=True)
        os.makedirs(os.path.join(prev, "vendor"), exist_ok=True)
        for name in os.listdir(HERE):
            p = os.path.join(HERE, name)
            if os.path.isfile(p) and (name.endswith(".py") or name in ("ui.html", "requirements.txt", "template.md",
                                                                        "update.json", "version.json", "logo.svg",
                                                                        "AppIcon.icns")):
                shutil.copy2(p, os.path.join(prev, name))
        v = os.path.join(HERE, "vendor", "mermaid.min.js")
        if os.path.exists(v):
            shutil.copy2(v, os.path.join(prev, "vendor", "mermaid.min.js"))

    def restore(self):
        """Go back to the version that was installed before the last update."""
        prev = os.path.join(HERE, "previous")
        if not os.path.isdir(prev):
            return {"ok": False, "error": "There's no earlier version kept."}
        if self.hub.recording:
            return {"ok": False, "error": "Pause recording first."}
        for name in os.listdir(prev):
            p = os.path.join(prev, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(HERE, name))
        v = os.path.join(prev, "vendor", "mermaid.min.js")
        if os.path.exists(v):
            shutil.copy2(v, os.path.join(HERE, "vendor", "mermaid.min.js"))
        shutil.rmtree(prev, ignore_errors=True)
        self.state.update(state="ready", current=installed().get("sha", ""), available=False, can_restore=False,
                          step="", error="")
        self._emit()
        return {"ok": True}

    def restart(self):
        """Quit and open the app again (macOS), so the new version runs."""
        if self.hub.recording:
            return {"ok": False, "error": "Pause recording first."}
        self.hub.persist()
        if MAC:
            app_path = os.path.expanduser("~/Applications/DenSemNotes.app")
            if not os.path.exists(app_path):  # installed before the rename
                app_path = os.path.expanduser("~/Applications/DenSem Class.app")
            subprocess.Popen(["/bin/sh", "-c", f'sleep 2; open "{app_path}"'], start_new_session=True)
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return {"ok": True}
        return {"ok": False, "error": "Restart the app to use the new version."}
