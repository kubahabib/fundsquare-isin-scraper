"""
fundsquare.net ISIN scraper - Streamlit app.

Paste one or more "Fund tree" links from fundsquare.net and get a table with the ISINs of all
share classes of every sub-fund. Built with requests + BeautifulSoup + pandas + Streamlit.

How the site works (Spring Web Flow, server-side rendering, no JavaScript needed):
  1. GET https://www.fundsquare.net/fund-tree?idInstr=<id>
     -> HTML with the fund tree: umbrella -> sub-funds (folders) -> share classes ("<name> - <ISIN>").
        Sub-funds are collapsed and expose an "expand" link that carries the current _flowExecutionKey.
  2. GET each "expand" link (always the one from the most recent response, the key changes every time)
     -> the response contains that sub-fund's share classes and their ISINs.
  3. Collect ISINs from every response and de-duplicate.

Scope: chosen by the user in the UI. "Whole fund" scrapes every sub-fund (folderId in the
link is ignored). "Selected sub-funds" scrapes only the sub-fund given by folderId in each link.
"""
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------- scraper
BASE_URL = "https://www.fundsquare.net"
REQUEST_DELAY = 1.0  # seconds between requests - be polite to the server
REQUEST_TIMEOUT = 20  # seconds

# Look like a regular browser; the default "python-requests/x.y" agent is often rejected.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/",
}

# ISIN: 2 letters (country) + 9 alphanumerics + 1 check digit, e.g. LU0425186540
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")


def parse_fund_tree_url(url: str) -> tuple[str, str | None]:
    """Validate a fund-tree URL; return (canonical session-free URL, folderId or None).

    Accepts e.g.
      https://www.fundsquare.net/fund-tree?idInstr=114412
        -> ("https://www.fundsquare.net/fund-tree?idInstr=114412", None)        whole fund
      https://www.fundsquare.net/fund-tree?_eventId=expand&_flowExecutionKey=e8s8&folderId=34166&idInstr=114412#34166
        -> ("https://www.fundsquare.net/fund-tree?idInstr=114412", "34166")     one sub-fund
    The second form is what the browser shows after clicking a sub-fund on fundsquare.net.
    """
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not (
        host == "fundsquare.net" or host.endswith(".fundsquare.net")
    ):
        raise ValueError("not a fundsquare.net URL")
    if parsed.path.rstrip("/") != "/fund-tree":
        raise ValueError("not a /fund-tree page (open the fund's 'Fund tree' tab and copy that URL)")
    query = parse_qs(parsed.query)
    id_instr = query.get("idInstr", [""])[0]
    if not id_instr.isdigit():
        raise ValueError("missing or invalid idInstr parameter")
    folder_id = query.get("folderId", [None])[0]
    if folder_id is not None and not folder_id.isdigit():
        raise ValueError("invalid folderId parameter")
    return f"{BASE_URL}/fund-tree?idInstr={id_instr}", folder_id


def parse_tree_page(html: str):
    """Parse one fund-tree page.

    Returns (fund_name, rows, folders):
      rows    - list of {isin, share_class, sub_fund, folder_id} for every share class visible on the page
      folders - dict folderId -> {"href": expand_href or None, "name": str, "expanded": bool}
                for every sub-fund node on the page
    """
    soup = BeautifulSoup(html, "html.parser")
    tree = soup.find("ul", id="tree")
    if tree is None:
        raise ValueError("no fund tree on the page (wrong URL, unknown idInstr or request blocked)")

    root_box = tree.find("div", class_="boxtxt")
    fund_name = root_box.get_text(" ", strip=True) if root_box else ""

    rows = []
    for li in tree.find_all("li"):
        box = li.find("div", class_="boxtxt", recursive=False)
        if box is None:
            continue
        match = ISIN_RE.search(box.get_text(" ", strip=True))
        if match is None:
            continue  # fund or sub-fund name - no ISIN here
        link = box.find("a")
        parent_li = li.find_parent("li")
        parent_box = parent_li.find("div", class_="boxtxt", recursive=False) if parent_li else None
        parent_icon = parent_li.find("div", class_="boximg", recursive=False) if parent_li else None
        rows.append(
            {
                "isin": match.group(0),
                "share_class": link.get_text(" ", strip=True) if link else "",
                "sub_fund": parent_box.get_text(" ", strip=True) if parent_box else "",
                "folder_id": parent_icon.get("id") if parent_icon else None,  # the sub-fund's folderId
            }
        )

    folders = {}
    for a in tree.select('a[href*="_eventId="]'):
        href = a["href"].split("#")[0]
        query = parse_qs(urlparse(href).query)
        folder_id = query.get("folderId", [""])[0]
        if not folder_id:
            continue
        expanded = query.get("_eventId", [""])[0] == "collapse"  # a "collapse" link = node is open
        name = a.get_text(" ", strip=True)  # the icon link has no text, the name link does
        entry = folders.setdefault(folder_id, {"href": None, "name": "", "expanded": expanded})
        entry["href"] = None if expanded else href
        entry["name"] = entry["name"] or name
    return fund_name, rows, folders


