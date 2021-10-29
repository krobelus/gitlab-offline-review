#!/usr/bin/env python3

import argparse
import json
import os
import re
import requests
import shlex
import shutil
import subprocess
import sys
import time
import urllib
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from subprocess import PIPE

import git
from git import GitCommandError


class OptionalDependency:
    pass


try:
    import unidiff
except ModuleNotFoundError:
    unidiff = OptionalDependency()

GLDIR = "gl"

USAGE = f"""\
Text-file based interface to GitLab.
All files are created in "./{GLDIR}".
"""

DRY_RUN = "N" in os.environ
DIFF_CONTEXT_LINES = 5
MARKER = "𑁍"
GITLAB_USER = os.environ.get("GITLAB_USER")

THE_REPOSITORY = git.Repo(search_parent_directories=True)
WORKING_TREE = THE_REPOSITORY.working_tree_dir
if "GIT_WORKTREE" in os.environ:
    WORKING_TREE = os.environ["GIT_WORKTREE"]
DIR = Path(WORKING_TREE) / GLDIR
DIR.mkdir(exist_ok=True)

# Try to guess the remote that, so we know which GitLab instance to talk to.


def guess_remote():
    for git_remote in THE_REPOSITORY.remotes:
        url = next(git_remote.urls)
        word_char = r"[\w.-]"
        remote = re.match(
            r"^(?P<Protocol>\w+://)?(?P<User>"
            + word_char
            + r"+@)?(?P<Host>[\w.-]+)"
            + r"(:(?P<Port>\d+))?[/:](?P<Project>"
            + word_char
            + r"+/"
            + word_char
            + r"+?)(\.git)?$",
            url,
        )
        if remote:
            return remote, git_remote.name
    return None, None


class UserError(Exception):
    pass


REMOTE, REMOTE_NAME = guess_remote()
if REMOTE is None:
    raise UserError("Missing remote, don't know how to talk to GitLab")
GITLAB = REMOTE["Host"]
GITHUB = GITLAB == "github.com"
GITLAB_PROJECT = REMOTE["Project"]
PROTOCOL = "http" if GITLAB == "localhost" else "https"

GITLAB_PROJECT_ESC = urllib.parse.quote(GITLAB_PROJECT, safe="")
TOKEN = None


def token():
    global TOKEN
    if TOKEN is not None:
        return TOKEN
    TOKEN = os.environ.get("GITHUB_TOKEN" if GITHUB else "GITLAB_TOKEN")
    if TOKEN is None:
        TOKEN = (subprocess.run(("pass", f"{GITLAB}/token"),
                                check=True,
                                stdout=PIPE).stdout.decode().strip())
    return TOKEN


MERGE_REQUESTS = "pulls" if GITHUB else "merge_requests"
DISCUSSIONS = "comments" if GITHUB else "discussions"
ISSUE_DESCRIPTION = "body" if GITHUB else "description"
ISSUE_ID = "number" if GITHUB else "iid"
MERGE_REQUEST_SOURCE_BRANCH = "head" if GITHUB else "source_branch"
MERGE_REQUEST_TARGET_BRANCH = "base" if GITHUB else "target_branch"


@lru_cache(maxsize=None)
def diff(base, head, context=None) -> unidiff.PatchSet:
    context = 123123123 if context is None else context
    uni_diff_text = THE_REPOSITORY.git.diff(base, head,
                                            f"-U{context}").encode()
    return unidiff.PatchSet.from_string(uni_diff_text, encoding="utf-8")


@lru_cache(maxsize=None)
def diff_for_newfile(base, head, path, context=None) -> unidiff.PatchedFile:
    files = diff(base, head, context=context)
    for f in files:
        name = f.path
        if f.is_rename:
            name = f.target_file
            assert name.startswith("b/")
            name = name[2:]
        if name == path:
            return f
    raise StopIteration


@lru_cache(maxsize=None)
def load_users():
    if not (DIR / "users.json").is_file():
        gather_users()
    with open(DIR / "users.json") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def lookup_user(*, name=None, username=None):
    for attempt in ("hit", "miss"):
        if attempt == "miss":
            gather_users()
            with open(DIR / "users.json") as f:
                us = json.load(f)
        else:
            us = load_users()
        for user in us:
            if name is not None and user["name"] == name:
                return user
            if username is not None and user["username"] == username:
                return user
    return {
        "username": "unknown user",
        "id": "unknown",
    }


@lru_cache(maxsize=None)
def load_milestones():
    if not (DIR / "milestones.json").is_file():
        fetch_milestones()
    with open(DIR / "milestones.json") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def milestone_title_to_id(milestone_title):
    for attempt in ("hit", "miss"):
        if attempt == "miss":
            if not DRY_RUN:
                fetch_milestones()
            with open(DIR / "milestones.json") as f:
                ms = json.load(f)
        else:
            ms = load_milestones()
        for milestone in ms:
            if milestone["title"] == milestone_title:
                return milestone["id"]
    return [0]


@lru_cache(maxsize=None)
def load_labels():
    if not (DIR / "labels.json").is_file():
        fetch_labels()
    with open(DIR / "labels.json") as f:
        return json.load(f)


def fetch_commit(ref):
    try:
        THE_REPOSITORY.commit(ref)
    except ValueError:
        THE_REPOSITORY.git().execute(("git", "fetch", REMOTE_NAME, ref))


def diff_context(base, head, old_line, new_path, new_line):
    fetch_commit(base)
    fetch_commit(head)
    try:
        hunk = diff_for_newfile(base, head, new_path)[0]
    except GitCommandError:
        return f" ? missing commits {base} or {head}\n"
    except UnicodeEncodeError:
        return f" ? UnicodeEncodeError parsing 'git diff {base} {head}'\n"
    except StopIteration:
        return " ? no file '{new_path}' in {base}..{head}\n"
    if new_line is not None:
        line = new_line
        rows = hunk.target
    else:
        line = old_line
        rows = hunk.source
    the_context = "".join(rows[max(0, line - DIFF_CONTEXT_LINES):line])
    if not the_context.endswith("\n"):
        the_context += "\n"
    return the_context


def isissue(branch_or_issue):
    return isinstance(branch_or_issue, int)


def mrdir_branch(mrdir):
    """
    input:  /path/to/repo/gl/my-branch
    output: my-branch
    """
    return Path(mrdir).name


def branch_mrdir(branch):
    """
    input:  my-branch
    output: /path/to/repo/gl/my-branch
    """
    return DIR / branch


def branch_name(merge_request, fqn=False):
    """
    input:  {"source_branch": "feature-branch"} OR {"head":{"ref": "feature-branch"}, ...}
    output: my-branch
    """
    branch = merge_request[MERGE_REQUEST_SOURCE_BRANCH]
    if GITHUB:
        if isinstance(branch, str):
            if fqn:
                return branch
            return branch.split(":")[1]
        else:
            pr_number = merge_request["number"]
            return f"pr-{pr_number}"
    else:
        return branch


def target_branch_name(merge_request):
    """
    input:  {"target_branch": "master"} OR {"base":{"ref": "master"}, ...}
    output: "master"
    """
    branch = merge_request[MERGE_REQUEST_TARGET_BRANCH]
    if GITHUB:
        if isinstance(branch, str):
            return branch
        else:
            return branch["ref"]
    else:
        return branch


