import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import task_manager


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def test_project_root_staging_respects_gitignore():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        project = repo / "apps" / "demo-project"

        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.invalid")

        (repo / "README.md").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "baseline")

        project.mkdir(parents=True)
        (project / ".gitignore").write_text(
            ".test-runtime/\n",
            encoding="utf-8",
        )
        (project / "app.py").write_text(
            "print('ok')\n",
            encoding="utf-8",
        )

        runtime = project / ".test-runtime"
        runtime.mkdir()
        (runtime / "state.json").write_text(
            '{"runtime": true}\n',
            encoding="utf-8",
        )

        git(
            repo,
            "add",
            "--all",
            "--",
            "apps/demo-project",
        )

        staged = git(
            repo,
            "diff",
            "--cached",
            "--name-only",
        ).stdout.splitlines()

        assert "apps/demo-project/.gitignore" in staged
        assert "apps/demo-project/app.py" in staged
        assert not any(
            ".test-runtime" in path
            for path in staged
        )

        ignored = git(
            repo,
            "check-ignore",
            "apps/demo-project/.test-runtime/state.json",
        ).stdout.strip()

        assert (
            ignored
            == "apps/demo-project/.test-runtime/state.json"
        )

    print("PASS GITIGNORE: project-root staging respects ignore rules")


def test_publish_backup_restores_ignored_files():
    original_base_dir = task_manager.BASE_DIR

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_project = root / "repo" / "apps" / "demo-project"

        repo_project.mkdir(parents=True)
        (repo_project / ".gitignore").write_text(
            "runtime/\n",
            encoding="utf-8",
        )
        (repo_project / "tracked.txt").write_text(
            "before\n",
            encoding="utf-8",
        )

        runtime = repo_project / "runtime"
        runtime.mkdir()
        (runtime / "state.txt").write_text(
            "keep-me\n",
            encoding="utf-8",
        )

        task_manager.BASE_DIR = root / "agent"

        try:
            backup = task_manager.create_git_publish_backup(
                repo_project
            )

            (repo_project / "tracked.txt").write_text(
                "after\n",
                encoding="utf-8",
            )
            shutil.rmtree(runtime)
            (repo_project / "new.txt").write_text(
                "new\n",
                encoding="utf-8",
            )

            task_manager.restore_git_publish_backup(
                repo_project,
                backup,
            )

            assert (
                repo_project / "tracked.txt"
            ).read_text(encoding="utf-8") == "before\n"

            assert (
                repo_project / "runtime" / "state.txt"
            ).read_text(encoding="utf-8") == "keep-me\n"

            assert not (
                repo_project / "new.txt"
            ).exists()

            task_manager.remove_git_publish_backup(
                backup
            )

            assert not Path(
                backup["backup_root"]
            ).exists()

        finally:
            task_manager.BASE_DIR = original_base_dir

    print("PASS ROLLBACK: ignored files restored exactly")


def main():
    test_project_root_staging_respects_gitignore()
    test_publish_backup_restores_ignored_files()
    print("ALL_PUBLISH_TRANSACTION_TESTS=PASS")


if __name__ == "__main__":
    main()

def test_precommit_failure_rolls_back_repository():
    original_base_dir = task_manager.BASE_DIR
    original_task_dir = task_manager.TASK_DIR
    original_snapshot_dir = task_manager.SNAPSHOT_DIR
    original_live_check = task_manager.check_live_matches_after

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        remote = root / "remote.git"
        repo = root / "repo"
        agent = root / "agent"
        source = root / "source"
        project_name = "rollback-project"
        task_id = "20260928-020000-ABC123"

        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )

        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "remote", "add", "origin", str(remote))

        (repo / "README.md").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "baseline")
        git(repo, "push", "-u", "origin", "main")

        baseline_head = git(
            repo,
            "rev-parse",
            "HEAD",
        ).stdout.strip()

        source.mkdir()
        (source / ".gitignore").write_text(
            ".test-runtime/\n",
            encoding="utf-8",
        )
        (source / "app.py").write_text(
            "print('release')\n",
            encoding="utf-8",
        )
        runtime = source / ".test-runtime"
        runtime.mkdir()
        (runtime / "state.txt").write_text(
            "runtime-only\n",
            encoding="utf-8",
        )

        task_manager.BASE_DIR = agent
        task_manager.TASK_DIR = agent / "data" / "tasks"
        task_manager.SNAPSHOT_DIR = agent / "data" / "snapshots"

        task = {
            "task_id": task_id,
            "user_id": 1,
            "project": project_name,
            "prompt": "transaction regression",
            "status": "success",
            "created_at": task_manager.now_text(),
            "started_at": task_manager.now_text(),
            "finished_at": task_manager.now_text(),
            "result": "ok",
            "error": None,
            "snapshot_before": False,
            "snapshot_after": False,
            "approval_status": "pending",
            "approved_at": None,
            "rejected_at": None,
            "git_commit": None,
        }

        try:
            task_manager.save_task(task)

            before_source = root / "before"
            before_source.mkdir()

            task_manager.create_snapshot(
                task_id,
                before_source,
                "before",
            )
            task_manager.create_snapshot(
                task_id,
                source,
                "after",
            )

            task_manager.check_live_matches_after = (
                lambda *args, **kwargs: {
                    "safe": True,
                    "conflicts": [],
                }
            )

            hooks = repo / ".git" / "hooks"
            hook = hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nexit 1\n",
                encoding="utf-8",
            )
            os.chmod(hook, 0o755)

            try:
                task_manager.publish_task_to_git(
                    task_id,
                    repo_root=repo,
                )
            except RuntimeError as exc:
                assert (
                    "rolled back" in str(exc)
                ), str(exc)
            else:
                raise AssertionError(
                    "Expected publish failure"
                )

            current_head = git(
                repo,
                "rev-parse",
                "HEAD",
            ).stdout.strip()

            assert current_head == baseline_head
            assert git(
                repo,
                "status",
                "--porcelain",
            ).stdout.strip() == ""

            assert not (
                repo
                / "apps"
                / project_name
            ).exists()

            saved_task = task_manager.load_task(
                task_id
            )

            assert (
                saved_task["approval_status"]
                == "pending"
            )
            assert not saved_task.get(
                "publish_state"
            )
            assert not saved_task.get(
                "git_commit"
            )

            backup_root = (
                agent
                / "data"
                / "publish-backups"
            )

            if backup_root.exists():
                assert not any(
                    backup_root.iterdir()
                )

        finally:
            task_manager.check_live_matches_after = original_live_check
            task_manager.BASE_DIR = original_base_dir
            task_manager.TASK_DIR = original_task_dir
            task_manager.SNAPSHOT_DIR = original_snapshot_dir

    print("PASS TRANSACTION: pre-commit failure rolled back cleanly")


