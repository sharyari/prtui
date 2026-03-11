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


def _detect_terminal_theme() -> str:
    """Detect light/dark via OSC 11. Returns a Textual theme name."""
    import sys, termios, tty, select
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