def issue_dir(issue_id):
    """
    input:  123
    output: /path/to/repo/gl/i/123
    """
    return DIR / "i" / str(issue_id)


def gitlab_request(method, path, **kwargs):
    """
    Send a request to the GitLab API and return the response.
    """
    r = None
    if path.startswith(f"{PROTOCOL}://"):
        url = path  # Absolute URL - use it.
    else:
        # Assume the path is relative to a "project" API.
        if GITHUB:
            url = f"{PROTOCOL}://api.{GITLAB}/repos/{GITLAB_PROJECT}/" + path
        else:
            url = f"{PROTOCOL}://{GITLAB}/api/v4/projects/{GITLAB_PROJECT_ESC}/" + path
    if GITHUB:
        curl_headers = "-H\"Authorization: token $GITHUB_TOKEN\""
        headers = {
            "Authorization": "token " + token(),
            # "Accept": "application/vnd.github.v3+json",
        }
    else:
        curl_headers = "-HPRIVATE-TOKEN:\\ $GITLAB_TOKEN"
        headers = {"PRIVATE-TOKEN": token()}
    if "data" in kwargs:
        curl_headers += " -HContent-Type:\\ application/json"
        if GITHUB:
            headers.update({"Content-Type": "application/json"})
    trace = f"curl {curl_headers} -X{method.upper()} '{url}'"
    if "data" in kwargs:
        data = json.dumps(kwargs["data"], indent=2).replace('"', '\\"')
        trace += f' --data "{data}"'
        if GITHUB:
            kwargs["json"] = kwargs["data"]
            del kwargs["data"]
    print(trace, file=sys.stderr)
    if not DRY_RUN:
        r = requests.request(method, url, headers=headers, **kwargs)
        if not r.ok:
            print(r, r.reason, file=sys.stderr)
            print(r.content.decode(), file=sys.stderr)
        assert r.ok, f"HTTP {method} failed: {url}"
    return r


def delete(path, **kwargs):
    "Send an HTTP DELETE request to GitLab"
    return gitlab_request("delete", path, **kwargs)


def get(path, per_page=100, all_pages=True, **kwargs):
    "Send an HTTP GET request to GitLab"
    ppath = path
    if per_page:
        if "?" in path:
            ppath = path + f"&per_page={per_page}"
        else:
            ppath = path + f"?per_page={per_page}"
    r = gitlab_request("get", ppath, **kwargs)
    if r is None:
        return
    data = r.json()
    next_page = r.headers.get("X-Next-Page")
    if all_pages:
        while next_page:
            r = gitlab_request("get", ppath + f"&page={next_page}")
            next_page = r.headers.get("X-Next-Page")
            data += r.json()
    return data


def post(path, **kwargs):
    "Send an HTTP POST request to GitLab"
    return gitlab_request("post", path, **kwargs)


def patch(path, **kwargs):
    "Send an HTTP PATCH request to GitLab"
    return gitlab_request("patch", path, **kwargs)


def put(path, **kwargs):
    "Send an HTTP PUT request to GitLab"
    return gitlab_request("put", path, **kwargs)


def load_discussions(mrdir):
    try:
        with open(mrdir / f"{DISCUSSIONS}.json") as f:
            return json.load(f)
    except FileNotFoundError:  # We never fetched (dry run?).
        return []


def fetch_global(what):
    if GITHUB:
        doc = get(f"{what}?state=open")
    else:
        doc = get(f"{what}?state=opened&scope=all")
    if doc is not None:
        (DIR / f"{what}.json").write_text(json.dumps(doc, indent=1))
    else:
        doc = load_global(what)
    return doc


def load_global(what):
    """
    Load data that is not specific to a single issue/MR.
    For example users, milestones, issue list or MR list.
    """
    path = DIR / f"{what}.json"
    if not path.exists():
        return fetch_global(what)
    with open(path) as f:
        return json.load(f)


def load_issues_and_merge_requests():
    return load_global("issues"), load_global(MERGE_REQUESTS)


def update_global(what, thing, fetch_first=True):
    """
    Edit the list of MRs/issues in-place, updating the given issue/MR.
    """
    if fetch_first:
        if DRY_RUN:
            return thing
        newthing = gitlab_request("get",
                                  f"{what}/{thing[ISSUE_ID]}").json()
    path = DIR / f"{what}.json"
    old = json.loads(path.read_bytes())
    if fetch_first:
        new = []
        added = False
        for t in old:
            if fetch_first:
                if t[ISSUE_ID] == newthing[ISSUE_ID]:
                    new += [newthing]
                    added = True
                    continue
            new += [t]
        if not added:
            new += [newthing]
    else:
        newthing = thing
        new = old + [thing]
    path.write_text(json.dumps(new, indent=1))
    return newthing


def lazy_fetch_merge_request(*, branch=None, iid=None):
    for attempt in ("hit", "miss"):
        try:
            if attempt == "hit":
                if GITHUB:
                    # need to update head["SHA"]
                    merge_requests = fetch_global(MERGE_REQUESTS)
                else:
                    merge_requests = load_global(MERGE_REQUESTS)
            if branch is not None:
                return next(
                    (mr for mr in merge_requests if branch_name(mr) == branch))
            elif iid is not None:
                return next(mr for mr in merge_requests
                            if mr[ISSUE_ID] == iid)
            assert False
        except StopIteration as e:
            if attempt == "miss" or GITHUB:
                print(f"no merge request branch={branch} iid={iid}",
                      file=sys.stderr)
                raise e
            merge_requests = fetch_global(MERGE_REQUESTS)


def discussion_notes(discussion):
    return [discussion] if GITHUB else discussion["notes"]


def show_discussion(discussions):
    out = ""
    for discussion in discussions:
        notes = discussion_notes(discussion)
        n0 = notes[0]
        if "position" not in n0:
            location = ""
        else:
            if GITHUB:
                head = n0["commit_id"]
                fetch_commit(head)
                base = f"{head}~"
                old_path = new_path = n0["path"]
                old_line = n0["original_position"]
                new_line = n0["position"]
            else:
                pos = n0["position"]
                base = pos["base_sha"]
                head = pos["head_sha"]
                old_path = pos["old_path"]
                old_line = pos["old_line"]
                new_path = pos["new_path"]
                new_line = pos["new_line"]
            path = new_path if new_path else old_path
            line = new_line if new_line else old_line
            location = f"{path}:{line}: "
        commit_human = ""
        if "commit_id" in n0:
            try:
                commit_human = " "
                commit_human += THE_REPOSITORY.git().log(
                    "--format=%h %s", "-1", f"{n0['commit_id']}")
                commit_human += "\n"
            except GitCommandError:
                commit_human = ""
        discussion_id = '0000000000000000000000000000000000000000' if GITHUB else discussion[
            "id"]
        if GITHUB:
            if location:
                out += f"{location}{discussion_id}\n"
        else:
            out += f"{location}{discussion_id}\n"
        out += commit_human
        if "position" in n0:
            out += diff_context(base, head, old_line, new_path, new_line)
        for note in notes:
            lines = note["body"].splitlines()
            author = note["user"]["login"] if GITHUB else note["author"][
                "username"]
            if author == GITLAB_USER:
                author = note["id"]
            text = f'[{author}] {note["body"]}'
            lines = text.splitlines()
            text = "\t" + lines[0] + \
                "".join("\n\t\t" + line for line in lines[1:])
            out += text + "\n"
        out += "\n"
    return out