test_precommit_failure_rolls_back_repository()


def test_backup_cleanup_failure_does_not_break_publish():
    original_base_dir = task_manager.BASE_DIR
    original_task_dir = task_manager.TASK_DIR
    original_snapshot_dir = task_manager.SNAPSHOT_DIR
    original_live_check = task_manager.check_live_matches_after
    original_remove_backup = task_manager.remove_git_publish_backup

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        remote = root / "remote.git"
        repo = root / "repo"
        agent = root / "agent"
        source = root / "source"

        project_name = "cleanup-failure-project"
        task_id = "20260928-021000-DEF456"

        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )

        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "remote", "add", "origin", str(remote))

        (repo / "README.md").write_text(
            "baseline\n",
            encoding="utf-8",
        )

        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "baseline")
        git(repo, "push", "-u", "origin", "main")

        source.mkdir()

        (source / ".gitignore").write_text(
            ".test-runtime/\n",
            encoding="utf-8",
        )

        (source / "app.py").write_text(
            "print('release')\n",
            encoding="utf-8",
        )

        runtime = source / ".test-runtime"
        runtime.mkdir()

        (runtime / "state.txt").write_text(
            "runtime-only\n",
            encoding="utf-8",
        )

        task_manager.BASE_DIR = agent
        task_manager.TASK_DIR = agent / "data" / "tasks"
        task_manager.SNAPSHOT_DIR = agent / "data" / "snapshots"

        task = {
            "task_id": task_id,
            "user_id": 1,
            "project": project_name,
            "prompt": "backup cleanup regression",
            "status": "success",
            "created_at": task_manager.now_text(),
            "started_at": task_manager.now_text(),
            "finished_at": task_manager.now_text(),
            "result": "ok",
            "error": None,
            "snapshot_before": False,
            "snapshot_after": False,
            "approval_status": "pending",
            "approved_at": None,
            "rejected_at": None,
            "git_commit": None,
        }

        try:
            task_manager.save_task(task)

            before_source = root / "before"
            before_source.mkdir()

            task_manager.create_snapshot(
                task_id,
                before_source,
                "before",
            )

            task_manager.create_snapshot(
                task_id,
                source,
                "after",
            )

            task_manager.check_live_matches_after = (
                lambda *args, **kwargs: {
                    "safe": True,
                    "conflicts": [],
                }
            )

            cleanup_calls = []

            def fail_backup_cleanup(backup):
                cleanup_calls.append(backup)
                raise OSError(
                    "synthetic backup cleanup failure"
                )

            task_manager.remove_git_publish_backup = (
                fail_backup_cleanup
            )

            result = task_manager.publish_task_to_git(
                task_id,
                repo_root=repo,
            )

            assert result["approved"] is True
            assert result["retry"] is False

            commit_sha = result["commit"]

            assert git(
                repo,
                "rev-parse",
                "HEAD",
            ).stdout.strip() == commit_sha

            git(
                repo,
                "fetch",
                "origin",
                "main",
            )

            assert git(
                repo,
                "rev-parse",
                "origin/main",
            ).stdout.strip() == commit_sha

            saved_task = task_manager.load_task(
                task_id
            )

            assert (
                saved_task["approval_status"]
                == "approved"
            )
            assert (
                saved_task["git_commit"]
                == commit_sha
            )

            assert cleanup_calls

            tracked = git(
                repo,
                "ls-files",
                f"apps/{project_name}",
            ).stdout.splitlines()

            assert (
                f"apps/{project_name}/app.py"
                in tracked
            )
            assert (
                f"apps/{project_name}/.gitignore"
                in tracked
            )
            assert not any(
                ".test-runtime" in item
                for item in tracked
            )

        finally:
            task_manager.remove_git_publish_backup = (
                original_remove_backup
            )
            task_manager.check_live_matches_after = (
                original_live_check
            )
            task_manager.BASE_DIR = original_base_dir
            task_manager.TASK_DIR = original_task_dir
            task_manager.SNAPSHOT_DIR = original_snapshot_dir

    print(
        "PASS POST-COMMIT: backup cleanup failure "
        "does not break publish"
    )


test_backup_cleanup_failure_does_not_break_publish()