def scrape_fund_tree(
    url: str,
    delay: float = 1.0,
    timeout: int = 20,
    max_requests: int = 200,
    progress=None,
    single_sub_fund: bool = False,
):
    """Collect the ISINs of all share classes of a fund (or of one sub-fund) from its fund-tree page.

    single_sub_fund=False: scrape every sub-fund of the fund (a folderId in the link is ignored).
    single_sub_fund=True:  scrape only the sub-fund given by folderId=<id> in the link.
    progress: optional callable progress(done, total, message) - used for the Streamlit progress bar.
    Returns (rows, stats) where rows is a list of {isin, share_class, sub_fund, folder_id}.
    """
    start_url, folder_id = parse_fund_tree_url(url)
    if single_sub_fund and folder_id is None:
        raise ValueError(
            "the link does not point to a sub-fund (no folderId in it) - on fundsquare.net click the "
            "sub-fund and copy the link from the address bar"
        )
    only_folder = folder_id if single_sub_fund else None
    session = requests.Session()  # keeps the cookies the flow key is bound to (JSESSIONID + load balancer)
    session.headers.update(HEADERS)

    # Warm-up: the site sits behind a load balancer that pins our session to one backend server
    # only from the 2nd response on (ApplicationGatewayAffinity cookie). Without this, the first
    # "click" lands on another server that does not know our flow key and is silently ignored.
    session.get(start_url, timeout=timeout).raise_for_status()
    time.sleep(delay)
    response = session.get(start_url, timeout=timeout)
    response.raise_for_status()
    fund_name, rows, folders = parse_tree_page(response.text)
    requests_made = 2

    if only_folder is not None:  # single sub-fund mode: forget about the other sub-funds
        if only_folder not in folders:
            raise ValueError(f"sub-fund folderId={only_folder} not found in the fund tree of '{fund_name}'")
        folders = {only_folder: folders[only_folder]}

    def wanted(row: dict) -> bool:
        """Keep every share class, or only those of the requested sub-fund."""
        return only_folder is None or row["folder_id"] == only_folder

    found = {row["isin"]: row for row in rows if wanted(row)}  # dict = de-duplication + insertion order
    known = dict(folders)                                       # folderId -> latest known link/name
    done = {fid for fid, f in folders.items() if f["expanded"]}  # already open -> ISINs harvested
    warnings = []

    while requests_made < max_requests:
        todo = [fid for fid in known if fid not in done and known[fid]["href"]]
        if not todo:
            break
        folder_id = todo[0]
        name = known[folder_id]["name"]
        done.add(folder_id)  # never loop on the same node, whatever the server does

        for attempt in (1, 2):  # verify the node really opened; retry once with a fresh link
            time.sleep(delay)  # be polite: roughly one request per second
            requests_made += 1
            try:
                response = session.get(urljoin(BASE_URL, known[folder_id]["href"]), timeout=timeout)
                response.raise_for_status()
                _, rows, folders = parse_tree_page(response.text)
            except (requests.RequestException, ValueError) as exc:
                warnings.append(f"could not expand '{name}' (folderId={folder_id}): {exc}")
                break
            for fid, f in folders.items():  # fresh hrefs (new flow key) + any newly revealed nested folders
                if only_folder is None or fid in known:  # single sub-fund mode: do not pick up the others
                    known[fid] = f
                    if f["expanded"]:
                        done.add(fid)
            if folders.get(folder_id, {}).get("expanded"):
                break
            if attempt == 2:
                warnings.append(f"'{name}' (folderId={folder_id}) did not expand - its ISINs may be missing")

        new = 0
        for row in rows:
            if wanted(row) and row["isin"] not in found:
                found[row["isin"]] = row
                new += 1
        if progress:
            progress(len(done), len(known), f"{name}: +{new} ISIN (total {len(found)})")

    if requests_made >= max_requests:
        warnings.append(f"stopped after {max_requests} requests - fund tree unusually large?")

    stats = {
        "fund_name": fund_name,
        "sub_fund": known[only_folder]["name"] if only_folder else None,  # set in single sub-fund mode
        "start_url": start_url,
        "sub_funds": len(known),
        "isins": len(found),
        "requests": requests_made,
        "warnings": warnings,
    }
    return list(found.values()), stats


