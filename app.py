import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import deque, defaultdict
import re
import datetime
import xml.etree.ElementTree as ET
import io

# ----------------------------
# helpers
# ----------------------------

def is_html(response: requests.Response) -> bool:
    ctype = response.headers.get("Content-Type", "")
    return "text/html" in ctype or "application/xhtml+xml" in ctype

def fetch(url: str, timeout: int = 10):
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "StreamlitSpider/1.0"})
        if resp.status_code == 200:
            return resp
    except Exception:
        return None
    return None

def parse_sitemap(sitemap_url: str):
    """Try to read sitemap.xml and return list of URLs. Return [] if not found or invalid."""
    resp = fetch(sitemap_url)
    if not resp:
        return []
    try:
        root = ET.fromstring(resp.text)
    except Exception:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for url in root.findall("sm:url/sm:loc", ns):
        if url.text:
            urls.append(url.text.strip())
    # also handle sitemap index
    if not urls:
        for smap in root.findall("sm:sitemap/sm:loc", ns):
            child_map = smap.text.strip()
            urls.extend(parse_sitemap(child_map))
    return list(dict.fromkeys(urls))

def clean_text(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # guess main content - simple heuristic
    body = soup.body or soup
    text = []
    for p in body.find_all(["p", "h1", "h2", "h3", "h4", "li"]):
        line = p.get_text(" ", strip=True)
        if line:
            text.append(line)
    return "\n\n".join(text)

def get_title(html: str, fallback: str):
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    # try first h1
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return fallback

def extract_links(base_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # join relative
        full = urljoin(base_url, href)
        # strip fragments
        full = full.split("#")[0]
        if full:
            links.add(full)
    return links

def same_domain(url1: str, url2: str) -> bool:
    return urlparse(url1).netloc == urlparse(url2).netloc

def normalize_url(url: str):
    # drop trailing slash unless root
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    normalized = parsed._replace(path=path, query="", fragment="").geturl()
    return normalized

def slugify(title: str):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip().lower()).strip("-")
    if not slug:
        slug = "page"
    return slug

# ----------------------------
# markdown builder
# ----------------------------
def build_markdown(pages_data, root_url):
    """
    pages_data: list of dicts
      { "url":..., "title":..., "content":..., "links": [...] }
    """
    lines = []
    lines.append(f"# Site export for {root_url}")
    lines.append("")
    lines.append("Generated on: " + datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    lines.append("")
    lines.append("## Sitemap")
    for p in pages_data:
        anchor = slugify(p["title"])
        lines.append(f"- [{p['title']}]('#{anchor}')")
    lines.append("")

    for p in pages_data:
        anchor = slugify(p["title"])
        lines.append(f"\n---\n")
        lines.append(f"## {p['title']}")
        lines.append(f"<a name=\"{anchor}\"></a>")
        lines.append("")
        lines.append(f"**Source:** {p['url']}")
        lines.append("")
        if p["content"].strip():
            lines.append(p["content"].strip())
        else:
            lines.append("_No text content found._")
        lines.append("")

    return "\n".join(lines)

# ----------------------------
# graphviz builder
# ----------------------------
def build_dot_graph(pages_data):
    # pages_data is list of dicts
    dot = ['digraph site {', 'rankdir=LR;', 'node [shape=box, style=rounded];']
    # map url -> short name
    # we keep url as label, but shorten a bit
    for p in pages_data:
        node_name = f'"{p["url"]}"'
        label = p["title"][:50].replace('"', "'")
        dot.append(f'{node_name} [label="{label}"];')
    # edges
    known_urls = {p["url"] for p in pages_data}
    for p in pages_data:
        src = f'"{p["url"]}"'
        for l in p["links"]:
            if l in known_urls:
                dot.append(f'{src} -> "{l}";')
    dot.append("}")
    return "\n".join(dot)

# ----------------------------
# crawler
# ----------------------------
def crawl_site(
    start_url: str,
    max_depth: int = 2,
    limit_to_domain: bool = True,
    external_depth: int = 1,
    progress_callback=None
):
    root_domain = urlparse(start_url).netloc
    start_url = normalize_url(start_url)

    # initial seeds from sitemap if available
    seeds = []
    if start_url.endswith("sitemap.xml"):
        seeds = parse_sitemap(start_url)
    else:
        # try to find sitemap at /sitemap.xml
        sitemap_url = f"{urlparse(start_url).scheme}://{root_domain}/sitemap.xml"
        seeds = parse_sitemap(sitemap_url)
        if not seeds:
            seeds = [start_url]

    # queue items: (url, depth, is_external_depth_left)
    queue = deque()
    seen = {}
    for u in seeds:
        u = normalize_url(u)
        queue.append((u, 0, external_depth))
        seen[u] = 0

    pages_data = []
    link_map = defaultdict(list)

    while queue:
        current_url, depth, ext_left = queue.popleft()

        if progress_callback:
            progress_callback(len(pages_data), len(queue))

        resp = fetch(current_url)
        if not resp or not is_html(resp):
            # record as empty page so it appears in sitemap
            pages_data.append({
                "url": current_url,
                "title": current_url,
                "content": "",
                "links": []
            })
            continue

        html = resp.text
        title = get_title(html, current_url)
        content = clean_text(html)
        links = extract_links(current_url, html)

        pages_data.append({
            "url": current_url,
            "title": title,
            "content": content,
            "links": list(links)
        })

        # discover new links
        for link in links:
            link = normalize_url(link)
            if link in seen:
                continue

            is_same = same_domain(current_url, link)
            if is_same:
                # internal
                if depth + 1 <= max_depth:
                    seen[link] = depth + 1
                    queue.append((link, depth + 1, external_depth))
            else:
                # external
                if not limit_to_domain and ext_left > 0:
                    # follow once
                    seen[link] = depth + 1
                    queue.append((link, depth + 1, ext_left - 1))

    return pages_data

# ----------------------------
# streamlit app
# ----------------------------
def main():
    st.set_page_config(page_title="Website → Markdown Spider", layout="wide")

    st.title("Website → Markdown Spider")

    with st.sidebar:
        st.header("Settings")
        start_url = st.text_input("Start URL (site or sitemap.xml)", "https://example.com")
        max_depth = st.number_input("Max depth (internal)", min_value=0, max_value=10, value=2, step=1)
        limit_to_domain = st.checkbox("Limit to this domain only", value=True)
        external_depth = st.number_input("External depth (when allowed)", min_value=0, max_value=3, value=1, step=1)
        run_spider = st.button("Run spider")

    # placeholders
    status_placeholder = st.empty()
    progress_bar = st.progress(0)
    queue_placeholder = st.empty()
    links_list_placeholder = st.empty()
    md_placeholder = st.empty()
    map_placeholder = st.empty()

    if "pages_data" not in st.session_state:
        st.session_state.pages_data = None
    if "markdown" not in st.session_state:
        st.session_state.markdown = None
    if "dot" not in st.session_state:
        st.session_state.dot = None

    if run_spider:
        # we will do a two-pass progress: we don't know total upfront, so we fake it
        # we update through the callback
        crawled_counter = {"done": 0, "queued": 0}

        def progress_cb(done, queued):
            crawled_counter["done"] = done
            crawled_counter["queued"] = queued
            total = done + queued if done + queued > 0 else 1
            progress_bar.progress(min(done / total, 1.0))
            status_placeholder.write(f"Pages crawled: {done} | In queue: {queued}")
            queue_placeholder.write(f"**Queue size:** {queued}")

        with st.spinner("Crawling..."):
            pages_data = crawl_site(
                start_url=start_url,
                max_depth=int(max_depth),
                limit_to_domain=limit_to_domain,
                external_depth=int(external_depth),
                progress_callback=progress_cb
            )

        st.session_state.pages_data = pages_data
        st.session_state.markdown = build_markdown(pages_data, start_url)
        st.session_state.dot = build_dot_graph(pages_data)

        st.success(f"Done. Crawled {len(pages_data)} pages.")

        # show discovered links
        all_links = []
        for p in pages_data:
            for l in p["links"]:
                all_links.append(l)
        all_links = sorted(set(all_links))
        links_list_placeholder.write("### Discovered links")
        links_list_placeholder.write("\n".join(f"- {l}" for l in all_links))

    # show markdown + download
    if st.session_state.markdown:
        st.subheader("Markdown preview")
        md_placeholder.markdown(st.session_state.markdown)

        md_bytes = st.session_state.markdown.encode("utf-8")
        st.download_button(
            label="Download Markdown",
            data=md_bytes,
            file_name="site_export.md",
            mime="text/markdown"
        )

    # show map
    if st.session_state.dot:
        st.subheader("Site link map")
        # use graphviz
        st.graphviz_chart(st.session_state.dot)

if __name__ == "__main__":
    main()
