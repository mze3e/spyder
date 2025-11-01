import os
import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import deque, defaultdict
import re
import datetime
import xml.etree.ElementTree as ET

EXPORT_DIR = "exports"

# ----------------------------
# filesystem helpers
# ----------------------------
def ensure_export_dir():
    if not os.path.exists(EXPORT_DIR):
        os.makedirs(EXPORT_DIR, exist_ok=True)

def make_run_id(url: str):
    # use domain + date
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{host}-{ts}"

def save_export(run_id: str, markdown: str, dot: str):
    ensure_export_dir()
    md_path = os.path.join(EXPORT_DIR, f"{run_id}.md")
    dot_path = os.path.join(EXPORT_DIR, f"{run_id}.dot")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    with open(dot_path, "w", encoding="utf-8") as f:
        f.write(dot)
    return md_path, dot_path

def list_exports():
    ensure_export_dir()
    # returns list of (run_id, md_path, dot_path)
    runs = []
    for fname in os.listdir(EXPORT_DIR):
        if fname.endswith(".md"):
            run_id = fname[:-3]
            md_path = os.path.join(EXPORT_DIR, fname)
            dot_path = os.path.join(EXPORT_DIR, f"{run_id}.dot")
            runs.append((run_id, md_path, dot_path))
    # newest first
    runs.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
    return runs

def load_export(run_id: str):
    md_path = os.path.join(EXPORT_DIR, f"{run_id}.md")
    dot_path = os.path.join(EXPORT_DIR, f"{run_id}.dot")
    md = ""
    dot = ""
    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    if os.path.exists(dot_path):
        with open(dot_path, "r", encoding="utf-8") as f:
            dot = f.read()
    return md, dot

# ----------------------------
# fetch helpers
# ----------------------------
def is_html(response: requests.Response) -> bool:
    ctype = response.headers.get("Content-Type", "")
    return "text/html" in ctype or "application/xhtml+xml" in ctype

@st.cache_data
def fetch(url: str, timeout: int = 10):
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "StreamlitSpider/1.0"})
        if resp.status_code == 200:
            return resp
    except Exception:
        return None
    return None

# ----------------------------
# sitemap
# ----------------------------
def parse_sitemap(sitemap_url: str):
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
    if not urls:
        for smap in root.findall("sm:sitemap/sm:loc", ns):
            child_map = smap.text.strip()
            urls.extend(parse_sitemap(child_map))
    return list(dict.fromkeys(urls))

# ----------------------------
# content extraction (improved)
# ----------------------------
def clean_text_rich(html: str):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    candidates = []
    main_like = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    if main_like:
        candidates.append(main_like)

    for sel in [".post-content", ".entry-content", ".content", "#content", "#main"]:
        found = soup.select_one(sel)
        if found and found not in candidates:
            candidates.append(found)

    if soup.body and soup.body not in candidates:
        candidates.append(soup.body)

    best_text = ""
    for cand in candidates:
        text = cand.get_text("\n", strip=True)
        if len(text) > len(best_text):
            best_text = text

    if not best_text:
        best_text = soup.get_text("\n", strip=True)

    lines = []
    for line in best_text.splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)

    text = "\n\n".join(lines)

    if len(text) < 50:
        return ""
    return text

def get_title(html: str, fallback: str):
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return fallback

