#!/usr/bin/env python3

import argparse
import git
import hashlib
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


class OptionalDependency:
    pass


try:
    import unidiff
except:
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
remote = None
for gitRemote in THE_REPOSITORY.remotes:
    REMOTE_NAME = gitRemote.name
    url = next(gitRemote.urls)
    word_char = r"[\w.-]"
    remote = re.match(
        r"^(?P<Protocol>\w+://)?(?P<User>"
        + word_char
        + r"+@)?(?P<Host>[\w.-]+)"
        + "(:(?P<Port>\d+))?[/:](?P<Project>"
        + word_char
        + "+/"
        + word_char
        + "+?)(\.git)?$",
        url,
    )
    if remote:
        break


class UserError(Exception):
    pass


if not remote:
    raise UserError("Missing remote, don't know how to talk to GitLab")
GITLAB = remote["Host"]
GITLAB_PROJECT = remote["Project"]
PROTOCOL = "http" if GITLAB == "localhost" else "https"

GITLAB_PROJECT_ESC = urllib.parse.quote(GITLAB_PROJECT, safe="")
TOKEN = None


def token():
    global TOKEN
    if TOKEN is not None:
        return TOKEN
    TOKEN = os.environ.get("GITLAB_TOKEN")
    if TOKEN is None:
        TOKEN = (
            subprocess.run(("pass", f"{GITLAB}/token"),
                           check=True, stdout=PIPE)
            .stdout.decode()
            .strip()
        )
    return TOKEN


@lru_cache(maxsize=None)
def diff(base, head) -> unidiff.PatchSet:
    uni_diff_text = THE_REPOSITORY.git.diff(base, head, "-U123123123").encode()
    return unidiff.PatchSet.from_string(uni_diff_text, encoding="utf-8")


@lru_cache(maxsize=None)
def diff_for_newfile(base, head, path) -> unidiff.PatchedFile:
    files = diff(base, head)
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
def users():
    if not (DIR / "users.json").is_file():
        Users()
    with open(DIR / "users.json") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def lookup_user(*, name=None, username=None):
    for attempt in ("hit", "miss"):
        if attempt == "miss":
            Users()
            with open(DIR / "users.json") as f:
                us = json.load(f)
        else:
            us = users()
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
def milestones():
    if not (DIR / "milestones.json").is_file():
        Milestones()
    with open(DIR / "milestones.json") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def milestone_title_to_id(milestone_title):
    for attempt in ("hit", "miss"):
        if attempt == "miss":
            if not DRY_RUN:
                Milestones()
            with open(DIR / "milestones.json") as f:
                ms = json.load(f)
        else:
            ms = milestones()
        for milestone in ms:
            if milestone["title"] == milestone_title:
                return milestone["id"]
    assert 0, f'no such milestone "{milestone_title}"'


@lru_cache(maxsize=None)
def labels():
    if not (DIR / "labels.json").is_file():
        Labels()
    with open(DIR / "labels.json") as f:
        return json.load(f)


def fetch_commit(ref):
    try:
        THE_REPOSITORY.commit(ref)
    except ValueError:
        THE_REPOSITORY.git().execute(("git", "fetch", REMOTE_NAME, ref))


def context(base, head, old_path, old_line, new_path, new_line):
    fetch_commit(base)
    fetch_commit(head)
    try:
        hunk = diff_for_newfile(base, head, new_path)[0]
    except git.exc.GitCommandError:
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
    the_context = "".join(rows[max(0, line - DIFF_CONTEXT_LINES): line])
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


def issue_dir(issue_id):
    """
    input:  123
    output: /path/to/repo/gl/i/123
    """
    return DIR / "i" / str(issue_id)


