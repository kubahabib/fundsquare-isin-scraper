"""Shared fundsquare.net ISIN scraping used by the Streamlit, Flask, and desktop apps."""
import re
import ssl
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import getproxies

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

BASE_URL = "https://www.fundsquare.net"
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/",
}

ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
RESULT_COLUMNS = ["ISIN", "Source URL", "Fund", "Sub-fund", "Share class"]
PROBLEM_COLUMNS = ["URL", "Status", "Details"]


class _WindowsCertAdapter(HTTPAdapter):
    """Use the Windows certificate store so a bank proxy's HTTPS inspection is trusted."""

    def _ssl_context(self):
        context = ssl.create_default_context()
        try:
            import certifi

            context.load_verify_locations(cafile=certifi.where())
        except (ImportError, OSError, ssl.SSLError):
            pass
        return context

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._ssl_context()
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._ssl_context()
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _os_proxies() -> dict[str, str]:
    """Proxy from the OS. Windows often stores the HTTPS proxy with an https:// scheme."""
    found = {}
    for scheme, proxy in getproxies().items():
        if scheme not in ("http", "https") or not proxy:
            continue
        proxy = str(proxy).strip()
        if sys.platform == "win32" and proxy.lower().startswith("https://"):
            proxy = "http://" + proxy[8:]
        found[scheme] = proxy
    return found


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    proxies = _os_proxies()
    if proxies:
        session.proxies.update(proxies)
    if sys.platform == "win32":
        session.mount("https://", _WindowsCertAdapter())
    return session


def parse_fund_tree_url(url: str) -> tuple[str, str | None]:
    """Validate a fund-tree URL; return (canonical session-free URL, folderId or None)."""
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
    """Parse one fund-tree page. Returns (fund_name, rows, folders)."""
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
            continue
        link = box.find("a")
        parent_li = li.find_parent("li")
        parent_box = parent_li.find("div", class_="boxtxt", recursive=False) if parent_li else None
        parent_icon = parent_li.find("div", class_="boximg", recursive=False) if parent_li else None
        rows.append(
            {
                "isin": match.group(0),
                "share_class": link.get_text(" ", strip=True) if link else "",
                "sub_fund": parent_box.get_text(" ", strip=True) if parent_box else "",
                "folder_id": parent_icon.get("id") if parent_icon else None,
            }
        )

    folders = {}
    for a in tree.select('a[href*="_eventId="]'):
        href = a["href"].split("#")[0]
        query = parse_qs(urlparse(href).query)
        folder_id = query.get("folderId", [""])[0]
        if not folder_id:
            continue
        expanded = query.get("_eventId", [""])[0] == "collapse"
        name = a.get_text(" ", strip=True)
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
    """Collect ISINs from one fund-tree URL. progress(done, total, message) is optional."""
    start_url, folder_id = parse_fund_tree_url(url)
    if single_sub_fund and folder_id is None:
        raise ValueError(
            "the link does not point to a sub-fund (no folderId in it) - on fundsquare.net click the "
            "sub-fund and copy the link from the address bar"
        )
    only_folder = folder_id if single_sub_fund else None
    session = _new_session()

    session.get(start_url, timeout=timeout).raise_for_status()
    time.sleep(delay)
    response = session.get(start_url, timeout=timeout)
    response.raise_for_status()
    fund_name, rows, folders = parse_tree_page(response.text)
    requests_made = 2

    if only_folder is not None:
        if only_folder not in folders:
            raise ValueError(f"sub-fund folderId={only_folder} not found in the fund tree of '{fund_name}'")
        folders = {only_folder: folders[only_folder]}

    def wanted(row: dict) -> bool:
        return only_folder is None or row["folder_id"] == only_folder

    found = {row["isin"]: row for row in rows if wanted(row)}
    known = dict(folders)
    done = {fid for fid, f in folders.items() if f["expanded"]}
    warnings = []

    while requests_made < max_requests:
        todo = [fid for fid in known if fid not in done and known[fid]["href"]]
        if not todo:
            break
        folder_id = todo[0]
        name = known[folder_id]["name"]
        done.add(folder_id)

        for attempt in (1, 2):
            time.sleep(delay)
            requests_made += 1
            try:
                response = session.get(urljoin(BASE_URL, known[folder_id]["href"]), timeout=timeout)
                response.raise_for_status()
                _, rows, folders = parse_tree_page(response.text)
            except (requests.RequestException, ValueError) as exc:
                warnings.append(f"could not expand '{name}' (folderId={folder_id}): {exc}")
                break
            for fid, f in folders.items():
                if only_folder is None or fid in known:
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
        "sub_fund": known[only_folder]["name"] if only_folder else None,
        "start_url": start_url,
        "sub_funds": len(known),
        "isins": len(found),
        "requests": requests_made,
        "warnings": warnings,
    }
    return list(found.values()), stats


def parse_url_list(text: str) -> list[str]:
    """Split text into unique, non-empty links (order preserved)."""
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def describe_error(exc: Exception) -> str:
    """Turn an exception into a short, human-readable reason."""
    if isinstance(exc, requests.exceptions.Timeout):
        return f"timeout - fundsquare.net did not respond within {REQUEST_TIMEOUT} s"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else "?"
        return f"HTTP error {status}"
    if isinstance(exc, requests.exceptions.SSLError):
        detail = str(exc).splitlines()[0][:180]
        return f"TLS certificate error - the Windows proxy is likely inspecting HTTPS: {detail}"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy error - the Windows proxy rejected the connection to fundsquare.net"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error - could not reach fundsquare.net"
    return str(exc)


def run_scraper(urls: list[str], single_sub_fund: bool = False, progress=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scrape every link. progress(fraction, message) is optional.

    Returns (results, problems) DataFrames.
    """
    results, problems = [], []
    for i, url in enumerate(urls):
        prefix = f"Link {i + 1}/{len(urls)}"
        if progress:
            progress(i / len(urls), f"{prefix}: fetching fund tree...")

        def on_progress(done: int, total: int, message: str, i: int = i) -> None:
            if progress:
                progress((i + done / max(total, 1)) / len(urls), f"{prefix}: {message}")

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

    if progress:
        progress(1.0, "Done")
    return pd.DataFrame(results, columns=RESULT_COLUMNS), pd.DataFrame(problems, columns=PROBLEM_COLUMNS)