def cmd_fetch(branches_and_issues):
    branches_and_issues = [parse_path(p)[0] for p in branches_and_issues]
    fetch(branches_and_issues)


def fetch(branches_and_issues):
    issues, merge_requests = None, None
    have_branches = set()
    try:
        issues, merge_requests = load_issues_and_merge_requests()
        have_branches = set(branch_name(mr) for mr in merge_requests)
    except FileNotFoundError:
        pass
    fetched_all_issues = False
    # If we already fetched the list of issues before, and are given an
    # explicit list of issues, then only fetch those.
    if (DIR / "issues.json").exists() and branches_and_issues:
        issues = [{
            ISSUE_ID: i
        } for i in branches_and_issues if isissue(i)]
    else:
        #  Fetch all open issues (this takes some time).
        issues = fetch_global("issues")
        fetched_all_issues = True
    want_branches = set(branch for branch in branches_and_issues
                        if not isissue(branch))
    if merge_requests is None or not want_branches.issubset(have_branches):
        merge_requests = fetch_global(MERGE_REQUESTS)
    for merge_request in merge_requests:
        if branch_name(merge_request) not in want_branches:
            continue
        merge_request = update_global(MERGE_REQUESTS, merge_request)
        fetch_mr_data(merge_request)
    for issue in issues:
        if not fetched_all_issues:
            issue = update_global("issues", issue)
        fetch_issue_data(issue)


def cmd_create():
    "Create an issue/MR"
    rows = sys.stdin.read().splitlines()
    for r in rows:
        print(r)
    _, data = parse_metadata_header(rows, None)
    if MERGE_REQUEST_SOURCE_BRANCH in data:
        what = MERGE_REQUESTS
    else:
        what = "issues"
    thing = post(what, data=data).json()
    update_global(what, thing, fetch_first=False)
    if MERGE_REQUEST_SOURCE_BRANCH in data:
        fetch_mr_data(thing)
    else:
        fetch_issue_data(thing)
    print()
    if GITHUB:
        print(thing["html_url"])
    else:
        print(thing["web_url"])


def issue_template_callback(data, branch, issue_id):
    del data, branch, issue_id


def cmd_template(branch=None, issue_id=None):
    data = {
        "title": "title",
        ISSUE_DESCRIPTION: "description\n",
        "assignees": (),
        "milestone": None,
        "labels": (),
    }
    target_branch = os.environ.get("GITLAB_TARGET_BRANCH", "master")
    if branch is not None:
        data[ISSUE_DESCRIPTION] = ""
        if branch is None:
            branch = MERGE_REQUEST_SOURCE_BRANCH
        else:
            shortlog = THE_REPOSITORY.git().log(
                "--format=%h %s", f"{REMOTE_NAME}/{target_branch}..{branch}")
            if shortlog.count("\n") == 0:
                data["title"] = shortlog.split(" ", maxsplit=1)[1].strip()
            else:
                data[ISSUE_DESCRIPTION] += shortlog
                data[ISSUE_DESCRIPTION] += "\n"
        if not GITHUB:
            data["assignees"] = (lookup_user(username=GITLAB_USER), )
            data["reviewers"] = ()
        data[
            MERGE_REQUEST_SOURCE_BRANCH] = f"{GITLAB_USER}:{branch}" if GITHUB else branch
        data[MERGE_REQUEST_TARGET_BRANCH] = target_branch
        data["remove_source_branch"] = True
        issue_template_callback(data, branch, issue_id)
    print(metadata_header(data).rstrip("\n"))


def dos2unix(s: str):
    return s.replace('\r\n', '\n')


def metadata_header(thing):
    extra = ""
    if MERGE_REQUEST_SOURCE_BRANCH in thing:
        extra += f"{MARKER} {MERGE_REQUEST_SOURCE_BRANCH}: {branch_name(thing, fqn=True)}\n"
        extra += f"{MARKER} {MERGE_REQUEST_TARGET_BRANCH}: {target_branch_name(thing)}\n"
        if not GITHUB:
            extra += (f"{MARKER} reviewers: "
                      + ",".join(a["username"]
                                 for a in thing["reviewers"]) + "\n")
        if "remove_source_branch" in thing:
            extra += f"{MARKER} remove_source_branch: {thing['remove_source_branch']}\n"
    if GITHUB:
        labels = ""
        assignees = ""
    else:
        labels = f"{MARKER}    labels: " + ",".join(thing["labels"]) + "\n"
        assignees = f"{MARKER} assignees: " + ",".join(
            a["username"] for a in thing["assignees"]) + "\n"
    return (f'{thing["title"]}\n\n'
            + ("\n" if thing[ISSUE_DESCRIPTION] is None else
               dos2unix(f'{thing[ISSUE_DESCRIPTION]}\n')) + extra
            + assignees + f"{MARKER} milestone: "
            + (thing["milestone"]["title"] if thing["milestone"] else "")
            + "\n" + labels + "\n")


def fetch_issue_data(issue):
    issue_id = issue[ISSUE_ID]
    discussions = get(f"issues/{issue_id}/{DISCUSSIONS}")
    idir = issue_dir(issue_id)
    idir.mkdir(exist_ok=True, parents=True)
    if discussions is not None:
        (idir / f"{DISCUSSIONS}.json").write_text(
            json.dumps(discussions, indent=1))
    else:
        discussions = load_discussions(idir)
    comments = metadata_header(issue) + show_discussion(
        discussions) + f"{MARKER}\n"
    comments_path = idir / "comments.gl"
    pristine_path = idir / "pristine-comments.gl"
    if comments_path.exists():
        new_pristine_path = idir / "new-pristine-comments.gl"
        new_pristine_path.write_text(comments)
        subprocess.run(
            ("git", "merge-file", comments_path, pristine_path,
             new_pristine_path),
            check=False,
        )
        new_pristine_path.rename(pristine_path)
    else:
        comments_path.write_text(comments)
        pristine_path.write_text(comments)