# ----------------------------------------------------------------------------- app helpers
RESULT_COLUMNS = ["ISIN", "Source URL", "Fund", "Sub-fund", "Share class"]
PROBLEM_COLUMNS = ["URL", "Status", "Details"]


def parse_url_list(text: str) -> list[str]:
    """Split the text area into unique, non-empty links (order preserved)."""
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def describe_error(exc: Exception) -> str:
    """Turn an exception into a short, human-readable reason for the problems table."""
    if isinstance(exc, requests.exceptions.Timeout):
        return f"timeout - fundsquare.net did not respond within {REQUEST_TIMEOUT} s"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else "?"
        return f"HTTP error {status}"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error - could not reach fundsquare.net"
    return str(exc)


def run_scraper(urls: list[str], progress_bar, single_sub_fund: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scrape every link, skipping the broken ones. Returns (results, problems) DataFrames."""
    results, problems = [], []
    for i, url in enumerate(urls):
        prefix = f"Link {i + 1}/{len(urls)}"
        progress_bar.progress(i / len(urls), text=f"{prefix}: fetching fund tree...")

        def on_progress(done: int, total: int, message: str, i: int = i) -> None:
            fraction = (i + done / max(total, 1)) / len(urls)
            progress_bar.progress(min(fraction, 1.0), text=f"{prefix}: {message}")

        try:
            rows, stats = scrape_fund_tree(
                url,
                delay=REQUEST_DELAY,
                timeout=REQUEST_TIMEOUT,
                progress=on_progress,
                single_sub_fund=single_sub_fund,
            )
        except (ValueError, requests.RequestException) as exc:
            problems.append({"URL": url, "Status": "failed", "Details": describe_error(exc)})
            continue

        for warning in stats["warnings"]:
            problems.append({"URL": url, "Status": "partial", "Details": warning})
        if not rows:
            problems.append({"URL": url, "Status": "failed", "Details": "no ISINs found on the page"})
        for row in rows:
            results.append(
                {
                    "ISIN": row["isin"],
                    "Source URL": url,
                    "Fund": stats["fund_name"],
                    "Sub-fund": row["sub_fund"],
                    "Share class": row["share_class"],
                }
            )

    progress_bar.progress(1.0, text="Done")
    return pd.DataFrame(results, columns=RESULT_COLUMNS), pd.DataFrame(problems, columns=PROBLEM_COLUMNS)


# ----------------------------------------------------------------------------- UI
MODE_FUND = "Whole fund structure"
MODE_SUB = "Specific sub-funds"

BNP_STAR_SVG = """
<svg class="bnp-stars" viewBox="0 0 72 28" aria-hidden="true">
  <path d="M10 2l1.6 6.2L18 10l-6.4 1.8L10 18l-1.6-6.2L2 10l6.4-1.8z"/>
  <path d="M28 0l1.3 5.1L34.5 6.5l-5.2 1.4L28 13l-1.3-5.1L21.5 6.5l5.2-1.4z"/>
  <path d="M46 2l1.6 6.2L54 10l-6.4 1.8L46 18l-1.6-6.2L38 10l6.4-1.8z"/>
  <path d="M64 0l1.3 5.1L70.5 6.5l-5.2 1.4L64 13l-1.3-5.1L57.5 6.5l5.2-1.4z"/>
</svg>
"""


def apply_theme_css() -> None:
    """Inject the BNP Paribas dark corporate theme."""
    tokens = {
        "bg": "#101614",
        "card": "#1A221F",
        "text": "#E8EEEB",
        "muted": "#9AABA3",
        "border": "#2C3A34",
        "input": "#121A18",
        "accent": "#2E8B57",
        "accent-deep": "#1F6B42",
        "accent-text": "#D8F3E4",
        "success-bg": "#1A3328",
        "success-border": "#2E8B57",
        "table-header": "#1E2A26",
        "table-hover": "#24352E",
        "shadow": "0 8px 24px rgba(0, 0, 0, 0.35)",
        "radio-fill": "#2E8B57",
    }

    st.markdown(
        f"""
<style>
@import url("https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap");

:root {{
  --bnp-bg: {tokens["bg"]};
  --bnp-card: {tokens["card"]};
  --bnp-text: {tokens["text"]};
  --bnp-muted: {tokens["muted"]};
  --bnp-border: {tokens["border"]};
  --bnp-input: {tokens["input"]};
  --bnp-accent: {tokens["accent"]};
  --bnp-accent-deep: {tokens["accent-deep"]};
  --bnp-accent-text: {tokens["accent-text"]};
  --bnp-success-bg: {tokens["success-bg"]};
  --bnp-success-border: {tokens["success-border"]};
  --bnp-table-header: {tokens["table-header"]};
  --bnp-table-hover: {tokens["table-hover"]};
  --bnp-shadow: {tokens["shadow"]};
}}

html, body, [data-testid="stAppViewContainer"], .stApp {{
  background: var(--bnp-bg) !important;
  color: var(--bnp-text) !important;
  font-family: "Source Sans 3", "Segoe UI", "Helvetica Neue", Arial, sans-serif !important;
}}
[data-testid="stHeader"] {{
  background: transparent !important;
}}
[data-testid="stToolbar"] {{
  background: transparent !important;
}}
.block-container {{
  padding-top: 1.4rem !important;
  max-width: 1180px !important;
}}
footer {{ visibility: hidden; }}

.bnp-header {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.35rem;
}}
.bnp-title-row {{
  display: flex;
  align-items: center;
  gap: 0.7rem;
}}
.bnp-title-row h1 {{
  margin: 0;
  font-size: 1.85rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--bnp-accent-text) !important;
}}
.bnp-stars {{
  width: 72px;
  height: 28px;
  fill: var(--bnp-accent);
  flex-shrink: 0;
}}
.bnp-sub {{
  margin: 0.15rem 0 1.1rem 0;
  color: var(--bnp-muted) !important;
  font-size: 1.02rem;
}}
.bnp-card-title {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--bnp-text);
  margin-bottom: 0.15rem;
}}
.bnp-card-title .bnp-icon {{
  color: var(--bnp-accent);
  font-size: 1.05rem;
}}
.bnp-results-kicker {{
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.92rem;
  font-weight: 600;
  color: var(--bnp-text);
  margin-bottom: 0.1rem;
}}
.bnp-results-title {{
  color: var(--bnp-accent-text) !important;
  font-size: 1.35rem;
  font-weight: 700;
  margin: 0 0 0.7rem 0;
}}
.bnp-success {{
  background: var(--bnp-success-bg);
  border: 1px solid var(--bnp-success-border);
  color: var(--bnp-accent-text);
  border-radius: 10px;
  padding: 0.7rem 0.95rem;
  font-weight: 600;
  margin-bottom: 0.85rem;
}}
.bnp-success:before {{
  content: "✓  ";
  font-weight: 700;
}}