def req(method, path, **kwargs):
    """
    Send a request to the GitLab API and return the response.
    """
    r = None
    if path.startswith(f"{PROTOCOL}://"):
        url = path  # Absolute URL - use it.
    else:
        # Assume the path is relative to a "project" API.
        url = f"{PROTOCOL}://{GITLAB}/api/v4/projects/{GITLAB_PROJECT_ESC}/" + path
    curl_headers = "-HPRIVATE-TOKEN:\\ $GITLAB_TOKEN"
    if "data" in kwargs:
        curl_headers += " -HContent-Type:\\ application/json"
    trace = f"curl {curl_headers} -X{method.upper()} '{url}'"
    if "data" in kwargs:
        data = json.dumps(kwargs["data"], indent=2).replace('"', '\\"')
        trace += f' --data "{data}"'
    print(trace)
    if not DRY_RUN:
        r = requests.request(method, url, headers={
                             "PRIVATE-TOKEN": token()}, **kwargs)
        if not r.ok:
            print(r, r.reason)
            print(r.content.decode())
        assert r.ok, f"HTTP {method} failed: {url}"
    return r


def delete(path, **kwargs):
    "Send an HTTP DELETE request to GitLab"
    return req("delete", path, **kwargs)


def get(path, **kwargs):
    "Send an HTTP GET request to GitLab"
    ppath = path
    if "?" in path:
        ppath = path + "&per_page=100"
    else:
        ppath = path + "?per_page=100"
    r = req("get", ppath, **kwargs)
    if r is None:
        return
    data = r.json()
    next_page = r.headers.get("X-Next-Page")
    while next_page:
        r = req("get", ppath + f"&page={next_page}")
        next_page = r.headers.get("X-Next-Page")
        data += r.json()
    return data


def post(path, **kwargs):
    "Send an HTTP POST request to GitLab"
    return req("post", path, **kwargs)


def put(path, **kwargs):
    "Send an HTTP PUT request to GitLab"
    return req("put", path, **kwargs)


def load_discussions(mrdir):
    try:
        with open(mrdir / "discussions.json") as f:
            return json.load(f)
    except FileNotFoundError:  # We never fetched (dry run?).
        return []


def fetch_global(what):
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
    with open(DIR / f"{what}.json") as f:
        return json.load(f)


def load_issues_and_merge_requests():
    with open(DIR / "issues.json") as i:
        with open(DIR / "merge_requests.json") as m:
            return json.load(i), json.load(m)


def update_global(what, thing, fetch=True):
    """
    Edit the list of MRs/issues in-place, updating the given issue/MR.
    """
    if fetch:
        try:
            newthing = req("get", f"{what}/{thing['iid']}").json()
        except:
            if DRY_RUN:
                return thing
            raise
    path = DIR / f"{what}.json"
    old = json.loads(path.read_bytes())
    if fetch:
        new = []
        added = False
        for t in old:
            if fetch:
                if t["iid"] == newthing["iid"]:
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
                merge_requests = load_global("merge_requests")
            if branch is not None:
                return next(
                    (mr for mr in merge_requests if mr["source_branch"] == branch)
                )
            elif iid is not None:
                return next(mr for mr in merge_requests if mr["iid"] == iid)
            assert False
        except Exception as e:
            if attempt == "miss":
                print(
                    f"no merge request branch={branch} iid={iid}", file=sys.stderr)
                raise e
            merge_requests = fetch_global("merge_requests")


