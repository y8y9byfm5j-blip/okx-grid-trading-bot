"""Explicit, complete, versioned paper profiles. No global defaults are changed."""
from dataclasses import dataclass, fields
import hashlib
from pathlib import Path
import tomllib

from .domain import RiskPolicy, TradingError, canonical, identifier, number


@dataclass(frozen=True)
class PaperProfile:
    name: str
    revision: int
    policy: RiskPolicy
    mode: str = "paper"
    leverage: str = "1"

    def __post_init__(self):
        identifier(self.name)
        if (type(self.revision) is not int or self.revision < 1 or not isinstance(self.policy, RiskPolicy)
                or self.mode != "paper" or number(self.leverage) != 1):
            raise TradingError("INVALID_PAPER_PROFILE")

    @property
    def identity(self):
        values = {"name": self.name, "revision": self.revision, "mode": self.mode,
                  "leverage": "1", "policy": self.policy.to_dict()}
        return {"name": self.name, "revision": self.revision,
                "sha256": hashlib.sha256(canonical(values).encode()).hexdigest()}


def load_profile_file(path):
    try:
        path = Path(path)
        if path.stat().st_size > 65536:
            raise TradingError("INVALID_PAPER_PROFILE")
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(values) != {"name", "revision", "mode", "leverage", "policy"}:
            raise TradingError("INCOMPLETE_PAPER_PROFILE")
        if set(values["policy"]) != {field.name for field in fields(RiskPolicy)}:
            raise TradingError("INCOMPLETE_PAPER_PROFILE")
        values["policy"] = RiskPolicy(**values["policy"])
        return PaperProfile(**values)
    except TradingError:
        raise
    except (OSError, TypeError, ValueError):
        raise TradingError("INVALID_PAPER_PROFILE") from None


def load_profile(name):
    if name != "v5-paper-500":
        raise TradingError("UNKNOWN_PAPER_PROFILE")
    return load_profile_file(Path(__file__).parent / "config" / "v5-paper-500.toml")