[data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"] {{
  background: var(--bnp-card) !important;
  border: 1px solid var(--bnp-border) !important;
  border-radius: 16px !important;
  box-shadow: var(--bnp-shadow) !important;
  padding: 1rem 1.15rem 1.05rem 1.15rem !important;
}}
[data-testid="stForm"] {{
  border: none !important;
  background: transparent !important;
  padding: 0 !important;
}}

[data-testid="stWidgetLabel"] p, label, .stMarkdown, .stCaption, p {{
  color: var(--bnp-text) !important;
}}
.stCaption, [data-testid="stCaptionContainer"] p {{
  color: var(--bnp-muted) !important;
}}
code {{
  background: transparent !important;
  color: var(--bnp-accent) !important;
  padding: 0 !important;
}}

[data-testid="stRadioOption"] > div > div > div:first-child {{
  background-color: transparent !important;
  box-shadow: inset 0 0 0 2px var(--bnp-accent) !important;
}}
[data-testid="stRadioOption"][data-selected="true"] > div > div > div:first-child {{
  background-color: var(--bnp-accent) !important;
  box-shadow: none !important;
}}
[data-testid="stRadioOption"][data-selected="true"] > div > div > div:first-child > div {{
  background-color: #FFFFFF !important;
}}
[data-testid="stRadioOption"]:not([data-selected="true"]) > div > div > div:first-child > div {{
  background-color: transparent !important;
}}
[data-testid="stRadio"] label p {{
  font-weight: 600 !important;
}}

