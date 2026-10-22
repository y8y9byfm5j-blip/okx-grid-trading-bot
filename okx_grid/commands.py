"""One explicit command registry for the existing owner's dispatch hook."""
from dataclasses import dataclass
import re

from .domain import TradingError, positive

REGISTRY = frozenset({"/durum", "/analiz", "/grid", "/radar", "/paraakisi",
                      "/momentum", "/gridplan", "/papergrid", "/emergency_stop",
                      "/diagnostics", "/paper"})
ALIASES = {"/radargrid": "/radar", "/gridstatus": "/papergrid status"}


def pair_name(value):
    if not isinstance(value, str):
        raise TradingError("INVALID_SPOT_SYMBOL")
    value = value.upper().replace("/", "-").replace("_", "-")
    if "-" not in value:
        value = value[:-4] + "-USDT" if value.endswith("USDT") else value + "-USDT"
    if not re.fullmatch(r"[A-Z0-9]{1,24}-(?:USDT|TRY)", value):
        raise TradingError("INVALID_SPOT_SYMBOL")
    return value


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple = ()

    @property
    def text(self):
        return " ".join((self.name, *self.args))


def parse_command(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 100 or not text.isascii():
        raise TradingError("TELEGRAM_COMMAND_REJECTED")
    parts = text.strip().split()
    if not parts:
        raise TradingError("TELEGRAM_COMMAND_REJECTED")
    name = parts[0].lower()
    if not name.startswith("/"):
        raise TradingError("TELEGRAM_COMMAND_REJECTED")
    alias = ALIASES.get(name, name).split()
    name, args = alias[0], alias[1:] + parts[1:]
    if name not in REGISTRY:
        raise TradingError("UNKNOWN_COMMAND")
    if name in {"/analiz", "/grid", "/gridplan"}:
        if name == "/grid" and not args:  # Preserve the old reviewed-template route.
            return Command(name)
        if len(args) != (2 if name == "/gridplan" else 1):
            raise TradingError("COMMAND_ARGUMENTS_REQUIRED")
        args[0] = pair_name(args[0])
        if name == "/gridplan":
            args[1] = format(positive(args[1]), "f")
    elif name in {"/papergrid", "/paper"}:
        options = {"start", "stop", "status"} if name == "/papergrid" else {"pause", "halt"}
        if name == "/paper" and not args:
            return Command(name)
        if len(args) != 1 or args[0].lower() not in options:
            raise TradingError("COMMAND_ARGUMENTS_REQUIRED")
        args[0] = args[0].lower()
    elif args:
        raise TradingError("COMMAND_ARGUMENTS_REJECTED")
    return Command(name, tuple(args))
