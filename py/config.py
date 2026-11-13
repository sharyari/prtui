"""Read configuration from config file."""

from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config"
STATE_PATH = Path(__file__).resolve().parent.parent / ".state"

def _default_exclude_columns(cfg):
    repos = [r for r in cfg.get("repos", "").split(",") if r.strip()]
    return "Author,Repo,Note" if len(repos) == 1 else "Author,Note"


# Appended to the config file when the key is missing, so changed defaults
# are visible and editable there. Existing keys, even empty ones, are kept.
# Values are computed from the raw config read so far.
DEFAULTS = {
    "exclude-columns": (_default_exclude_columns,
                        "Columns to hide, e.g. Author,Repo. Empty shows all."),
}


def _add_missing_defaults(text, cfg):
    missing = [key for key in DEFAULTS if key not in cfg]
    if not missing:
        return
    lines = [] if not text or text.endswith("\n") else [""]
    for key in missing:
        default, comment = DEFAULTS[key]
        value = default(cfg)
        lines += [f"# {comment}", f"{key}:{value}"]
        cfg[key] = value
    with open(CONFIG_PATH, "a") as fp:
        fp.write("\n".join(lines) + "\n")


def read_config():
    """Read key:value pairs from the config file."""
    cfg = {}
    text = CONFIG_PATH.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        cfg[key] = value
    _add_missing_defaults(text, cfg)

    repos = []
    repo_name_map = {}
    for r in cfg.get("repos", "").split(","):
        r = r.strip()
        if ':' in r:
            [r, r_to] = r.split(':', 1)
            repo_name_map[r] = r_to
        if r:
            repos.append(r)
    cfg["repos"] = repos
    cfg["repo-name-map"] = repo_name_map
    cfg["jenkins-user"] = cfg.get("jenkins-user", "")
    cfg["auto-update"] = cfg.get("auto-update", "false").strip().lower() != "false"
    cfg["show-drafts"] = cfg.get("show-drafts", "true").strip().lower() != "false"
    cfg["exclude-columns"] = {c.strip().lower()
                              for c in cfg.get("exclude-columns", "").split(",")
                              if c.strip()}

    return cfg


def _detect_terminal_theme() -> str:
    """Detect light/dark via OSC 11. Returns a Textual theme name."""
    import sys, termios, tty, select
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "textual-dark"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\033]11;?\007")
        sys.stdout.flush()
        if not select.select([sys.stdin], [], [], 0.2)[0]:
            return "textual-dark"
        response = ""
        while True:
            char = sys.stdin.read(1)
            response += char
            if char == "\007" or response.endswith("\033\\"):
                break
    except Exception:
        return "textual-dark"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if "rgb:" in response:
        parts = response.split("rgb:")[1].split("/")
        r, g, b = (int(p[:2], 16) / 255.0 for p in parts[:3])
        return "textual-light" if 0.2126*r + 0.7152*g + 0.0722*b > 0.5 else "textual-dark"
    return "textual-dark"


def load_theme() -> str:
    """Return saved theme name, or detect from terminal if none saved."""
    try:
        return STATE_PATH.read_text().strip()
    except FileNotFoundError:
        return _detect_terminal_theme()


def save_theme(theme: str) -> None:
    """Persist theme name."""
    STATE_PATH.write_text(theme)
