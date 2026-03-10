"""Read configuration from config file."""

from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config"
STATE_PATH = Path(__file__).resolve().parent.parent / ".state"


def read_config():
    """Read key:value pairs from the config file."""
    cfg = {}
    with open(CONFIG_PATH) as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split(":", 1)
            cfg[key] = value

    repos = []
    for r in cfg.get("repos", "").split(","):
        r = r.strip()
        if r:
            repos.append(r)
    cfg["repos"] = repos
    cfg["jenkins-user"] = cfg.get("jenkins-user", "")

    return cfg


def load_theme() -> str:
    """Return saved theme name, defaulting to textual-dark."""
    try:
        return STATE_PATH.read_text().strip()
    except FileNotFoundError:
        return "textual-dark"


def save_theme(theme: str) -> None:
    """Persist theme name."""
    STATE_PATH.write_text(theme)
