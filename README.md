# prtui

A terminal UI for managing your GitHub pull request inbox, built with [Textual](https://textual.textualize.io/).

Shows your PRs, PRs you've reviewed, and PRs requested via team assignment. Displays comments, reviews, commits, and CI status inline.

## Install

1. Ensure your Python has sqlite3 support:

```bash
python3 -c "import sqlite3"
```

If this fails, install the dev library and rebuild Python:

```bash
# Debian/Ubuntu
sudo apt install libsqlite3-dev
```

If using pyenv, reinstall the Python version afterwards:
`pyenv install --force <version>`

2. Create a Python venv and source it:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

NOTE: the prtui wrapper script is assuming the venv name .venv, update it
if you want to use something else

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create a `config` file in the project root:

```
username:<github-username>
team:<org>/<team-slug>
token:<github-personal-access-token>
repos:<owner/repo>,<owner/repo2>
jenkins-user:<jenkins-bot-username>
```

5. Add to PATH
Assuming zsh, run this when standing in the root of the repo
```
echo "export PATH=\"\$PATH:$(pwd)\"" >> ~/.zshrc
```

## Usage

```bash
cd py && python prtui.py
```
or if added to PATH:
```
prtui
```

### Window mode

Launch prtui in a native desktop window, detached from the terminal:

```
prtui --window
```

The window remembers its size, position, and screen between launches. The first
launch is maximized. If the previous screen is unavailable, the window is fitted
onto an available screen. Window geometry is saved in `.window-state.json` in the
project root; delete that file to reset it.

The UI is served on a random port on `127.0.0.1` only, embedded via
[textual-serve](https://github.com/Textualize/textual-serve) and
[pywebview](https://pywebview.flowrl.com/). Requires a local graphical
desktop (not SSH).

## Data

PR data is stored in a SQLite database at `/tmp/prtui.db`. Delete it to force a full re-fetch.

## Tests

Unit tests live in `tests/` and use the standard library `unittest`. Run them
from the repo root with the venv active:

```bash
python -m unittest discover -s tests -v
```


## Configuration
In order to have working quick links to CI and ticketing system, optional
regex configurations can be set in the config.

Ticket: the PR title is scanned and matched against regex
CI: The latest comment from the CI user is scanned and matched against regex

`team` must include the org (`<org>/<team-slug>`); prtui refuses to start
otherwise.

An optional fourth table can be added with a raw
[GitHub search query](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests).
The query is used as-is, so add `is:open`, `repo:` etc. yourself. PRs already
shown in one of the other tables are not repeated in it.

```
custom-query:is:pr is:open mentions:@me
custom-query-label:Mentions
```

Table columns can be hidden by listing their titles (case-insensitive) in
`exclude-columns`. Available columns: `#`, `Repo`, `Target`, `Source`,
`Title`, `Author`, `Stack`, `Rdy`, `CI`, `App`, `Mrg`. By default `Author` is
hidden, and `Repo` too if only one repo is configured; leave the value empty
to show all columns.

```
exclude-columns:Repo,Stack
```

When a setting with a default (currently only `exclude-columns`) is missing
from `config`, prtui appends it with its default value and a short comment.
Existing settings are never changed.
