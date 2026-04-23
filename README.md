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

Launch prtui in a maximized native desktop window, detached from the terminal:

```
prtui --window
```

The UI is served on a random port on `127.0.0.1` only, embedded via
[textual-serve](https://github.com/Textualize/textual-serve) and
[pywebview](https://pywebview.flowrl.com/). Requires a local graphical
desktop (not SSH).

## Data

PR data is stored in a SQLite database at `/tmp/prtui.db`. Delete it to force a full re-fetch.


## Configuration
In order to have working quick links to CI and ticketing system, optional
regex configurations can be set in the config.

Ticket: the PR title is scanned and matched against regex
CI: The latest comment from the CI user is scanned and matched against regex