def show_discussion(discussions):
    out = ""
    for discussion in discussions:
        notes = discussion["notes"]
        n0 = notes[0]
        url = (
            f'{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}/-/merge_requests/1#note_{n0["id"]}'
        )
        if "position" not in n0:
            location = ""
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
                    "--format=%h %s", "-1", f"{n0['commit_id']}"
                )
                commit_human += "\n"
            except:
                commit_human = ""
        out += f"{location}{discussion['id']}\n"
        out += commit_human
        if "position" in n0:
            out += context(base, head, old_path, old_line, new_path, new_line)
        for note in notes:
            lines = note["body"].splitlines()
            author = note["author"]["username"]
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
    try:
        issues, merge_requests = load_issues_and_merge_requests()
        have_branches = set(mr["source_branch"] for mr in merge_requests)
    except FileNotFoundError:
        pass
    fetched_all_issues = False
    # If we already fetched the list of issues before, and are given an
    # explicit list of issues, then only fetch those.
    if (DIR / "issues.json").exists() and branches_and_issues:
        issues = [{"iid": i} for i in branches_and_issues if isissue(i)]
    else:
        #  Fetch all open issues (this takes some time).
        issues = fetch_global("issues")
        fetched_all_issues = True
    want_branches = set(
        branch for branch in branches_and_issues if not isissue(branch))
    if merge_requests is None or not want_branches.issubset(have_branches):
        merge_requests = fetch_global("merge_requests")
    for merge_request in merge_requests:
        if merge_request["source_branch"] not in want_branches:
            continue
        merge_request = update_global("merge_requests", merge_request)
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
    if "source_branch" in data:
        what = "merge_requests"
    else:
        what = "issues"
    thing = post(what, data=data).json()
    update_global(what, thing, fetch=False)
    if "source_branch" in data:
        fetch_mr_data(thing)
    else:
        fetch_issue_data(thing)
    print()
    print(thing["web_url"])


def issue_template_callback(data, branch, issue_id):
    pass


def cmd_template(branch=None, issue_id=None):
    data = {
        "title": "title",
        "description": "description\n",
        "assignees": (),
        "milestone": None,
        "labels": (),
    }
    target_branch = os.environ.get("GITLAB_TARGET_BRANCH", "master")
    if branch is not None:
        data["description"] = ""
        if branch is None:
            branch = "source_branch"
        else:
            data["description"] += THE_REPOSITORY.git().log(
                "--format=%s", f"{REMOTE_NAME}/{target_branch}..{branch}"
            )
            data["description"] += "\n"
        data["assignees"] = (lookup_user(username=GITLAB_USER),)
        data["reviewers"] = ()
        data["source_branch"] = branch
        data["target_branch"] = target_branch
        data["remove_source_branch"] = True
        issue_template_callback(data, branch, issue_id)
    print(metadata_header(data).rstrip("\n"))


def metadata_header(thing):
    extra = ""
    if "source_branch" in thing:
        extra += f"{MARKER} source_branch: {thing['source_branch']}\n"
        extra += f"{MARKER} target_branch: {thing['target_branch']}\n"
        extra += (
            f"{MARKER} reviewers: "
            + ",".join(a["username"] for a in thing["reviewers"])
            + "\n"
        )
    return (
        f'{thing["title"]}\n\n'
        + ("\n" if thing["description"]
           is None else f'{thing["description"]}\n')
        + extra
        + f"{MARKER} assignees: "
        + ",".join(a["username"] for a in thing["assignees"])
        + "\n"
        + f"{MARKER} milestone: "
        + (thing["milestone"]["title"] if thing["milestone"] else "")
        + "\n"
        + f"{MARKER}    labels: "
        + ",".join(thing["labels"])
        + "\n"
        + "\n"
    )


def fetch_issue_data(issue):
    issue_id = issue["iid"]
    idir = issue_dir(issue_id)
    idir.mkdir(exist_ok=True, parents=True)
    discussions = get(f"issues/{issue_id}/discussions")
    if discussions is not None:
        (idir / "discussions.json").write_text(json.dumps(discussions, indent=1))
    else:
        discussions = load_discussions(idir)
    url = f"{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}/-/issues/{issue_id}"
    comments = metadata_header(
        issue) + show_discussion(discussions) + f"{MARKER}\n"
    comments_path = idir / "comments.gl"
    pristine_path = idir / "pristine-comments.gl"
    if comments_path.exists():
        new_pristine_path = idir / "new-pristine-comments.gl"
        new_pristine_path.write_text(comments)
        subprocess.run(
            ("git", "merge-file", comments_path, pristine_path, new_pristine_path)
        )
        new_pristine_path.rename(pristine_path)
    else:
        comments_path.write_text(comments)
        pristine_path.write_text(comments)


