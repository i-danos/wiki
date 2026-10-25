#!/usr/bin/env python3
import json
import os
import re
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

BASE_URL = "https://danosproject.atlassian.net/wiki"
SPACE_KEY = "DAN"
WORKSPACE = Path(__file__).resolve().parent
DATA_DIR = WORKSPACE / "wiki_clone"
PAGES_DIR = DATA_DIR / "pages"
INDEX_PATH = DATA_DIR / "index.json"
EPUB_PATH = WORKSPACE / "danos_wiki.epub"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def configure_authentication():
    cookie = os.environ.get("DANOS_COOKIE", "").strip()
    cookie_file = os.environ.get("DANOS_COOKIE_FILE", "").strip()
    if not cookie and cookie_file:
        cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
    if cookie:
        SESSION.headers["Cookie"] = cookie


def fetch_json(url: str):
    resp = SESSION.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def slugify(title: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", title.strip().lower())
    value = value.strip("-")
    return value or "page"


def normalize_title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def normalize_html(raw_html: str) -> str:
    if not raw_html:
        return "<p>No content available.</p>"

    soup = BeautifulSoup(raw_html, "html.parser")

    for macro in list(soup.find_all("ac:structured-macro")):
        if (macro.get("ac:name") or macro.get("name") or "").lower() != "code":
            continue
        body = macro.find("ac:plain-text-body")
        if body is None:
            continue
        language = ""
        for parameter in macro.find_all("ac:parameter"):
            if (parameter.get("ac:name") or parameter.get("name") or "").lower() == "language":
                language = parameter.get_text(strip=True)
                break
        code = soup.new_tag("code")
        if language:
            code["class"] = [f"language-{slugify(language)}"]
        code.string = body.get_text()
        pre = soup.new_tag("pre")
        pre.append(code)
        macro.replace_with(pre)

    for tag in list(soup.find_all(True)):
        name = tag.name.lower()
        if name.startswith(("ac", "ri", "atlassian")) or ":" in tag.name:
            tag.unwrap()

    for tag in list(soup.find_all(True)):
        if tag.name and any(token in tag.name.lower() for token in ["placeholder", "parameter", "inline-comment-marker", "structured-macro", "content-by-label", "link-body"]):
            tag.unwrap()

    cleaned = str(soup)
    cleaned = cleaned.replace("&nbsp;", " ")
    cleaned = re.sub(r"<br\s*/?>", "<br />", cleaned, flags=re.I)
    return cleaned


def resolve_page_for_url(href: str, lookup: dict[str, dict]) -> dict | None:
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        if "/wiki/spaces/" not in href and "/wiki/rest/" not in href:
            return None
    text = href.strip()
    if "pageId=" in text:
        q = re.search(r"pageId=(\d+)", text)
        if q:
            pid = q.group(1)
            return lookup.get(pid)
    m = re.search(r"/pages/(\d+)", text)
    if m:
        pid = m.group(1)
        return lookup.get(pid)
    last = text.rstrip("/").split("/")[-1]
    if last:
        last_decoded = last.replace("+", " ")
        if "?" in last_decoded:
            last_decoded = last_decoded.split("?", 1)[0]
        if "%" in last_decoded:
            try:
                from urllib.parse import unquote
                last_decoded = unquote(last_decoded)
            except Exception:
                pass
        for key in [normalize_title_key(last_decoded), normalize_title_key(last_decoded.replace("-", " "))]:
            page = lookup.get(key)
            if page:
                return page
    return None


def rewrite_internal_links(html: str, page_lookup: dict[str, dict], href_prefix: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    replacements = 0
    unresolved = 0

    for tag in list(soup.find_all('ac:link')):
        ri = tag.find('ri:page')
        if ri is None:
            continue
        title = ri.get("ri:content-title") or ri.get("content-title") or tag.get_text(strip=True)
        page = page_lookup.get(normalize_title_key(title))
        if page is None:
            unresolved += 1
            continue
        new_tag = soup.new_tag('a', href=f"{href_prefix}{page['file_name']}")
        new_tag.string = title
        tag.replace_with(new_tag)
        replacements += 1

    for tag in soup.find_all('a', href=True):
        href = tag.get('href', '')
        if href.startswith('#'):
            continue
        page = resolve_page_for_url(href, page_lookup)
        if page is None:
            continue
        tag['href'] = f"{href_prefix}{page['file_name']}"
        replacements += 1

    html_out = str(soup)
    html_out = html_out.replace('<ac:link', '<a').replace('</ac:link>', '</a>')
    html_out = html_out.replace('<ac:link-body', '<span').replace('</ac:link-body>', '</span>')
    html_out = html_out.replace('<ri:page', '<span').replace('</ri:page>', '</span>')
    return html_out


def fetch_page_comments(page_id: str) -> list[dict]:
    comments = []
    start = 0
    while True:
        data = fetch_json(
            f"{BASE_URL}/rest/api/content/{page_id}/child/comment"
            f"?limit=100&start={start}&expand=body.view,version"
        )
        results = data.get("results", [])
        comments.extend(results)
        if len(results) < 100:
            return comments
        start += len(results)


def comments_to_html(page: dict) -> str:
    comments = fetch_page_comments(page["id"])
    if not comments:
        return ""

    sections = ["<section class='official-comments'><h2>Official comments</h2>"]
    for comment in comments:
        version = comment.get("version", {})
        author = version.get("by", {}).get("publicName", "Unknown author")
        when = version.get("friendlyWhen") or version.get("when", "")
        value = comment.get("body", {}).get("view", {}).get("value", "")
        content = normalize_html(value)
        if not BeautifulSoup(content, "html.parser").get_text(" ", strip=True):
            continue
        metadata = escape(f"{author} - {when}" if when else author)
        sections.append(f"<article class='official-comment'><p class='comment-meta'>{metadata}</p>{content}</article>")
    sections.append("</section>")
    return "".join(sections) if len(sections) > 2 else ""


RELATED_CONTENT = {
    "Community": [
        "Working with configuration",
        "Configd",
        "Basic system configuration",
        "Configd Client Library",
        "C Configd Client Library",
        "Ruby Configd Client Library",
    ],
}


def related_content_to_html(page: dict, lookup: dict[str, dict]) -> str:
    titles = RELATED_CONTENT.get(page["title"], [])
    links = []
    for title in titles:
        related = lookup.get(normalize_title_key(title))
        if related is None:
            continue
        links.append(
            f"<li><a href='{escape(related['file_name'], quote=True)}'>{escape(related['title'])}</a></li>"
        )
    if not links:
        return ""
    return "<section class='related-content'><h2>Related content</h2><ul>" + "".join(links) + "</ul></section>"


def page_to_html(page: dict, lookup: dict[str, dict], href_prefix: str = "") -> str:
    title = page["title"]
    body_values = [
        page.get("body_html", ""),
        page.get("body", {}).get("storage", {}).get("value", ""),
        page.get("body", {}).get("view", {}).get("value", ""),
        page.get("body", {}).get("export_view", {}).get("value", ""),
    ]
    cleaned = max((normalize_html(body) for body in body_values if body), key=len, default="<p>No content available.</p>")
    if not BeautifulSoup(cleaned, "html.parser").get_text(" ", strip=True):
        cleaned = comments_to_html(page) or "<p>No content available.</p>"
    related = related_content_to_html(page, lookup)
    cleaned = rewrite_internal_links(cleaned, lookup, href_prefix)
    return f"<h1>{escape(title)}</h1>{related}{cleaned}"


def build_navigation(pages: list[dict], current_page: dict | None, href_prefix: str, overview_href: str) -> str:
    children = {page["id"]: [] for page in pages}
    roots = []
    for page in pages:
        ancestors = page.get("ancestors", [])
        parent_id = ancestors[-1]["id"] if ancestors else None
        if parent_id in children and parent_id != page["id"]:
            children[parent_id].append(page)
        else:
            roots.append(page)

    def contains_current(page: dict) -> bool:
        if current_page is None:
            return False
        return page["id"] == current_page["id"] or any(
            ancestor.get("id") == page["id"] for ancestor in current_page.get("ancestors", [])
        )

    def render_nodes(nodes: list[dict]) -> str:
        rendered = []
        for page in nodes:
            active = " active" if current_page and page["id"] == current_page["id"] else ""
            page_link = f"{href_prefix}{page['file_name']}"
            label = f"<a href='{escape(page_link, quote=True)}'>{escape(page['title'])}</a>"
            nested = children.get(page["id"], [])
            if nested:
                open_attr = " open" if (
                    current_page is not None and contains_current(page)
                ) or (current_page is None and page["id"] == "393488") else ""
                rendered.append(
                    f"<li class='nav-group{active}'><details{open_attr}>"
                    f"<summary>{label}</summary><ul>{render_nodes(nested)}</ul>"
                    f"</details></li>"
                )
            else:
                rendered.append(f"<li class='nav-item{active}'>{label}</li>")
        return "".join(rendered)

    return (
        f"<li class='nav-overview'><a href='{escape(overview_href, quote=True)}'>Overview</a></li>"
        f"{render_nodes(roots)}"
    )


def render_page_shell(title: str, body_html: str, pages: list[dict], current_page: dict | None = None, href_prefix: str = "", overview_href: str = "../index.html") -> str:
    nav_items = build_navigation(pages, current_page, href_prefix, overview_href)

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <title>{escape(title)} - DANOS Wiki Mirror</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 17px; margin: 0; background: #f8f9fb; color: #1f2937; }}
    .layout {{ --sidebar-width: clamp(220px, 25vw, 520px); display: flex; min-height: 100vh; }}
    .sidebar {{ width: var(--sidebar-width); flex: 0 0 var(--sidebar-width); min-width: 220px; max-width: 520px; background: #111827; color: #f9fafb; padding: 1rem; overflow: auto; position: sticky; top: 0; height: 100vh; box-sizing: border-box; }}
    .sidebar-resizer {{ flex: 0 0 6px; cursor: col-resize; background: transparent; position: sticky; top: 0; height: 100vh; z-index: 2; }}
    .sidebar-resizer:hover, .sidebar-resizer.dragging {{ background: #93c5fd; }}
    .sidebar h2 {{ font-size: 1.1rem; margin: 0 0 1rem; }}
    .sidebar ul {{ list-style: none; margin: 0; padding: 0; }}
    .sidebar li {{ margin: 0; }}
    .sidebar a {{ color: #dbeafe; text-decoration: none; }}
    .sidebar a:hover {{ text-decoration: underline; }}
    .sidebar .active, .sidebar summary:hover, .sidebar .nav-overview:hover {{ background: rgba(255,255,255,0.08); border-radius: 6px; }}
    .sidebar .active a {{ font-weight: 700; }}
    .sidebar .nav-overview, .sidebar summary, .sidebar .nav-item {{ padding: 0.35rem 0.5rem; }}
    .sidebar details > ul {{ padding-left: 1rem; }}
    .sidebar summary {{ cursor: pointer; color: #dbeafe; list-style-position: outside; }}
    .sidebar summary::marker {{ color: #94a3b8; }}
    .content {{ flex: 1 1 auto; min-width: 0; max-width: 1100px; padding: 2rem 2.5rem 4rem; overflow: hidden; }}
    .content article {{ background: white; border-radius: 12px; padding: 2rem 2.5rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); min-width: 0; overflow: hidden; overflow-wrap: anywhere; }}
    .contentLayout2, .columnLayout, .cell, .innerCell {{ max-width: 100%; box-sizing: border-box; }}
    .columnLayout {{ width: auto !important; }}
    h1, h2, h3 {{ color: #111827; }}
    p, li {{ line-height: 1.7; }}
    code {{ font-size: 1rem; background: #f3f4f6; padding: 0.15rem 0.3rem; border-radius: 4px; }}
    pre {{ font-size: 1rem; background: #111827; color: #f9fafb; padding: 1rem; border-radius: 8px; overflow-x: auto; line-height: 1.5; }}
    pre code {{ background: transparent; padding: 0; color: inherit; }}
    img, video, iframe {{ max-width: 100%; height: auto; }}
    .content article img {{ display: block; object-fit: contain; }}
    .table-wrap {{ max-width: 100%; overflow-x: auto; margin: 1rem 0; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 100%; margin: 1rem 0; }}
    th, td {{ border: 1px solid #d1d5db; padding: 0.5rem 0.75rem; text-align: left; }}
    pre {{ max-width: 100%; box-sizing: border-box; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .official-comments {{ border-top: 1px solid #d1d5db; margin-top: 2rem; padding-top: 1rem; }}
    .official-comment {{ border-bottom: 1px solid #e5e7eb; padding: 0.75rem 0; }}
    .comment-meta {{ color: #6b7280; font-size: 0.9rem; margin: 0 0 0.5rem; }}
    .related-content {{ border-top: 1px solid #d1d5db; margin-top: 2rem; padding-top: 1rem; }}
    .related-content ul {{ columns: 2; }}
    a {{ color: #0f172a; }}
    @media (max-width: 900px) {{ .layout {{ display: block; }} .sidebar {{ width: auto; max-width: none; height: auto; position: static; }} .sidebar-resizer {{ display: none; }} .content {{ padding: 1rem; }} article {{ padding: 1rem; }} }}
  </style>
</head>
<body>
  <div class='layout'>
    <aside class='sidebar'>
      <ul>
        {nav_items}
      </ul>
    </aside>
        <div class='sidebar-resizer' role='separator' aria-label='Resize navigation' aria-orientation='vertical' tabindex='0'></div>
    <main class='content'>
      <article>
        {body_html}
      </article>
    </main>
  </div>
    <script>
        (() => {{
            const layout = document.querySelector('.layout');
            const resizer = document.querySelector('.sidebar-resizer');
            const storageKey = 'danos-wiki-sidebar-width';
            const minWidth = 220;
            const maxWidth = 520;
            const savedWidth = Number(localStorage.getItem(storageKey));
            if (savedWidth >= minWidth && savedWidth <= maxWidth) {{
                layout.style.setProperty('--sidebar-width', `${{savedWidth}}px`);
            }}
            let dragging = false;
            const setWidth = (clientX) => {{
                const width = Math.max(minWidth, Math.min(maxWidth, clientX - layout.getBoundingClientRect().left));
                layout.style.setProperty('--sidebar-width', `${{width}}px`);
                localStorage.setItem(storageKey, String(Math.round(width)));
            }};
            resizer.addEventListener('pointerdown', (event) => {{
                dragging = true;
                resizer.classList.add('dragging');
                resizer.setPointerCapture(event.pointerId);
                setWidth(event.clientX);
            }});
            resizer.addEventListener('pointermove', (event) => {{
                if (dragging) setWidth(event.clientX);
            }});
            resizer.addEventListener('pointerup', () => {{
                dragging = false;
                resizer.classList.remove('dragging');
            }});
            resizer.addEventListener('keydown', (event) => {{
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
                event.preventDefault();
                const current = parseFloat(getComputedStyle(layout).getPropertyValue('--sidebar-width')) || 300;
                setWidth(layout.getBoundingClientRect().left + current + (event.key === 'ArrowRight' ? 16 : -16));
            }});
        }})();
    </script>
</body>
</html>
"""


def page_from_api(item: dict) -> dict:
    title = item.get("title", "Untitled")
    return {
        "id": str(item.get("id", "")),
        "title": title,
        "slug": slugify(title),
        "url": item.get("_links", {}).get("webui", ""),
        "ancestors": [
            {"id": str(ancestor.get("id", "")), "title": ancestor.get("title", "")}
            for ancestor in item.get("ancestors", [])
        ],
        "body": item.get("body", {}),
        "body_html": item.get("body", {}).get("storage", {}).get("value", ""),
    }


def fetch_child_pages(parent_id: str) -> list[dict]:
    pages = []
    start = 0
    while True:
        data = fetch_json(
            f"{BASE_URL}/rest/api/content/{parent_id}/child/page"
            f"?limit=100&start={start}&expand=body.storage,body.view,body.export_view,metadata.labels,ancestors"
        )
        results = [item for item in data.get("results", []) if item.get("type") == "page"]
        pages.extend(results)
        if len(results) < 100:
            return pages
        start += len(results)


def fetch_space_pages():
    root = page_from_api(fetch_json(
        f"{BASE_URL}/rest/api/content/393488"
        "?expand=body.storage,body.view,body.export_view,metadata.labels,ancestors"
    ))
    pages = []

    def append_branch(page: dict):
        pages.append(page_from_api(page) if "body_html" not in page else page)
        for child in fetch_child_pages(page["id"]):
            append_branch(child)

    append_branch(root)
    return pages


def build_web_index(pages):
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    normalized_pages = []
    for page in pages:
        file_name = f"{page['id']}-{page['slug']}.html"
        page['file_name'] = file_name
        normalized_pages.append(page)

    page_lookup = {p['id']: p for p in normalized_pages}
    for p in normalized_pages:
        page_lookup[normalize_title_key(p['title'])] = p
        page_lookup[normalize_title_key(p['slug'])] = p

    validation = []
    for page in normalized_pages:
        html_body = page_to_html(page, page_lookup)
        page_path = PAGES_DIR / page['file_name']
        page_html = render_page_shell(page["title"], html_body, normalized_pages, page)
        page_path.write_text(page_html, encoding="utf-8")

        soup = BeautifulSoup(html_body, "html.parser")
        internal_links = []
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if href.startswith('http') or href.startswith('#'):
                continue
            if href.startswith('pages/'):
                internal_links.append(href)
            elif '/wiki/' in href or 'pageId=' in href:
                internal_links.append(href)
        validation.append({
            "id": page['id'],
            "title": page['title'],
            "file": page['file_name'],
            "internal_links": internal_links,
            "links_count": len(internal_links),
            "status": "ok" if len(internal_links) >= 0 else "check"
        })

        homepage = next((page for page in normalized_pages if page["id"] == "393488"), None)
        if homepage is None:
                homepage = {"id": "", "title": "DANOS", "ancestors": [], "body_html": "<p>DANOS Wiki</p>"}
        overview_body = page_to_html(homepage, page_lookup, "pages/")
        overview_html = render_page_shell(
                "DANOS",
                overview_body,
                normalized_pages,
                None,
                "pages/",
                "index.html",
        )
        (DATA_DIR / "index.html").write_text(overview_html, encoding="utf-8")
    (DATA_DIR / "validation.json").write_text(json.dumps({"space": SPACE_KEY, "total_pages": len(normalized_pages), "pages": validation}, ensure_ascii=False, indent=2), encoding="utf-8")

    metadata = {"space": SPACE_KEY, "total_pages": len(pages), "pages": [
        {"id": p["id"], "title": p["title"], "slug": p["slug"], "ancestors": p.get("ancestors", []), "source_url": f"{BASE_URL}{p['url']}", "html_file": f"pages/{p['id']}-{p['slug']}.html"}
        for p in normalized_pages
    ]}
    INDEX_PATH.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def build_epub(pages):
    page_lookup = {p['id']: p for p in pages}
    for p in pages:
        page_lookup[normalize_title_key(p['title'])] = p
        page_lookup[normalize_title_key(p['slug'])] = p

    book = epub.EpubBook()
    book.set_identifier(f"danos-{SPACE_KEY.lower()}-wiki")
    book.set_title("DANOS Wiki")
    book.set_language("en")
    book.add_author("DANOS Project")

    chapters = []
    for page in pages:
        chapter = epub.EpubHtml(
            title=page["title"],
            file_name=f"chap_{page['id']}-{page['slug']}.xhtml",
            lang="en",
        )
        chapter.content = page_to_html(page, page_lookup)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [epub.EpubHtml(title="Title", file_name="title.xhtml", content="<h1>DANOS Wiki</h1>"), *chapters]
    book.spine = ["nav", *chapters]
    epub.write_epub(str(EPUB_PATH), book, {})
    print(f"EPUB created at: {EPUB_PATH}")


def main():
    configure_authentication()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    pages = fetch_space_pages()
    build_web_index(pages)
    build_epub(pages)
    print(f"Collected {len(pages)} pages into {DATA_DIR}")


if __name__ == "__main__":
    main()