def extract_links(base_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(base_url, href)
        full = full.split("#")[0]
        if full:
            links.add(full)
    return links

def same_domain(url1: str, url2: str) -> bool:
    return urlparse(url1).netloc == urlparse(url2).netloc

def normalize_url(url: str):
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
        lines.append("\n---\n")
        lines.append(f"## {p['title']}")
        lines.append(f"<a name=\"{anchor}\"></a>")
        lines.append("")
        lines.append(f"**Source:** {p['url']}")
        lines.append("")
        if p["content"].strip():
            lines.append(p["content"].strip())
        else:
            lines.append("_No text content found. Page may be JS-rendered or mostly non-text._")
        lines.append("")

    return "\n".join(lines)

# ----------------------------
# graphviz builder
# ----------------------------
def build_dot_graph(pages_data):
    dot = ['digraph site {', 'rankdir=LR;', 'node [shape=box, style=rounded];']
    for p in pages_data:
        node_name = f'"{p["url"]}"'
        label = p["title"][:50].replace('"', "'")
        dot.append(f'{node_name} [label="{label}"];')
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

    if start_url.endswith("sitemap.xml"):
        seeds = parse_sitemap(start_url)
    else:
        sitemap_url = f"{urlparse(start_url).scheme}://{root_domain}/sitemap.xml"
        seeds = parse_sitemap(sitemap_url)
        if not seeds:
            seeds = [start_url]

    queue = deque()
    seen = {}
    for u in seeds:
        u = normalize_url(u)
        queue.append((u, 0, external_depth))
        seen[u] = 0

    pages_data = []

    while queue:
        current_url, depth, ext_left = queue.popleft()
        if progress_callback:
            progress_callback(len(pages_data), len(queue))

        resp = fetch(current_url)
        if not resp or not is_html(resp):
            pages_data.append({
                "url": current_url,
                "title": current_url,
                "content": "",
                "links": []
            })
            continue

        html = resp.text
        title = get_title(html, current_url)
        content = clean_text_rich(html)
        links = extract_links(current_url, html)

        pages_data.append({
            "url": current_url,
            "title": title,
            "content": content,
            "links": list(links)
        })

        for link in links:
            link = normalize_url(link)
            if link in seen:
                continue

            is_same = same_domain(current_url, link)
            if is_same:
                if depth + 1 <= max_depth:
                    seen[link] = depth + 1
                    queue.append((link, depth + 1, external_depth))
            else:
                if not limit_to_domain and ext_left > 0:
                    seen[link] = depth + 1
                    queue.append((link, depth + 1, ext_left - 1))

    return pages_data

# ----------------------------
# streamlit app
# ----------------------------
def main():
    st.set_page_config(page_title="Website → Markdown Spider", layout="wide")
    st.title("Website → Markdown Spider")

    # list saved exports first
    saved_runs = list_exports()
    saved_run_labels = ["(none)"] + [r[0] for r in saved_runs]

    with st.sidebar:
        st.header("Settings")
        start_url = st.text_input("Start URL", "https://example.com")
        max_depth = st.number_input("Max depth (internal)", 0, 10, 2, 1)
        limit_to_domain = st.checkbox("Limit to this domain only", True)
        external_depth = st.number_input("External depth (when allowed)", 0, 3, 1, 1)
        run_spider = st.button("Run spider")

        st.markdown("---")
        st.subheader("Previous runs")
        selected_saved = st.selectbox("Load from disk", saved_run_labels, index=0)

    status_placeholder = st.empty()
    progress_bar = st.progress(0)
    links_list_placeholder = st.empty()

    if "pages_data" not in st.session_state:
        st.session_state.pages_data = None
    if "markdown" not in st.session_state:
        st.session_state.markdown = None
    if "dot" not in st.session_state:
        st.session_state.dot = None
    if "current_run_id" not in st.session_state:
        st.session_state.current_run_id = None

    # run new crawl
    if run_spider:
        def progress_cb(done, queued):
            total = done + queued if (done + queued) > 0 else 1
            progress_bar.progress(min(done / total, 1.0))
            status_placeholder.write(f"Pages crawled: {done} | In queue: {queued}")

        with st.spinner("Crawling..."):
            pages_data = crawl_site(
                start_url=start_url,
                max_depth=int(max_depth),
                limit_to_domain=limit_to_domain,
                external_depth=int(external_depth),
                progress_callback=progress_cb
            )

        markdown = build_markdown(pages_data, start_url)
        dot = build_dot_graph(pages_data)

        # save to disk
        run_id = make_run_id(start_url)
        md_path, dot_path = save_export(run_id, markdown, dot)

        st.session_state.pages_data = pages_data
        st.session_state.markdown = markdown
        st.session_state.dot = dot
        st.session_state.current_run_id = run_id

        # show links
        all_links = []
        for p in pages_data:
            all_links.extend(p["links"])
        all_links = sorted(set(all_links))
        links_list_placeholder.write("### Discovered links")
        links_list_placeholder.write("\n".join(f"- {l}" for l in all_links))

        st.success(f"Done. Crawled {len(pages_data)} pages. Saved as {run_id}")

    # load existing export
    if selected_saved != "(none)" and (not run_spider):
        # load from disk
        md, dot = load_export(selected_saved)
        st.session_state.markdown = md
        st.session_state.dot = dot
        st.session_state.current_run_id = selected_saved
        status_placeholder.write(f"Loaded saved run: {selected_saved}")

    # show markdown + downloads
    if st.session_state.markdown:
        st.subheader("Markdown preview")
        st.markdown(st.session_state.markdown[:10000] + ("\n\n... (truncated)" if len(st.session_state.markdown) > 10000 else ""))

        st.sidebar.download_button(
            "Download Markdown",
            data=st.session_state.markdown.encode("utf-8"),
            file_name=f"{st.session_state.current_run_id or 'site_export'}.md",
            mime="text/markdown"
        )

    # show map
    if st.session_state.dot:
        st.subheader("Site link map")
        st.graphviz_chart(st.session_state.dot)
        st.sidebar.download_button(
            "Download DOT (map)",
            data=st.session_state.dot.encode("utf-8"),
            file_name=f"{st.session_state.current_run_id or 'site_export'}.dot",
            mime="text/vnd.graphviz"
        )

if __name__ == "__main__":
    main()