def fetch_mr_data(merge_request):
    branch = merge_request["source_branch"]
    mrdir = branch_mrdir(branch)
    mrdir.mkdir(exist_ok=True)
    discussions = get(f"merge_requests/{merge_request['iid']}/discussions")
    if DRY_RUN:
        return
    if discussions is not None:
        (mrdir / "discussions.json").write_text(json.dumps(discussions, indent=1))
    else:
        discussions = load_discussions(mrdir)
    unresolvable = (d for d in discussions if "resolved" not in d["notes"][0])
    todo = (
        d
        for d in discussions
        if "resolved" in d["notes"][0] and not d["notes"][0]["resolved"]
    )
    resolved = (
        d
        for d in discussions
        if "resolved" in d["notes"][0] and d["notes"][0]["resolved"]
    )
    todo = show_discussion(todo) + f"{MARKER}\n"
    pristine_path = mrdir / "pristine-todo.gl"
    todo_path = mrdir / "todo.gl"
    if todo_path.exists():
        new_pristine_path = mrdir / "new-pristine-todo.gl"
        new_pristine_path.write_text(todo)
        subprocess.run(
            ("git", "merge-file", todo_path, pristine_path, new_pristine_path)
        )
        new_pristine_path.rename(pristine_path)
    else:
        todo_path.write_text(todo)
        pristine_path.write_text(todo)
    (mrdir / "resolved.gl").write_text(show_discussion(resolved))
    (mrdir / "meta.gl").write_text(
        metadata_header(merge_request) + show_discussion(unresolvable)
    )


def cmd_submit(branches_and_issues):
    branches_and_issues = [parse_path(p)[0] for p in branches_and_issues]
    issues, merge_requests = load_issues_and_merge_requests()
    if not branches_and_issues:
        branches_and_issues = [mr["source_branch"] for mr in merge_requests] + [
            issue["iid"] for issue in issues
        ]
    for branch_or_issue in branches_and_issues:
        if isissue(branch_or_issue):
            idir = issue_dir(branch_or_issue)
            try:
                issue = next(i for i in issues if i["iid"] == branch_or_issue)
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
            merge_request = next(
                mr for mr in merge_requests if mr["source_branch"] == branch_or_issue
            )
        except StopIteration:
            assert mrdir.is_dir()
            print(f"MR not open, removing {mrdir}", file=sys.stderr)
            if not DRY_RUN:
                shutil.rmtree(mrdir)
            continue
        changed, mr_changed = submit_mr_data(merge_request)
        if mr_changed:
            merge_request = update_global("merge_requests", merge_request)
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
        for optional_field in ("source_branch", "target_branch", "state_event"):
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
                a["username"] for a in thing["assignees"]
            ):
                if not assignees:
                    data["assignee_ids"] = [0]
                else:
                    data["assignee_ids"] = [
                        lookup_user(username=a)["id"] for a in assignees.split(",")
                    ]
            continue
        # TODO unclone
        prefix = f"{MARKER} reviewers:"
        if row.startswith(prefix):
            reviewers = row[len(prefix):].lstrip()
            if thing is None or reviewers != ",".join(
                a["username"] for a in thing["reviewers"]
            ):
                if not reviewers:
                    data["reviewer_ids"] = [0]
                else:
                    data["reviewer_ids"] = [
                        lookup_user(username=a)["id"] for a in reviewers.split(",")
                    ]
            continue
        prefix = f"{MARKER} milestone:"
        if row.startswith(prefix):
            milestone = row[len(prefix):].lstrip()
            if (
                thing is None
                or (bool(milestone) != bool(thing["milestone"]))
                or (
                    thing["milestone"] is not None
                    and milestone != thing["milestone"]["title"]
                )
            ):
                if not milestone:
                    data["milestone_id"] = [0]
                else:
                    try:
                        data["milestone_id"] = milestone_title_to_id(milestone)
                    except Exception as e:
                        print(e)
            continue
        prefix = f"{MARKER}    labels:"
        if row.startswith(prefix):
            labels = row[len(prefix):].lstrip()
            if thing is None or labels != ",".join(thing["labels"]):
                data["labels"] = labels
            continue
        break
    description = "\n".join(description)
    if thing is None or description != thing["description"]:
        data["description"] = description
    assert j == 0 or rows[j - 1].startswith(MARKER)
    assert j == len(rows) or not rows[j].startswith(MARKER)
    return j, data


