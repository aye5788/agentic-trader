"""Model pins for every unattended role — one config, a local override, a chain.

WHY (2026-09-03). The session runner, the exit executor and the newsletter
each pinned a model id as a literal in code. When Opus 5 went down, swapping
the runner meant a code edit, a reload, a commit of a temporary value to main
and a memory note to revert it. The exit executor — which IS the stop — had
nothing behind its pin at all. Now every role reads config/models.toml at
spawn time; config/models.local.toml (git-ignored, human) merges over it the
same way strategy.local.toml does; and each role has an ordered chain that
src/fallback.py walks on a clean failure.

Pure over two TOML files. 3.10-SAFE: the monitor imports this under
/usr/bin/python3 (tomli stands in for tomllib there, as in strategy.py).

    .venv/bin/python -m models <role>     # prints the role's primary id
"""
from __future__ import annotations

try:
    import tomllib                      # stdlib, Python 3.11+
except ImportError:                     # pragma: no cover - system python3.10
    import tomli as tomllib             # type: ignore[no-redef]
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE_PATH = REPO / "config" / "models.toml"
LOCAL_PATH = REPO / "config" / "models.local.toml"
ROLES = ("session", "exit", "newsletter")
#: Roles that walk a chain. The newsletter does not: no money, and a failed
#: letter is re-run by hand.
CHAINED = ("session", "exit")
NONE = "none"


def _merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(base: Path = BASE_PATH, local: Path = LOCAL_PATH) -> dict:
    """The merged config. Local (human) wins over base (committed)."""
    with open(base, "rb") as f:
        cfg = tomllib.load(f)
    if local.exists():
        with open(local, "rb") as f:
            _merge(cfg, tomllib.load(f))
    cfg.setdefault("roles", {})
    cfg.setdefault("chain", {}).setdefault("fallbacks", [])
    cfg["chain"].setdefault("cli_retry_after_s", 30)
    cfg.setdefault("terminal", {})
    cfg.setdefault("budget", {}).setdefault("step", NONE)
    cfg.setdefault("requires", {})
    return cfg


def primary(role: str, cfg: dict | None = None) -> str:
    """The role's pinned model id. Raises KeyError for a role nobody declared."""
    cfg = cfg or load()
    if role not in ROLES:
        raise KeyError(f"unknown model role {role!r}; roles are {ROLES}")
    m = cfg["roles"].get(role)
    if not isinstance(m, str) or not m.strip():
        raise KeyError(f"models.toml [roles] has no id for {role!r}")
    return m.strip()


def chain(role: str, cfg: dict | None = None) -> list:
    """[primary, *fallbacks] with duplicates removed, in order. Unchained
    roles get [primary] only."""
    cfg = cfg or load()
    first = primary(role, cfg)
    if role not in CHAINED:
        return [first]
    out = [first]
    for m in cfg["chain"].get("fallbacks") or []:
        m = str(m).strip()
        if m and m not in out:
            out.append(m)
    return out


def terminal(role: str, cfg: dict | None = None) -> str:
    """The role's model-free last step, or "none"."""
    cfg = cfg or load()
    return str(cfg["terminal"].get(role) or NONE)


def cli_retry_after_s(cfg: dict | None = None) -> int:
    cfg = cfg or load()
    return int(cfg["chain"].get("cli_retry_after_s") or 30)


def requires(cfg: dict | None = None) -> dict:
    """{model id: minimum Claude Code version string}."""
    cfg = cfg or load()
    return {str(k): str(v) for k, v in (cfg.get("requires") or {}).items()}


def provenance() -> dict:
    """Which files produced the live config — for health and the dashboard."""
    return {"base": str(BASE_PATH),
            "local": str(LOCAL_PATH) if LOCAL_PATH.exists() else None}


def _selftest() -> None:
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "models.toml"
        local = Path(td) / "models.local.toml"
        base.write_text(
            '[roles]\nsession = "m-a"\nexit = "m-b"\nnewsletter = "m-a"\n'
            '[chain]\nfallbacks = ["m-c", "m-a", "m-d"]\ncli_retry_after_s = 5\n'
            '[terminal]\nsession = "none"\nexit = "code_seller"\n'
            '[requires]\n"m-d" = "2.1.251"\n')
        cfg = load(base, local)
        assert primary("session", cfg) == "m-a" and primary("exit", cfg) == "m-b"
        # the primary is deduped out of the fallbacks, order kept
        assert chain("session", cfg) == ["m-a", "m-c", "m-d"], chain("session", cfg)
        assert chain("exit", cfg) == ["m-b", "m-c", "m-a", "m-d"], chain("exit", cfg)
        # the newsletter never walks a chain
        assert chain("newsletter", cfg) == ["m-a"]
        assert terminal("exit", cfg) == "code_seller" and terminal("session", cfg) == "none"
        assert cli_retry_after_s(cfg) == 5
        assert requires(cfg) == {"m-d": "2.1.251"}
        assert load(base, local)["budget"]["step"] == "none"      # defaulted
        # the local override wins, per key, and can replace the fallback list
        local.write_text('[roles]\nsession = "m-z"\n[chain]\nfallbacks = ["m-y"]\n')
        cfg = load(base, local)
        assert primary("session", cfg) == "m-z" and primary("exit", cfg) == "m-b"
        assert chain("session", cfg) == ["m-z", "m-y"], chain("session", cfg)
        # unknown / undeclared roles raise rather than default
        for bad in ("reviewer", "monitor"):
            try:
                primary(bad, cfg)
            except KeyError:
                pass
            else:
                raise AssertionError(f"{bad} must raise")
        base.write_text('[roles]\nsession = ""\n')
        try:
            primary("session", load(base, Path(td) / "absent.toml"))
        except KeyError:
            pass
        else:
            raise AssertionError("an empty id must raise")
    # the committed file itself parses and names every role
    live = load()
    for r in ROLES:
        assert primary(r, live)
    assert chain("session", live)[0] == primary("session", live)
    print("models: OK -- merge precedence, dedupe, unchained newsletter, terminal, "
          "requires, unknown/empty role raise; committed config names every role")


if __name__ == "__main__":
    import sys  # noqa: PLC0415
    if len(sys.argv) > 1 and sys.argv[1] == "--" + "selftest":
        _selftest()
    elif len(sys.argv) > 1:
        print(primary(sys.argv[1]))
    else:
        print(load())
