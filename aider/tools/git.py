from aider.repo import ANY_GIT_ERROR

from .base_tool import BaseAiderTool


class GitDiff(BaseAiderTool):
    """
    Show the diff between the current working directory and a git branch or commit.
    """

    name = "git_diff"
    description = "Show the diff between the current working directory and a git branch or commit."
    parameters = {
        "type": "object",
        "properties": {
            "branch": {
                "type": "string",
                "description": "The branch or commit hash to diff against. Defaults to HEAD.",
            },
        },
        "required": [],
    }

    def run(self, branch=None):
        if not self.coder.repo:
            return "Not in a git repository."

        try:
            if branch:
                diff = self.coder.repo.diff_commits(False, branch, "HEAD")
            else:
                diff = self.coder.repo.diff_commits(False, "HEAD", None)

            if not diff:
                return "No differences found."
            return diff
        except ANY_GIT_ERROR as e:
            self.coder.io.tool_error(f"Error running git diff: {e}")
            return f"Error running git diff: {e}"


class GitLog(BaseAiderTool):
    """
    Show the git log.
    """

    name = "git_log"
    description = "Show the git log."
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "The maximum number of commits to show. Defaults to 10.",
            },
        },
        "required": [],
    }

    def run(self, limit=10):
        if not self.coder.repo:
            return "Not in a git repository."

        try:
            commits = list(self.coder.repo.repo.iter_commits(max_count=limit))
            log_output = []
            for commit in commits:
                short_hash = commit.hexsha[:8]
                message = commit.message.strip().split("\n")[0]
                log_output.append(f"{short_hash} {message}")
            return "\n".join(log_output)
        except ANY_GIT_ERROR as e:
            self.coder.io.tool_error(f"Error running git log: {e}")
            return f"Error running git log: {e}"


class GitShow(BaseAiderTool):
    """
    Show various types of objects (blobs, trees, tags, and commits).
    """

    name = "git_show"
    description = "Show various types of objects (blobs, trees, tags, and commits)."
    parameters = {
        "type": "object",
        "properties": {
            "object": {
                "type": "string",
                "description": "The object to show. Defaults to HEAD.",
            },
        },
        "required": [],
    }

    def run(self, object="HEAD"):
        if not self.coder.repo:
            return "Not in a git repository."

        try:
            return self.coder.repo.repo.git.show(object)
        except ANY_GIT_ERROR as e:
            self.coder.io.tool_error(f"Error running git show: {e}")
            return f"Error running git show: {e}"


class GitStatus(BaseAiderTool):
    """
    Show the working tree status.
    """

    name = "git_status"
    description = "Show the working tree status."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def run(self):
        if not self.coder.repo:
            return "Not in a git repository."

        try:
            return self.coder.repo.repo.git.status()
        except ANY_GIT_ERROR as e:
            self.coder.io.tool_error(f"Error running git status: {e}")
            return f"Error running git status: {e}"


def _execute_git_diff(coder, branch=None):
    return GitDiff(coder).run(branch=branch)


def _execute_git_log(coder, limit=10):
    return GitLog(coder).run(limit=limit)


def _execute_git_show(coder, object="HEAD"):
    return GitShow(coder).run(object=object)


def _execute_git_status(coder):
    return GitStatus(coder).run()