def SubmitDiscussion(discussions, rows, merge_request=None, issue=None):
    thing = merge_request if merge_request is not None else issue
    what = "merge_requests" if merge_request is not None else "issues"
    comments = {}  # discussion ID => note ID => text
    new_comments = {}  # discussion ID => text
    new_discussions = []
    state = "CONTEXT"
    changed = False
    desc_changed = False
    note_id = None
    i, data = parse_metadata_header(rows, thing)
    if data:
        if not DRY_RUN:  # TODO
            try:  # These are a bit unreliable.
                current = req("get", f"{what}/{thing['iid']}").json()
                for key in data:
                    if (
                        key.endswith("_id")
                        or key.endswith("_ids")
                        or key.endswith("_event")
                    ):
                        continue
                    if thing[key] != current[key]:
                        assert (
                            0
                        ), f"outdated {key} - have {thing[key]} but upstream has {current[key]}"
                put(
                    f"{what}/{thing['iid']}",
                    data=data,
                )
                changed = True
                desc_changed = True
            except Exception as e:
                print(e, file=sys.stderr)
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
        note_header = re.match(f"^\t\[(\d+)\] ", row)
        if note_header:
            note_id = int(note_header.group(1))
        location = re.match(r"^(?:[^:]+:\d+: )?([0-9a-f]{40})$", row)
        if location:
            note_id = None
            discussion_id = location.group(1)
            did, discussion = next(
                (i, discussion)
                for i, discussion in enumerate(discussions)
                if discussion["id"] == discussion_id
            )
            n0 = discussion["notes"][0]
            what_id = n0["noteable_iid"]
            comments[did] = {}
            state = "CONTEXT"
            continue
        if merge_request is not None:
            resolve = re.match(r"^r$", row)
            if resolve:
                if not n0["resolved"]:
                    put(
                        f'{what}/{what_id}/discussions/{discussion["id"]}?resolved=true'
                    )
                    changed = True
                continue
            unresolve = re.match(r"^u$", row)
            if unresolve:
                if n0["resolved"]:
                    put(
                        f'{what}/{what_id}/discussions/{discussion["id"]}?resolved=false'
                    )
                    changed = False
                continue
            if re.match(r"^!!!merge$", row):
                put(f"{what}/{what_id}/merge")
                changed = True
                continue
        if re.match(r"^!!!delete$", row):
            delete(
                f'{what}/{what_id}/discussions/{discussion["id"]}/notes/{note_id}')
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
            f'{what}/{thing["iid"]}/discussions/',
            data={
                "body": body,
            },
        )
        changed = True
    for did, comment in new_comments.items():
        discussion = discussions[did]
        post(
            f'{what}/{what_id}/discussions/{discussion["id"]}/notes',
            data={
                "note_id": discussion["notes"][0]["id"],
                "body": comment,
            },
        )
        changed = True
    for did, note_comments in comments.items():
        discussion = discussions[did]
        for note_id, comment in note_comments.items():
            note = next((n for n in discussion["notes"] if n["id"] == note_id))
            # fix this?
            new_body = "\n".join(comment).strip()
            old_body = note["body"].strip()
            if new_body != old_body:
                put(
                    f'{what}/{what_id}/discussions/{discussion["id"]}/notes/{note_id}',
                    data={
                        "body": new_body,
                    },
                )
                changed = True
    return changed, desc_changed


