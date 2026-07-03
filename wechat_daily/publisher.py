"""Public repo management: clone, commit, preview, push."""

from __future__ import annotations

import subprocess
from pathlib import Path

import markdown as md_lib

from wechat_daily.config import PUBLIC_REPO_DIR, PUBLIC_REPO_URL, debug_dir_for


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def ensure_repo() -> Path:
    """Return the local public repo path, cloning if necessary.

    Raises RuntimeError if an existing repo has a mismatched remote URL,
    preventing silent commits to the wrong repository.
    """
    if (PUBLIC_REPO_DIR / ".git").exists():
        result = _run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=PUBLIC_REPO_DIR, check=False,
        )
        if result.returncode == 0:
            actual = result.stdout.strip()
            if actual != PUBLIC_REPO_URL:
                raise RuntimeError(
                    f"公开仓库 remote URL 不匹配！\n"
                    f"期望: {PUBLIC_REPO_URL}\n"
                    f"实际: {actual}\n"
                    f"请检查或删除 {PUBLIC_REPO_DIR}"
                )
        return PUBLIC_REPO_DIR

    PUBLIC_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    _run(['git', 'clone', PUBLIC_REPO_URL, str(PUBLIC_REPO_DIR)])
    return PUBLIC_REPO_DIR


def write_post(date_str: str, markdown: str) -> Path:
    """Write the public Markdown post into _posts/YYYY/MM/."""
    repo = ensure_repo()
    year, month, _ = date_str.split('-')
    post_dir = repo / '_posts' / year / month
    post_dir.mkdir(parents=True, exist_ok=True)
    post_path = post_dir / f"{date_str}-daily.md"
    post_path.write_text(markdown, encoding='utf-8')
    return post_path


def commit(date_str: str) -> bool:
    """Stage and commit the post for *date_str*.

    Returns True if a commit was made, False if nothing changed (idempotent).
    """
    repo = ensure_repo()
    year, month, _ = date_str.split('-')
    rel = f"_posts/{year}/{month}/{date_str}-daily.md"
    _run(['git', 'add', rel], cwd=repo)

    # Only commit if there are staged changes (handles re-runs on same date)
    diff = _run(['git', 'diff', '--cached', '--quiet'], cwd=repo, check=False)
    if diff.returncode == 0:
        return False  # nothing to commit

    _run(['git', 'commit', '-m', f"Add daily report for {date_str}"], cwd=repo)
    return True


def push_pending() -> bool:
    """Push any unpushed local commits to origin/main.

    Returns True if commits were pushed.
    Raises RuntimeError if git push fails.
    """
    repo = ensure_repo()

    # Check whether origin/main exists (first push scenario)
    remote_check = _run(
        ['git', 'rev-parse', '--verify', 'origin/main'],
        cwd=repo, check=False,
    )
    if remote_check.returncode != 0:
        # origin/main doesn't exist yet; check if we have any local commits
        head_check = _run(
            ['git', 'rev-parse', '--verify', 'HEAD'],
            cwd=repo, check=False,
        )
        if head_check.returncode != 0:
            return False  # empty repo, nothing to push
        result = _run(
            ['git', 'push', '-u', 'origin', 'main'],
            cwd=repo, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git push 失败:\n{result.stderr}")
        return True

    # Check for unpushed commits
    count_result = _run(
        ['git', 'rev-list', '--count', 'origin/main..HEAD'],
        cwd=repo, check=False,
    )
    if count_result.returncode != 0:
        raise RuntimeError(f"无法检查待推送 commit:\n{count_result.stderr}")
    if count_result.stdout.strip() == '0':
        return False

    push_result = _run(['git', 'push', 'origin', 'main'], cwd=repo, check=False)
    if push_result.returncode != 0:
        raise RuntimeError(f"git push 失败:\n{push_result.stderr}")
    return True


def preview(date_str: str, markdown: str, open_browser: bool = True) -> Path:
    """Generate a standalone HTML preview. Opens browser unless open_browser=False."""
    import subprocess as sp
    debug_day = debug_dir_for(date_str)
    debug_day.mkdir(exist_ok=True, parents=True)
    html_body = md_lib.markdown(
        markdown,
        extensions=["tables", "fenced_code", "toc"],
    )
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: sans-serif; max-width: 900px; margin: 40px auto; line-height: 1.7; }}
  blockquote {{ background: #f0f5ff; border-left: 4px solid #93c5fd; padding: 8px 16px; }}
  code {{ background: #f0f0f0; padding: 2px 4px; border-radius: 3px; }}
  pre code {{ display: block; padding: 12px; overflow-x: auto; }}
  .mention {{ color: #1a56db; font-weight: 600; text-decoration: none;
              white-space: nowrap; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    out = debug_day / "preview.html"
    out.write_text(full_html, encoding='utf-8')
    if open_browser:
        sp.run(['open', str(out)], check=False)
    return out
