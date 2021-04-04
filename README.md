# gitlab-offline-review

GitLab and GitHub have many advantages but their web-based code review UI
lacks flexibility. I prefer to read and write review comments directly in
my code editor, where I can view and edit related files efficiently.  This
feels faster and allows to avoid context switches between website and editor.

[git-bug](https://github.com/MichaelMure/git-bug) can provide a
good offline experience. However, it [doesn't support merge requests
yet](https://github.com/MichaelMure/git-bug/issues/17).

I mostly need to write comments, comment on specific lines of commit diffs,
and resolve threads.  That sounded fairly simple so I wrote my own tool
`gl.py` to handle the interaction with GitLab.  It is a very specific tool,
but it works for the most part ;)

`gl.py` should be fairly easy to integrate in your favorite editor, or your
Git frontend of choice - speak up if you see something that can be improved.

## Tutorial

`gl.py` needs to be run inside a Git repository.  It assumes that the first
remote is the GitLab host.

The first step is to [create your personal access token for the GitLab
API](https://gitlab.com/-/profile/personal_access_tokens) (use `api` scope).
Then add the following environment variables:

```shell
$ export GITLAB_USER=your-user-name-on-gitlab
$ export GITLAB_TOKEN=your-secret-access-token
```

Install the dependencies (Python 3.6 or later is required):

```shell
pip3 install unidiff GitPython
```

Install the `gl.py` script:

```shell
$ git clone https://gitlab.com/krobelus/gitlab-offline-review && cd gitlab-offline-review
$ ln -s $PWD/gl.py ~/bin/
```

Since the repository is hosted on gitlab.com, here's how you
can draft a new comment for the designated [test issue number
1](https://gitlab.com/krobelus/gitlab-offline-review/-/issues/1):

```
$ gl.py fetch 1
$ echo "my new comment on issue #1" >> gl/i/1/comments.gl
$ gl.py --dry-run submit 1
```

Running without the `--dry-run` option would post the comment.  Either way,
the output shows that this command would perform two API requests:

```
curl -HPRIVATE-TOKEN:\ $GITLAB_TOKEN -HContent-Type:\ application/json -XPOST 'https://gitlab.com/api/v4/projects/krobelus%2Fgitlab-offline-reviews/issues/1/discussions/' --data "{
  \"body\": \"my new comment on issue #1\"
}"
curl -HPRIVATE-TOKEN:\ $GITLAB_TOKEN -XGET 'https://gitlab.com/api/v4/projects/krobelus%2Fgitlab-offline-reviews/issues/1/discussions?per_page=100'
```

First, this posts the new comment. Second, this downloads the issue's comments,
updating the files stored in `gl/i/1`.

## Documentation

### Issues

`gl.py fetch` retrieves comments from GitLab and stores them as text files
in a directory named `gl` at the root of your Git repository.

Comments can be fetched for issues and merge requests.  To facilitate
integration with editors and browsers, issues and merge requests can be
specified in a number of ways - each of these three commands fetches the
same issue number 123:

```shell
$ gl.py fetch https://gitlab.com/example/example/-/issues/123
$ gl.py fetch 123
$ gl.py fetch gl/i/123/comments.gl
```

Now the directory `gl/i/123` contains all data relevant to issue 123.
The file `gl/i/123/comments.gl`, contains the issue description and all
comments. It might look like this:

```
The issue title

The issue description.
Multiple lines here.

𑁍 assignees: artour
𑁍 milestone: yesteryear
𑁍    labels: Bug

ec486c4d974e9342144a10c54a367aa74c709d18
	[artour] test comment please ignore
	[539329142] ok

𑁍
```

Observe:
- The first line contains the issue title; followed by a blank line and the
  issue description. These are currently not editable.
- The special marker `𑁍` at the start of the line marks metadata fields.
  These are currently not editable.
- An internal ID at the start of a line marks a thread.
- The actual comments are always indented with a tab character.
- Comments are prefixed  with the name of the author in square brackets. If
  you are the author, then a number will appear instead. This is the ID of
  this comment, which is used internally, so you can edit this comment.
- To add a new comment to a thread, simply write your comment in a new
  line (after the "ok"), just make sure the first line of the comment doesn't
  start with a tab character ;)
- To start a new thread, add some text after the final line containing only
  the special marker `𑁍`.  You can copy this marker line to start several
  new threads.

After modifying the `*.gl` files you can run `gl.py submit issue-or-mr` to
send your comments to GitLab. Pass the same parameter you used for
`fetch`.

This can submit these updates to GitLab:
- Update your modified comments.
- Add your new comments to existing threads.
- Add your new threads.
- Finally, fetch all new comments from GitLab.

Remember, you can use the `--dry-run` option to first check what API requests
would be performed before blowing up your coworker's inbox.


### Merge Requests (MRs)

MRs are fairly similar to issues, with some differences explained here.

Assuming MR 456 is based off branch `the-source-branch` in the first remote,
each of these three commands fetches the same MR comments:

```shell
$ gl.py fetch https://gitlab.com/example/example/-/merge_requests/5
$ gl.py fetch the-source-branch
$ gl.py fetch gl/the-source-branch/todo.gl
```

An MR is identified by its sourch-branch name, not the numeric ID.  This is
because it's easier to remember (at least parts of) the branch name than a
meaningless ID.

The file structure looks a bit different from the an issues single `comments.gl` file:

```shell
$ tree gl/the-source-branch
gl/
└── the-source-branch
    ├── meta.gl
    ├── resolved.gl
    └── todo.gl
```

- `meta.gl` contains the equivalent of the "metadata" part of an issue's `comments.gl`.
  Hence this includes the MR title, the description and metadata like reviewers, lables, etc..
- `todo.gl` contains all *unresolved* threads.
- `resolved.gl` is just like `todo.gl` but contains all *resolved* threads.
- `review.gl` is missing by default, but will hold your pending review
  comments (see the following section).

A single MR thread looks like this:

```diff
README.md:2: 36fec6809fa431d765cc1654a3e8c2d8d04b7cbc
 476046058 The commit subject
 # My to-do list
-* [ ] Publish `gl.py`
+* [x] Publish `gl.py`
	[rreviewer] 🐣
```

Observe:
- The start of the thread is marked by a line starting with `<file>:<line> <thread ID>`. 
- The next line shows the abbreviated commit SHA and subject
- There are a few lines of diff context.
- Finally, the thread's comments are shown.

Adding comments works just like for issues.
Also, anywhere in the above thread, you can add lines that only
contain single letters `r` or `u` to resolve or unresolve the surrounding
thread, respectively. These lines are otherwise ignored.

Running `gl.py submit <branch>` will submit the same data as for issues.
Additionally it will resolve/unresolve threads as specified, and submit
review comments from `review.gl`.

#### Drafting review comments

`gl.py discuss` can be used to draft review comments on specific diff lines.
It currently takes a very specific set of positional parameters.  This is
not a problem when using the [Tig integration](#Tig), but maybe this could
be simplified.
1. the source branch name.
2. the commit SHA of the diff to comment on.
3. the filename; the full relative path.
4. the first character of the diff line
   - `"+"` if the line is added
   - `"-"` if the line deleted
   - `" "` (a space) for context lines
   You can also pass the entire line, only the first character is relevant.
5. the line number in the old version of the file
6. the line number in the new version of the file

A command like `gl.py discuss <branch> <commit> <file> + <old_line> <new_line>`
will add an entry like this to a MR's `review.gl`:

```diff
𑁍 9d163415852de25b9c1f0706126c75d8ad8aef85 README.md:7 + 3
+1
+2
+3
+4
+5
```

- The first line contains most of the parameters passed to `gl.py discuss`:
  the commit SHA, filename, new  line number, and old line number of the
  pending comment.
- This is followed by some context lines. The last line (here `+5`) is the one
  you are commenting on.

After appending the above template to `review.gl`, `gl.py discuss` will invoke
`$EDITOR +123123 gl/<branch>/review.gl`, so your cursor should be placed
right after the `+5` line. Simply add your comment here. It will be
sent the next time you run `gl.py submit`.

### Miscellanea

- `gl.py fetchstatic` fetches users, milestone and label data. It may be necessary
  to run this once. TODO: run it automatically.
  - Set `GITLAB_GROUP` to your GitLab group to fetch group-scoped milestones and labels.
- The `gl` file extension was chosen to simplify filetype detection in editors.
- There are several other subcommands which are not covered here, see `gl.py --help`.

### Resolving conflicts between local and new incoming comments

Whenever you fetch the comments of an issue or MR, there is the potential
for a conflict between incoming comments, and comments you have not
yet submitted. The diverging local and remote threads are merged using
[`git merge-file`](https://git-scm.com/docs/git-merge-file). If there is
a merge conflict, please resolve them and remove conflict markers before
fetching/submitting again. Currently there is no error when there is
a conflict - I always run `gl.py fetch` from my editor, with the file I
edited already open, so I'd notice straight away.

## Integrations

### [Tig](https://jonas.github.io/tig/)

Browse MR commits by going to the `refs` view (shortcut `r`) and selecting
one of the MR branches (usually `origin/the-source-branch`).  When scrolling
through a commit diff, you can add review comments with a binding like this
one (by typing `ac`):

```
bind generic ac !gl.py discuss %(branch) %(commit) %(file) %(text) %(lineno_old) %(lineno)
```

### [Kakoune](https://kakoune.org)

Some basic comfort features:

```kak
hook global BufCreate .*[.]gl %{
	# The "file:line" locations  are compatible with the "grep" filetype,
	# so we can use that to jump to the referenced line by pressing Enter.
	set-option buffer filetype grep
	# Highlight diff context lines.
	add-highlighter buffer/gl-diff ref diff
	# GitLab comments tend to be long lines, soft-wrap them.
	add-highlighter buffer/gl-wrap wrap -word -indent -marker <
}
```

Since we are already editing `*.gl` files in our editor, we should teach
the editor to run `gl.py fetch` and `gl.py submit` for the current file.

```kak
define-command -override gl-fetch -docstring %{
	Fetch new comments for the current issue/MR
} %{
	evaluate-commands %sh{
		gl.py fetch "$kak_buffile" >&2 ||
		echo fail "gl.py error"
	}
}
define-command -override gl-submit -docstring %{
	Submit comments for the current issue/MR
} %{
	write -sync
	evaluate-commands %sh{
		gl.py submit "$kak_buffile" >&2 ||
		echo fail "gl.py error"
		# The review draft will be deleted, so  switch to the unresolved threads.
		[ ${kak_bufname##*/} = review.gl ] && echo edit ${kak_bufname%/*}/todo.gl
	}
}
```

GitLab email notifications and browser tabs both give you URLs to issues
or MRs. Let's teach the editor to visit those links:

```kak
define-command -override gl-visit-url-from-clipboard -docstring %{
	Read a GitLab URL from system clipboard and visit the corresponding file.
	Fetch the latest comments of this issue or MR in the background.
} %{
	edit %sh{
		set -e
		path="$(gl.py url2path "$(xclip -out)")"
		printf %s "$path"
		( gl.py fetch "$path" </dev/null >/dev/null 2>&1 ) &
	}
}
```

Finally, it can be convenient to quickly switch to the browser for some tasks:

```kak
define-command gl-browse-url -docstring %{
	Open the current file's GitLab issue or MR page in the browser
} %{ nop %sh{
	xdg-open "$(gl.py path2url "$kak_buffile")"
} }
```
