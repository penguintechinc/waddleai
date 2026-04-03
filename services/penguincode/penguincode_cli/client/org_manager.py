"""GitHub organization repo manager.

Clones or refreshes repos listed in provisioning config.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_REPOS_DIR = Path.home() / ".penguincode" / "repos"


def setup_github_orgs(orgs: list[dict]) -> None:
    """Clone or refresh repos for each configured GitHub organisation."""
    if not orgs:
        return

    gh = shutil.which("gh")
    git = shutil.which("git")
    if not gh and not git:
        logger.warning("Neither 'gh' nor 'git' found; skipping org repo setup")
        return

    _REPOS_DIR.mkdir(parents=True, exist_ok=True)

    for org_cfg in orgs:
        org = org_cfg.get("org", "")
        repos = org_cfg.get("default_repos", [])
        token_env = org_cfg.get("token_env", "GITHUB_TOKEN")

        if not org or not repos:
            continue

        org_dir = _REPOS_DIR / org
        org_dir.mkdir(parents=True, exist_ok=True)

        for repo in repos:
            repo_dir = org_dir / repo
            if repo_dir.exists():
                # Refresh
                logger.info("Refreshing %s/%s", org, repo)
                try:
                    subprocess.run(
                        [git or "git", "pull", "--ff-only"],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=60,
                    )
                except Exception as e:
                    logger.warning("Failed to refresh %s/%s: %s", org, repo, e)
            else:
                # Clone
                logger.info("Cloning %s/%s", org, repo)
                clone_url = f"https://github.com/{org}/{repo}.git"
                try:
                    cmd = [gh or git or "git"]
                    if gh and os.environ.get(token_env):
                        cmd = [gh, "repo", "clone", f"{org}/{repo}", str(repo_dir)]
                    else:
                        cmd = [git or "git", "clone", clone_url, str(repo_dir)]
                    subprocess.run(cmd, capture_output=True, timeout=120)
                except Exception as e:
                    logger.warning("Failed to clone %s/%s: %s", org, repo, e)
