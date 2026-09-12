#!/usr/bin/env python3
"""Real-process contracts for the A/B estate benchmark.

Isolation here means environment redirection plus after-the-fact checks, not a
sandbox; the escaped-arm test below exercises a ref that ignores the redirect.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent / "ab_estate_benchmark.py"
SOURCE = Path(__file__).resolve().parents[1]


# The benchmark runs a real `claude doctor` configuration-load smoke per arm and
# records the real Claude Code version, so a host without the CLI cannot run it at
# all. Declared here rather than worked around: a stub executable or a smoke that
# skips itself would report a benchmark that never happened.
@unittest.skipUnless(shutil.which("claude"), "the Claude Code CLI is unavailable")
class ABEstateBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-benchmark-"))
        self.clone = self.tmp / "source"
        # DEVIN_ESTATE_HOME deliberately points at a scratch directory, the way
        # hooks/tests/run.sh invokes the suite. Anchoring isolation on that variable
        # instead of the installed estate rewrote nothing and hashed an empty
        # directory, so the benchmark reported isolation it had not established.
        # HOME too, so the run is exercised on a host whose home is not the machine
        # that authored settings.json. Anchoring the estate prefix on Path.home()
        # rewrote nothing there and left every arm pointing at the live estate; CI
        # is such a host, and the suite passed locally only by coincidence.
        self.env = {
            **os.environ,
            "HOME": str(self.tmp / "foreign-home"),
            "DEVIN_ESTATE_HOME": str(self.tmp / "scratch-home"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.run_git("clone", "--quiet", str(SOURCE), str(self.clone), cwd=self.tmp)
        self.run_git("config", "user.email", "benchmark@example.invalid")
        self.run_git("config", "user.name", "Benchmark Harness")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout.strip()

    def benchmark(self, baseline: str, candidate: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        """Drive the real command as a subprocess and return it with its artifact."""
        out = self.tmp / f"out-{baseline}-{candidate}".replace("~", "-")
        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", baseline,
             "--candidate", candidate, "--out", str(out)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        artifact = out / "benchmark.json"
        # A run that dies before writing the artifact must report why, not KeyError
        # in whichever assertion happens to read the empty result first.
        self.assertTrue(artifact.exists(), f"no artifact was written\n{result.stdout}{result.stderr}")
        return result, json.loads(artifact.read_text(encoding="utf-8"))

    def benchmark_at(self, out: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
        """Run the real command against a fixed output directory, so a rerun reuses arm keys.

        The previous artifact is removed first: without that, a rerun that dies before
        writing one is read as the earlier run's success.
        """
        (out / "benchmark.json").unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", "HEAD", "--candidate", "HEAD",
             "--out", str(out)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        artifact = out / "benchmark.json"
        self.assertTrue(artifact.exists(), f"no artifact was written\n{result.stdout}{result.stderr}")
        return result, json.loads(artifact.read_text(encoding="utf-8"))

    def repo_key(self, repo: Path) -> str:
        """The repository key, computed the way hooks/lib/repo_identity.py documents it."""
        root = subprocess.run(["realpath", "-e", str(repo)], text=True, capture_output=True,
                              check=True).stdout.rstrip("\n")
        return subprocess.run(["cksum"], input=root.encode(), capture_output=True,
                              check=True).stdout.decode().split()[0]

    def declared_estate(self) -> str:
        """The estate root this checkout's tracked settings name, read the way the command does."""
        value = json.loads((self.clone / "settings.json").read_text(encoding="utf-8"))
        roots = {token[: token.index("/hooks/")] for group in value["hooks"].values() for entry in group
                 for hook in entry["hooks"] for token in shlex.split(hook["command"]) if "/hooks/" in token}
        self.assertEqual(len(roots), 1, f"expected one declared estate, got {sorted(roots)}")
        return roots.pop()

    def test_arms_built_from_one_ref_report_parity_isolation_and_exact_identities(self) -> None:
        head = self.run_git("rev-parse", "HEAD")
        tree = self.run_git("rev-parse", "HEAD^{tree}")

        result, artifact = self.benchmark("HEAD", "HEAD")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(artifact.get("ok"), json.dumps(artifact.get("scenarios"), indent=2)[:4000])

        baseline, candidate = artifact["arms"]["baseline"], artifact["arms"]["candidate"]
        for arm in (baseline, candidate):
            self.assertEqual(arm["commit"], head, "the arm drifted off the resolved commit")
            self.assertEqual(arm["tree"], tree, "the arm drifted off the resolved tree")
            self.assertEqual(arm["configSmokeExit"], 0, "the no-model configuration-load smoke failed")
        for key in ("root", "home", "stateRoot"):
            self.assertNotEqual(baseline[key], candidate[key], f"the arms shared a {key}")

        observed = {}
        for scenario in artifact["scenarios"]:
            self.assertEqual(scenario["differences"], [], json.dumps(scenario, indent=2)[:2000])
            for arm in ("baseline", "candidate"):
                self.assertEqual(len(scenario[arm]["seconds"]), 5, "five repetitions per arm are required")
            observed[scenario["name"]] = scenario["observed"]

        # Parity alone would hold for two identically-broken arms, so each scenario
        # must also be shown to have done the thing it is named for.
        self.assertEqual(observed["begin"]["phase"], "intake")
        self.assertEqual(observed["status-and-summary"]["summary"]["slug"], "estate-benchmark")
        self.assertIs(observed["checkpoint-not-ready"]["ready"], False)
        self.assertIn("repo-context-forge", observed["checkpoint-not-ready"]["missing"])
        self.assertIs(observed["post-edit-hook"]["gateRejected"], True, "the arm's quality gate never ran")
        self.assertEqual(observed["post-edit-hook"]["exits"][0], 2, "the rejected payload was not rejected")
        self.assertEqual(observed["post-edit-hook"]["after"]["phase"], "implementation")
        self.assertIs(observed["prune-report"]["applied"], False, "prune must report, never apply")

        isolation = artifact["isolation"]
        self.assertTrue(isolation["ok"], json.dumps(isolation, indent=2)[:2000])
        self.assertEqual(isolation["estateDigestBefore"], isolation["estateDigestAfter"])
        self.assertEqual(isolation["stateRootEntriesBefore"], isolation["stateRootEntriesAfter"])
        self.assertTrue(isolation["armKeys"], "no arm ever created a repository key to check")
        self.assertEqual(isolation["leakedKeys"], [], "an arm's key reached a live workflow-state root")
        # The estate the tracked settings name is what the arms could actually have
        # reached, so it must be monitored, unchanged, and gone from every arm — on a
        # host whose home does not happen to match it. Its digest is deliberately not
        # asserted non-empty: on a clean host it legitimately holds nothing.
        declared = self.declared_estate()
        self.assertIn(declared, isolation["liveEstates"], "the declared estate went unmonitored")
        self.assertEqual(isolation["estateDigestBefore"][declared], isolation["estateDigestAfter"][declared])
        self.assertIn(str(Path(self.env["HOME"]) / ".claude"), isolation["liveEstates"])
        self.assertIn(self.env["DEVIN_ESTATE_HOME"], isolation["liveEstates"])
        for arm in (baseline, candidate):
            written = (Path(arm["home"]) / "settings.json").read_text(encoding="utf-8")
            self.assertNotIn(declared, written, "the arm settings still name the declared estate")
        self.assertEqual(self.run_git("status", "--porcelain"), "", "the benchmark edited its source checkout")

    def test_an_out_path_inside_a_protected_root_is_refused_before_anything_is_deleted(self) -> None:
        # build_arm clears <out>/<arm> unconditionally, so an --out aimed at a live
        # estate destroys what is there before any isolation check has run.
        protected = Path(self.env["DEVIN_ESTATE_HOME"])
        (protected / "baseline").mkdir(parents=True)
        sentinel = protected / "baseline" / "sentinel.txt"
        sentinel.write_text("live data\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", "HEAD", "--candidate", "HEAD",
             "--out", str(protected)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

        self.assertTrue(sentinel.exists(), "the run deleted live data before refusing")
        self.assertNotEqual(result.returncode, 0, "an --out inside a monitored root was accepted")
        self.assertIn("overlaps", result.stderr + result.stdout)

        # Refusing must precede every filesystem mutation, including creating the
        # output directory itself: a descendant of a protected root must stay absent.
        descendant = protected / "state" / "not-created-by-a-refused-run"
        refused = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", "HEAD", "--candidate", "HEAD",
             "--out", str(descendant)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertNotEqual(refused.returncode, 0, "an --out under a monitored root was accepted")
        self.assertFalse(descendant.exists(), "the refused run created a directory inside a protected root")

    def test_an_arm_trace_left_under_a_monitored_state_root_fails_isolation(self) -> None:
        # The shared `sessions/<session>/<repo-key>.json` tree is what an arm escaping its
        # redirected state root would write, and a slot-name comparison cannot see it.
        # Arm keys are the cksum of each fixture path, so a rerun against the same --out
        # reproduces them exactly.
        out = self.tmp / "out-trace"
        first, artifact = self.benchmark_at(out)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # Derived from the fixture path through the same POSIX contract the estate uses,
        # not read back from arm state: a redirect failure would leave that state empty,
        # so keys learned from it would be exactly the ones an escape hides behind.
        # The migration fixture is a separate repository, so its key belongs in the
        # traced set too; without it an escape during migration would go unseen.
        # The replay gives each arm a fixture of its own, and the migration fixture is a
        # separate repository again; without all of them an escape during the replayed
        # sequence or during migration would go unseen.
        expected = {self.repo_key(out / arm / "repos" / str(index))
                    for arm in ("baseline", "candidate") for index in range(5)}
        expected.update(self.repo_key(out / arm / "replay" / "repo") for arm in ("baseline", "candidate"))
        expected.add(self.repo_key(out / "migration" / "repo"))
        self.assertEqual(set(artifact["isolation"]["armKeys"]), expected,
                         "arm keys are not derived from the fixture paths")
        key = sorted(expected)[0]

        root = Path(self.env["DEVIN_ESTATE_HOME"]) / "state"
        (root / "sessions" / "some-session").mkdir(parents=True, exist_ok=True)
        (root / "sessions" / "some-session" / f"{key}.json").write_text("{}\n", encoding="utf-8")

        second, replay = self.benchmark_at(out)

        self.assertNotEqual(second.returncode, 0, "an arm trace under a live state root passed")
        self.assertFalse(replay["isolation"]["ok"])
        self.assertIn(f"sessions/some-session/{key}.json", replay["isolation"]["leakedKeys"])
        self.assertEqual(replay["isolation"]["stateRootEntriesBefore"],
                         replay["isolation"]["stateRootEntriesAfter"],
                         "slot names alone were enough, so this proves nothing about the sessions tree")

    def test_a_symlinked_arm_root_is_refused_rather_than_followed(self) -> None:
        # shutil.rmtree(ignore_errors=True) silently does nothing to a symlink, so the
        # arm is then built straight through it, outside the output directory and past
        # the overlap guard, which only ever resolves --out itself.
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        sentinel = elsewhere / "sentinel.txt"
        sentinel.write_text("live data\n", encoding="utf-8")
        out = self.tmp / "out-symlink"
        out.mkdir()
        (out / "baseline").symlink_to(elsewhere, target_is_directory=True)

        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", "HEAD", "--candidate", "HEAD",
             "--out", str(out)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

        self.assertEqual(sorted(path.name for path in elsewhere.iterdir()), ["sentinel.txt"],
                         "the run built the arm through the symlink")
        self.assertNotEqual(result.returncode, 0, "a symlinked arm root was accepted")
        self.assertIn("symlink", result.stderr + result.stdout)
        self.assertFalse((out / "candidate").exists(), "the second arm was built before refusing")
        self.assertFalse((out / "benchmark.json").exists(), "a refused run still wrote an artifact")

    def test_an_unavailable_repository_key_refuses_before_any_scenario(self) -> None:
        """The real identity owner, made to fail by taking cksum off PATH.

        repo_identity.py derives the key with `realpath -e` piped to `cksum`, so removing
        only cksum makes it fail through its own documented non-zero Interface. Nothing is
        stubbed: an empty key would silently give arm_traces nothing to match, and the run
        would report clean isolation it never measured.
        """
        crippled = self.tmp / "bin"
        crippled.mkdir()
        for directory in os.environ["PATH"].split(os.pathsep):
            for entry in Path(directory).iterdir() if Path(directory).is_dir() else []:
                if entry.name != "cksum" and not (crippled / entry.name).exists():
                    (crippled / entry.name).symlink_to(entry)
        self.assertFalse((crippled / "cksum").exists(), "cksum is still reachable")

        out = self.tmp / "out-nokey"
        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", "HEAD", "--candidate", "HEAD",
             "--out", str(out)],
            cwd=self.clone, env={**self.env, "PATH": str(crippled)}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

        self.assertNotEqual(result.returncode, 0, "an unavailable repository key was accepted")
        self.assertIn("repository key", result.stderr + result.stdout)
        self.assertFalse((out / "benchmark.json").exists(),
                         "an indeterminate isolation measurement still produced an artifact")

    def test_one_ref_named_by_both_arms_pins_one_commit_while_it_moves(self) -> None:
        """Reproduced with a real `git update-ref`, no interception of git.

        Each arm used to resolve the ref itself, so a ref moving between those two
        adjacent reads pinned the arms at different revisions and the run reported a
        behavioural diff for what the operator named as one ref.
        """
        first = self.run_git("rev-parse", "HEAD")
        self.run_git("commit", "--quiet", "--allow-empty", "-m", "a second revision")
        second = self.run_git("rev-parse", "HEAD")
        self.run_git("update-ref", "refs/heads/moving", first)

        trace = self.tmp / "git-trace.log"
        toggler = subprocess.Popen(
            ["bash", "-c", f"while :; do git update-ref refs/heads/moving {first}; "
                           f"git update-ref refs/heads/moving {second}; done"],
            cwd=self.clone, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for index in range(3):
                out = self.tmp / f"out-moving-{index}"
                subprocess.run(
                    [sys.executable, str(BENCHMARK), "--baseline", "moving",
                     "--candidate", "moving", "--out", str(out)],
                    cwd=self.clone, env={**self.env, "GIT_TRACE": str(trace)}, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                arms = json.loads((out / "benchmark.json").read_text(encoding="utf-8"))["arms"]
                self.assertEqual(arms["baseline"]["commit"], arms["candidate"]["commit"],
                                 "one ref pinned the two arms at different commits")
                self.assertEqual(arms["baseline"]["tree"], arms["candidate"]["tree"])
        finally:
            toggler.terminate()
            toggler.wait(timeout=30)

        # Equal commits alone would also hold if the ref were read twice and both arms
        # happened to reuse the later value, so count the real resolutions instead.
        resolutions = [line for line in trace.read_text(encoding="utf-8", errors="replace").splitlines()
                       if "rev-parse" in line and "moving^{commit}" in line]
        self.assertEqual(len(resolutions), 3, f"expected one resolution per run, got {resolutions}")

    def test_the_migration_differential_continues_a_baseline_seeded_state_root(self) -> None:
        """The candidate continues state the baseline wrote, which is what a real handover is.

        Both arms are the same ref here, so this proves the mechanism: a real seed,
        two real continuations, equal projected state, one store throughout. The
        two-engine differential — a candidate that adopts a different store and never
        converts what it was given — is the separate case below.
        """
        _, artifact = self.benchmark("HEAD", "HEAD")
        migration = artifact["migration"]

        self.assertEqual(migration["seedStores"], ["workflow.sqlite3"],
                         "the baseline CLI did not leave state for the candidate to continue")
        self.assertEqual(migration["exits"], [0, 0], "a continuation refused the seeded state root")
        self.assertTrue(migration["match"], json.dumps(migration, indent=2)[:2000])
        self.assertIn("workflow.sqlite3", migration["candidateStores"],
                      "the candidate did not read the store it was given")

    def test_a_candidate_that_escapes_its_state_root_is_caught(self) -> None:
        """An arm whose redirect genuinely fails, not a planted trace.

        The escaped arm writes no state into its own root, so keys read back from
        there would never include it and the escape would be invisible. Keys derived
        from the fixture path survive that, which is the whole point.
        """
        parent = self.run_git("rev-parse", "HEAD")
        escape = Path(self.env["DEVIN_ESTATE_HOME"]) / "state"
        store = self.clone / "hooks" / "lib" / "state_store.py"
        original = store.read_text(encoding="utf-8")
        # arm_env passes through every variable it does not override, so the candidate's
        # real state_root() can be made to answer with a monitored root instead.
        perturbed = original.replace(
            '    override = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")',
            '    override = os.environ.get("BENCHMARK_ESCAPE_ROOT") or os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")')
        self.assertNotEqual(perturbed, original, "the state_root override point moved")
        store.write_text(perturbed, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "escape the arm state root")
        escaped = self.run_git("rev-parse", "HEAD")

        out = self.tmp / "out-escape"
        env = {**self.env, "BENCHMARK_ESCAPE_ROOT": str(escape)}
        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", parent, "--candidate", escaped,
             "--out", str(out)],
            cwd=self.clone, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        artifact = json.loads((out / "benchmark.json").read_text(encoding="utf-8"))

        isolation = artifact["isolation"]
        self.assertNotEqual(result.returncode, 0, "an arm that escaped its state root passed")
        self.assertFalse(isolation["ok"])
        escaped_key = self.repo_key(out / "candidate" / "repos" / "0")
        self.assertTrue([trace for trace in isolation["leakedKeys"] if escaped_key in trace],
                        f"the escaped arm left no reported trace: {isolation['leakedKeys']}")

    def test_a_behavioural_mismatch_fails_the_run_and_is_recorded_verbatim(self) -> None:
        parent = self.run_git("rev-parse", "HEAD")
        # One requirement label in the checkpoint's readiness contract. It reaches
        # the compared `missing` array and nothing else, so a single scenario must
        # diverge while the rest stay in parity.
        state = self.clone / "hooks" / "lib" / "workflow_state.py"
        original = state.read_text(encoding="utf-8")
        perturbed_text = original.replace(
            '(("repo-context-forge", _evidence_ready(state, "repo-context-forge")),)',
            '(("repo-context-forge-graph", _evidence_ready(state, "repo-context-forge")),)',
        )
        self.assertNotEqual(perturbed_text, original, "the perturbation point moved; the test is no longer valid")
        state.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "perturb the checkpoint requirement label")
        perturbed = self.run_git("rev-parse", "HEAD")

        result, artifact = self.benchmark(parent, perturbed)

        self.assertNotEqual(result.returncode, 0, "a behavioural mismatch was reported as a pass")
        self.assertFalse(artifact["ok"])
        diverged = {item["name"]: item for item in artifact["scenarios"] if item["differences"]}
        self.assertEqual(list(diverged), ["checkpoint-not-ready"], "the wrong scenarios diverged")
        differences = diverged["checkpoint-not-ready"]["differences"]
        self.assertEqual(len(differences), 5, "a deterministic mismatch must show in every repetition")
        self.assertIn("repo-context-forge", differences[0]["baseline"]["missing"])
        self.assertIn("repo-context-forge-graph", differences[0]["candidate"]["missing"])
        self.assertTrue(artifact["isolation"]["ok"], "a behavioural mismatch is not an isolation failure")

    def test_an_unrelated_exit_two_does_not_satisfy_the_retired_transition(self) -> None:
        """Exit 2 is not a reason, and the state that follows it was set by the seed.

        `gitnexus` reads `passed` from the moment the seed records Repo Context Forge
        evidence, so the status read after the transition cannot testify about the
        transition. If the invariant also accepts any exit 2, an arm that failed for an
        unrelated reason looks identical to one that correctly refused a retired step.
        Both arms are that ref here, so nothing diverges and only the invariant can
        catch it — which is the identically-broken pair the benchmark exists to reject.
        """
        cli = self.clone / "hooks" / "lib" / "workflow_cli.py"
        original = cli.read_text(encoding="utf-8")
        perturbed_text = original.replace(
            '"gitnexus": "gitnexus is no longer a workflow step; Repo Context Forge records the graph "',
            '"gitnexus": "workflow-id binding failed for this instance; Repo Context Forge records the graph "',
        )
        self.assertNotEqual(perturbed_text, original, "the retired-transition message moved")
        cli.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "refuse the retired transition for an unrelated reason")
        head = self.run_git("rev-parse", "HEAD")

        result, artifact = self.benchmark(head, head)

        step = [item for item in artifact["scenarios"] if item["name"] == "replay-gitnexus"][0]
        self.assertEqual(step["observed"]["exits"], [2, 0], "the perturbed arm did not refuse with exit 2")
        self.assertFalse(step["differences"], "two identical arms cannot differ; only the invariant can catch this")
        self.assertFalse(
            step["invariantsHeld"],
            "an exit 2 carrying an unrelated reason satisfied the retired-transition invariant",
        )
        self.assertFalse(artifact["ok"])
        self.assertNotEqual(result.returncode, 0, "an identically broken pair was reported as a pass")

    def test_a_candidate_that_never_converts_the_seeded_store_fails(self) -> None:
        """The false green the migration differential exists to catch.

        This candidate reads and writes the baseline's legacy file perfectly, so its
        projection matches and both continuations exit zero. What it never does is move
        that state to the engine it uses everywhere else. Comparing projections cannot
        see that; comparing each arm's own native store against what it left behind can.
        """
        parent = self.run_git("rev-parse", "HEAD")
        state = self.clone / "hooks" / "lib" / "_workflow_db.py"
        original = state.read_text(encoding="utf-8")
        perturbed_text = original.replace(
            "        path = repo_state_dir(identity) / DATABASE_NAME",
            "        legacy = repo_state_dir(identity) / DATABASE_NAME\n"
            '        path = legacy if legacy.exists() else repo_state_dir(identity) / "workflow.v2.sqlite3"')
        self.assertNotEqual(perturbed_text, original, "the workflow store path moved")
        state.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "adopt a new store without converting legacy state")
        unconverted = self.run_git("rev-parse", "HEAD")

        result, artifact = self.benchmark(parent, unconverted)

        migration = artifact["migration"]
        self.assertEqual(migration["exits"], [0, 0], "the candidate refused the seeded root for some other reason")
        self.assertTrue(migration["match"], "this candidate must agree on state; only its storage differs")
        self.assertEqual(migration["candidateNativeStores"], ["workflow.v2.sqlite3"])
        self.assertEqual(migration["candidateStores"], ["workflow.sqlite3"],
                         "the candidate was supposed to leave the seeded store unconverted")
        self.assertFalse(migration["ok"], "an unconverted store was accepted")
        self.assertFalse(artifact["ok"])
        self.assertNotEqual(result.returncode, 0, "an unconverted store was reported as a pass")
        # The public summary, not only the artifact. Acceptance judges both arms, so a line
        # that reports FAILED while showing one of them tells the operator nothing about
        # which arm caused it. These two arms hold deliberately different native stores, so
        # a swapped or duplicated label cannot satisfy this assertion.
        self.assertEqual(migration["baselineNativeStores"], ["workflow.sqlite3"])
        self.assertIn("native baseline workflow.sqlite3 | candidate workflow.v2.sqlite3", result.stdout,
                      f"the summary does not name the arm that failed\n{result.stdout}")

    def test_the_workflow_state_replay_times_every_downstream_operation(self) -> None:
        """Capability 2: the governed sequence past Repo Context Forge, timed per operation.

        This proves that two estate refs persist identical state from identical input and
        what each operation costs. It is not evidence that an advisor consult or a code
        review happened; the inputs are benchmark stimulus.
        """
        head = self.run_git("rev-parse", "HEAD")
        result, artifact = self.benchmark(head, head)

        for name in ("baseline", "candidate"):
            seed = artifact["replaySeed"][name]
            self.assertEqual(seed["exit"], 0, f"{name} could not run its own Repo Context Forge: {seed['blocker']}")
            self.assertEqual(seed["repoContextForge"], "passed", "the real adapter did not record the phase")
            self.assertGreater(seed["seconds"], 0)
        replay = [item for item in artifact["scenarios"] if item["name"].startswith("replay-")]
        self.assertEqual([item["name"] for item in replay], [
            "replay-gitnexus", "replay-advisor-preflight", "replay-advisor-disposition", "replay-preflight",
            "replay-tdd", "replay-production-code", "replay-implementation", "replay-verification",
            "replay-quality-gate", "replay-code-review", "replay-advisor-final", "replay-final-disposition",
            "replay-complete"])
        for item in replay:
            self.assertTrue(item["invariantsHeld"], f"{item['name']} did not make its own transition")
            self.assertFalse(item["differences"], f"{item['name']}: {json.dumps(item['differences'])[:400]}")
            self.assertGreater(item["baseline"]["medianSeconds"], 0, item["name"])
            self.assertGreater(item["candidate"]["medianSeconds"], 0, item["name"])
        self.assertEqual(replay[-1]["observed"]["phase"], "complete", "the replay never reached completion")
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertLessEqual(len(result.stdout.strip().splitlines()), 20, "the clean summary outgrew its budget")

    def test_the_migration_fixture_is_traced_under_both_arms_keys(self) -> None:
        """A candidate that names repositories differently must still be searched for.

        Only the baseline is asked for the migration fixture's key. A candidate whose
        identity function disagrees writes under a name nothing looks for, so an escape
        that happened only during the migration comparison leaves no trace the run reads.
        """
        parent = self.run_git("rev-parse", "HEAD")
        identity = self.clone / "hooks" / "lib" / "repo_identity.py"
        original = identity.read_text(encoding="utf-8")
        perturbed_text = original.replace(
            "        key = checksum_output.split()[0]",
            '        key = f"9{checksum_output.split()[0]}"')
        self.assertTrue(perturbed_text != original, "the key derivation moved")
        identity.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "name repositories differently")
        renaming = self.run_git("rev-parse", "HEAD")

        out = self.tmp / "out-identity"
        result = subprocess.run(
            [sys.executable, str(BENCHMARK), "--baseline", parent, "--candidate", renaming, "--out", str(out)],
            cwd=self.clone, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        artifact = json.loads((out / "benchmark.json").read_text(encoding="utf-8"))
        asked = subprocess.run(
            [sys.executable, str(out / "candidate/home/hooks/lib/repo_identity.py"),
             "--field", "key", "--path", str(out / "migration/repo")],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertIn(asked.stdout.strip(), artifact["isolation"]["armKeys"],
                      "the candidate's own name for the migration fixture is never searched for")
        self.assertTrue(result.stdout, "the run produced no summary")

    def test_an_arm_without_the_typed_gate_records_not_supported(self) -> None:
        """Two refs that do not ship `verify --kind`, so the step must be declared absent.

        This branch does ship the typed gate, so an arm without it has to be built: the
        option is renamed, which is exactly what a ref predating it looks like to the
        capability probe. Both arms are that ref, so the honest record is a step with no
        runs on either side. A synthesised pass would be exactly the fake green the
        comparison exists to catch, and because both arms agree there is no capability
        delta to report either.
        """
        cli = self.clone / "hooks" / "lib" / "workflow_cli.py"
        original = cli.read_text(encoding="utf-8")
        # Renamed rather than removed: the runner still needs its own `kind` attribute
        # for the generic verification the replay depends on.
        perturbed_text = original.replace(
            'command.add_argument("--kind", choices=("generic", "quality-gate"), default="generic")',
            'command.add_argument("--gate-kind", dest="kind", choices=("generic", "quality-gate"), default="generic")')
        self.assertNotEqual(perturbed_text, original, "the typed-gate option moved")
        cli.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "retire the typed quality gate option")
        head = self.run_git("rev-parse", "HEAD")

        result, artifact = self.benchmark(head, head)

        for name in ("baseline", "candidate"):
            self.assertFalse(artifact["replaySeed"][name]["qualityGate"],
                             "this branch's runner was detected as shipping --kind")
            self.assertEqual(artifact["replaySeed"][name]["helpExit"], 0, "the capability probe itself failed")
        typed = [item for item in artifact["scenarios"] if item["name"] == "replay-quality-gate"][0]
        self.assertEqual(typed["observed"], {"exits": [], "supported": False},
                         "an unshipped step was recorded as something other than absent")
        self.assertFalse(typed["capabilityDelta"], "two identical arms cannot differ in capability")
        self.assertTrue(typed["invariantsHeld"])
        self.assertFalse(typed["differences"])
        # The typed gate now carries the final-review tree binding and completion
        # requires its evidence, so an arm without it genuinely cannot finish the
        # governed sequence. The run must fail for exactly that reason and no other:
        # the absent step itself stays absent rather than being synthesised into a pass.
        self.assertNotEqual(result.returncode, 0, "an arm that cannot complete was reported as a pass")
        self.assertEqual(
            [item["name"] for item in artifact["scenarios"] if not item["invariantsHeld"]],
            ["replay-code-review", "replay-advisor-final", "replay-final-disposition", "replay-complete"],
            "the missing typed gate broke something other than the steps that depend on it",
        )
        self.assertTrue(artifact["isolation"]["ok"], "a capability gap is not an isolation failure")

    def test_a_baseline_that_cannot_seed_is_reported_rather_than_crashing(self) -> None:
        """A failed seed must reach the artifact, because a traceback reports nothing.

        Only the seed is broken here, so the five per-arm scenarios still run and the
        operator still gets their comparison alongside the migration failure.
        """
        parent = self.run_git("rev-parse", "HEAD")
        # The entry point behind every shim, so the refusal reaches the seed whichever
        # script the benchmark invokes.
        cli = self.clone / "hooks" / "lib" / "workflow_cli.py"
        original = cli.read_text(encoding="utf-8")
        perturbed_text = original.replace(
            "def main(argv: list[str] | None = None) -> int:\n    try:",
            "def main(argv: list[str] | None = None) -> int:\n"
            '    if "migration seed" in sys.argv:\n'
            '        sys.stderr.write("seeding refused\\n")\n'
            "        return 3\n"
            "    try:")
        self.assertTrue(perturbed_text != original, "the CLI entry point moved")
        cli.write_text(perturbed_text, encoding="utf-8")
        self.run_git("commit", "--quiet", "--all", "-m", "refuse the migration seed")
        refusing = self.run_git("rev-parse", "HEAD")

        result, artifact = self.benchmark(refusing, parent)

        self.assertEqual(artifact["migration"]["seedExit"], 3)
        self.assertFalse(artifact["migration"]["ok"], "a failed seed was accepted")
        self.assertFalse(artifact["ok"])
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(artifact["scenarios"], "the per-arm scenarios must still be reported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
