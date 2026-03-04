# quoteit

Check AI tool usage quotas from the command line.

```bash
quoteit cc          # Claude Code usage (human-readable)
quoteit cc --json   # JSON output
quoteit cc -v       # verbose (shows which fetch method was used)
```

## Install

```bash
uv tool install git+https://github.com/Jarki/quoteit
```

## How it works

Claude Code integration fetches quota data via the OAuth API in `~/.claude/.credentials.json`. Falls back to delegated token refresh (spawns `claude /status` in a PTY) and then to full CLI scraping if the token is expired or missing.

## Adding integrations

Add a file under `src/quoteit/integrations/` that exposes a `fetch_usage() -> UsageResult` function, then register a subcommand in `src/quoteit/cli.py`.