[data-testid="stTextAreaRootElement"],
[data-testid="stTextArea"] textarea {{
  background: var(--bnp-input) !important;
  color: var(--bnp-text) !important;
  border: 1px solid var(--bnp-border) !important;
  border-radius: 12px !important;
}}
[data-testid="stTextArea"] textarea {{
  min-height: 118px !important;
}}
[data-testid="stTextArea"] textarea:focus {{
  border-color: var(--bnp-accent) !important;
  box-shadow: 0 0 0 3px rgba(0, 102, 51, 0.15) !important;
}}
[data-testid="stTextArea"] textarea::placeholder {{
  color: var(--bnp-muted) !important;
  opacity: 0.75 !important;
}}

.stFormSubmitButton button,
button[data-testid="stBaseButton-primary"],
.stButton > button[kind="primary"],
[data-testid="stDownloadButton"] button {{
  background: var(--bnp-accent) !important;
  background-color: var(--bnp-accent) !important;
  color: #FFFFFF !important;
  border: none !important;
  border-radius: 10px !important;
  font-weight: 700 !important;
  letter-spacing: 0.06em !important;
  text-transform: uppercase !important;
  padding: 0.7rem 1rem !important;
}}
.stFormSubmitButton button:hover,
button[data-testid="stBaseButton-primary"]:hover,
.stButton > button[kind="primary"]:hover,
[data-testid="stDownloadButton"] button:hover {{
  background: var(--bnp-accent-deep) !important;
  background-color: var(--bnp-accent-deep) !important;
  border-color: var(--bnp-accent-deep) !important;
}}

[data-testid="stCheckbox"] [data-baseweb="checkbox"] {{
  border-color: var(--bnp-accent) !important;
}}
[data-testid="stCheckbox"] [aria-checked="true"] [data-baseweb="checkbox"] {{
  background-color: var(--bnp-accent) !important;
  border-color: var(--bnp-accent) !important;
}}

[data-testid="stProgressBar"] > div,
[data-testid="stProgressBarTrack"] > div {{
  background-color: var(--bnp-accent) !important;
}}

[data-testid="stDataFrame"] a, [data-testid="stDataFrame"] [role="link"] {{
  color: var(--bnp-accent) !important;
}}
[data-testid="stDataFrame"] {{
  border: 1px solid var(--bnp-border) !important;
  border-radius: 10px !important;
  overflow: hidden;
}}

[data-testid="stToggle"] label p {{
  color: var(--bnp-muted) !important;
  font-size: 0.88rem !important;
}}
[data-testid="stToggle"] [aria-checked="true"] {{
  background-color: var(--bnp-accent) !important;
}}

