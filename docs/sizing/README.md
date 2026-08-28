# Sizing calculator

`index.html` is the page published at
<https://overcuriousity.github.io/Vestigo/sizing/>; `sizing-constants.json` is what it
computes with, **generated** — never edited by hand:

```bash
uv run python scripts/gen_sizing_constants.py
```

`tests/test_sizing_constants.py` fails when the checked-in JSON no longer matches what
`core/config.py`, `db/_scan.py` and `deploy/clickhouse/memory.xml` say, so a public sizing
page cannot recommend values the app stopped using. It also asserts the page fetches
nothing off-host: an operator sizing an airgapped deployment may well be offline.

## Why `docs/.nojekyll` exists

GitHub Pages runs Jekyll over the published folder by default, and Jekyll parses Liquid
(`{{ … }}`) inside fenced code blocks — including the TSX and template-literal snippets in
the markdown under `docs/`. One such snippet took the *whole site* down, this page included,
which is how it first shipped as a 404.

`.nojekyll` turns that pipeline off. Nothing here needs it: the page is self-contained
static HTML, and the markdown under `docs/` is read on github.com, not served from Pages.