def fetch_mr_data(merge_request):
    branch = branch_name(merge_request)
    mrdir = branch_mrdir(branch)
    mrdir.mkdir(exist_ok=True, parents=True)
    discussions = get(
        f"{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/{DISCUSSIONS}"
    )
    if DRY_RUN:
        return
    if GITHUB:
        unresolvable = get(
            f"issues/{merge_request[ISSUE_ID]}/{DISCUSSIONS}")
        todo = discussions
        discussions = unresolvable + todo
    if discussions is not None:
        (mrdir / f"{DISCUSSIONS}.json").write_text(
            json.dumps(discussions, indent=1))
    else:
        discussions = load_discussions(mrdir)
    if GITHUB:
        resolved = []  # TODO
    else:
        unresolvable = (d for d in discussions
                        if "resolved" not in d["notes"][0])
        todo = (
            d for d in discussions
            if "resolved" in d["notes"][0] and not d["notes"][0]["resolved"])
        resolved = (
            d for d in discussions
            if "resolved" in d["notes"][0] and d["notes"][0]["resolved"])
    todo = show_discussion(todo) + f"{MARKER}\n"
    pristine_path = mrdir / "pristine-todo.gl"
    todo_path = mrdir / "todo.gl"
    if todo_path.exists():
        new_pristine_path = mrdir / "new-pristine-todo.gl"
        new_pristine_path.write_text(todo)
        subprocess.run(
            ("git", "merge-file", todo_path, pristine_path, new_pristine_path),
            check=False,
        )
        new_pristine_path.rename(pristine_path)
    else:
        todo_path.write_text(todo)
        pristine_path.write_text(todo)
    (mrdir / "resolved.gl").write_text(show_discussion(resolved))
    (mrdir / "meta.gl").write_text(
        metadata_header(merge_request) + show_discussion(unresolvable))


def cmd_submit(branches_and_issues):
    branches_and_issues = [parse_path(p)[0] for p in branches_and_issues]
    issues, merge_requests = load_issues_and_merge_requests()
    if not branches_and_issues:
        branches_and_issues = [branch_name(mr) for mr in merge_requests
                               ] + [issue[ISSUE_ID] for issue in issues]
    for branch_or_issue in branches_and_issues:
        if isissue(branch_or_issue):
            idir = issue_dir(branch_or_issue)
            try:
                issue = next(i for i in issues
                             if i[ISSUE_ID] == branch_or_issue)
            except StopIteration:
                assert idir.is_dir()
                print(f"issue not open, removing {idir}", file=sys.stderr)
                if not DRY_RUN:
                    shutil.rmtree(idir)
                continue
            changed, issue_changed = submit_issue_data(issue)
            if issue_changed:
                issue = update_global("issues", issue)
            if changed:
                fetch_issue_data(issue)
            continue
        mrdir = branch_mrdir(branch_or_issue)
        try:
            merge_request = next(mr for mr in merge_requests
                                 if branch_name(mr) == branch_or_issue)
        except StopIteration:
            assert mrdir.is_dir()
            print(f"MR not open, removing {mrdir}", file=sys.stderr)
            if not DRY_RUN:
                shutil.rmtree(mrdir)
            continue
        changed, mr_changed = submit_mr_data(merge_request)
        if mr_changed:
            merge_request = update_global(MERGE_REQUESTS,
                                          merge_request)
        if changed:
            fetch_mr_data(merge_request)


def parse_metadata_header(rows, thing):
    data = {}
    description = []
    state = "DESCRIPTION"
    j = -1
    while j < len(rows):
        j += 1
        if j == len(rows):
            break
        row = rows[j]
        if j == 0:
            if thing is None or row != thing["title"]:
                data["title"] = row
            continue
        if j == 1:
            assert row == ""
            continue
        if state == "DESCRIPTION":
            if row.startswith(MARKER):
                state = "META"
                j -= 1
            else:
                description += [row]
            continue
        consumed = False
        for optional_field in (MERGE_REQUEST_SOURCE_BRANCH,
                               MERGE_REQUEST_TARGET_BRANCH, "state_event"):
            prefix = f"{MARKER} {optional_field}:"
            if row.startswith(prefix):
                if thing is None or optional_field in "state_event":
                    data[f"{optional_field}"] = row[len(prefix):].lstrip()
                consumed = True
        if consumed:
            continue
        prefix = f"{MARKER} assignees:"
        if row.startswith(prefix):
            assignees = row[len(prefix):].lstrip()
            if thing is None or assignees != ",".join(
                    a["username"] for a in thing["assignees"]):
                if not assignees:
                    data["assignee_ids"] = [0]
                else:
                    data["assignee_ids"] = [
                        lookup_user(username=a)["id"]
                        for a in assignees.split(",")
                    ]
            continue
        # TODO unclone
        prefix = f"{MARKER} reviewers:"
        if row.startswith(prefix):
            reviewers = row[len(prefix):].lstrip()
            if thing is None or reviewers != ",".join(
                    a["username"] for a in thing["reviewers"]):
                if not reviewers:
                    data["reviewer_ids"] = [0]
                else:
                    data["reviewer_ids"] = [
                        lookup_user(username=a)["id"]
                        for a in reviewers.split(",")
                    ]
            continue
        prefix = f"{MARKER} remove_source_branch:"
        if row.startswith(prefix):
            arg = bool(row[len(prefix):].lstrip())
            if thing is None or arg != thing.get("remove_source_branch"):
                data["remove_source_branch"] = arg
            continue
        prefix = f"{MARKER} milestone:"
        if row.startswith(prefix):
            milestone = row[len(prefix):].lstrip()
            if (thing is None or (bool(milestone) != bool(thing["milestone"]))
                    or (thing["milestone"] is not None
                        and milestone != thing["milestone"]["title"])):
                if not milestone:
                    data["milestone_id"] = [0]
                else:
                    data["milestone_id"] = milestone_title_to_id(milestone)
            continue
        prefix = f"{MARKER}    labels:"
        if row.startswith(prefix):
            labels = row[len(prefix):].lstrip()
            if thing is None or labels != ",".join(thing["labels"]):
                data["labels"] = labels
            continue
        break
    description = "\n".join(description)
    if thing is None or description != dos2unix(thing[ISSUE_DESCRIPTION]):
        data[ISSUE_DESCRIPTION] = description
    assert j == 0 or rows[j - 1].startswith(MARKER)
    assert j == len(rows) or not rows[j].startswith(MARKER)
    if GITHUB:
        if MERGE_REQUEST_SOURCE_BRANCH in data:
            data["maintainer_can_modify"] = True

        keys = {
            "reviewer_ids", "assignee_ids", "labels", "milestone_id",
            "remove_source_branch"
        }
        # del data["maintainer_can_modify"]
        # TODO set issue (numeric ID)
        for key in keys:
            if key in data:
                del data[key]
    return j, data