[data-testid="stExpander"] {{
  background: var(--bnp-card) !important;
  border: 1px solid var(--bnp-border) !important;
  border-radius: 16px !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="BNP Paribas ISIN Scraper Utility", layout="wide")
apply_theme_css()

st.markdown(
    f"""
<div class="bnp-title-row">
  <h1>BNP Paribas ISIN Scraper Utility</h1>
  {BNP_STAR_SVG}
</div>
<p class="bnp-sub">Streamline the extraction of ISIN identifiers from fundsquare.net fund structures.</p>
    """,
    unsafe_allow_html=True,
)

config_col, urls_col = st.columns(2, gap="medium")
with config_col:
    with st.container(border=True):
        st.markdown(
            '<div class="bnp-card-title"><span class="bnp-icon">⚙</span> Scraping Configuration</div>',
            unsafe_allow_html=True,
        )
        mode = st.radio(
            "Identify data to extract?",
            (MODE_FUND, MODE_SUB),
            horizontal=True,
            help="The choice applies to every link you paste on the right.",
        )
        single_sub_fund = mode == MODE_SUB
        if single_sub_fund:
            st.caption(
                "On fundsquare.net click the sub-fund, then copy the link from the address bar "
                "(it contains folderId=...). One link = one sub-fund; you can paste several."
            )
            placeholder = (
                "https://www.fundsquare.net/fund-tree?_eventId=expand&folderId=34166&idInstr=114412#34166\n"
                "https://www.fundsquare.net/fund-tree?_eventId=expand&folderId=169257&idInstr=114412#169257"
            )
        else:
            st.caption(
                "Paste the fund-tree link of the fund (for example ...fund-tree?idInstr=114412). "
                "The app opens every sub-fund and collects all ISINs. Any folderId in the link is ignored."
            )
            placeholder = "https://www.fundsquare.net/fund-tree?idInstr=114412"

with urls_col:
    with st.container(border=True):
        st.markdown(
            '<div class="bnp-card-title"><span class="bnp-icon">🔗</span> Fund Tree URLs</div>',
            unsafe_allow_html=True,
        )
        with st.form("scrape_form"):
            urls_text = st.text_area(
                "Fund tree URLs (one per line)",
                height=118,
                placeholder=placeholder,
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("🔍  SCRAPE ISINs", type="primary", use_container_width=True)

if submitted:
    urls = parse_url_list(urls_text)
    if not urls:
        st.warning("Paste at least one link first.")
    else:
        progress_bar = st.progress(0.0, text="Starting...")
        results, problems = run_scraper(urls, progress_bar, single_sub_fund=single_sub_fund)
        st.session_state["results"] = results
        st.session_state["problems"] = problems
        st.session_state["n_links"] = len(urls)

if "results" in st.session_state:
    results: pd.DataFrame = st.session_state["results"]
    problems: pd.DataFrame = st.session_state["problems"]
    n_links: int = st.session_state["n_links"]
    n_failed = problems.loc[problems["Status"] == "failed", "URL"].nunique()

    with st.container(border=True):
        st.markdown(
            """
<div class="bnp-results-kicker">📊 Results</div>
<div class="bnp-results-title">Results Summary</div>
            """,
            unsafe_allow_html=True,
        )
        if results.empty:
            st.error("No ISINs found. Check the links in the table below.")
        else:
            st.markdown(
                f'<div class="bnp-success">Found {results["ISIN"].nunique()} unique ISINs from '
                f"{n_links - n_failed} of {n_links} links.</div>",
                unsafe_allow_html=True,
            )
            show_details = st.checkbox("Show fund, sub-fund and share class columns")
            columns = RESULT_COLUMNS if show_details else ["ISIN", "Source URL"]
            st.dataframe(
                results[columns],
                hide_index=True,
                column_config={"Source URL": st.column_config.LinkColumn("Source URL")},
            )
            st.download_button(
                "Download CSV",
                data=results[columns].to_csv(index=False).encode("utf-8-sig"),
                file_name="fundsquare_isins.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )

        if not problems.empty:
            st.markdown("**Links with problems**")
            st.dataframe(problems, hide_index=True)

with st.expander("How it works"):
    st.markdown(
        """
        - The fund tree on fundsquare.net is rendered server-side: every click on a sub-fund is a plain
          HTTP request that returns the whole page with that node expanded.
        - The app keeps one `requests.Session` per link (the server ties its state to cookies), "clicks"
          each sub-fund with roughly one request per second and parses every response with BeautifulSoup.
        - ISINs are extracted from the share-class rows with a regular expression
          (`2 letters + 9 alphanumerics + 1 check digit`) and de-duplicated per fund.
        - **Whole fund structure** scrapes every sub-fund (a `folderId` in the link is ignored).
          **Specific sub-funds** scrapes only the sub-fund of each pasted link (the link must contain
          `folderId=...`, which appears in the address bar after you click the sub-fund).
        - Broken links are skipped and reported in the *Links with problems* table.
        """
    )
