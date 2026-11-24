"""What a sandboxed project writes in its own services reaches nothing outside its sandbox (docs/design/capabilities.md,
"11-25: service units"): its unit is a skeleton, its environment and log are applied inside, and a unit file that isn't
the platform's is never touched. The service manager is a stand-in and every folder a temporary one.

    bin/py -m unittest tests.test_service_units
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import isolation  # noqa: E402
import registry  # noqa: E402
import systemd_units  # noqa: E402
import testguard  # noqa: E402

PLATFORM_PATH = "/usr/bin:/bin"
SR = str(hubconf.ROOT / "bin" / "sandboxed-run")


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class World(unittest.TestCase):
    """A registry index, a unit folder and a service manager of the test's own."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rs = load("render_services_units", "render-services.py")
        self.agents = self.tmp / "LaunchAgents"
        self.state = self.tmp / "state"
        for d in (self.agents, self.state, self.tmp / "logs"):
            d.mkdir()
        self.loaded: dict[str, str] = {}    # label -> the file it came from
        self.calls: list[tuple] = []
        for target, name, value in (
                (self.rs, "AGENTS", self.agents), (self.rs, "RENDERED", self.state / "rendered-services.json"),
                (self.rs, "WAITED", self.state / "waiting.json"), (self.rs, "launchctl", self.launchctl),
                (self.rs, "DEFAULT_PATH", PLATFORM_PATH), (hubconf, "LAUNCHD_PATH", PLATFORM_PATH),
                (hubconf, "STATE", self.state), (hubconf, "LOGS", self.tmp / "logs"),
                (hubconf, "service_manager", lambda: "launchd"), (hubconf, "cli_folder", lambda: self.tmp / "cli"),
                (hubconf, "projects", lambda: self.index["projects"]), (hubconf, "REGISTRY_INDEX", self.tmp / "index.json"),
                (isolation, "can_sandbox", lambda: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.sent: list[str] = []
        self.sends = True
        p = mock.patch.object(self.rs.alert, "configured", lambda: ["a channel"])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.rs.time, "sleep", lambda _s: None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.rs.alert, "send", lambda text, *a, **kw: self.sent.append(text) or self.sends)
        p.start()
        self.addCleanup(p.stop)
        self.index = {"v": 1, "errors": {}, "modules": {}, "projects": {}}

    refuses: tuple = ()

    def launchctl(self, *args):
        self.calls.append(args)
        if args[0] == "bootstrap" and "bootstrap" not in self.refuses:
            data = plistlib.loads(pathlib.Path(args[2]).read_bytes())
            self.loaded[data["Label"]] = args[2]
        label = args[1].split("/", 2)[-1]
        if args[0] == "print":
            if label not in self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "")
            return subprocess.CompletedProcess(args, 0, f"x = {{\n\tpath = {self.loaded[label]}\n\tstate = running\n}}\n", "")
        if args[0] in self.refuses:
            return subprocess.CompletedProcess(args, 5, "", "Input/output error")
        if args[0] == "bootout":
            self.loaded.pop(label, None)
        return subprocess.CompletedProcess(args, 0, "", "")

    def project(self, name, *services, sandbox=True, **kw):
        ws = self.tmp / "ws" / name
        (ws / "bin").mkdir(parents=True, exist_ok=True)
        (ws / "bin" / "serve.sh").write_text("#!/bin/sh\n")
        (ws / "bin" / "serve.sh").chmod(0o755)
        self.index["projects"][name] = {"v": 2, "name": name, "paths": {"workspace": str(ws)}, "services": list(services),
                                        **({"enforce": {"sandbox": True}} if sandbox else {}), **kw}
        return ws

    def unit(self, label):
        return self.agents / f"{label}.plist"

    def plist(self, label):
        return plistlib.loads(self.unit(label).read_bytes())

    def record(self, **labels):
        self.rs.units_record().write_text(json.dumps(labels))

    def list_rendered(self, *labels):
        """The list as a version without the record wrote it"""
        self.rs.RENDERED.write_text(json.dumps(sorted(labels)))

    def apply_index(self):
        (self.tmp / "index.json").write_text(json.dumps(self.index, default=str))

    def apply(self):
        self.apply_index()
        with contextlib.redirect_stdout(io.StringIO()):
            return [(c.op, c.target.removeprefix("service:")) for c in self.rs.apply(self.index)]

    def problems(self):
        """What a run says, as main() collects it."""
        said = []
        with mock.patch.object(self.rs.registry, "refresh", lambda: self.index), \
                mock.patch.object(self.rs.alert, "once", lambda key, p, *a, **kw: said.extend(p)), \
                mock.patch.object(hubconf, "worktree_lookup", lambda: False), \
                mock.patch.object(sys, "argv", ["render-services.py"]), contextlib.redirect_stdout(io.StringIO()):
            self.rs.main()
        return said

    def booted_out(self):
        return [c[1].split("/", 2)[-1] for c in self.calls if c[0] == "bootout"]

    def old_plist(self, label, project, env=None, cli=False, port=None, args=None, **kw):
        """What the platform wrote before the skeleton: sandboxed-run behind the interpreter (or the shell that puts the
        CLI folder on PATH), the entry's environment and log in it; args: an unsandboxed service's command."""
        ws = self.tmp / "ws" / project
        args = args or [sys.executable, SR, "--project", project, "--cwd", str(ws),
                        *(["--port", port] if port else []), "--", "/bin/sh", "-c", "x"]
        if cli:
            args = self.rs.cli_last(args)
        return {"Label": label, "ProgramArguments": args, "WorkingDirectory": str(ws), "RunAtLoad": True,
                **({"EnvironmentVariables": env} if env else {}),
                **dict(zip(("StandardOutPath", "StandardErrorPath"), [str(ws / "x.log")] * 2)), **kw}

    def old_unit(self, label, project, *a, **kw):
        self.unit(label).write_bytes(plistlib.dumps(self.old_plist(label, project, *a, **kw)))
        self.loaded[label] = str(self.unit(label))

    def foreign(self, label, program="/usr/local/bin/vendor"):
        data = plistlib.dumps({"Label": label, "ProgramArguments": [program, "--project", "box", SR]})
        self.unit(label).write_bytes(data)
        self.loaded[label] = str(self.unit(label))
        return data


def quiet(calls=None):
    """A systemd that does what it is told, runs nothing and has no unit"""
    def systemctl(*args):
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, 0, {"is-active": "inactive\n", "show": "LoadState=not-found\nFragmentPath=\n"}.get(args[0], ""), "")
    return systemctl


class Skeleton(World):
    """r1 tests 1, 3, 5; r2 sections 1 and 3's newline; r3 1 and 3."""

    LOUD = {"name": "web", "run": "bin/serve.sh --port 1", "keepalive": True, "log": "web.log", "label": "com.vendor.agent",
            "env": {"PYTHONPATH": "lib-marker", "DYLD_INSERT_LIBRARIES": "/x/dylib-marker", "PATH": "/path-marker",
                    "LD_PRELOAD": "/x/so-marker"}}

    def test_a_sandboxed_units_text_holds_nothing_of_its_entrys_environment_log_or_label(self):
        self.project("box", self.LOUD)
        plist, problem = self.rs.render("box", self.index["projects"]["box"], self.LOUD)
        self.assertIsNone(problem)
        for text in (plistlib.dumps(plist).decode(), "".join(systemd_units.files(plist).values())):
            for word in ("marker", "PYTHONPATH", "DYLD_", "LD_PRELOAD", "web.log", "com.vendor"):
                self.assertNotIn(word, text)
        self.assertEqual(plist["Label"], "local.box.web")
        self.assertEqual(plist["ProgramArguments"][1:4], ["-I", "-B", SR])
        self.assertEqual(set(plist), {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "StandardOutPath",
                                      "StandardErrorPath"})
        for k in ("StandardOutPath", "StandardErrorPath"):
            self.assertEqual(pathlib.Path(plist[k]).parent, self.tmp / "logs")

    def test_one_started_by_name_has_the_platforms_path(self):
        svc = {"name": "web", "run": "sh -c true", "env": {"PATH": "/path-marker"}}
        self.project("box", svc)
        plist, _ = self.rs.render("box", self.index["projects"]["box"], svc)
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": f"{PLATFORM_PATH}:{self.tmp / 'cli'}"})

    def test_a_log_at_a_link_or_anywhere_else_isnt_opened_by_the_service_manager(self):
        ws = self.project("box")
        outside = self.tmp / "outside"
        (ws / "link.log").symlink_to(outside)
        for log in ("link.log", str(outside), "../other/x.log"):
            with self.subTest(log=log):
                plist, _ = self.rs.render("box", self.index["projects"]["box"], {"name": "w", "run": "bin/serve.sh", "log": log})
                self.assertEqual(pathlib.Path(plist["StandardOutPath"]).parent, self.tmp / "logs")

    def test_nothing_of_the_projects_is_looked_up_outside(self):
        """the words are as written; a command that can't be run changes nothing of the unit"""
        ws = self.project("box")
        svc = {"name": "w", "run": "bin/serve.sh conf.json"}
        doc = self.index["projects"]["box"]
        first = self.rs.render("box", doc, svc)
        (ws / "conf.json").write_text("{}")
        (ws / "bin" / "serve.sh").chmod(0o644)
        self.assertEqual(self.rs.render("box", doc, svc), first)
        self.assertEqual(first[0]["ProgramArguments"][-2:], ["bin/serve.sh", "conf.json"])

    def test_an_unsandboxed_and_an_exempt_service_keep_their_environment_log_and_label(self):
        ws = self.project("open", sandbox=False)
        self.project("exempt")
        (ws / "conf.json").write_text("{}")
        svc = {"name": "web", "label": "local.web", "run": "bin/serve.sh conf.json", "interval_s": 60, "log": "web.log",
               "env": {"PATH": "$PLATFORM_PATH", "CONF": "conf.json", "N": 1}}
        real = hubconf.get
        with mock.patch.object(hubconf, "get", lambda k, d=None: ["exempt"] if k == "service_sandbox_exempt" else real(k, d)):
            for name in ("open", "exempt"):
                with self.subTest(project=name):
                    folder = self.tmp / "ws" / name
                    plist, problem = self.rs.render(name, self.index["projects"][name], svc)
                    self.assertIsNone(problem)
                    self.assertEqual(plist, {
                        "Label": "local.web", "WorkingDirectory": str(folder), "RunAtLoad": True, "StartInterval": 60,
                        "ProgramArguments": [str(folder / "bin" / "serve.sh"),
                                             str(folder / "conf.json") if name == "open" else "conf.json"],
                        "EnvironmentVariables": {"PATH": f"{PLATFORM_PATH}:{self.tmp / 'cli'}", "N": "1",
                                                 "CONF": str(folder / "conf.json") if name == "open" else "conf.json"},
                        "StandardOutPath": str(folder / "web.log"), "StandardErrorPath": str(folder / "web.log")})

    def test_an_environment_only_change_changes_the_unit(self):
        svc = {"name": "web", "run": "bin/serve.sh", "env": {"A": "1"}}
        self.project("box", svc)
        self.assertEqual(self.apply(), [("add", "local.box.web")])
        self.project("box", {**svc, "env": {"A": "2"}})
        self.assertEqual([c.op for c in self.rs.plan(self.index)], ["update"])
        self.assertEqual(self.apply(), [("update", "local.box.web")])
        self.assertNotIn("2", self.plist("local.box.web").get("EnvironmentVariables", {}).values())
        self.assertEqual(self.apply(), [])

    def test_yaml_in_memory_and_the_json_index_digest_the_same(self):
        import yaml
        doc = yaml.safe_load('{name: 8080, run: "bin/serve.sh", interval_s: "300", throttle_s: 5.0, keepalive: 1,\n'
                             ' env: {PORT: 8080, DEBUG: true, RATE: 1.5, DAY: 2026-01-02, NONE: null}, log: ""}')
        back = json.loads(json.dumps(doc, default=str))
        self.assertEqual(isolation.service_digest(doc), isolation.service_digest(back))
        n = isolation.normal_service(doc)
        self.assertEqual(n, {"name": "8080", "run": "bin/serve.sh", "interval_s": 300, "throttle_s": 5, "keepalive": True,
                             "env": {"PORT": "8080", "DEBUG": "True", "RATE": "1.5", "DAY": "2026-01-02", "NONE": "None"}})
        self.assertEqual(isolation.normal_service(isolation.normal_service(doc)), n)


