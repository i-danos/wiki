# DANOS Wiki mirror and ebook

This workspace contains a local mirror of the public DANOS Confluence space and an EPUB export.

## Steps

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python export_wiki.py
```

If the Confluence space requires login, log in through the browser first and
run the exporter with the authenticated browser Cookie header. Keep the
Cookie value outside the repository:

```bash
DANOS_COOKIE='name=value; another=value' .venv/bin/python export_wiki.py
```

Alternatively, put that header value in a local file and use
`DANOS_COOKIE_FILE=/actual/path/to/cookie.txt`. The exporter does not store
account passwords or Cookie values in the generated files.

```bash
DANOS_COOKIE_FILE=/actual/path/to/cookie.txt .venv/bin/python export_wiki.py
```

Outputs:
- `wiki_clone/index.json` — metadata for the exported page list
- `danos_wiki.epub` — generated ebook

## Source

- https://danosproject.atlassian.net/wiki/spaces/DAN/overview?homepageId=39348
