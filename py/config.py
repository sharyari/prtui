"""Read configuration from config file."""

from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config"


def read_config():
    """Read key:value pairs from the config file."""
    cfg = {}
    with open(CONFIG_PATH) as fp:
        for line in fp:
            line = line.strip()
            if not line:
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