class Inside(unittest.TestCase):
    """End to end through the real sandboxed-run, started as a service manager would from the rendered unit (r1 test 2; r2
    section 2; r3 5 and 6)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        if isolation.in_system_tmp(self.tmp):
            self.skipTest("the test's folder is in a temp area every sandbox writes")
        self.rs = load("render_services_inside", "render-services.py")
        self.ws = self.tmp / "workspace" / "box"
        self.out = self.tmp / "outside"
        self.state = self.tmp / "state"
        for d in (self.ws / "bin", self.ws / "lib", self.out, self.state / "shared" / "registry", self.tmp / "home",
                  self.tmp / "config" / "projects", self.tmp / "bus"):
            d.mkdir(parents=True)
        (self.tmp / "config" / "hub.json").write_text(json.dumps({
            "v": 1, "registry_dir": "projects", "owner": {"address": "owner", "tag": "owner"},
            "runtime": {"default": "none"}, "workspace_root": str(self.tmp / "workspace"), "bus_dir": str(self.tmp / "bus")}))
        # run outside, it would leave a mark where the sandbox can't write
        (self.ws / "lib" / "sitecustomize.py").write_text(f"open({str(self.out / 'ran')!r}, 'a').close()\n")
        (self.ws / "bin" / "show.sh").write_text('#!/bin/sh\nenv > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"\necho said\n')
        (self.ws / "bin" / "show.sh").chmod(0o755)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BRUNNA_", "AGENT_HUB_", "AGENTHUB_"))},
                    "HOME": str(self.tmp / "home"), "AGENT_HUB_CONFIG": str(self.tmp / "config"),
                    "AGENT_HUB_STATE": str(self.state), "AGENT_HUB_LANG_UI": "en", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

    def start(self, svc, digest=None, index=None):
        svc = {**svc, "env": {"OUT": "seen", **svc.get("env", {})}}
        doc = {"v": 2, "name": "box", "paths": {"workspace": str(self.ws)}, "enforce": {"sandbox": True}, "services": [svc]}
        written = doc if index is None else {**doc, "services": index}
        (self.state / "shared" / "registry" / "index.json").write_text(json.dumps({"projects": {"box": written}}))
        with mock.patch.object(isolation, "can_sandbox", lambda: None):
            plist, _problem = self.rs.render("box", doc, svc)
        self.plist = plist
        env = {**self.env, **plist.get("EnvironmentVariables", {})}
        return testguard.bounded(plist["ProgramArguments"], 120, text=True, env=env,
                                 cwd=plist.get("WorkingDirectory") or str(self.tmp))

    def seen(self):
        lines = (self.ws / "seen").read_text().splitlines()
        return dict(x.split("=", 1) for x in lines if "=" in x)

    needs = unittest.skipUnless(isolation.can_sandbox() is None, "needs this machine's adapter (sandbox-exec, Landlock ABI 4)")

    @needs
    def test_the_entrys_environment_reaches_the_command_and_nothing_outside(self):
        r = self.start({"name": "web", "run": "bin/show.sh", "env": {
            "PYTHONPATH": str(self.ws / "lib"), "GREETING": "a b=c", "DATA": "lib", "MISSING": "nowhere", "EMPTY": "",
            "ABS": "/etc", "UV_CACHE_DIR": "/mine", "BRUNNA_STATE": "/mine", "AGENT_HUB_STATE": "/mine",
            "n": "5", "log": "x"}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.out / "ran").exists(), "the project's code ran outside its sandbox")
        got = self.seen()
        self.assertEqual(got["GREETING"], "a b=c")
        self.assertEqual(got["PYTHONPATH"], str(self.ws / "lib"))
        self.assertEqual((got["n"], got["log"]), ("5", "x"))
        # a relative value that names something under its folder is made absolute, as an unsandboxed service's is
        self.assertEqual((got["DATA"], got["MISSING"], got["EMPTY"], got["ABS"]),
                         (str(self.ws / "lib"), "nowhere", str(self.ws), "/etc"))
        # the platform's win
        self.assertTrue(got["UV_CACHE_DIR"].startswith(str(self.ws / ".git")), got["UV_CACHE_DIR"])
        self.assertEqual(got["AGENT_HUB_STATE"], str(self.state))
        self.assertNotIn("BRUNNA_STATE", got)

    @needs
    def test_path(self):
        cli = str(self.state / "shared" / "cli")
        for what, svc, want in (
                ("by path: the service manager's", {"run": "bin/show.sh"}, f"{self.env['PATH']}:{cli}"),
                ("its own", {"run": "bin/show.sh", "env": {"PATH": "/opt/mine:/usr/bin:/bin"}}, f"/opt/mine:/usr/bin:/bin:{cli}"),
                ("with the platform's", {"run": "sh bin/show.sh", "env": {"PATH": "/opt/mine:$PLATFORM_PATH"}},
                 f"/opt/mine:{hubconf.LAUNCHD_PATH}:{cli}"),
                ("by name: the platform's", {"run": "sh bin/show.sh"}, f"{hubconf.LAUNCHD_PATH}:{cli}")):
            with self.subTest(what):
                r = self.start({"name": "web", **svc})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.seen()["PATH"], want)

    @needs
    def test_a_command_found_only_on_its_own_path(self):
        (self.ws / "lib" / "mine").write_text('#!/bin/sh\necho ok > found\n')
        (self.ws / "lib" / "mine").chmod(0o755)
        r = self.start({"name": "web", "run": "mine", "env": {"PATH": f"{self.ws}/lib:/usr/bin:/bin"}})
        self.assertEqual((r.returncode, (self.ws / "found").exists()), (0, True), r.stdout + r.stderr)

    @needs
    def test_an_equals_sign_in_the_command(self):
        (self.ws / "bin" / "a=b").write_text('#!/bin/sh\necho "$1" > found\n')
        (self.ws / "bin" / "a=b").chmod(0o755)
        r = self.start({"name": "web", "run": "bin/a=b c=d"})
        self.assertEqual((self.ws / "found").read_text() if (self.ws / "found").exists() else r.stderr, "c=d\n")

    @needs
    def test_the_log_is_opened_inside(self):
        r = self.start({"name": "web", "run": "bin/show.sh", "log": "logs $(touch x).log"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual((self.ws / "logs $(touch x).log").read_text(), "said\n")
        self.assertFalse((self.ws / "x").exists())

    @needs
    def test_a_log_it_may_not_write_is_not_written(self):
        kept = self.out / "kept"
        kept.write_text("kept\n")
        (self.ws / "link.log").symlink_to(kept)
        for log in ("link.log", str(kept)):
            with self.subTest(log=log):
                r = self.start({"name": "web", "run": "bin/show.sh", "log": log})
                self.assertEqual(kept.read_text(), "kept\n")
                self.assertNotIn(r.returncode, (0, 78), "it fails, inside")
                self.assertFalse((self.ws / "seen").exists(), "not run without its log")
                self.assertTrue(r.stderr, "said where the platform's log is")

    @needs
    def test_an_entry_that_has_moved_on_is_refused(self):
        svc = {"name": "web", "run": "bin/show.sh", "env": {"OUT": "seen", "A": "1"}}
        for what, index in (("env", [{**svc, "env": {"OUT": "seen", "A": "2"}}]), ("run", [{**svc, "run": "bin/show.sh x"}]),
                            ("log", [{**svc, "log": "x"}]), ("gone", []), ("twice", [svc, svc])):
            with self.subTest(what):
                r = self.start(svc, index=index)
                self.assertEqual(r.returncode, 78, r.stdout + r.stderr)
                self.assertFalse((self.ws / "seen").exists())
        self.assertEqual(self.start(svc, index=[svc]).returncode, 0)

    @needs
    def test_an_entry_accepted_under_older_rules_is_checked_again(self):
        for what, svc in (("a control character", {"env": {"A": "\x07"}}),
                          ("log", {"log": "a\x00b"})):
            bad = {"name": "web", "run": "bin/show.sh", **svc}
            good = {"name": "web", "run": "bin/show.sh"}
            with self.subTest(what):
                digest = isolation.service_digest(bad)
                with mock.patch.object(isolation, "service_digest", lambda s, d=digest: d):
                    r = self.start(good, index=[bad])
                self.assertEqual(r.returncode, 78, r.stdout + r.stderr)
                self.assertFalse((self.ws / "seen").exists())

    @needs
    def test_a_variable_no_shell_can_have_is_left_out_not_the_service(self):
        svc = {"name": "web", "run": "bin/show.sh", "env": {"OUT": "seen", "my-key": "1", "A B": "2", "GOOD": "3"}}
        r = self.start(svc, index=[svc])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = self.seen()
        self.assertEqual((got["GOOD"], "my-key" in got, "A B" in got), ("3", False, False))


class Jobs(unittest.TestCase):
    def test_every_command_behind_the_sandbox_starts_the_platforms_python_isolated(self):
        for argv in (isolation.wrap("p", "/w", ["x"]), isolation.wrap("p", "/w", ["x"], 80)):
            self.assertEqual(argv[1:4], ["-I", "-B", SR])

    def test_the_nightly_tests_are_tried_once(self):
        doc = {"name": "p", "paths": {"workspace": "/w"}, "tests": [{"tier": "t", "run": "x", "when": ["nightly"]}]}
        job = registry.nightly_test_jobs({"projects": {"p": doc}}, set())[0]
        self.assertEqual(job.get("max_attempts_per_day"), 1)

    def test_whose_command_it_is_goes_by_position(self):
        w = isolation.wrapped_project
        for argv in ([sys.executable, SR, "--project", "p", "--cwd", "/", "--", "x"],
                     [sys.executable, SR, "--project", "p", "--cwd", "/", "--port", "80", "--", "x", "--project", "q"],
                     [sys.executable, "-I", "-B", SR, "--project", "p", "--service", "s", "--cwd", "/", "--digest", "d", "--", "x"]):
            self.assertEqual(w(argv), "p")
        for argv in (["/usr/bin/vendor", "--", SR, "--project", "p"], ["/usr/bin/vendor", "x", SR, "--project", "p"],
                     [sys.executable, SR, "--cwd", "/", "--", "--project", "p"], [sys.executable, SR], [],
                     [sys.executable, "/elsewhere/bin/sandboxed-run", "--project", "p"],
                     [sys.executable, SR, "--project", "p", "--", "--cwd", "/", "--", "x"],
                     [sys.executable, SR, "--cwd", "/", "--project", "p", "--", "x"],
                     ["python3", SR, "--project", "p", "--cwd", "/", "--", "x"],
                     [sys.executable, SR, "--project", "p", "--cwd", "/", "--"],
                     [sys.executable, SR, "--project", "p", "--service", "s", "--cwd", "/", "--digest", "d", "--", "x"],
                     [sys.executable, "-I", "-B", SR, "--project", "p", "--cwd", "/", "--", "x"],
                     ["/usr/bin/env", SR, "--project", "p", "--cwd", "/", "--", "x"],
                     ["/bin/sh", "-c", f"{sys.executable} {SR} --project p"]):
            self.assertIsNone(w(argv), argv)


class OneNeverStopsTheOthers(World):
    """r2 section 4; r3 8."""

    def test_an_entry_that_cant_be_rendered_beside_good_ones(self):
        self.project("a-bad", {"name": "x", "run": "bin/serve.sh", "interval_s": {"a": 1}}, {"name": "y", "run": 'bin/"'},
                     "not a mapping", {"run": "x"}, {"name": "z", "run": "bin/serve.sh\x01"})
        self.project("b-bad", {"name": "x", "run": "bin/serve.sh", "cwd": "nowhere"}, {"name": "e", "run": "sh", "env": [1]},
                     sandbox=False)
        self.project("good", {"name": "web", "run": "bin/serve.sh"})
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.assertEqual(self.apply(), [("add", "local.good.web"), ("add", "local.open.web")])
        rec = json.loads(self.rs.units_record().read_text())
        self.assertEqual(sorted(rec), ["local.good.web", "local.open.web"])
        said = "\n".join(self.problems())
        for label in ("local.a-bad.x", "local.a-bad.y", "local.a-bad.z", "local.b-bad.x", "local.b-bad.e"):
            self.assertIn(label, said)

    def test_one_that_fails_to_apply_is_said_and_the_rest_recorded(self):
        self.project("a", {"name": "web", "run": "bin/serve.sh"})
        self.project("b", {"name": "web", "run": "bin/serve.sh"})
        real = self.rs.bootstrap

        def bootstrap(uid, label, path):
            if label == "local.a.web":
                raise OSError("no room")
            return real(uid, label, path)
        with mock.patch.object(self.rs, "bootstrap", bootstrap):
            self.apply()
        self.assertTrue(self.unit("local.b.web").exists())
        self.assertEqual(sorted(json.loads(self.rs.units_record().read_text())), ["local.a.web", "local.b.web"])

    def test_an_unreadable_unit_is_not_ours_unless_recorded(self):
        self.project("a", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.project("b", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.unit("local.a.web").write_bytes(b"not a plist")
        self.assertEqual(self.apply(), [("add", "local.b.web")])
        self.assertEqual(self.unit("local.a.web").read_bytes(), b"not a plist")
        self.assertIn("local.a.web.plist", "\n".join(self.problems()))
        data = b"\x00broken"
        self.unit("local.b.web").write_bytes(data)
        self.record(**{"local.b.web": {"project": "b", "files": {"local.b.web.plist": [hashlib.sha256(data).hexdigest()]}}})
        self.assertEqual(self.apply(), [("update", "local.b.web")])
        self.assertEqual(self.plist("local.b.web")["Label"], "local.b.web")


class Migration(World):
    """r2 section 5 and S7; r3 2, 3 and 4."""

    def test_an_installed_unit_is_replaced_whatever_the_project_does_to_its_command(self):
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh", "env": {"PYTHONPATH": "lib"}})
        (ws / "bin" / "serve.sh").chmod(0o644)
        for cli in (False, True):
            with self.subTest(behind_a_shell=cli):
                self.old_unit("local.box.web", "box", {"PYTHONPATH": "lib"}, cli=cli)
                self.list_rendered("local.box.web")
                self.assertEqual(self.apply(), [("update", "local.box.web")])
                self.assertNotIn("EnvironmentVariables", self.plist("local.box.web"))
                self.assertIn("local.box.web", self.booted_out())
                self.unit("local.box.web").unlink()
                self.rs.units_record().unlink()

    def test_a_unit_nobody_lists_is_found_by_what_it_runs(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.old_unit("local.box.old", "box", {"PYTHONPATH": "lib"})         # its service is gone from the entry
        self.old_unit("com.vendor.lookalike", "box", {"PYTHONPATH": "lib"})  # a label it chose once
        self.old_unit("local.gone.web", "gone", {"PYTHONPATH": "lib"})       # its project is gone
        keep = self.foreign("com.vendor.agent")
        self.assertEqual(sorted(self.apply()), [("add", "local.box.web"), ("remove", "com.vendor.lookalike"),
                                                ("remove", "local.box.old"), ("remove", "local.gone.web")])
        self.assertEqual(sorted(f.stem for f in self.agents.iterdir()), ["com.vendor.agent", "local.box.web"])
        self.assertEqual(self.unit("com.vendor.agent").read_bytes(), keep)
        self.assertEqual(sorted(self.loaded), ["com.vendor.agent", "local.box.web"])

    def test_one_that_cant_be_rendered_is_unloaded_and_removed(self):
        self.project("box", {"name": "my web", "run": "bin/serve.sh"}, {"name": "web", "run": 'bin/"'})
        for label in ("local.box.my web", "local.box.web"):
            self.old_unit(label, "box", {"PYTHONPATH": "lib"})
        self.assertEqual(sorted(self.apply()), [("remove", "local.box.web")])
        self.assertEqual([f.stem for f in self.agents.iterdir()], ["local.box.my web"], "no name: told, and nothing is done")
        self.assertIn("local.box.my web.plist", "\n".join(self.sent))
        self.assertEqual(len([p for p in self.problems() if "local.box" in p]), 3)

    def test_another_entry_on_its_label_doesnt_keep_it(self):
        self.project("box", {"name": "web", "run": 'bin/"'})
        self.project("open", {"name": "web", "run": "bin/serve.sh", "label": "local.box.web"}, sandbox=False)
        self.old_unit("local.box.web", "box", {"PYTHONPATH": "lib"})
        self.assertEqual(self.apply(), [("update", "local.box.web")])
        self.assertIn(str(self.tmp / "ws" / "open" / "bin" / "serve.sh"), self.plist("local.box.web")["ProgramArguments"])

    def test_no_sandbox_here_leaves_no_unit(self):
        self.project("box", {"name": "a", "run": "bin/serve.sh"}, {"name": "b", "run": "bin/serve.sh"})
        self.apply()
        self.old_unit("local.box.b", "box", {"PYTHONPATH": "lib"})
        with mock.patch.object(isolation, "can_sandbox", lambda: "no adapter here"):
            detail = {c.target: c.detail for c in self.rs.plan(self.index)}
            self.assertIn("box", detail["service:local.box.b"], "said as what it is, not as gone from the registry")
            self.assertEqual(self.apply(), [("remove", "local.box.a"), ("remove", "local.box.b")])
            self.assertIn("no adapter here", "\n".join(self.problems()))
        self.assertEqual(self.loaded, {})
        self.assertEqual(self.apply(), [("add", "local.box.a"), ("add", "local.box.b")], "and they are back where it can")

    def test_a_declared_label_is_dropped_once(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh", "label": "local.box-web"})
        self.old_unit("local.box-web", "box", cli=True)
        self.list_rendered("local.box-web")
        self.assertEqual(sorted(self.apply()), [("add", "local.box.web"), ("remove", "local.box-web")])
        self.assertEqual(sorted(f.stem for f in self.agents.iterdir()), ["local.box.web"])
        self.assertEqual(sorted(self.loaded), ["local.box.web"])
        self.assertEqual(len(self.sent), 1, "config apply says it too: an upgrade goes through it")
        self.assertIn("local.box-web", self.sent[0])
        self.assertEqual((self.problems(), len(self.sent)), ([], 1), "said once")

    def test_the_unit_it_had_unsandboxed_is_replaced_only_when_listed_and_the_same_command(self):
        svc = {"name": "web", "run": "bin/serve.sh", "env": {"A": "1"}, "label": "local.mine"}
        self.project("box", svc, sandbox=False)
        self.apply()
        self.rs.units_record().unlink(missing_ok=True)
        self.list_rendered("local.mine")
        self.project("box", svc)
        self.assertEqual(sorted(self.apply()), [("add", "local.box.web"), ("remove", "local.mine")])
        # another program at its label: not the platform's, listed or not
        for listed in (["local.box.web"], []):
            with self.subTest(listed=listed):
                self.rs.units_record().unlink(missing_ok=True)
                self.sent.clear()
                data = self.foreign("local.box.web")
                self.list_rendered(*listed)
                self.calls.clear()
                self.assertEqual(self.apply(), [])
                self.assertEqual(self.unit("local.box.web").read_bytes(), data)
                self.assertEqual(self.booted_out(), [])
                self.assertIn("local.box.web.plist", "\n".join(self.problems()))

    def test_an_unsandboxed_unit_is_taken_over_once_and_the_kept_ones_recorded(self):
        ws = self.project("open", {"name": "web", "run": "bin/serve.sh"}, {"name": "lost", "run": "bin/gone.sh"},
                          {"name": "same", "run": "bin/serve.sh"}, sandbox=False)
        self.index["modules"]["sandbox"] = {"name": "sandbox", "type": "module", "services": [
            {"name": "cap-runner", "label": "local.mod.cap", "run": "/bin/echo"}]}
        for label in ("local.open.web", "local.open.lost", "local.mod.cap"):
            self.old_unit(label, "open", args=["/bin/echo", "older"],
                          **({"WorkingDirectory": str(hubconf.ROOT)} if label == "local.mod.cap" else {}))
        same, _ = self.rs.render("open", self.index["projects"]["open"], {"name": "same", "run": "bin/serve.sh"})
        self.unit("local.open.same").write_bytes(plistlib.dumps(same, fmt=plistlib.FMT_BINARY))   # laid out otherwise
        self.loaded["local.open.same"] = str(self.unit("local.open.same"))
        self.list_rendered("local.open.web", "local.open.lost", "local.mod.cap")
        with mock.patch.object(self.rs, "busy", lambda p, s: s["name"] == "cap-runner"):
            self.assertEqual(self.apply(), [("update", "local.open.web")])
        rec = json.loads(self.rs.units_record().read_text())
        self.assertEqual(sorted(rec), ["local.mod.cap", "local.open.lost", "local.open.same", "local.open.web"])
        for label in rec:
            self.assertEqual(rec[label]["files"], {f"{label}.plist": [hashlib.sha256(self.unit(label).read_bytes()).hexdigest()]})
        # once: later changes are the platform's by the record, whoever else lists them
        self.assertEqual(self.apply(), [("update", "local.mod.cap")])
        (ws / "bin" / "gone.sh").write_text("#!/bin/sh\n")
        (ws / "bin" / "gone.sh").chmod(0o755)
        self.assertEqual(self.apply(), [("update", "local.open.lost")])

    def test_a_systemd_service_and_its_timer_are_recorded_and_found(self):
        units = self.tmp / "units"
        units.mkdir()
        calls = []
        with mock.patch.object(hubconf, "service_manager", lambda: "systemd"), \
                mock.patch.object(systemd_units, "UNIT_DIR", units), \
                mock.patch.object(systemd_units, "systemctl", quiet(calls)):
            self.project("box", {"name": "tick", "run": "bin/serve.sh", "interval_s": 60})
            old = systemd_units.files(self.old_plist("local.box-old", "box", {"PYTHONPATH": "lib"}, StartInterval=5))
            for n, text in {**old, "dbus.service": "[Service]\nExecStart=/usr/bin/dbus\n"}.items():
                (units / n).write_text(text)
            self.assertEqual(self.apply(), [("remove", "local.box-old"), ("add", "local.box.tick")])
            self.assertEqual(sorted(f.name for f in units.iterdir()),
                             ["dbus.service", "local.box.tick.service", "local.box.tick.timer"])
            rec = json.loads(self.rs.units_record().read_text())
            self.assertEqual(sorted(rec["local.box.tick"]["files"]), ["local.box.tick.service", "local.box.tick.timer"])
            (units / "local.box.tick.timer").write_text("[Timer]\nUnit=other.service\n")
            self.assertEqual(self.apply(), [], "a timer that isn't as written is not the platform's")
            (units / "local.box.tick.timer").write_text(next(
                x for n, x in systemd_units.files({"Label": "l", "ProgramArguments": ["x"], "StartInterval": 9}).items()
                if n.endswith(".timer")))
            self.assertEqual(self.apply(), [("update", "local.box.tick")], "one it wrote, beside its own service, is")


class Whose(World):
    """r1 test 4; r2 section 6; r3 7."""

    def test_a_file_that_isnt_the_platforms_is_untouched_at_apply_removal_and_uninstall(self):
        for sandbox in (True, False):
            with self.subTest(sandbox=sandbox):
                self.project("box", {"name": "web", "run": "bin/serve.sh", "label": "com.vendor.agent"}, sandbox=sandbox)
                data = self.foreign("com.vendor.agent")
                self.calls.clear()
                self.apply()
                self.assertEqual(self.unit("com.vendor.agent").read_bytes(), data)
                self.index["projects"]["box"]["services"] = []
                # a list an older version wrote, or that was made to name it, is no evidence when deleting
                self.rs.RENDERED.write_text(json.dumps(["com.vendor.agent"]))
                self.apply()
                self.assertEqual(self.unit("com.vendor.agent").read_bytes(), data)
                self.rs.RENDERED.write_text(json.dumps(["com.vendor.agent"]))
                self.assertNotIn("com.vendor.agent", self.rs.uninstall(apply=False))
                self.assertIn(str(self.unit("com.vendor.agent")), self.rs.left_behind())
                self.rs.uninstall(apply=True)
                self.assertEqual(self.unit("com.vendor.agent").read_bytes(), data)
                self.assertNotIn("com.vendor.agent", self.booted_out())
                self.assertIn("com.vendor.agent", self.loaded)

    def test_a_label_loaded_from_another_folder_is_left_to_it(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh", "label": "com.vendor.agent"}, sandbox=False)
        self.loaded["com.vendor.agent"] = "/Library/LaunchAgents/com.vendor.agent.plist"
        self.assertEqual(self.apply(), [])
        self.assertFalse(self.unit("com.vendor.agent").exists())
        self.assertEqual(self.booted_out(), [])

    def test_a_unit_at_a_link_is_not_followed(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        target = self.tmp / "theirs"
        target.write_text("theirs")
        self.unit("local.open.web").symlink_to(target)
        self.rs.RENDERED.write_text(json.dumps(["local.open.web"]))
        self.assertEqual(self.apply(), [])
        self.assertEqual(target.read_text(), "theirs")

    def test_uninstall_takes_out_the_platforms(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        self.old_unit("local.box.stray", "box")
        self.assertEqual(self.rs.uninstall(apply=True), ["local.box.stray", "local.box.web", "local.open.web"])
        self.assertEqual(list(self.agents.iterdir()), [])
        self.assertEqual(self.loaded, {})

    def test_adopt(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        data = self.foreign("local.open.web")
        self.apply()
        import owner_terminal

        def adopt(answer, refusal=None, tty=True, label="local.open.web"):
            with mock.patch.object(self.rs.registry, "refresh", lambda: self.index), \
                    mock.patch.object(owner_terminal, "refusal", lambda: refusal), \
                    mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: tty)), \
                    mock.patch("builtins.input", lambda *a: answer), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return self.rs.adopt(label)
        for kw in ({"answer": ""}, {"answer": "no"}, {"answer": "yes", "refusal": "session"}, {"answer": "yes", "tty": False},
                   {"answer": "yes", "label": "com.vendor.other"}):
            with self.subTest(**kw):
                self.assertEqual(adopt(**kw), 1)
                self.assertEqual(self.apply(), [])
                self.assertEqual(self.unit("local.open.web").read_bytes(), data)
        # one no service has: never taken over, so never deleted on its account
        other = self.foreign("com.vendor.other")
        self.assertEqual(adopt("yes", label="com.vendor.other"), 1)
        self.assertEqual(adopt("yes"), 0)
        self.assertEqual([f.read_bytes() for f in (self.state / "service-units-adopted").iterdir()], [data], "a copy stays")
        self.assertEqual(self.apply(), [("update", "local.open.web")])
        self.assertEqual(self.plist("local.open.web")["ProgramArguments"][-1], str(self.tmp / "ws/open/bin/serve.sh"))
        self.assertEqual(self.unit("com.vendor.other").read_bytes(), other)

    def test_two_entries_on_one_label(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.shared"}
        self.project("b", svc, sandbox=False)
        self.apply()
        self.project("a", {**svc, "label": "LOCAL.shared"}, sandbox=False)
        self.assertEqual(self.apply(), [], "the one whose unit it is keeps it")
        self.project("a", svc, sandbox=False)
        self.project("b", {**svc, "interval_s": 60}, sandbox=False)
        self.assertEqual(self.apply(), [("update", "local.shared")], "and is the one applied")
        self.project("a", {**svc, "label": "LOCAL.shared"}, sandbox=False)
        self.assertIn(str(self.tmp / "ws" / "b" / "bin" / "serve.sh"), self.plist("local.shared")["ProgramArguments"])
        self.assertIn("LOCAL.shared", "\n".join(self.problems()))
        self.rs.units_record().unlink()
        self.assertEqual(self.apply(), [], "installed and the same: still its own")
        self.project("a", svc, sandbox=False)
        self.rs.units_record().unlink()
        self.assertEqual(self.apply(), [])
        # neither has it: the first name
        self.unit("local.shared").unlink()
        self.loaded.clear()
        self.rs.units_record().unlink(missing_ok=True)
        self.rs.RENDERED.unlink()
        self.project("a", svc, sandbox=False)
        self.apply()
        self.assertIn(str(self.tmp / "ws" / "a" / "bin" / "serve.sh"), self.plist("local.shared")["ProgramArguments"])

    def test_a_core_label_is_never_a_projects(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.agent-hub-scheduler"}
        self.project("open", svc, sandbox=False)
        self.foreign("local.agent-hub-scheduler")
        self.rs.RENDERED.write_text(json.dumps(["local.agent-hub-scheduler"]))
        self.calls.clear()
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.booted_out(), [])


class Hostile(World):
    """What an older version may have written for a project, or been made to list (the second review: B2, B3, B4)."""

    def test_a_label_that_is_a_path_names_no_unit(self):
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh"})
        victim = self.foreign("com.vendor.agent")
        self.loaded["victim"] = "/Library/LaunchAgents/victim.plist"
        # a file the project writes, naming a vendor's label
        for where in (ws / "victim.plist", self.agents / "sub" / "x.plist"):
            where.parent.mkdir(exist_ok=True)
            where.write_bytes(plistlib.dumps(self.old_plist("com.vendor.agent", "box", {"PYTHONPATH": "lib"})))
        paths = [str(ws / "victim"), "../ws/box/victim", "sub/x", "../../x", "", "a\x00b"]
        for listed in (True, False):
            with self.subTest(listed=listed):
                if listed:
                    self.list_rendered(*paths)
                self.record(**{x: {"project": "box", "service": "web", "files": {
                    f"{x}.plist": [hashlib.sha256((ws / "victim.plist").read_bytes()).hexdigest()]}} for x in paths})
                self.project("box", {"name": "web", "run": "bin/serve.sh"}, *({"name": f"w{i}", "run": "x", "label": x}
                                                                               for i, x in enumerate(paths) if x))
                self.assertFalse([x for op, x in self.apply() if op == "remove"])
                for x in (ws / "victim.plist", self.agents / "sub" / "x.plist"):   # told, once, with a command to look
                    self.assertEqual(sum(f"ls -l -- {x}" in s for s in self.sent), 1 if listed else 0, self.sent)
                    self.assertIn(str(x), self.rs.left_behind())
                self.sent.clear()
                self.assertFalse([c for c in self.calls if "victim" in " ".join(c) or ".." in " ".join(c) or "sub/" in " ".join(c)])
                self.rs.uninstall(apply=True)
                self.assertTrue((ws / "victim.plist").exists() and (self.agents / "sub" / "x.plist").exists())
                self.assertEqual(self.unit("com.vendor.agent").read_bytes(), victim)
                self.assertEqual(self.loaded.get("victim"), "/Library/LaunchAgents/victim.plist")
                self.assertIn("com.vendor.agent", self.loaded)

    def test_a_unit_that_calls_itself_another_unloads_only_what_came_from_its_file(self):
        self.project("box")
        self.loaded["com.vendor.agent"] = "/Library/LaunchAgents/com.vendor.agent.plist"
        for label, inner in (("local.box.a", "com.vendor.agent"), ("local.box.b", "com.inner.one"), ("local.box.c", "../LaunchAgents/c")):
            self.unit(label).write_bytes(plistlib.dumps(self.old_plist(inner, "box")))
        self.loaded["com.inner.one"] = str(self.unit("local.box.b"))
        self.assertEqual(self.apply(), [("remove", "local.box.a"), ("remove", "local.box.b"), ("remove", "local.box.c")])
        self.assertEqual(self.loaded, {"com.vendor.agent": "/Library/LaunchAgents/com.vendor.agent.plist"})
        # one it calls itself that is no name is told, and launchd isn't asked about it
        self.assertFalse([c for c in self.calls if ".." in " ".join(c)])
        self.assertIn("gui/%d/../LaunchAgents/c" % os.getuid(), "\n".join(self.sent))

    def test_replacing_one_that_calls_itself_another_unloads_it_too(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        inner = "com.inner.one"
        self.unit("local.box.web").write_bytes(plistlib.dumps(self.old_plist(inner, "box")))
        self.loaded[inner] = str(self.unit("local.box.web"))
        self.assertEqual(self.apply(), [("update", "local.box.web")])
        self.assertEqual(sorted(self.loaded), ["local.box.web"])

    ODD = ("\uffff", "\ufdd0", "\u2028", "\x85", "\x7f", "\U0001ffff")

    def test_an_old_unit_this_platform_could_not_read_is_replaced_or_removed(self):
        for odd in self.ODD:
            for listed in (True, False):
                with self.subTest(odd=odd, listed=listed):
                    self.project("box", {"name": "web", "run": "bin/serve.sh"})
                    for label in ("local.box.web", "com.chosen.once"):
                        self.old_unit(label, "box", {"PYTHONPATH": f"lib{odd}"})
                    if listed:
                        self.list_rendered("local.box.web", "com.chosen.once")
                    self.assertEqual(self.apply(), [("remove", "com.chosen.once"), ("update", "local.box.web")])
                    self.assertEqual(sorted(self.loaded), ["local.box.web"])
                    self.assertNotIn("lib", self.unit("local.box.web").read_text())
                    self.rs.units_record().unlink()
                    self.rs.RENDERED.unlink()

    def test_the_same_on_systemd(self):
        units = self.tmp / "units"
        units.mkdir()
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        for i, odd in enumerate((*self.ODD, "\x0c", "\r", "\n", "\nExecStart=/bin/x\n")):
            unit = systemd_units.files(self.old_plist("local.box-old", "box", {"A": "ODD"}))["local.box-old.service"]
            (units / "local.box-old.service").write_bytes(unit.replace("ODD", f"a{odd}b").encode())
            unit = systemd_units.files(self.old_plist("local.box.web", "box"))["local.box.web.service"]
            (units / "local.box.web.service").write_bytes(unit.replace('"x"', f'"x{odd}y"').encode())
            with self.subTest(odd=odd), mock.patch.object(hubconf, "service_manager", lambda: "systemd"), \
                    mock.patch.object(systemd_units, "UNIT_DIR", units), \
                    mock.patch.object(systemd_units, "systemctl", quiet()):
                self.assertEqual(self.apply(), [("remove", "local.box-old"), ("update", "local.box.web")])
                self.assertEqual([f.name for f in units.iterdir()], ["local.box.web.service"])

    def test_a_truncated_one_under_a_sandboxed_projects_own_label(self):
        """a write that ended half way left it; the process may still run"""
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.project("open", sandbox=False)
        for label in ("local.box.web", "local.box.gone", "local.open.gone", "local.box.notlisted"):
            self.old_unit(label, "box")
            self.unit(label).write_bytes(self.unit(label).read_bytes()[:300])
        self.list_rendered("local.box.web", "local.box.gone", "local.open.gone")
        self.assertEqual(self.apply(), [("remove", "local.box.gone"), ("update", "local.box.web")])
        self.assertEqual(sorted(self.loaded), ["local.box.notlisted", "local.box.web", "local.open.gone"])

    def test_a_project_just_sandboxed_keeps_no_unsandboxed_unit(self):
        """whatever it makes of its entry at the same time"""
        plain = {"name": "web", "run": "bin/serve.sh", "env": {"A": "1"}}
        for what, services, gone in (
                ("run changed", [{**plain, "run": "bin/other.sh"}], []),
                ("its label taken by its other service", [{**plain, "label": "local.mine"}, {"name": "b", "run": "x", "label": "local.mine"}],
                 ["local.mine"]),
                ("its label changed", [{**plain, "label": "local.mine2"}], ["local.mine"]),
                ("the service dropped", [], ["local.box.web", "local.mine"])):
            with self.subTest(what):
                self.project("box", plain, {**plain, "name": "m", "label": "local.mine"}, sandbox=False)
                self.old_unit("local.box.web", "box", {"PATH": f"/x:{self.tmp / 'cli'}"}, args=["/bin/echo", "x"])
                self.old_unit("local.mine", "box", {"PATH": f"/x:{self.tmp / 'cli'}"}, args=["/bin/echo", "x"])
                self.list_rendered("local.box.web", "local.mine")
                self.project("box", *services)
                done = self.apply()
                for label in gone:
                    self.assertIn(("remove", label), done)
                self.assertNotIn("local.mine", self.loaded)
                for label in self.loaded:
                    self.assertEqual(self.plist(label)["ProgramArguments"][1:4], ["-I", "-B", SR])
                self.index["projects"] = {}
                self.apply()
                self.rs.units_record().unlink(missing_ok=True)

    def test_a_dropped_unsandboxed_service_an_older_version_rendered(self):
        self.project("open", sandbox=False)
        self.old_unit("local.open.gone", "open", args=["/bin/echo"])
        self.list_rendered("local.open.gone")
        self.assertEqual(self.apply(), [("remove", "local.open.gone")])
        self.assertEqual(self.loaded, {})

    def test_the_users_own_unit_is_never_taken_for_the_platforms(self):
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh", "label": "com.me.thing"})
        self.project("open", sandbox=False)
        own = self.old_plist("com.me.thing", "box")
        args = own["ProgramArguments"]
        hand = {"another key": {**own, "ProcessType": "Background"}, "not run at load": {**own, "RunAtLoad": False},
                "other options": {**own, "ProgramArguments": [*args[:2], "--verbose", "--", *args[2:]]},
                "its own shell": {**own, "ProgramArguments": ["/bin/sh", "-c", 'cd /x; exec "$@"', "sh", *args]},
                "another program": {**own, "ProgramArguments": ["/usr/bin/env", *args[1:]]},
                "in the project's folder": {**own, "ProgramArguments": [str(ws / "x")]},
                "logging to the platform's folder, from one it doesn't know": {
                    **own, "ProgramArguments": [str(ws / "x")], "WorkingDirectory": str(self.tmp / "elsewhere"),
                    **{f"Standard{k}Path": f"{self.tmp}/logs/com.me.thing-std{k[:3].lower()}.log" for k in ("Out", "Error")}},
                "called something else": {**own, "ProgramArguments": [str(ws / "x")], "Label": "com.me.other",
                                          "EnvironmentVariables": {"PATH": str(self.tmp / "cli")}}}
        for what, plist in hand.items():
            for text in (plistlib.dumps(plist), plistlib.dumps(plist).replace(b"\t", b"    "),
                         plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)):
                for label in ("com.me.thing", "local.open.mine"):
                    with self.subTest(what, label=label):
                        self.unit(label).write_bytes(text)
                        self.loaded[label] = str(self.unit(label))
                        if what != "in the project's folder" or label == "com.me.thing":
                            self.list_rendered(label)
                        self.apply()
                        self.assertEqual(self.unit(label).read_bytes(), text)
                        self.assertIn(label, self.loaded)
                        self.unit(label).unlink()
        # reformatted by hand: no longer as the platform writes it
        text = plistlib.dumps(own).replace(b"\t", b"    ")
        self.unit("com.me.thing").write_bytes(text)
        self.apply()
        self.assertEqual(self.unit("com.me.thing").read_bytes(), text)

    def test_a_list_with_anything_in_it_is_read(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        for text in ('[["x"], {"a": 1}, null, "local.open.web"]', '{"local.open.web": 1}', "5", "not json"):
            with self.subTest(text):
                self.old_unit("local.open.web", "open", args=["/bin/echo", "older"])
                self.rs.RENDERED.write_text(text)
                self.rs.units_record().unlink(missing_ok=True)
                self.assertEqual(self.apply(), [("update", "local.open.web")] if text[0] == "[" else [])

    def test_the_list_counts_only_as_an_older_version_wrote_it(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        self.old_unit("local.open.web", "open", args=["/bin/echo", "changed by hand"])
        self.assertEqual(self.apply(), [], "its own list is no evidence")
        self.list_rendered("local.open.web")   # an older version ran again (brunna upgrade --to)
        with mock.patch.object(self.rs, "place", mock.Mock(side_effect=OSError("no room"))):
            self.apply()
        self.assertEqual(self.apply(), [("update", "local.open.web")])

    def test_what_an_older_version_lists_that_isnt_the_platforms_is_told_once(self):
        self.project("open", sandbox=False)
        data = self.foreign("com.vendor.agent")
        self.list_rendered("com.vendor.agent", "local.open.nofile")
        self.assertEqual(self.apply(), [])
        self.assertEqual(len(self.sent), 1, "config apply, which an upgrade runs, says it")
        self.assertIn(str(self.unit("com.vendor.agent")), self.sent[0])
        self.apply()
        self.assertEqual((len(self.sent), self.unit("com.vendor.agent").read_bytes()), (1, data))

    def test_one_changed_by_hand_whose_service_is_gone_is_told_once(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        data = self.foreign("local.open.web")
        self.project("open", sandbox=False)
        for _run in range(3):
            self.assertEqual(self.apply(), [])
        self.assertEqual((len(self.sent), self.unit("local.open.web").read_bytes()), (1, data))


class Writes(World):
    """A write that fails or ends half way (the second review: B5 and its should-fix)."""

    def systemd(self):
        units = self.tmp / "units"
        units.mkdir(exist_ok=True)
        self.sysctl = []
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(hubconf, "service_manager", lambda: "systemd"))
        stack.enter_context(mock.patch.object(systemd_units, "UNIT_DIR", units))
        stack.enter_context(mock.patch.object(systemd_units, "systemctl", lambda *a: self.sysctl.append(a) or quiet()(*a)))
        return stack

    def both(self):
        yield "launchd", contextlib.nullcontext(), self.agents
        yield "systemd", self.systemd(), self.tmp / "units"

    def restarted(self):
        return [c for c in self.calls if c[0] in ("bootout", "bootstrap")] + \
            [c for c in getattr(self, "sysctl", []) if c[0] in ("enable", "restart")]

    def test_no_room_leaves_the_unit_that_runs_and_the_next_run_recovers(self):
        for what, manager, folder in self.both():
            with self.subTest(what), manager:
                self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 60}, sandbox=False)
                self.apply()
                before = {f.name: f.read_bytes() for f in folder.iterdir()}
                self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 90}, sandbox=False)
                self.calls.clear()
                real = os.fsync
                for where in ("fsync", "record"):
                    if where == "fsync":
                        broken = mock.patch.object(os, "fsync", mock.Mock(side_effect=OSError(28, "No space left on device")))
                    else:
                        broken = mock.patch.object(self.rs, "record", mock.Mock(side_effect=OSError(28, "no room")))
                    self.sysctl = []
                    with broken:
                        self.apply()
                    self.assertEqual({f.name: f.read_bytes() for f in folder.iterdir()}, before, where)
                    self.assertEqual(self.restarted(), [], where)
                self.assertIs(os.fsync, real)
                self.assertEqual(self.apply(), [("update", "local.open.web")])
                self.assertNotEqual({f.name: f.read_bytes() for f in folder.iterdir()}, before)
                self.assertTrue(self.restarted())
                self.assertEqual(self.apply(), [])
                self.index["projects"] = {}
                self.apply()

    def test_a_run_that_ends_between_writing_and_loading_is_taken_up(self):
        for what, manager, folder in self.both():
            with self.subTest(what), manager:
                self.project("box", {"name": "web", "run": "bin/serve.sh", "interval_s": 60})
                self.apply()
                self.project("box", {"name": "web", "run": "bin/serve.sh", "interval_s": 90})
                with mock.patch.object(self.rs, "load", mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
                    self.apply()
                self.calls.clear()
                self.sysctl = []
                self.assertEqual(self.apply(), [("update", "local.box.web")], "in place, and not loaded yet")
                self.assertTrue(self.restarted())
                self.assertEqual(self.apply(), [])
                # and when the entry has moved on again, unsandboxed: still the platform's, by either hash
                self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 60}, sandbox=False)
                self.apply()
                for interval, stop in ((90, "replace"), (120, "load")):
                    self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": interval}, sandbox=False)
                    target = os if stop == "replace" else self.rs
                    with mock.patch.object(target, stop, mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
                        real = self.rs.hubconf.write_atomic
                        with mock.patch.object(self.rs.hubconf, "write_atomic",
                                               lambda p, text: pathlib.Path(p).write_text(text)):
                            self.apply()
                    self.assertTrue(real)
                self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 150}, sandbox=False)
                self.assertEqual(self.apply(), [("update", "local.open.web")])
                self.assertEqual(self.apply(), [])
                self.index["projects"] = {}
                self.apply()

    def test_it_is_loaded_even_if_the_record_fails_then(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        real, n = self.rs.record, []

        def record(label, entry):
            n.append(label)
            if len(n) == 2:
                raise OSError("no room")
            real(label, entry)
        with mock.patch.object(self.rs, "record", record):
            self.apply()
        self.assertIn("local.open.web", self.loaded)
        self.assertEqual(self.apply(), [("update", "local.open.web")], "loaded again, and recorded")
        self.assertEqual(self.apply(), [])

    def test_one_label_that_cant_be_recorded_at_first_stops_no_other(self):
        self.project("open", {"name": "a", "run": "bin/serve.sh"}, {"name": "b", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        self.rs.units_record().unlink()
        real = self.rs.record

        def record(label, entry):
            if label == "local.open.a":
                raise OSError("no room")
            real(label, entry)
        with mock.patch.object(self.rs, "record", record):
            self.assertEqual(self.apply(), [])
        self.assertEqual(list(json.loads(self.rs.units_record().read_text())), ["local.open.b"])

    def test_what_an_older_version_listed_is_known_before_anything_is_changed(self):
        """a first run whose steps fail leaves each unit known as the platform's"""
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        for label in ("local.open.web", "local.open.gone"):
            self.old_unit(label, "open", args=["/bin/echo", "older"])
        self.list_rendered("local.open.web", "local.open.gone")
        with mock.patch.object(self.rs, "place", mock.Mock(side_effect=OSError("no room"))), \
                mock.patch.object(self.rs, "remove", mock.Mock(side_effect=OSError("no room"))):
            self.apply()
        self.assertEqual(self.apply(), [("remove", "local.open.gone"), ("update", "local.open.web")])

    def test_the_older_list_stays_until_every_unit_is_known(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.old_unit("local.open.web", "open", args=["/bin/echo", "older"])
        self.list_rendered("local.open.web")
        with mock.patch.object(self.rs, "record", mock.Mock(side_effect=OSError("no room"))):
            self.apply()
        self.assertEqual(self.apply(), [("update", "local.open.web")])

    def test_a_manager_that_doesnt_say_where_from(self):
        """its own label is unloaded as before; another one the file calls itself isn't"""
        real = self.rs.launchctl

        def launchctl(*args):
            r = real(*args)
            r.stdout = "\n".join(x for x in r.stdout.splitlines() if "path" not in x)
            return r
        self.project("box")
        self.old_unit("local.box.a", "box")
        self.unit("local.box.b").write_bytes(plistlib.dumps(self.old_plist("com.vendor.agent", "box")))
        self.loaded["com.vendor.agent"] = "x"
        with mock.patch.object(self.rs, "launchctl", launchctl):
            self.assertIn("com.vendor.agent", "\n".join(self.problems()))
        self.assertEqual(list(self.loaded), ["com.vendor.agent"])
        self.assertEqual([f.stem for f in self.agents.iterdir()], ["local.box.b"], "kept: whose that label is it doesn't say")

    def test_one_that_doesnt_say_where_from_after_a_failed_bootout_is_still_loaded(self):
        real, refused = self.rs.launchctl, []

        def launchctl(*args):
            if args[0] == "bootout":
                refused.append(args)
                return subprocess.CompletedProcess(args, 5, "", "Input/output error")
            r = real(*args)
            if refused:
                r.stdout = "\n".join(x for x in r.stdout.splitlines() if "path" not in x)
            return r
        self.project("box")
        self.old_unit("local.box.a", "box")
        with mock.patch.object(self.rs, "launchctl", launchctl):
            self.assertIn("local.box.a", "\n".join(self.problems()))
        self.assertTrue(self.unit("local.box.a").exists() and "local.box.a" in self.rs.recorded())

    def test_one_loaded_whose_plist_is_gone_is_unloaded(self):
        self.project("open", sandbox=False)
        self.loaded.update({"local.open.gone": str(self.unit("local.open.gone")), "com.vendor.agent": "/Library/LaunchAgents/com.vendor.agent.plist"})
        self.list_rendered("local.open.gone", "local.open.never", "com.vendor.agent")
        self.assertEqual(self.apply(), [("remove", "local.open.gone")])
        self.assertEqual(list(self.loaded), ["com.vendor.agent"])
        # at uninstall too: one only listed, and one recorded
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        self.unit("local.open.web").unlink()
        self.loaded["local.open.gone"] = str(self.unit("local.open.gone"))
        self.list_rendered("local.open.gone", "com.vendor.agent")
        self.refuses = ("bootout",)
        self.assertEqual(self.rs.uninstall(apply=True), [])
        self.refuses = ()
        self.assertEqual(self.rs.uninstall(apply=True), ["local.open.gone", "local.open.web"])
        self.assertEqual(list(self.loaded), ["com.vendor.agent"])

    def test_a_label_that_is_no_name_in_a_file_is_told_once(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.unit("local.box.web").write_bytes(plistlib.dumps(self.old_plist("../x/y", "box")))
        for _run in range(3):
            with mock.patch.object(self.rs, "record", mock.Mock(side_effect=OSError("no room"))):
                self.apply()
        self.apply()
        self.assertEqual(sum("../x/y" in x for x in self.sent), 1)

    def test_the_record_is_changed_under_a_lock(self):
        import fcntl
        import threading
        self.rs.record("local.x.a", {"files": {}})
        with open(self.rs.units_record().with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            t = threading.Thread(target=self.rs.record, args=("local.x.b", {"files": {}}))
            t.start()
            t.join(0.3)
            self.assertNotIn("local.x.b", self.rs.recorded())
            fcntl.flock(held, fcntl.LOCK_UN)
            t.join(30)
        self.assertEqual(sorted(self.rs.recorded()), ["local.x.a", "local.x.b"])

    def test_the_old_label_goes_only_when_the_new_one_is_in_place(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.old"}
        self.project("open", svc, sandbox=False)
        self.apply()
        self.project("open", {**svc, "label": "local.new"}, sandbox=False)
        data = self.foreign("local.new")
        self.assertEqual(self.apply(), [], "the new one's place is another's")
        self.assertIn("local.old", self.loaded)
        self.assertIn("local.new.plist", "\n".join(self.problems()))
        self.assertEqual(self.unit("local.new").read_bytes(), data)
        self.unit("local.new").unlink()
        self.loaded.pop("local.new")
        with mock.patch.object(os, "fsync", mock.Mock(side_effect=OSError(28, "no room"))):
            self.apply()
        self.assertEqual(sorted(self.loaded), ["local.old"], "nor when it can't be written")
        self.assertEqual(self.apply(), [("remove", "local.old"), ("add", "local.new")])
        self.assertEqual(sorted(self.loaded), ["local.new"])

    def test_a_sandboxed_projects_old_unit_goes_whatever_becomes_of_the_new_one(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh", "label": "local.box-web"})
        self.old_unit("local.box-web", "box", cli=True)
        self.foreign("local.box.web")
        said = "\n".join(self.problems())
        self.assertEqual(sorted(self.loaded), ["local.box.web"])
        self.assertIn("local.box-web", said)
        self.assertIn("local.box-web", "\n".join(self.sent))

    def test_nor_does_the_unit_it_had_before_it_was_sandboxed_wait(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.old", "cwd": "hub"}
        for cwd in ("hub", "workspace"):
            with self.subTest(cwd):
                self.project("box", {**svc, "cwd": cwd, "run": "/bin/echo"}, sandbox=False)
                self.apply()
                self.project("box", {**svc, "label": "local.other"})
                self.foreign("local.box.web")
                self.assertEqual(self.apply(), [("remove", "local.old")])
                self.assertEqual(sorted(self.loaded), ["local.box.web"])
                self.unit("local.box.web").unlink()
                self.loaded.clear()
        # nor one only an older version lists
        self.rs.units_record().unlink()
        self.project("box", svc)
        self.old_unit("local.old", "box", args=["/bin/echo"], WorkingDirectory=str(hubconf.ROOT))
        self.unit("local.old").write_bytes(plistlib.dumps({**self.plist("local.old"), "StandardOutPath": f"{self.tmp}/logs/old-stdout.log",
                                                           "StandardErrorPath": f"{self.tmp}/logs/old-stderr.log"}))
        self.list_rendered("local.old")
        self.index["modules"]["m"] = {"name": "m", "type": "module", "services": []}
        self.foreign("local.box.web")
        self.assertEqual(self.apply(), [("remove", "local.old")])

    def test_a_label_the_manager_has_from_another_file_is_neither_unloaded_nor_replaced(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        self.loaded["local.open.web"] = "/Library/LaunchAgents/local.open.web.plist"
        self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 60}, sandbox=False)
        self.calls.clear()
        self.assertEqual(self.apply(), [])
        self.assertIn("/Library/LaunchAgents", "\n".join(self.problems()))
        self.project("open", sandbox=False)
        self.assertEqual(self.apply(), [("remove", "local.open.web")])
        self.assertEqual(self.booted_out(), [])

    def test_a_label_that_differs_only_in_case(self):
        self.project("box", {"name": "Web", "run": "bin/serve.sh"})
        self.apply()
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.assertEqual(self.apply(), [("remove", "local.box.Web"), ("add", "local.box.web")])
        self.assertEqual([f.stem for f in self.agents.iterdir()], ["local.box.web"])
        self.assertEqual(list(self.loaded), ["local.box.web"])
        # another's file, spelled so: the volume may fold the two into one
        self.index["projects"] = {}
        self.apply()
        data = self.foreign("local.box.WEB")
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.assertEqual(self.apply(), [])
        self.assertEqual([f.name for f in self.agents.iterdir()], ["local.box.WEB.plist"])
        self.assertEqual(self.unit("local.box.WEB").read_bytes(), data)


class Systemd:
    """A systemd user manager over a folder: what is enabled (links), what runs, and what it was told."""

    def __init__(self, units):
        self.units, self.running, self.calls, self.refuses, self.silent = units, {}, [], set(), set()

    def start(self, unit):
        f = self.units / unit if "/" not in unit else pathlib.Path(unit)
        self.running[os.path.basename(unit)] = f.read_text()

    def __call__(self, *args):
        self.calls.append(args)
        op, name = args[0], args[-1]
        if op in self.silent or (op, name) in self.silent:
            return subprocess.CompletedProcess(args, 0, "", "")
        if op != "daemon-reload":
            assert args[-2] == "--" and not name.startswith("-"), args
        if op in self.refuses or (op, name) in self.refuses:
            return subprocess.CompletedProcess(args, 1, "", f"{op} refused")
        base = os.path.basename(name)
        if op == "enable":
            if "/" in name:
                (self.units / base).symlink_to(name)
            if "[Install]" in (self.units / base).read_text() and not (self.units / base).name.endswith(".timer"):
                d = self.units / "default.target.wants"
                d.mkdir(exist_ok=True)
                if not os.path.lexists(d / base):
                    (d / base).symlink_to(name if "/" in name else self.units / base)
            if base not in self.running:
                self.start(name)
        elif op == "disable":
            self.running.pop(base, None)
            for p in (self.units / "default.target.wants" / base, self.units / base):
                if os.path.islink(p):
                    os.unlink(p)
        elif op == "restart":
            self.running.pop(base, None)
            self.start(name)
        elif op == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n" if base in self.running else "inactive\n", "")
        elif op == "show" and "FragmentPath" in args:
            p = self.units / base
            path = os.path.join(self.units, os.readlink(p)) if p.is_symlink() else str(p) if p.exists() else ""
            return subprocess.CompletedProcess(args, 0, f"LoadState={'loaded' if path else 'not-found'}\nFragmentPath={path}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class Paths(World):
    """Labels that are no names, and links into a project's folder: told, and nothing more; what systemd goes on running, and
    its answers (the third and fourth reviews)."""

    def odd_world(self, units):
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.project("open", sandbox=False)
        for d in ("default.target.wants", "x.requires", "y.upholds"):
            (units / d).mkdir()
            (units / d / f"{d}.service").symlink_to(ws / f"{d}.service")
        (units / "evil.service").symlink_to(ws / "evil.service")
        (ws / "evil.service").write_text("[Service]\n")
        (units / "mine.service").symlink_to(self.tmp / "ws" / "open" / "mine.service")   # the user's own: nobody's business
        (units / "-.service").write_text("[Service]\n")
        for label in (".", "..", "a b"):
            (units / f"{label}.service").write_text(systemd_units.files(self.old_plist(label, "box"))[f"{label}.service"])
        self.list_rendered(str(ws / "evil"), "--now", "a b")
        return ws

    def test_nothing_is_called_for_what_is_no_name_or_a_link_on_systemd(self):
        units = self.on_systemd()
        ws = self.odd_world(units)
        before = {p: p.read_bytes() if not p.is_symlink() else os.readlink(p) for p in units.rglob("*") if p.is_symlink() or p.is_file()}
        self.assertEqual(self.apply(), [("add", "local.box.web")])
        self.assertEqual({p: p.read_bytes() if not p.is_symlink() else os.readlink(p) for p in before}, before)
        self.assertEqual({c[-1] for c in self.sysd.calls} - {"daemon-reload"}, {"local.box.web.service"})
        told = "\n".join(self.sent)
        for x in ("evil.service", "default.target.wants/default.target.wants.service", "x.requires/", "y.upholds/", "..service", "...service",
                  "a b.service"):
            self.assertIn(x, told)
        self.assertNotIn("mine.service", told)
        self.assertIn(str(units / "evil.service"), self.rs.left_behind())
        # once, until it is gone; and said in every run's problems
        n = len(self.sent)
        self.apply()
        self.assertEqual(len(self.sent), n)
        self.assertIn("evil.service", "\n".join(self.problems()))
        (units / "evil.service").unlink()
        (ws / "evil.service").unlink()
        self.apply()
        (units / "evil.service").symlink_to(ws / "evil.service")
        self.apply()
        self.assertEqual(len(self.sent), n + 1, "and again when it is back")
        self.assertEqual(self.rs.uninstall(apply=True), ["local.box.web"])
        self.assertTrue((units / "evil.service").is_symlink() and (units / "a b.service").exists())

    def test_nothing_is_called_for_what_is_no_name_or_a_link_on_launchd(self):
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.unit("com.me").symlink_to(ws / "x.plist")
        for label in (".", "a b"):
            self.old_unit(label, "box")
        self.list_rendered(str(ws / "evil"), "a b")
        self.loaded[str(ws / "evil")] = str(ws / "evil.plist")
        was = dict(self.loaded)
        self.assertEqual(self.apply(), [("add", "local.box.web")])
        self.assertEqual({k: v for k, v in self.loaded.items() if k in was}, was)
        self.assertEqual({c[1].split("/", 2)[-1] for c in self.calls if c[0] != "bootstrap"}, {"local.box.web"})
        told = "\n".join(self.sent)
        for x in (str(self.unit("com.me")), str(self.unit("a b")), "launchctl print"):
            self.assertIn(x, told)
        self.assertTrue(self.unit("com.me").is_symlink())

    def test_a_regular_file_beside_such_a_link_is_refused_as_before(self):
        units = self.on_systemd()
        ws = self.project("box", {"name": "web", "run": "bin/serve.sh"})
        (units / "local.box.web.service").symlink_to(ws / "x.service")
        theirs = units / "local.box.web.timer"
        theirs.write_text("[Timer]\nOnActiveSec=1\n")
        self.assertEqual(self.apply(), [])
        self.assertEqual((theirs.read_text(), (units / "local.box.web.service").is_symlink()), ("[Timer]\nOnActiveSec=1\n", True))
        self.assertIn("--adopt local.box.web", "\n".join(self.problems()))
        self.assertEqual([c for c in self.sysd.calls if c[0] not in ("show", "is-active")], [])

    def test_what_is_told_is_bounded(self):
        units = self.on_systemd()
        ws = self.project("box")
        for i in range(50):
            (units / f"l{i}.service").symlink_to(ws / ("x" * 100) / f"{i}")
        self.apply()
        self.assertEqual(len(self.sent), self.rs.ODD_AT_MOST + 1)
        self.assertTrue(all(len(x) < 2000 for x in self.sent))
        self.apply()
        self.assertEqual(len(self.sent), self.rs.ODD_AT_MOST + 1)

    # --- every answer of the manager

    def test_an_answer_that_isnt_one_keeps_everything_on_systemd(self):
        units = self.on_systemd()
        svc = {"name": "web", "run": "bin/serve.sh", "keepalive": True}
        for op in ("is-active", "show", "disable", "daemon-reload", "kill"):
            for how in ("silent", "refuses"):
                if how == "silent" and op not in ("is-active", "show"):
                    continue   # these say nothing when they did it
                with self.subTest(op=op, how=how):
                    self.project("box", svc)
                    self.hostile(units, "local.box.web", KeepAlive=True) if op == "kill" else self.apply()
                    self.project("box")
                    setattr(self.sysd, how, {op})
                    if op == "show":
                        (units / "local.box.web.service").unlink()
                    said = "\n".join(self.problems())
                    self.assertIn("local.box.web", said)
                    self.assertIn("local.box.web", self.rs.recorded())
                    if op != "show":
                        self.assertTrue((units / "local.box.web.service").exists())
                    self.assertEqual(self.rs.uninstall(apply=True), [])
                    setattr(self.sysd, how, set())
                    self.assertEqual(self.apply(), [("remove", "local.box.web")])
                    self.assertEqual((self.sysd.running, list(units.glob("local*"))), ({}, []))
        self.project("box", svc)
        self.sysd.silent = {"show"}
        self.assertEqual(self.apply(), [], "nothing is put where systemd doesn't say what it has")
        self.assertIn("local.box.web", "\n".join(self.problems()))

    def test_an_answer_that_isnt_one_keeps_everything_on_launchd(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.apply()
        real = self.rs.launchctl
        for i, answer in enumerate(((1, "", "Could not connect"), (0, "", ""), (0, "something else\n", ""), (64, "x", ""))):
            with self.subTest(answer=answer):
                self.project("open", sandbox=False)
                with mock.patch.object(self.rs, "launchctl", lambda *a: subprocess.CompletedProcess(a, *answer) if a[0] == "print" else real(*a)):
                    said = "\n".join(self.problems())
                    self.assertEqual(self.rs.uninstall(apply=True), [])
                self.assertIn("local.open.web", said)
                self.assertTrue(self.unit("local.open.web").exists() and "local.open.web" in self.loaded and "local.open.web" in self.rs.recorded())
                self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": i + 5}, sandbox=False)
                with mock.patch.object(self.rs, "launchctl", lambda *a: subprocess.CompletedProcess(a, *answer) if a[0] == "print" else real(*a)):
                    self.assertIn("local.open.web", "\n".join(self.problems()))
                self.assertEqual(self.apply(), [("update", "local.open.web")])

    def test_what_is_taken_out_says_nothing_of_its_own_by_then(self):
        """an Install section of a unit an older version wrote names other units: disable never reads it"""
        units = self.on_systemd()
        read = []
        real = self.sysd

        def systemctl(*args):
            if args[0] == "disable":
                f = units / args[-1]
                read.append((args[-1], f.read_text(), ("daemon-reload",) in real.calls))
            return real(*args)
        with mock.patch.object(systemd_units, "systemctl", systemctl):
            for services, done in (([], "remove"), ([{"name": "web", "run": "bin/serve.sh", "keepalive": True}], "update"),
                                   ([{"name": "web", "run": "bin/serve.sh", "interval_s": 60}], "update")):
                with self.subTest(done=done, n=len(services)):
                    self.hostile(units, "local.box.web", KeepAlive=True)
                    f = units / "local.box.web.service"
                    f.write_text(f.read_text() + "[Install]\nAlso=dbus.service theirs.service\nAlias=theirs.service\n")
                    read.clear()
                    self.project("box", *services)
                    self.assertEqual(self.apply(), [(done, "local.box.web")])
                    self.assertTrue(read)
                    for _name, text, reloaded in read:
                        self.assertNotIn("Also=", text)
                        self.assertNotIn("Alias=", text)
                        self.assertTrue(reloaded)
                    self.project("box")
                    self.apply()
                    self.rs.record("local.box.web", None)

    def on_systemd(self):
        units = self.tmp / "units"
        units.mkdir(exist_ok=True)
        self.sysd = Systemd(units)
        for target, name, value in ((hubconf, "service_manager", lambda: "systemd"), (systemd_units, "UNIT_DIR", units),
                                    (systemd_units, "systemctl", self.sysd)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        return units

    def base_enabled(self, units, label, text="[Service]\nExecStart=/ws/evil\n[Install]\nWantedBy=default.target\n"):
        """What the base's `enable --now` made of a label that is a path"""
        f = pathlib.Path(f"{label}.service")
        f.write_text(text)
        self.sysd("enable", "--now", "--", str(f))
        return f

    # --- what systemd goes on running

    def hostile(self, units, label, **kw):
        unit = systemd_units.files(self.old_plist(label, "box", {"PYTHONPATH": "lib"}, **kw))
        for n, text in unit.items():
            (units / n).write_text(text)
        self.sysd("enable", "--now", "--", systemd_units.target(self.old_plist(label, "box", **kw)))
        if f"{label}.timer" in unit:
            self.sysd.start(f"{label}.service")
        self.sysd.calls.clear()

    def test_a_service_replaced_by_an_interval_unit_is_stopped(self):
        for was in ({"KeepAlive": True}, {"StartInterval": 5}, "skeleton"):
            with self.subTest(was=was):
                units = self.on_systemd()
                if was == "skeleton":
                    self.project("box", {"name": "web", "run": "bin/serve.sh", "keepalive": True})
                    self.apply()
                else:
                    self.hostile(units, "local.box.web", **was)
                self.sysd.calls.clear()
                self.project("box", {"name": "web", "run": "bin/serve.sh", "interval_s": 60})
                self.assertEqual(self.apply(), [("update", "local.box.web")])
                self.assertEqual(list(self.sysd.running), ["local.box.web.timer"])
                self.assertFalse([p for p in units.rglob("default.target.wants/*")], "nor started at the next login")
                self.assertIn(("daemon-reload",), self.sysd.calls)
                if was != "skeleton":
                    self.assertIn(("kill", "-s", "KILL", "--", "local.box.web.service"), self.sysd.calls)
                shutil.rmtree(units)
                self.rs.units_record().unlink()

    def test_a_service_replaced_by_a_service_is_the_new_one(self):
        units = self.on_systemd()
        self.hostile(units, "local.box.web", KeepAlive=True)
        self.project("box", {"name": "web", "run": "bin/serve.sh", "keepalive": True})
        self.apply()
        self.assertNotIn("PYTHONPATH", self.sysd.running["local.box.web.service"])
        self.assertIn(("kill", "-s", "KILL", "--", "local.box.web.service"), self.sysd.calls)
        # an unsandboxed one's is restarted as before, read again first
        self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True}, sandbox=False)
        self.apply()
        self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True, "throttle_s": 7}, sandbox=False)
        self.sysd.calls.clear()
        self.apply()
        self.assertEqual([c for c in self.sysd.calls if c[0] in ("daemon-reload", "enable", "restart", "kill")],
                         [("daemon-reload",), ("enable", "--now", "--", "local.open.web.service"), ("restart", "--", "local.open.web.service")])
        self.assertIn("RestartSec=7", self.sysd.running["local.open.web.service"])

    def test_a_dropped_timer_is_disabled_before_its_file_goes(self):
        units = self.on_systemd()
        self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 60}, sandbox=False)
        self.apply()
        self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True}, sandbox=False)
        self.sysd.refuses = {("disable", "local.open.web.timer")}
        self.apply()
        self.assertTrue((units / "local.open.web.timer").exists(), "the manager still has it")
        self.sysd.refuses = set()
        self.assertEqual(self.apply(), [("update", "local.open.web")])
        self.assertEqual(list(self.sysd.running), ["local.open.web.service"])
        self.assertEqual([f.name for f in units.glob("local*")], ["local.open.web.service"])
        self.assertEqual(self.apply(), [])

    def test_a_run_that_ends_in_between_still_stops_what_ran(self):
        units = self.on_systemd()
        self.hostile(units, "local.box.web", StartInterval=5)
        for interval in (None, None, 60):
            self.project("box", {"name": "web", "run": "bin/serve.sh", **({"interval_s": interval} if interval else {"keepalive": True})})
            if interval:
                self.apply()
            else:
                with mock.patch.object(self.rs, "load", mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
                    self.apply()
        self.assertIn(("kill", "-s", "KILL", "--", "local.box.web.service"), self.sysd.calls)
        self.assertEqual(list(self.sysd.running), ["local.box.web.timer"])
        self.assertEqual(sorted(f.name for f in units.glob("local*")), ["local.box.web.service", "local.box.web.timer"])
        # a timer dropped in the run that ended goes in the next one too
        self.project("box", {"name": "web", "run": "bin/serve.sh", "keepalive": True})
        with mock.patch.object(self.rs, "load", mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
            self.apply()
        (units / "local.box.web.timer").unlink()   # by hand, and the manager still has it
        self.apply()
        self.assertEqual(list(self.sysd.running), ["local.box.web.service"])

    def test_a_skeleton_is_stopped_as_any_service_is(self):
        units = self.on_systemd()
        self.project("box", {"name": "web", "run": "bin/serve.sh", "keepalive": True})
        self.apply()
        self.project("box", {"name": "web", "run": "bin/serve.sh", "keepalive": True, "throttle_s": 9})
        self.sysd.calls.clear()
        self.apply()
        self.project("box")
        self.apply()
        self.assertNotIn("kill", [c[0] for c in self.sysd.calls])
        self.assertEqual((self.sysd.running, list(units.glob("local*"))), ({}, []))

    def test_a_removal_ends_all_of_a_hostile_one_the_timer_first(self):
        units = self.on_systemd()
        self.project("box")
        self.hostile(units, "local.box.web", StartInterval=5)
        self.apply()
        stops = [c for c in self.sysd.calls if c[0] in ("disable", "kill")]
        self.assertEqual(stops, [("disable", "--now", "--", "local.box.web.timer"), ("kill", "-s", "KILL", "--", "local.box.web.service"),
                                 ("disable", "--now", "--", "local.box.web.service")])
        self.assertEqual(self.sysd.calls[-1], ("daemon-reload",), "read again once the files are gone")

    def test_one_whose_file_is_gone_is_stopped_before_another_takes_its_label(self):
        units = self.on_systemd()
        for confined in (True, False):
            with self.subTest(confined=confined):
                svc = {"name": "web", "run": "bin/serve.sh"}
                self.project("box", {**svc, "keepalive": True}, sandbox=confined)
                self.apply()
                (units / "local.box.web.service").unlink()
                self.project("box", {**svc, "interval_s": 60}, sandbox=confined)
                self.sysd.calls.clear()
                self.assertEqual([x for _op, x in self.apply()], ["local.box.web"])
                self.assertEqual(list(self.sysd.running), ["local.box.web.timer"])
                self.assertEqual(("kill", "-s", "KILL", "--", "local.box.web.service") in self.sysd.calls, confined)
                self.project("box")
                self.apply()

    def test_one_an_older_version_lists_whose_file_is_gone_is_stopped(self):
        units = self.on_systemd()
        self.project("open", sandbox=False)
        self.sysd.running["local.open.gone.service"] = "x"
        self.list_rendered("local.open.gone", "local.open.never")
        self.assertEqual(self.apply(), [("remove", "local.open.gone")])
        self.assertEqual((self.sysd.running, list(units.iterdir())), ({}, []))

    def test_one_whose_file_is_gone_is_stopped(self):
        units = self.on_systemd()
        self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True}, sandbox=False)
        self.apply()
        (units / "local.open.web.service").unlink()
        self.project("open", sandbox=False)
        self.assertEqual(self.apply(), [("remove", "local.open.web")])
        self.assertEqual((self.sysd.running, self.rs.recorded()), ({}, {}))

    def test_one_removal_that_cant_be_judged_stops_no_other(self):
        self.project("box")
        for label in ("local.box.a", "local.box.b"):
            self.old_unit(label, "box")
        real = self.rs._unwanted
        with mock.patch.object(self.rs, "_unwanted", lambda label, *a: real(label, *a) if label.endswith("b") else 1 / 0):
            self.assertEqual(self.apply(), [("remove", "local.box.b")])

    def test_what_isnt_confirmed_is_tried_again_on_systemd(self):
        units = self.on_systemd()
        self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True}, sandbox=False)
        for refused in ("daemon-reload", "enable", "restart"):
            with self.subTest(refused):
                self.project("open", {"name": "web", "run": "bin/serve.sh", "keepalive": True, "throttle_s": len(refused)}, sandbox=False)
                self.sysd.refuses = {refused}
                self.apply()
                self.sysd.refuses = set()
                self.assertEqual([x for _op, x in self.apply()], ["local.open.web"])
                self.assertEqual(self.apply(), [])
        self.project("open", sandbox=False)
        self.sysd.refuses = {("disable", "local.open.web.service")}
        self.apply()
        self.assertTrue((units / "local.open.web.service").exists())
        self.assertIn("local.open.web", self.rs.recorded())
        self.assertEqual(self.rs.uninstall(apply=True), [])
        self.assertIn("local.open.web", self.rs.recorded())
        self.sysd.refuses = set()
        self.assertEqual(self.apply(), [("remove", "local.open.web")])
        self.assertEqual((self.sysd.running, self.rs.recorded()), ({}, {}))

    def test_what_isnt_confirmed_is_tried_again_on_launchd(self):
        self.project("open", {"name": "web", "run": "bin/serve.sh"}, sandbox=False)
        self.refuses = ("bootstrap",)
        self.apply()
        self.refuses = ()
        self.assertEqual(self.apply(), [("update", "local.open.web")])
        self.project("open", {"name": "web", "run": "bin/serve.sh", "interval_s": 60}, sandbox=False)
        self.refuses = ("bootout",)
        self.apply()
        self.assertIn("loading", self.rs.recorded()["local.open.web"])
        self.refuses = ()
        self.assertEqual(self.apply(), [("update", "local.open.web")])
        self.assertEqual(self.apply(), [])
        self.project("open", sandbox=False)
        self.refuses = ("bootout",)
        self.apply()
        self.assertTrue(self.unit("local.open.web").exists() and "local.open.web" in self.rs.recorded())
        self.assertEqual(self.rs.uninstall(apply=True), [])
        self.assertTrue(self.rs.units_record().exists())
        self.refuses = ()
        self.assertEqual(self.rs.uninstall(apply=True), ["local.open.web"])
        self.assertEqual((self.loaded, self.unit("local.open.web").exists()), ({}, False))

    def test_every_label_a_replaced_file_had_is_unloaded_however_many_runs_it_takes(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        inner = "com.inner.one"
        self.unit("local.box.web").write_bytes(plistlib.dumps(self.old_plist(inner, "box")))
        self.loaded[inner] = str(self.unit("local.box.web"))
        for interval in (60, 90):
            self.project("box", {"name": "web", "run": "bin/serve.sh", "interval_s": interval})
            with mock.patch.object(self.rs, "load", mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
                self.apply()
        self.apply()
        self.assertEqual(list(self.loaded), ["local.box.web"])
        # and when the service is gone by then
        self.unit("local.box.web").write_bytes(plistlib.dumps(self.old_plist(inner, "box")))
        self.loaded[inner] = str(self.unit("local.box.web"))
        with mock.patch.object(self.rs, "load", mock.Mock(side_effect=SystemExit)), self.assertRaises(SystemExit):
            self.apply()
        self.project("box")
        self.apply()
        self.assertEqual(self.loaded, {})

    def test_a_lone_timer_is_the_platforms_only_when_listed(self):
        units = self.on_systemd()
        timer = systemd_units.files({"Label": "l", "ProgramArguments": ["x"], "StartInterval": 9})["l.timer"]
        self.project("open", sandbox=False)
        for label, service in (("local.open.a", None), ("local.open.b", "[Service]\nExecStart=/usr/bin/theirs\n")):
            (units / f"{label}.timer").write_text(timer)
            if service:
                (units / f"{label}.service").write_text(service)
        self.assertEqual(self.apply(), [])
        self.list_rendered("local.open.a", "local.open.b")
        self.assertEqual(self.apply(), [("remove", "local.open.a")])
        self.assertEqual(sorted(f.name for f in units.iterdir()), ["local.open.b.service", "local.open.b.timer"])


class Declared(World):
    """The third review's S2 to S4 and its notes."""

    def test_a_declared_label_touches_no_other_projects_unit(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.theirs"}
        self.project("open", svc, sandbox=False)
        self.apply()
        self.project("box", {"name": "evil", "run": "bin/serve.sh", "label": "local.theirs"})
        # open renames its own, and the new one's place is another's: the old one stays, whatever box declares
        self.project("open", {**svc, "label": "local.new"}, sandbox=False)
        self.foreign("local.new")
        self.assertEqual(self.apply(), [("add", "local.box.evil")])
        self.assertIn("local.theirs", self.loaded)
        self.unit("local.new").unlink()
        self.loaded.pop("local.new")
        self.sent.clear()
        detail = {c.target: c.detail for c in self.rs.plan(self.index)}["service:local.theirs"]
        self.assertNotIn("local.box.evil", detail)
        self.assertEqual(self.apply(), [("remove", "local.theirs"), ("add", "local.new")])
        self.assertEqual(self.sent, [])

    def test_the_suffix_counts_for_case_too(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        theirs = self.agents / "local.box.web.PLIST"
        theirs.write_text("theirs")
        self.assertEqual(self.apply(), [])
        self.assertEqual(([f.name for f in self.agents.iterdir()], theirs.read_text()), ([theirs.name], "theirs"))
        self.assertIn(theirs.name, "\n".join(self.problems()))

    def test_a_notice_is_kept_until_it_was_sent(self):
        self.project("open", sandbox=False)
        self.foreign("com.vendor.agent")
        self.list_rendered("com.vendor.agent")
        self.sends = False
        for n in (1, 2):
            self.apply()
            self.assertEqual(len(self.sent), n)
        self.sends = True
        self.apply()
        self.apply()
        self.assertEqual(len(self.sent), 3)
        # with no channel there is no one to send to: printed, once
        self.sends = False
        with mock.patch.object(self.rs.alert, "configured", lambda: []):
            self.rs.notice_once("x")
            self.apply()
        self.assertEqual(json.loads(self.rs.notices().read_text()), [])

    def test_a_notice_of_a_dropped_label_is_kept_too(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh", "label": "local.box-web"})
        self.old_unit("local.box-web", "box", cli=True)
        self.sends = False
        self.apply()
        self.sends = True
        self.apply()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("local.box-web", self.sent[1])

    def test_a_label_its_project_had_unsandboxed_is_said_when_it_goes(self):
        svc = {"name": "web", "run": "bin/serve.sh", "label": "local.mine"}
        self.project("box", svc, sandbox=False)
        self.apply()
        self.project("box", svc)
        self.apply()
        self.assertEqual(sorted(self.loaded), ["local.box.web"])
        self.assertIn("local.mine", "\n".join(self.sent))

    def test_a_variable_that_is_no_name_is_said_and_the_service_kept(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh", "env": {"my-key": "1", "GOOD": "2"}})
        self.assertEqual(self.apply(), [("add", "local.box.web")])
        self.assertIn("my-key", "\n".join(self.problems()))
        self.assertEqual(self.apply(), [])

    def test_a_unit_of_any_size_or_number_is_read(self):
        self.project("box", {"name": "web", "run": "bin/serve.sh"})
        self.old_unit("local.box.web", "box", {"A": "x" * (6 << 20)})
        big = plistlib.dumps(self.old_plist("com.vendor.agent", "box", StartInterval=1)).replace(
            b"<integer>1<", b"<integer>" + b"9" * 5000 + b"<")
        self.unit("com.vendor.agent").write_bytes(big)
        self.assertEqual(sorted(self.apply()), [("remove", "com.vendor.agent"), ("update", "local.box.web")])


class Entries(unittest.TestCase):
    """The registry's side (r2 section 3; r3 3)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "ws" / "bin").mkdir(parents=True)
        self.dirs = {"runtime": None, "workspace": self.tmp / "ws", "hub": hubconf.ROOT}

    def errors(self, *services, jobs=(), sandbox=True, stem="p", **kw):
        doc = {"name": stem, "services": list(services), "jobs": list(jobs),
               **({"enforce": {"sandbox": True}} if sandbox else {}), **kw}
        return registry.validate_runs(registry.normalise(doc) if hasattr(registry, "normalise") else doc, stem, self.dirs)

    def test_names_and_labels(self):
        ok = {"run": "sh -c true"}
        for name in ("web", "Web_2", "a-b", "_x", "with.dot"):
            self.assertEqual(self.errors({"name": name, **ok}), [], name)
        for name in ("../x", "a/b", "a..b", "-x", ".x", "my web", "a\n", "x" * 102, "é", ["a"]):
            self.assertTrue(self.errors({"name": name, **ok}), name)
        for label in ("../x", "a/b", "x\n", "a..b", "local.agent-hub-scheduler", "local.brunna-scheduler-watchdog",
                      "LOCAL.Status-Page", "local.agent-hub.cap-runner", "local.brunna.sync-all-memory", 5):
            self.assertTrue(self.errors({"name": "w", "label": label, **ok}, sandbox=False), label)
        self.assertEqual(self.errors({"name": "w", "label": "local.mine", **ok}, sandbox=False), [])
        self.assertTrue(self.errors({"name": "web", **ok}, {"name": "Web", **ok}), "the same but for case")
        self.assertTrue(self.errors({"name": "w", "label": "local.status-page", **ok}), "nor declared by a sandboxed one")
        self.assertEqual(self.errors({"name": "w", "label": "local.mine", **ok}), [])

    def test_every_service_value_is_checked(self):
        ok = {"name": "w", "run": "sh -c true"}
        good = ({"interval_s": 60}, {"interval_s": "300"}, {"throttle_s": 5.0}, {"keepalive": True}, {"log": "x.log"},
                {"expose": {"local_port": "8080", "tailnet_https": 8443}}, {"run": "sh -c\n  true\n"},
                {"env": {"A": "1", "B": 2, "C": True, "D": 1.5}}, {"cwd": "hub"}, {"env": None}, {"log": ""}, {"interval_s": 0})
        for part in good:
            self.assertEqual(self.errors({**ok, **part}), [], part)
        for part in ({"interval_s": -1}, {"interval_s": "soon"}, {"interval_s": [1]}, {"throttle_s": 10 ** 12},
                     {"keepalive": ["x"]}, {"expose": [1]}, {"expose": {"local_port": "auto"}}, {"expose": {"local_port": 70000}},
                     {"env": ["A"]}, {"env": {"A B": "1"}}, {"env": {"A": ["x"]}}, {"env": {"A": "x\ny"}}, {"env": {"A": "x\0"}},
                     {"env": {"A\n": "x"}}, {"log": 5}, {"log": "a\nb"}, {"run": "sh -c 'a\nb'"}, {"run": ["sh"]}, {"run": "sh \x01"},
                     {"cwd": "home"}, {"cwd": ["hub"]}, {"cwd": "runtime"}, {"cwd": "a\nb"}, {"label": ["x"]}):
            self.assertTrue(self.errors({**ok, **part}), part)
        self.assertTrue(self.errors("x"))
        self.assertTrue(self.errors({**ok, "expose": {"local_port": 8080, "other": 1}}))
        # what reads a unit doesn't agree on these
        for odd in ("\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029", "\ufdd0", "\uffff", "\U0010fffe", "\ud800"):
            for part in ({"run": f"sh 'x{odd}'"}, {"env": {"A": odd}}, {"log": odd}, {"name": f"w{odd}"}, {"label": f"w{odd}"}):
                self.assertTrue(self.errors({**ok, **part}, sandbox="label" not in part), (odd, part))
        self.assertEqual(self.errors({**ok, "env": {"A": "\u00e9\u4e2d\U0001f600"}}), [])

    def test_every_job_value_is_checked(self):
        ok = {"name": "j", "at": "03:00", "run": "sh -c true"}
        for part in ({}, {"days": ["mon"]}, {"days_of_month": [1, 31]}, {"months": [12]}, {"until": "2026-11-01"},
                     {"between": "08:00-22:00"}, {"max_attempts_per_day": 2}, {"run": "cd x &&\nsh -c true"}):
            self.assertEqual(self.errors(jobs=[{**ok, **part}]), [], part)
        for part in ({"days": [["mon"]]}, {"days": []}, {"days": 5}, {"days_of_month": 5}, {"days_of_month": [0]}, {"months": []},
                     {"months": ["jan"]}, {"until": [1]}, {"until": "soon"}, {"between": "8"}, {"between": 5},
                     {"max_attempts_per_day": "2"}, {"timeout": "1h\n"}, {"timeout": "9" * 30 + "h"}, {"run": ["x"]}, {"run": "a\0"},
                     {"cwd": ["hub"]}, {"cwd": "runtime"}, {"name": 5}):
            self.assertTrue(self.errors(jobs=[{**ok, **part}]), part)
        self.assertTrue(self.errors(jobs=["x"]))
        self.assertTrue(self.errors(jobs=[{**ok, "after": ["k"]}, {**ok, "name": "k", "after": ["p/j"]}]), "a cycle of its own")

    def test_a_long_chain_of_jobs_is_read(self):
        graph = {f"j{i}": {f"j{i + 1}"} for i in range(5000)}
        self.assertEqual(registry.find_cycles(graph), [])
        self.assertEqual(registry.find_cycles({**graph, "j5000": {"j4999"}}), [["j4999", "j5000", "j4999"]])

    def test_a_sandboxed_projects_command_isnt_looked_up(self):
        """what a path names is told to whoever sends the entry: nothing is looked at outside"""
        there, gone = {"name": "w", "run": "../there/x"}, {"name": "w", "run": "../gone/x"}
        (self.tmp / "there").mkdir()
        (self.tmp / "there" / "x").write_text("")
        self.assertEqual(self.errors(there), self.errors(gone))
        self.assertNotEqual(self.errors(there, sandbox=False), self.errors(gone, sandbox=False))
        job = {"name": "j", "at": "03:00"}
        self.assertEqual(self.errors(jobs=[{**job, "run": "../there/x"}]), self.errors(jobs=[{**job, "run": "../gone/x"}]))
        path = {"name": "w", "env": {"PATH": str(self.tmp / "there")}}
        self.assertEqual(self.errors({**path, "run": "x"}), self.errors({**path, "run": "y"}))
        self.assertTrue(self.errors({"name": "w", "run": "no-such-command-anywhere"}), "one on the platform's PATH is")

    def test_another_projects_job_is_never_put_second(self):
        job = {"name": "j", "every": "1h", "run": "sh -c true", "before": ["other/backup", "mine"]}
        docs = {"p": {"name": "p", "paths": {"workspace": "/w"}, "jobs": [job, {**job, "name": "mine", "before": None}]},
                "other": {"name": "other", "paths": {"workspace": "/o"}, "jobs": [{**job, "name": "backup", "before": None}]}}
        for d in docs.values():
            for j in d["jobs"]:
                if j.get("before") is None:
                    j.pop("before", None)
        # an unsandboxed project's works as it always has, an exempt one's too
        real = hubconf.get
        for enforce, exempt in (({}, []), ({"sandbox": True}, ["p"])):
            docs["p"]["enforce"] = enforce
            with mock.patch.object(hubconf, "get", lambda k, d=None: exempt if k == "service_sandbox_exempt" else real(k, d)):
                jobs = {j["name"]: j for j in registry.scheduler_jobs({"projects": docs}, set())[0]}
                self.assertEqual(jobs["other/backup"]["after"], ["p/j"])
                self.assertEqual(registry.ignored_before({"projects": docs}), {})
        docs["p"]["enforce"] = {"sandbox": True}
        jobs = {j["name"]: j for j in registry.scheduler_jobs({"projects": docs}, set())[0]}
        self.assertNotIn("after", jobs["other/backup"])
        self.assertEqual(jobs["p/mine"]["after"], ["p/j"])
        self.assertEqual(list(registry.ignored_before({"projects": docs})), ["p/j"])
        self.assertEqual(self.errors(jobs=docs["p"]["jobs"]), [], "the entry stays valid")

    def test_a_cached_job_with_values_the_scheduler_refuses_is_the_schedulers_to_judge(self):
        """scheduler.unsound refuses what it can't run and tells it; the registry takes away nothing that ran before."""
        ok = {"every": "1h", "run": "x"}
        jobs = [{"name": "j", **ok, "until": "soon"}, {"name": "good", **ok}]
        index = {"projects": {"p": {"name": "p", "paths": {"workspace": "/w"}, "jobs": jobs}}}
        self.assertEqual([j["name"] for j in registry.scheduler_jobs(index, set())[0]], ["p/j", "p/good"])
        self.assertNotIn("refused", index)

    def test_a_job_that_cant_be_read_never_stops_the_others(self):
        docs = {"a": {"name": "a", "paths": {"workspace": "/w"}, "jobs": ["x", {"name": "j", "every": "soon", "run": "x"}]},
                "b": {"name": "b", "paths": {"workspace": "/w"}, "jobs": [{"name": "j", "every": "1h", "run": "x"}]}}
        index = {"projects": docs}
        self.assertEqual([j["name"] for j in registry.scheduler_jobs(index, set())[0]], ["b/j"])
        self.assertIn("a/j", index["refused"])

    def test_what_a_letter_may_not_add(self):
        cur = {"v": 2, "name": "p", "paths": {"workspace": str(self.tmp / "ws")}, "enforce": {"sandbox": True},
               "services": [{"name": "old.dotted", "run": "sh -c true"}],
               "jobs": [{"name": "j", "every": "1h", "run": "sh -c true", "before": ["brunna/todo"], "after": ["brunna/librarian"]}]}
        index = {"projects": {"q": {"name": "q", "services": [{"name": "w", "label": "local.p.taken", "run": "x"}]}, "p": cur}}

        def letter(**patch):
            return registry.manifest_update(patch, "p", cur, *([index] if registry.manifest_update.__code__.co_argcount > 3 else []))[1]
        self.assertEqual(letter(commits="conventional"), [], "what it has it keeps")
        svc = {"run": "sh -c true"}
        self.assertEqual(letter(services=[*cur["services"], {"name": "new_one", **svc}]), [])
        self.assertTrue(letter(services=[{"name": "new.dotted", **svc}]))
        self.assertTrue(letter(services=[{"name": "taken", **svc}]), "a label another entry has")
        self.assertTrue(letter(services=[{"name": "TAKEN", **svc}]))
        job = {"name": "k", "every": "1h", "run": "sh -c true"}
        self.assertEqual(letter(jobs=[{**job, "after": ["brunna/librarian"]}]), [])
        self.assertTrue(letter(jobs=[{**job, "before": ["brunna/todo"]}]))
        self.assertTrue(letter(jobs=[{**job, "after": ["q/j"]}]), "an ordinary project's job")
        cur["enforce"] = {}
        self.assertEqual(letter(jobs=[{**job, "before": ["brunna/todo"], "after": ["q/j"]}]), [], "an unsandboxed project's own choice")


class Refresh(unittest.TestCase):
    """registry.py check through a scratch instance: one entry never stops the others, the newcomer to a label is the one
    refused, an entry that became invalid stays as it last was."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = self.tmp / "config" / "projects"
        self.reg.mkdir(parents=True)
        (self.tmp / "config" / "hub.json").write_text(json.dumps({"v": 1, "registry_dir": "projects",
                                                                  "owner": {"address": "owner", "tag": "owner"}}))

    def add(self, name, text="", sandbox=False):
        ws = self.tmp / "ws" / name
        ws.mkdir(parents=True, exist_ok=True)
        (self.reg / f"{name}.yaml").write_text(f"v: 2\nname: {name}\npaths:\n  workspace: {ws}\n" +
                                               ("enforce: {sandbox: true}\n" if sandbox else "") + text)

    def check(self):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith(("AGENT_HUB_", "BRUNNA_"))
                  or k in ("AGENT_HUB_PYTHON", "BRUNNA_PYTHON")},
               "HOME": str(self.tmp / "home"), "AGENT_HUB_CONFIG": str(self.tmp / "config"),
               "AGENT_HUB_STATE": str(self.tmp / "state"), "AGENT_HUB_LANG_UI": "en"}
        r = subprocess.run([sys.executable, str(BIN / "registry.py"), "check"], env=env, capture_output=True, text=True,
                           timeout=60)
        self.index = json.loads((self.tmp / "state" / "shared" / "registry" / "index.json").read_text())
        return r

    def services(self, name):
        return self.index["projects"][name].get("services")

    def test_an_entry_that_crashes_the_check_is_its_own_problem(self):
        self.add("a", "services: [{name: w, run: 'sh -c true', cwd: [hub]}]\njobs: [5]\n")
        self.add("b", "mailbox: 5\nenforce: [1]\nprivacy: [1]\n")
        self.add("good", "services: [{name: w, run: 'sh -c true', env: {N: 1}, interval_s: '300'}]\n")
        r = self.check()
        self.assertEqual(sorted(self.index["errors"]), ["a", "b"], r.stdout + r.stderr)
        self.assertEqual(self.services("good"), [{"name": "w", "run": "sh -c true", "env": {"N": "1"}, "interval_s": 300}])

    def test_an_entry_that_cant_be_held_is_its_own_problem(self):
        bad = {"a": "services: [{name: w, run: 'sh -c true', expose: {2020-01-01: 1}}]\n", "b": 'purpose: "\\uD800"\n',
               "c": f"provides: [{{contract: x, v: 0x{'f' * 5000}}}]\n", "d": "publishes: [{2020-01-01: 1}]\n",
               "e": "provides: " + "[" * 300 + "]" * 300 + "\n", "f": "provides: [.inf]\n", "g": "code: [!!binary aGk=]\n",
               "h": '"\\uD800x": 1\n', "i": "provides: [{1: 2}]\n"}
        for name, text in bad.items():
            self.add(name, text)
        self.add("good")
        # one accepted before, whose cache can't be held either
        (self.tmp / "state" / "shared" / "registry").mkdir(parents=True)
        for name in ("a", "gone"):
            (self.tmp / "state" / "shared" / "registry" / f"{name}.json").write_text('{"name": "a", "v": 2, "purpose": "\\ud800"}')
        r = self.check()
        self.assertEqual(sorted(self.index["errors"]), sorted(bad), r.stdout + r.stderr)
        self.assertIn("good", self.index["projects"])
        self.assertEqual(set(self.index["projects"]) & set(bad), set())

    def test_what_others_read_of_an_entry_has_its_shape(self):
        bad = {"a": "mailbox: [1]\n", "b": "consumes: 5\n", "c": "consumes: [x]\n", "d": "provides: [{contract: 5, schema: x}]\n",
               "e": "provides: [{contract: x}]\n", "f": "consumes: [{contract: x, copy: [y]}]\n", "g": "publishes: [{path: [x]}]\n",
               "h": "consumes: [{contract: x, copy: y, from: [z]}]\n", "i": "provides: [{contract: x, schema: y, v: {a: 1}}]\n"}
        for name, text in bad.items():
            self.add(name, text)
        self.add("good", "mailbox: {escalate: true}\nprovides: [{contract: x, v: 1, schema: s.json}]\n"
                         "consumes: [{contract: x, v: 1, from: good, copy: c.json}]\npublishes: [{kind: state, path: S, contract: t, v: 2}]\n")
        self.check()
        self.assertEqual(sorted(self.index["errors"]), sorted(bad))
        # one accepted before that is kept without what its readers couldn't read
        cache = self.tmp / "state" / "shared" / "registry" / "a.json"
        cache.write_text(json.dumps({"v": 2, "name": "a", "paths": {"workspace": "/w"}, "mailbox": [1], "consumes": 5, "provides": [
            5, {"contract": "x", "schema": "s"}], "publishes": {"a": 1}}))
        self.check()
        a = self.index["projects"]["a"]
        self.assertNotIn("mailbox", a)
        self.assertEqual((a["consumes"], a["provides"], a["publishes"]), ([], [{"contract": "x", "schema": "s"}], []))

    def test_contracts_check_reads_the_rest(self):
        cc = load("contracts_check_units", "contracts-check.py")
        ws = self.tmp / "ws" / "p"
        ws.mkdir(parents=True)
        (ws / "s.json").write_text("{}")
        good = {"contract": "x", "v": 1, "from": "p", "copy": "gone.json"}
        docs = {n: {"name": n, "paths": {"workspace": str(ws)}, "consumes": c, "provides": p} for n, c, p in (
            ("a", 5, 7), ("b", ["x", {"contract": "x"}, {"contract": "x", "copy": 5, "from": "p"}, {"contract": "x", "copy": "c", "from": [1]}], ["y"]),
            ("p", [good], ["x", {"contract": "x", "v": 1, "schema": "s.json"}]), ("q", [good], [{"schema": 5}]))}
        errs = cc.cross_copies({"projects": docs})
        self.assertEqual(sum("gone.json" in e for e in errs), 2, errs)
        self.assertEqual(sum(e.startswith("b") for e in errs), 4, errs)

    def test_the_newcomer_to_a_label_is_refused(self):
        self.add("z", "services: [{name: w, label: local.one, run: 'sh -c true'}]\n")
        self.add("a", "services: [{name: w, run: 'sh -c true'}]\n")
        self.assertEqual(self.check().returncode, 0)
        self.add("a", "services: [{name: w, label: LOCAL.One, run: 'sh -c true'}]\n")
        self.check()
        self.assertEqual(list(self.index["errors"]), ["a"])
        self.assertEqual(self.services("a"), [{"name": "w", "run": "sh -c true"}], "its last valid version")
        self.assertEqual(self.services("z")[0]["label"], "local.one")
        self.add("z")   # given up: the other may have it
        self.assertEqual(self.check().returncode, 0)

    def test_two_already_there_both_stay(self):
        for n in ("a", "b"):
            self.add(n, "services: [{name: w, label: local.one, run: 'sh -c true'}]\n")
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.check().returncode, 0)

    def test_an_entry_that_became_invalid_keeps_its_last_valid_services(self):
        self.add("a", "services: [{name: w, run: 'sh -c true'}]\n", sandbox=True)
        self.check()
        self.add("a", "services: [{name: w, run: 'sh -c true', interval_s: soon}]\n", sandbox=True)
        self.check()
        self.assertEqual(list(self.index["errors"]), ["a"])
        self.assertEqual(self.services("a"), [{"name": "w", "run": "sh -c true"}])


if __name__ == "__main__":
    unittest.main()