def submit_discussion(discussions, rows, merge_request=None, issue=None):
    thing = merge_request if merge_request is not None else issue
    what = MERGE_REQUESTS if merge_request is not None else "issues"
    what_id = thing[ISSUE_ID]
    comments = {}  # discussion ID => note ID => text
    # if GITHUB: # TODO
    #     did = 0
    #     comments[did] = {}
    new_comments = {}  # discussion ID => text
    new_discussions = []
    state = "CONTEXT"
    changed = False
    desc_changed = False
    note_id = None
    i, data = parse_metadata_header(rows, thing)
    if data and False:  # TODO
        if not DRY_RUN:  # TODO
            current = gitlab_request("get", f"{what}/{what_id}").json()
            for key in data:
                if (key.endswith("_id") or key.endswith("_ids")
                        or key.endswith("_event")):
                    continue
                if thing[key] != current[key]:
                    assert (
                        0
                    ), f"outdated {key} - have {thing[key]} but upstream has {current[key]}"
            put(
                f"{what}/{what_id}",
                data=data,
            )
            changed = True
            desc_changed = True
    for row in rows[i:]:
        if state == "NEW_DISCUSSION":
            if row == MARKER:
                new_discussions += [[]]
                continue
            if not new_discussions:
                new_discussions = [[]]
            new_discussions[len(new_discussions) - 1] += [row]
            continue
        if row.startswith(MARKER):
            state = "NEW_DISCUSSION"
            continue
        note_header = re.match(r"^\t\[(\d+)\] ", row)
        if note_header:
            note_id = int(note_header.group(1))
            if GITHUB:
                did = next(i for i, discussion in enumerate(discussions)
                           if discussion["id"] == note_id)
                comments[did] = {}
        location = re.match(r"^(?:[^:]+:\d+: )?([0-9a-f]{40})$", row)
        if location:
            if not GITHUB:
                note_id = None
                discussion_id = location.group(1)
                did, discussion = next(
                    (i, discussion) for i, discussion in enumerate(discussions)
                    if str(discussion["id"]) == discussion_id)
                n0 = discussion_notes(discussion)[0]
                what_id = n0["noteable_iid"]
                comments[did] = {}
            state = "CONTEXT"
            continue
        if merge_request is not None:
            resolve = re.match(r"^r$", row)
            if resolve:
                if not n0["resolved"]:
                    put(f'{what}/{what_id}/{DISCUSSIONS}/{discussion["id"]}?resolved=true'
                        )
                    changed = True
                continue
            unresolve = re.match(r"^u$", row)
            if unresolve:
                if n0["resolved"]:
                    put(f'{what}/{what_id}/{DISCUSSIONS}/{discussion["id"]}?resolved=false'
                        )
                    changed = False
                continue
            if re.match(r"^!merge$", row):
                put(f"{what}/{merge_request[ISSUE_ID]}/merge", data={
                    "merge_when_pipeline_succeeds": True,
                })
                changed = True
                continue
        if re.match(r"^!close$", row):
            patch(f"{what}/{what_id}", data={
                "state": "closed",
            })
            changed = True
            continue
        if re.match(r"^!delete$", row):
            if GITHUB:
                delete(f'{what}/{DISCUSSIONS}/{note_id}', data=data)
            else:
                delete(
                    f'{what}/{what_id}/{DISCUSSIONS}/{discussion["id"]}/notes/{note_id}'
                )
            changed = True
            continue
        if state == "CONTEXT":
            if row.startswith("\t"):
                state = "COMMENTS"
            elif re.match(r"^[ +-]", row):
                continue
        if state == "COMMENTS":
            if re.match(r"^\t\[\w+\]", row):
                note_id = None
            tag = re.match(r"^\t\[(\d+)\] (.*)", row)
            if tag:
                note_id = int(tag.group(1))
                row = "\t" + tag.group(2)
            if row == "":
                row = "\t"
            if row.startswith("\t"):
                row = row[1:]
                if row.startswith("\t"):
                    row = row[1:]  # subsequent_indent
                if note_id is not None:
                    if note_id not in comments[did]:
                        comments[did][note_id] = []
                    comments[did][note_id] += [row]
            else:
                state = "NEW_COMMENT"
                new_comments[did] = row
            continue
        if state == "NEW_COMMENT":
            new_comments[did] += "\n" + row
            continue
    for new_discussion in new_discussions:
        body = "\n".join(new_discussion)
        if not body.strip():
            continue
        post(
            f'issues/{what_id}/{DISCUSSIONS}'
            if GITHUB else f'{what}/{what_id}/{DISCUSSIONS}',
            data={
                "body": body,
            },
        )
        changed = True
    for did, comment in new_comments.items():
        discussion = discussions[did]
        data = {
            "body": comment,
        }
        if GITHUB:
            data["in_reply_to"] = discussion["id"]
        else:
            data["note_id"] = discussion["notes"][0]["id"],
        post(
            f'{what}/{what_id}/{DISCUSSIONS}' if GITHUB else
            f'{what}/{what_id}/{DISCUSSIONS}/{discussion["id"]}/notes',
            data=data,
        )
        changed = True
    for did, note_comments in comments.items():
        if not GITHUB:
            discussion = discussions[did]
        for note_id, comment in note_comments.items():
            if GITHUB:
                note = next((n for n in discussions if n["id"] == note_id))
            else:
                note = next(
                    (n for n in discussion["notes"] if n["id"] == note_id))
            # fix this?
            new_body = "\n".join(comment).strip()
            old_body = dos2unix(note["body"].strip())
            if new_body != old_body:
                data = {"body": new_body}
                if GITHUB:
                    if "commit_id" in note:
                        patch(f'pulls/{DISCUSSIONS}/{note_id}',
                              data=data)
                    else:
                        patch(f'issues/{DISCUSSIONS}/{note_id}',
                              data=data)
                else:
                    put(
                        f'{what}/{what_id}/{DISCUSSIONS}/{discussion["id"]}/notes/{note_id}',
                        data=data,
                    )
                changed = True
    return changed, desc_changed


def submit_mr_data(merge_request):
    branch = branch_name(merge_request)
    mrdir = branch_mrdir(branch)
    discussions = load_discussions(mrdir)
    try:
        contents = ""
        contents += (mrdir / "meta.gl").read_text()
        contents += (mrdir / "resolved.gl").read_bytes().decode()
        contents += (mrdir / "todo.gl").read_bytes().decode()
        rows = contents.splitlines()
    except FileNotFoundError:  # When there is no thread.
        rows = ()
    changed, desc_changed = submit_discussion(discussions,
                                              rows,
                                              merge_request=merge_request)
    pristine_path = mrdir / "pristine-todo.gl"
    todo_path = mrdir / "todo.gl"
    if not DRY_RUN:
        try:
            shutil.copy(pristine_path, todo_path)
        except FileNotFoundError:  # When there was no thread.
            pass
    if submit_review(merge_request, mrdir):
        changed = True
    return changed, desc_changed


def submit_issue_data(issue):
    issue_id = issue[ISSUE_ID]
    idir = issue_dir(issue_id)
    discussions = load_discussions(idir)
    try:
        contents = (idir / "comments.gl").read_bytes().decode()
        rows = contents.splitlines()
    except FileNotFoundError:  # When there is no thread.
        rows = ()
    changed, desc_changed = submit_discussion(discussions, rows, issue=issue)
    pristine_path = idir / "pristine-comments.gl"
    comments_path = idir / "comments.gl"
    if not DRY_RUN:
        try:
            shutil.copy(pristine_path, comments_path)
        except FileNotFoundError:  # When there was no thread.
            pass
    return changed, desc_changed