def submit_mr_data(merge_request):
    branch = merge_request["source_branch"]
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
    changed, desc_changed = SubmitDiscussion(
        discussions, rows, merge_request=merge_request
    )
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
    issue_id = issue["iid"]
    idir = issue_dir(issue_id)
    discussions = load_discussions(idir)
    try:
        contents = (idir / "comments.gl").read_bytes().decode()
        rows = contents.splitlines()
    except FileNotFoundError:  # When there is no thread.
        rows = ()
    changed, desc_changed = SubmitDiscussion(discussions, rows, issue=issue)
    pristine_path = idir / "pristine-comments.gl"
    comments_path = idir / "comments.gl"
    if not DRY_RUN:
        try:
            shutil.copy(pristine_path, comments_path)
        except FileNotFoundError:  # When there was no thread.
            pass
    return changed, desc_changed


def cmd_discuss(branch, commit, file, line_type, old_line, new_line):
    "Draft a review comment."
    merge_request = lazy_fetch_merge_request(branch=branch)

    line_type = line_type[:1]
    assert line_type in " -+"
    old_line = int(old_line)
    new_line = int(new_line)

    try:
        hunk = diff_for_newfile(f"{commit}~", commit, file)[0]
        rows = []
        for i, row in enumerate(hunk):
            if row.target_line_no is None:
                continue
            if row.target_line_no >= new_line:
                rows = hunk[: i + 1]
                break
        context = "".join(
            r.line_type + r.value for r in rows[-DIFF_CONTEXT_LINES:])
    except UnicodeEncodeError:
        context = f" ? UnicodeEncodeError {commit}\n"

    mrdir = branch_mrdir(merge_request["source_branch"])
    mrdir.mkdir(exist_ok=True)

    review = mrdir / "review.gl"
    with open(review, "a") as f:
        header = f"\n{MARKER} {commit} {file}:{new_line} {line_type} {old_line}\n"
        f.write(header)
        f.write(context + "\n")
    subprocess.run(
        os.environ["EDITOR"] + " +123123 " + shlex.quote(str(review)), shell=True
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
        r = re.match(r"^" + MARKER +
                     r" (\S+) ([^:]+):(\d+) ([ +-]) (\d+)", row)
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

    for commit, file, line_type, old_line, new_line, body in discussions:
        base_sha = THE_REPOSITORY.rev_parse(f"{commit}~")
        start_sha = base_sha
        head_sha = commit
        position_type = "text"
        line_start_type = {
            "+": "new",
            "-": "old",
            " ": "old",
        }[line_type]
        line_start_code = (
            f"{hashlib.sha1(file.encode()).hexdigest()}_{old_line}_{new_line}"
        )
        post(
            f'merge_requests/{merge_request["iid"]}/discussions',
            data={
                "commit_id": commit,
                "body": body,
                "position[base_sha]": str(base_sha),
                "position[start_sha]": str(start_sha),
                "position[head_sha]": str(head_sha),
                "position[old_path]": file,
                "position[new_path]": file,
                "position[old_line]": None if line_type == "+" else old_line,
                "position[new_line]": None if line_type == "-" else new_line,
                "position[position_type]": position_type,
            },
        )
    if not DRY_RUN:
        review.unlink()
    return True  # changed


def Context(branch, discussion_id):
    mrdir = branch_mrdir(branch)
    discussions = load_discussions(mrdir)
    discussion = next(d for d in discussions if d["id"] == discussion_id)
    n0 = discussion["notes"][0]
    pos = n0["position"]
    argv = (
        "git",
        "diff",
        pos["base_sha"],
        pos["head_sha"],
        "--",
        pos["old_path"],
        pos["new_path"],
    )
    print(" ".join(argv))
    print()
    sys.stdout.flush()
    subprocess.run(
        argv,
        check=True,
    )


def cmd_url2path(url):
    branch_or_issue = parse_path(url)[0]
    if isissue(branch_or_issue):  # Issue.
        path = f"gl/i/{branch_or_issue}/comments.gl"
    else:  # MR.
        path = f"gl/{branch_or_issue}/todo.gl"
    print(path)


def url_to_path(arg, merge_requests=None):
    match = re.match(
        r".*?\b(merge_requests|issues)/(\d+).*?(?:#note_(\d+))?$", arg)
    assert match
    is_issue = match.group(1) == "issues"
    note_id = match.group(3)
    if note_id is not None:
        note_id = int(note_id)

    if is_issue:
        issue_id = int(match.group(2))
        return f"i/{issue_id}", note_id
    mr_id = int(match.group(2))
    if merge_requests is None:
        merge_request = lazy_fetch_merge_request(iid=mr_id)
    return merge_request["source_branch"], note_id


def cmd_path2url(branches_and_issues):
    branches_and_issues = [parse_path(p)[0] for p in branches_and_issues]
    for branch_or_issue in branches_and_issues:
        if isissue(branch_or_issue):
            print(
                f"{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}/-/issues/{branch_or_issue}")
        merge_requests = load_global("merge_requests")
        merge_request = next(
            mr for mr in merge_requests if mr["source_branch"] == branch_or_issue
        )
        print(
            f'{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}/-/merge_requests/{merge_request["iid"]}'
        )


def parse_path(path, merge_requests=None):
    if isinstance(path, Path):
        path = str(path)
    note_id = None
    if path.startswith(f"{PROTOCOL}://"):
        path, note_id = url_to_path(path, merge_requests)
    if "/" not in path:
        try:
            return int(path), note_id
        except:
            return path, note_id
    # returns branch, or issue number
    p = Path(path)
    if path.endswith(".gl") or path.endswith(".json"):
        p = p.parent
    if p.parent.name == "i":
        return int(p.name), note_id
    return p.name, note_id


def atom_updated(ns, element):
    updated_elem = element.find(ns + "updated")
    return datetime.strptime(updated_elem.text, "%Y-%m-%dT%H:%M:%SZ")


def cmd_activity():
    feed = DIR / "project.atom"
    seen = None
    try:
        root = ET.parse(feed).getroot()
        ns = root.tag[: -len("feed")]
        seen = atom_updated(ns, root)
    except:
        pass
    if not DRY_RUN:
        r = req("get", f"{PROTOCOL}://{GITLAB}/{GITLAB_PROJECT}.atom")
        feed.write_text(r.text)
        root = ET.fromstring(r.text)
        ns = root.tag[: -len("feed")]
    entries = []
    stale = set()
    merge_requests = load_global("merge_requests")
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
            try:
                branch_or_issue, note_id = parse_path(link, merge_requests)
                stale.add(branch_or_issue)
            except:
                pass
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
            dir = issue_dir(branch_or_issue)
        else:
            dir = branch_mrdir(branch_or_issue)
        brissue_to_discussions[branch_or_issue] = load_discussions(dir)
    file_to_contents = {}
    entries_with_file = []
    for link, title, branch_or_issue, note_id in entries:
        if branch_or_issue is None:
            entries_with_file += [(link, title,
                                   branch_or_issue, note_id, None, None)]
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
            discussion = next(
                d for d in discussions if any(n["id"] == note_id for n in d["notes"])
            )
            if not isissue(branch_or_issue):
                n0 = discussion["notes"][0]
                if "resolved" not in n0:
                    name = "meta.gl"
                elif n0["resolved"]:
                    name = "resolved.gl"
                else:
                    name = "todo.gl"
        filepath = path / name
        entries_with_file += [
            (link, title, branch_or_issue, note_id, discussion, filepath)
        ]
        if discussion is None:
            continue
        if filepath in file_to_contents:
            continue
        file_to_contents[filepath] = filepath.read_text().splitlines()
    s = ""
    us = users()
    user_re = re.compile(
        r"\b(" + "|".join(re.escape(u["name"]) for u in us) + r")\b")
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
                if location is not None and location.group(1) == discussion["id"]:
                    line = i + 1
                    break
        s += f"{filepath.relative_to(WORKING_TREE)}:{line}:\t{title}\n"
    prettyfeed = DIR / "feed.gl"
    if prettyfeed.exists():
        s += "\n" + prettyfeed.read_text()
    prettyfeed.write_text(s)


def Users():
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
                us = (us,)
            for user in us:
                if user is None or user["id"] in user_ids:
                    continue
                user_ids.add(user["id"])
                users.append(user)
    if not DRY_RUN:
        (DIR / "users.json").write_text(json.dumps(users, indent=1))
    return True


def Milestones():
    milestones = get("milestones?state=active")
    if "GITLAB_GROUP" in os.environ:
        group = os.environ["GITLAB_GROUP"]
        url = f"{PROTOCOL}://{GITLAB}/api/v4/groups/{group}/milestones?state=active"
        milestones += get(url)
    if not DRY_RUN:
        (DIR / "milestones.json").write_text(json.dumps(milestones, indent=1))
    return True


def Labels():
    labels = get("labels")
    if "GITLAB_GROUP" in os.environ:
        group = os.environ["GITLAB_GROUP"]
        url = f"{PROTOCOL}://{GITLAB}/api/v4/groups/{group}/labels"
        labels += get(url)
    (DIR / "labels.json").write_text(json.dumps(labels, indent=1))
    return True


def cmd_staticwords():
    print(
        "\n".join(
            [x["username"] for x in users()]
            + [x["title"] for x in milestones()]
            + [x["name"] for x in labels()]
        )
    )


def cmd_fetchstatic():
    Users()
    Milestones()
    Labels()


def cmd_retry(branch):
    branch = parse_path(branch)[0]
    merge_request = lazy_fetch_merge_request(branch=branch)
    blessed_sha = None
    while True:
        pipeline = sorted(
            get(
                f"merge_requests/{merge_request['iid']}/pipelines/",
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


def main():
    parser = argparse.ArgumentParser(description=USAGE)
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="do not perform any network requests, only print what would happen",
    )
    subparser = parser.add_subparsers(
        metavar="<subcommand>",
        # required=True, # TODO needs > Python3.6
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
        help=f'fetch latest repository activity to "feed.gl"',
        description=f'Letch latest repository activity to "feed.gl".',
    )
    parser_activity.set_defaults(func=cmd_activity)

    parser_discuss = subparser.add_parser(
        "discuss",
        help="compose a review comment for the given MR/commit/file/line",
        description="Compose a review comment for the given MR/commit/file/line.",
    )
    parser_discuss.add_argument(
        "branch", metavar="<branch>", help="source branch of the MR"
    )
    parser_discuss.add_argument("commit", metavar="<commit>", help="commit ID")
    parser_discuss.add_argument(
        "file", metavar="<file>", help="file name, relative to top level"
    )
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
    parser_template.add_argument(
        "branch", metavar="<branch>", nargs="?", help="source branch of the MR"
    )
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
    parser_retry.add_argument(
        "branch", metavar="<MR URL or branch>", help="The MR ID")
    parser_retry.set_defaults(func=cmd_retry)

    parser_staticwords = subparser.add_parser(
        "staticwords",
        help="print a list of usernames, milestones and labels, suitable for completion",
        description="print a list of usernames, milestones and labels, suitable for completion",
    )
    parser_staticwords.set_defaults(func=cmd_staticwords)

    parser_path2url = subparser.add_parser(
        "path2url",
        help=f'convert an issue number, branch, or path to "*.gl" file to a GitLab URL',
        description=f'Convert an issue number, branch, or path to "*.gl" file to a GitLab URL',
    )
    parser_path2url.add_argument(
        metavar="<branch/MR/issue>", dest="branches_and_issues", nargs="+"
    )
    parser_path2url.set_defaults(func=cmd_path2url)

    parser_url2path = subparser.add_parser(
        "url2path",
        help=f'convert a GitLab issue URL or MR URL to the corresponding "*.gl" file',
        description=f'Convert a GitLab issue URL or MR URL to the corresponding "*.gl" file.',
    )
    parser_url2path.add_argument(
        metavar="<GitLab issue URL or MR URL>", dest="url")
    parser_url2path.set_defaults(func=cmd_url2path)

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
