from afcommon.state import DaprStateStore

from .config import ConfigRepo

_config_repo: ConfigRepo | None = None


def get_config_repo() -> ConfigRepo:
    global _config_repo
    if _config_repo is None:
        _config_repo = ConfigRepo(DaprStateStore())
    return _config_repo