def cmd_discuss(branch, commit, old_file, new_file, line_type, old_line,
                new_line):
    "Draft a review comment."
    merge_request = lazy_fetch_merge_request(branch=branch)

    line_type = line_type[:1]
    assert line_type in " -+"
    old_line = int(old_line)
    new_line = int(new_line)
    file = new_file
    line = new_line
    if new_file == "/dev/null":
        file = old_file
        line = old_line

    if GITHUB:
        github_line = 0
        base = merge_request["base"]["sha"]
        head = merge_request["head"]["sha"]
        fetch_commit(base)
        fetch_commit(head)
        hunks = diff_for_newfile(base, head, file, context=3)
        done = False
        for hunk in hunks:
            for line in hunk:
                github_line += 1
                if line_type == "-":
                    if line.source_line_no == old_line:
                        done = True
                        break
                else:
                    if line.target_line_no == new_line:
                        done = True
                        break
            if done:
                break
            github_line += 1
        # GitHub wants the delta that was added by commits in the range commit..head before this line.
        try:
            hunks = diff_for_newfile(commit, head, file, context=3)
        except StopIteration:
            hunks = []
        done = False
        delta = 0
        for hunk in hunks:
            for line in hunk:
                # TODO
                if line.is_added:
                    delta += 1
                elif line.is_removed:
                    delta -= 1
                elif line.target_line_no is not None and line.target_line_no >= github_line:
                    done = True
                    break
            if line.target_line_no is not None and line.target_line_no >= github_line:
                break
            if done:
                break
        old_line = github_line + delta
    try:
        hunk = diff_for_newfile(f"{commit}~", commit, file)[0]
        rows = []
        for i, row in enumerate(hunk):
            if new_file == "/dev/null":
                if row.source_line_no is None:
                    continue
                if row.source_line_no >= old_line:
                    rows = hunk[:i + 1]
                    break
            else:
                if row.target_line_no is None:
                    continue
                if row.target_line_no >= new_line:
                    rows = hunk[:i + 1]
                    break
        context = "".join(r.line_type + r.value
                          for r in rows[-DIFF_CONTEXT_LINES:])
    except UnicodeEncodeError:
        context = f" ? UnicodeEncodeError {commit}\n"

    mrdir = branch_mrdir(branch_name(merge_request))
    mrdir.mkdir(exist_ok=True, parents=True)

    review = mrdir / "review.gl"
    with open(review, "a") as f:
        header = f"\n{MARKER} {commit} {file}:{new_line} {line_type} {old_line}\n"
        f.write(header)
        f.write(context + "\n")
    subprocess.run(
        os.environ["EDITOR"] + " +123123 " + shlex.quote(str(review)),
        shell=True,
        check=True,
    )


def submit_review(merge_request, mrdir):
    state = 0
    discussions = []
    review = mrdir / "review.gl"
    try:
        rows = review.read_bytes().decode().splitlines()
    except FileNotFoundError:  # No review.
        return
    for row in rows:
        r = re.match(r"^" + MARKER + r" (\S+) ([^:]+):(\d+) ([ +-]) (\d+)",
                     row)
        if r:
            commit, file, new_line, line_type, old_line = (
                r.group(1),
                r.group(2),
                r.group(3),
                r.group(4),
                r.group(5),
            )
            new_line = int(new_line)
            old_line = int(old_line)
            discussions += [[commit, file, line_type, old_line, new_line, ""]]
            state = 1
            continue
        elif state == 1:
            if re.match(r"^[ +-]", row):
                continue
            state = 2
        if state >= 2:
            if state == 2 and row == "":
                continue
            state = 3
            discussions[-1][-1] += row + "\n"

    # if GITHUB and len(discussions) > 0:
    if GITHUB:
        cancelreview(merge_request)
        comments = []
        for commit, file, line_type, old_line, new_line, body in discussions:
            comments += [{
                "body": body,
                # "commit_id": commit,
                "path": file,
                "position": old_line,
            }]
        data = {
            "body": " ",
            "event": "COMMENT",
            "comments": comments,
        }
        if comments:
            post(
                f'{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/reviews',
                data=data,
            )
    else:
        for commit, file, line_type, old_line, new_line, body in discussions:
            base_sha = THE_REPOSITORY.rev_parse(f"{commit}~")
            start_sha = base_sha
            head_sha = commit
            position_type = "text"
            data = {
                "body": body,
                "commit_id": commit,
            }
            if GITHUB:
                data.update({
                    "path": file,
                    "position": old_line,
                })
            else:
                data.update({
                    "position[base_sha]":
                    str(base_sha),
                    "position[start_sha]":
                    str(start_sha),
                    "position[head_sha]":
                    str(head_sha),
                    "position[old_path]":
                    file,
                    "position[new_path]":
                    file,
                    "position[old_line]":
                    None if line_type == "+" else old_line,
                    "position[new_line]":
                    None if line_type == "-" else new_line,
                    "position[position_type]":
                    position_type,
                })
            post(
                f'{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/{DISCUSSIONS}',
                data=data,
            )
    if not DRY_RUN:
        review.unlink()
    return True  # changed


def cmd_url2path(url):
    branch_or_issue = parse_path(url)[0]
    if isissue(branch_or_issue):  # Issue.
        path = f"gl/i/{branch_or_issue}/comments.gl"
    else:  # MR.
        path = f"gl/{branch_or_issue}/todo.gl"
    print(path)


def url_to_path(arg, merge_requests=None):
    match = re.match(
        r".*?\b(" + MERGE_REQUESTS
        + r"?|issues?)/(\d+).*?(?:#note_(\d+))?$", arg)
    assert match
    is_issue = match.group(1) in ("issues", "issue")
    note_id = match.group(3)
    if note_id is not None:
        note_id = int(note_id)

    if is_issue:
        issue_id = int(match.group(2))
        return f"i/{issue_id}", note_id
    mr_id = int(match.group(2))
    if merge_requests is None:
        merge_request = lazy_fetch_merge_request(iid=mr_id)
    else:
        merge_request = next(mr for mr in merge_requests
                             if mr[ISSUE_ID] == mr_id)
    return branch_name(merge_request), note_id


def cmd_path2url(branches_and_issues):
    branches_and_issues = [parse_path(p)[0] for p in branches_and_issues]
    for branch_or_issue in branches_and_issues:
        dash = "/" if GITHUB else "/-/"
        if isissue(branch_or_issue):
            print(
                f"{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}{dash}issues/{branch_or_issue}"
            )
            continue
        merge_requests = load_global(MERGE_REQUESTS)
        merge_request = next(mr for mr in merge_requests
                             if branch_name(mr) == branch_or_issue)
        mr = "pull" if GITHUB else MERGE_REQUESTS
        print(
            f'{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}{dash}{mr}/{merge_request[ISSUE_ID]}'
        )


def test_parse_path():
    # Branch
    assert parse_path(WORKING_TREE
                      + "/gl/some-branch/todo.gl")[0] == "some-branch"
    assert parse_path("gl/some-branch/todo.gl")[0] == "some-branch"
    assert parse_path("gl/some-branch/")[0] == "some-branch"
    assert parse_path("some-branch")[0] == "some-branch"
    assert parse_path(WORKING_TREE
                      + "/gl/branch/with/slashes")[0] == "branch/with/slashes"
    assert parse_path("gl/branch/with/slashes")[0] == "branch/with/slashes"
    assert parse_path("branch/with/slashes")[0] == "branch/with/slashes"

    # Issues
    assert parse_path(WORKING_TREE + "/gl/i/123/comments.gl")[0] == 123
    assert parse_path("gl/i/123/comments.gl")[0] == 123
    assert parse_path("gl/i/123/")[0] == 123
    assert parse_path("i/123/")[0] == 123
    assert parse_path("123")[0] == 123


