# x-mcp

Read X/Twitter **without an account**: a core library that scrapes public
Nitter mirrors, a console CLI (`x-cli`) and an MCP stdio server (`x-mcp`)
exposing the same functionality to AI agents.

## How it works

1. `tracker.py` discovers live instances from the community status page
   (https://status.d420.de/).
2. `instances.py` probes each one concurrently (max 8), classifies it
   (`ok`, `anubis`, `gandalf`, `cloudflare`, `forbidden`, `timeout`, `error`)
   and checks whether search is enabled. Results are cached in
   `$XDG_CACHE_HOME/x-mcp/instances.json` (TTL 1 hour).
3. `challenge.py` solves Anubis proof-of-work challenges (sha256 "fast"/"slow")
   in ~100 ms at difficulty 4. Cloudflare and Gandalf are marked unsupported —
   no browser automation.
4. `client.py` fetches a path with automatic failover across the pool and
   reports which instance served the response. An empty timeline is a valid
   result, not a failure.
5. `parse.py` turns Nitter HTML into dataclasses (tweets, quotes, media,
   stats, profile cards, cursors).
6. `media.py` downloads photos through the mirror's `/pic/orig/` proxy
   (falling back to `pbs.twimg.com`) and videos from direct mp4 URLs;
   m3u8-only streams use ffmpeg if it is installed.

## Install

```sh
uv sync                      # inside the project
uv tool install --editable . # installs x-cli and x-mcp globally
```

## CLI

```sh
x-cli instances [--refresh] [--all]
x-cli user NASA --limit 20
x-cli user NASA --replies --pages 2
x-cli user NASA --search-in webb --download-media -o ./x-media
x-cli tweet https://x.com/elonmusk/status/2114765150431682772 --no-replies
x-cli tweet 2114765150431682772 --json
x-cli search "artemis" --pages 2 --limit 30
x-cli search "nasa" --users
x-cli media 2115129428158087498 -o ./x-media
```

Global flags: `--json` (machine output, includes `instance` and
`next_cursor`), `--instance HOST` (pin an instance, repeatable), `-v`
(log every request and instance decision to stderr). `NO_COLOR` is
respected; ANSI colour is only emitted on a tty.

Pin instances with the environment variable `X_MCP_INSTANCES=host1,host2`.

## MCP server

Claude Code:

```sh
claude mcp add x -- x-mcp
```

or in `~/.claude.json` / `.mcp.json`:

```json
{
  "mcpServers": {
    "x": {
      "command": "/home/marcusmae/forge/x-mcp/.venv/bin/x-mcp",
      "args": []
    }
  }
}
```

Tools: `x_instances`, `x_user_timeline`, `x_tweet`, `x_search`,
`x_download_media` (with `return_images: true` to also get MCP image
content). Pagination is via the opaque `next_cursor` string each call
returns.

## Tests

```sh
uv run pytest          # offline, fixture-driven
uv run ruff check      # lint
uv run python scripts/smoke.py     # live end-to-end smoke test
uv run python scripts/mcp_check.py # spawn x-mcp over stdio and call tools
```