def parse_path(path, merge_requests=None):
    if isinstance(path, Path):
        path = str(path)
    note_id = None
    if path.startswith(f"{PROTOCOL}://"):
        path, note_id = url_to_path(path, merge_requests)
    if "/" not in path:
        try:
            return int(path), note_id
        except ValueError:
            return path, note_id
    # returns branch, or issue number
    p = Path(path)
    if path.endswith(".gl") or path.endswith(".json"):
        p = p.parent
    if p.parent.name == "i":
        return int(p.name), note_id
    if p.is_absolute():
        p = p.relative_to(WORKING_TREE)
    try:
        p = p.relative_to(GLDIR)
    except ValueError:
        pass
    return str(p), note_id


def atom_updated(ns, element):
    updated_elem = element.find(ns + "updated")
    return datetime.strptime(updated_elem.text, "%Y-%m-%dT%H:%M:%SZ")


def cmd_activity():
    feed = DIR / "project.atom"
    seen = None
    if feed.exists():
        root = ET.parse(feed).getroot()
        ns = root.tag[:-len("feed")]
        seen = atom_updated(ns, root)
    if not DRY_RUN:
        r = gitlab_request("get",
                           f"{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}.atom")
        feed.write_text(r.text)
        root = ET.fromstring(r.text)
        ns = root.tag[:-len("feed")]
    entries = []
    stale = set()
    merge_requests = load_global(MERGE_REQUESTS)
    for entry in root.findall(ns + "entry"):
        if not DRY_RUN:
            if seen is not None and atom_updated(ns, entry) <= seen:
                continue
        link = entry.find(ns + "link").attrib["href"]
        title = entry.find(ns + "title").text
        if " deleted project branch " in title:
            continue
        pushed = re.match(r"^(.+?) pushed to project branch (\S+) .*$", title)
        branch_or_issue, note_id = None, None
        if pushed:
            branch = pushed.group(2)
            username = lookup_user(name=pushed.group(1))["username"]
            title = f"gl/{branch}/todo.gl:1: {username} pushed"
            stale.add(branch)
        else:
            branch_or_issue, note_id = parse_path(link, merge_requests)
            stale.add(branch_or_issue)
        entries += [(link, title, branch_or_issue, note_id)]

    if not DRY_RUN:
        if stale:
            fetch(list(stale))

    brissue_to_discussions = {}
    for link, title, branch_or_issue, note_id in entries:
        if branch_or_issue is None:
            continue
        if branch_or_issue in brissue_to_discussions:
            continue
        if isissue(branch_or_issue):
            directory = issue_dir(branch_or_issue)
        else:
            directory = branch_mrdir(branch_or_issue)
        brissue_to_discussions[branch_or_issue] = load_discussions(directory)
    file_to_contents = {}
    entries_with_file = []
    for link, title, branch_or_issue, note_id in entries:
        if branch_or_issue is None:
            entries_with_file += [(link, title, branch_or_issue, note_id, None,
                                   None)]
            continue
        if isissue(branch_or_issue):
            path = issue_dir(branch_or_issue)
        else:
            path = branch_mrdir(branch_or_issue)
        discussions = brissue_to_discussions[branch_or_issue]
        discussion = None
        filepath = None
        if isissue(branch_or_issue):
            name = "comments.gl"
        if note_id is None:
            if not isissue(branch_or_issue):
                name = "todo.gl"
        else:
            discussion = next(d for d in discussions
                              if any(n["id"] == note_id for n in d["notes"]))
            if not isissue(branch_or_issue):
                n0 = discussion_notes(discussion)[0]
                if "resolved" not in n0:
                    name = "meta.gl"
                elif n0["resolved"]:
                    name = "resolved.gl"
                else:
                    name = "todo.gl"
        filepath = path / name
        entries_with_file += [(link, title, branch_or_issue, note_id,
                               discussion, filepath)]
        if discussion is None:
            continue
        if filepath in file_to_contents:
            continue
        file_to_contents[filepath] = filepath.read_text().splitlines()
    s = ""
    us = load_users()
    user_re = re.compile(r"\b(" + "|".join(re.escape(u["name"])
                                           for u in us) + r")\b")
    for (
            link,
            title,
            branch_or_issue,
            note_id,
            discussion,
            filepath,
    ) in entries_with_file:
        my_title = ""
        for piece in user_re.split(title):
            if user_re.match(piece):
                my_title += lookup_user(name=piece)["username"]
            else:
                my_title += piece
        title = my_title
        if branch_or_issue is None:
            s += f"{title}\t{link}\n"
            continue
        assert filepath is not None
        line = 1
        if discussion is not None:
            for i, row in enumerate(file_to_contents[filepath]):
                location = re.match(r"^(?:[^:]+:\d+: )?([0-9a-f]{40})$", row)
                if location is not None and location.group(1) == str(
                        discussion["id"]):
                    line = i + 1
                    break
        s += f"{filepath.relative_to(WORKING_TREE)}:{line}:\t{title}\n"
    prettyfeed = DIR / "feed.gl"
    if prettyfeed.exists():
        s += "\n" + prettyfeed.read_text()
    prettyfeed.write_text(s)


def gather_users():
    issues, merge_requests = load_issues_and_merge_requests()
    keys = ("assignee", "assignees", "author", "reviewers")
    user_ids = set()
    users = []
    for thing in issues + merge_requests:
        for key in keys:
            if key not in thing:
                continue
            us = thing[key]
            if not isinstance(us, list) and not isinstance(us, tuple):
                us = (us, )
            for user in us:
                if user is None or user["id"] in user_ids:
                    continue
                user_ids.add(user["id"])
                users.append(user)
    if not DRY_RUN:
        (DIR / "users.json").write_text(json.dumps(users, indent=1))
    return True


def fetch_milestones():
    if GITHUB:
        return True
    milestones = get("milestones?state=active")
    if "GITLAB_GROUP" in os.environ:
        group = os.environ["GITLAB_GROUP"]
        url = f"{PROTOCOL}://{GITLAB}/api/v4/groups/{group}/milestones?state=active"
        milestones += get(url)
    if not DRY_RUN:
        (DIR / "milestones.json").write_text(json.dumps(milestones, indent=1))
    return True


def fetch_labels():
    labels = get("labels")
    if "GITLAB_GROUP" in os.environ:
        group = os.environ["GITLAB_GROUP"]
        url = f"{PROTOCOL}://{GITLAB}/api/v4/groups/{group}/labels"
        labels += get(url)
    (DIR / "labels.json").write_text(json.dumps(labels, indent=1))
    return True


def cmd_staticwords():
    print("\n".join([x["username"] for x in load_users()]
                    + [x["title"] for x in load_milestones()]
                    + [x["name"] for x in load_labels()]))


def cmd_fetchstatic():
    gather_users()
    fetch_milestones()
    fetch_labels()


def cmd_fetchmilestones():
    fetch_milestones()


def cmd_retry(branch):
    branch = parse_path(branch)[0]
    merge_request = lazy_fetch_merge_request(branch=branch)
    blessed_sha = None
    while True:
        pipeline = sorted(
            get(
                f"{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/pipelines/",
            ),
            key=lambda p: p["id"],
        )[-1]
        if blessed_sha is None:
            blessed_sha = pipeline["sha"]
        if pipeline["sha"] != blessed_sha:
            print(
                f"Did someone push? Blessed revision is {blessed_sha} but latest pipeline is running on {pipeline['sha']}"
            )
            return
        if pipeline["status"] == "success":
            print("Success!")
            return
        if pipeline["status"] in ("failed", "canceled"):
            post(f'pipelines/{pipeline["id"]}/retry')
        time.sleep(180)


def cancelreview(merge_request):
    reviews = get(
        f'{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/reviews')
    if reviews is None:
        return
    for review in reviews:
        if review["state"] != "PENDING":
            continue
        delete(
            f'{MERGE_REQUESTS}/{merge_request[ISSUE_ID]}/reviews/{review["id"]}'
        )


def cmd_cancelreview(branch):
    branch = parse_path(branch)[0]
    merge_request = lazy_fetch_merge_request(branch=branch)
    cancelreview(merge_request)


def main():
    parser = argparse.ArgumentParser(description=USAGE)
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="do not perform any network requests, only print what would happen",
    )
    kwargs = {}
    if (sys.version_info.major, sys.version_info.minor) >= (3, 7):
        kwargs["required"] = True
    subparser = parser.add_subparsers(
        metavar="<subcommand>",
        **kwargs,
    )

    parser_fetch = subparser.add_parser(
        "fetch",
        help="download discussions from an issue or MR",
        description="Download discussions from an issue or MR.",
    )
    parser_fetch.add_argument(
        "branches_and_issues",
        metavar="<branch/MR/issue>",
        nargs="*",
        help='branch, issue number, URL, or path to a "*.gl" file',
    )
    parser_fetch.set_defaults(func=cmd_fetch)

    parser_submit = subparser.add_parser(
        "submit",
        help='submit changes drafted in "*.gl" files',
        description="Submit comments based on local files.",
    )
    parser_submit.add_argument(
        "branches_and_issues",
        metavar="<branch/MR/issue>",
        nargs="+",
        help='branch, issue number, URL, or path to a "*.gl" file',
    )
    parser_submit.set_defaults(func=cmd_submit)

    parser_activity = subparser.add_parser(
        "activity",
        help='fetch latest repository activity to "feed.gl"',
        description='Letch latest repository activity to "feed.gl".',
    )
    parser_activity.set_defaults(func=cmd_activity)

    parser_discuss = subparser.add_parser(
        "discuss",
        help="compose a review comment for the given MR/commit/file/line",
        description="Compose a review comment for the given MR/commit/file/line.",
    )
    parser_discuss.add_argument("branch",
                                metavar="<branch>",
                                help="source branch of the MR")
    parser_discuss.add_argument("commit", metavar="<commit>", help="commit ID")
    parser_discuss.add_argument(
        "old_file",
        metavar="<old_file>",
        help="file name before rename or deletion, relative to top level")
    parser_discuss.add_argument(
        "new_file",
        metavar="<new_file>",
        help="current file name, relative to top level")
    parser_discuss.add_argument(
        "line_type",
        metavar="<line type>",
        help="""
            The first character of the diff line. You can also pass the entire line, the tail is ignored.
            "+" if the line is added
            "-" if the line deleted
            " " for context lines
        """,
    )
    parser_discuss.add_argument(
        "old_line",
        metavar="<old line>",
        type=int,
        help="line number in the old version of the file",
    )
    parser_discuss.add_argument(
        "new_line",
        metavar="<new line>",
        type=int,
        help="line number in the new version of the file",
    )
    parser_discuss.set_defaults(func=cmd_discuss)

    parser_fetchstatic = subparser.add_parser(
        "fetchstatic",
        help="fetch users, milestones and labels for this repository",
        description="Fetch users, milestones and labels for this repository",
    )
    parser_fetchstatic.set_defaults(func=cmd_fetchstatic)

    parser_fetchmilestones = subparser.add_parser(
        "fetchmilestones",
        help="fetch  milestones for this repository",
        description="Fetch milestones for this repository",
    )
    parser_fetchmilestones.set_defaults(func=cmd_fetchmilestones)

    parser_template = subparser.add_parser(
        "template",
        help='print an issue or MR template that can be passed to "gl create"',
        description="""
        Print an issue or MR template that can be passed to "gl create".
        If no arguments are given, create an issue template.
        For MR templates, pass at least the source branch name.
        The target branch will currently be "master", unless overridden by
        environment variable GITLAB_TARGET_BRANCH.
        """,
    )
    parser_template.add_argument("branch",
                                 metavar="<branch>",
                                 nargs="?",
                                 help="source branch of the MR")
    parser_template.add_argument(
        "issue_id",
        metavar="<issue>",
        nargs="?",
        help="optional issue to be closed by this MR",
    )
    parser_template.set_defaults(func=cmd_template)

    parser_create = subparser.add_parser(
        "create",
        help="create an issue or MR from a template read from stdin",
        description="""
        Create an issue or MR from the template read from stdin.
        A template can be created with "gl template".
        """,
    )
    parser_create.set_defaults(func=cmd_create)

    parser_retry = subparser.add_parser(
        "retry",
        help="retry the pipeline of the given MR until it passes, or someone pushes another version",
        description="retry the pipeline of the given MR until it passes, or someone pushes another version",
    )
    parser_retry.add_argument("branch",
                              metavar="<MR URL or branch>",
                              help="The MR ID")
    parser_retry.set_defaults(func=cmd_retry)

    parser_staticwords = subparser.add_parser(
        "staticwords",
        help="print a list of usernames, milestones and labels, suitable for completion",
        description="print a list of usernames, milestones and labels, suitable for completion",
    )
    parser_staticwords.set_defaults(func=cmd_staticwords)

    parser_path2url = subparser.add_parser(
        "path2url",
        help='convert an issue number, branch, or path to "*.gl" file to a GitLab URL',
        description='Convert an issue number, branch, or path to "*.gl" file to a GitLab URL',
    )
    parser_path2url.add_argument(metavar="<branch/MR/issue>",
                                 dest="branches_and_issues",
                                 nargs="+")
    parser_path2url.set_defaults(func=cmd_path2url)

    parser_url2path = subparser.add_parser(
        "url2path",
        help='convert a GitLab issue URL or MR URL to the corresponding "*.gl" file',
        description='Convert a GitLab issue URL or MR URL to the corresponding "*.gl" file.',
    )
    parser_url2path.add_argument(metavar="<GitLab issue URL or MR URL>",
                                 dest="url")
    parser_url2path.set_defaults(func=cmd_url2path)

    parser_cmd_cancelreview = subparser.add_parser(
        "cancelreview",
        help='Delete a pending review',
        description='Delete a pending review',
    )
    parser_cmd_cancelreview.add_argument(metavar="<MR URL or branch>",
                                         dest="branch")
    parser_cmd_cancelreview.set_defaults(func=cmd_cancelreview)

    args = parser.parse_args()
    if args.dry_run:
        global DRY_RUN
        DRY_RUN = True

    kwargs = dict(args.__dict__)
    del kwargs["dry_run"]
    del kwargs["func"]
    args.func(**kwargs)


if __name__ == "__main__":
    main()
