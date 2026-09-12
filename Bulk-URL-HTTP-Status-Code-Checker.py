"""
HTTP Status Checker — Bulk URL & Redirect Checker
Production-grade Streamlit application.

Run with:
    streamlit run app.py
"""

import io
import re
import csv
import json
import time
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

# ----------------------------------------------------------------------------
# Page configuration (also doubles as basic on-page / entity SEO signal)
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="HTTP Status Checker — Bulk URL & Redirect Checker Tool",
    page_icon="🟢",
    layout="wide",
    menu_items={
        "About": (
            "HTTP Status Checker is a free bulk URL status and redirect "
            "checker. Check HTTP status codes, redirect chains, and "
            "response times for many URLs at once."
        )
    },
)

# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
st.markdown(
    """
    <style>
        .block-container {padding-top: 2rem; padding-bottom: 3rem;}
        .status-2xx {color: #16a34a; font-weight: 600;}
        .status-3xx {color: #ca8a04; font-weight: 600;}
        .status-4xx {color: #dc2626; font-weight: 600;}
        .status-5xx {color: #b91c1c; font-weight: 600;}
        .status-err {color: #6b7280; font-weight: 600;}
        .metric-card {
            background: #f8fafc; border: 1px solid #e2e8f0;
            border-radius: 10px; padding: 14px 16px;
        }
        .support-box {
            border: 1px solid #fbbf24; border-radius: 12px;
            padding: 18px 22px; margin-top: 10px;
        }
        code {word-break: break-all;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HTTPStatusCheckerBot/1.0; "
        "+https://devendergupta.netlify.app/)"
    )
}

STATUS_MEANINGS = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found (Temporary Redirect)",
    303: "See Other", 307: "Temporary Redirect", 308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    410: "Gone", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}

# Hard safety ceilings for a public/shared deployment.
MAX_URLS = 5_000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB per uploaded file
MAX_WORKERS = 30

# A URL token must look like scheme://host or www.host - used when scanning
# free-form pasted text / CSV cells for embedded URLs.
_URL_TOKEN_RE = re.compile(
    r'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*://\S+|www\.[^\s,]+\.[^\s,]+)$'
)

# Thread-local storage so each worker thread reuses one Session (connection
# pooling) instead of creating/destroying a Session per URL.
_thread_local = threading.local()

# ----------------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------------
def normalize_url(raw: str):
    """
    Clean and validate a single URL token.
    Returns a normalized absolute URL string, or None if it doesn't look
    like a plausible URL at all.
    """
    u = raw.strip().strip('"\'')
    if not u:
        return None

    if u.lower().startswith("www."):
        u = "https://" + u
    elif not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', u):
        u = "https://" + u

    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    host = parsed.hostname or ""
    # Require at least one dot (a real TLD) unless it's localhost, to avoid
    # treating stray words ("hello", "foo bar") as valid hosts.
    if "." not in host and host.lower() != "localhost":
        return None

    return u


def canonical_key(url: str) -> str:
    """Case-insensitive-on-host key used only for de-duplication."""
    p = urlparse(url)
    netloc = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{netloc}{path}?{p.query}"


def extract_url_tokens(line: str):
    """
    Split a raw line on whitespace/commas/tabs and yield tokens that look
    like URLs. Handles pasted spreadsheet rows like
    'https://example.com, some notes' without swallowing the notes as
    a second bogus URL.
    """
    for tok in re.split(r"[\s,]+", line.strip()):
        tok = tok.strip()
        if not tok:
            continue
        if _URL_TOKEN_RE.match(tok) or "." in tok:
            yield tok


def parse_input_urls(text_block: str, uploaded_files):
    """
    Merge pasted URLs + uploaded TXT/CSV URLs, validate, dedupe
    (case-insensitive on host), preserve input order.
    Returns (urls, skipped_count).
    """
    raw_tokens = []

    if text_block:
        for line in text_block.splitlines():
            if line.strip():
                raw_tokens.extend(extract_url_tokens(line))

    for f in uploaded_files or []:
        content = f.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            st.warning(
                f"⚠️ '{f.name}' is larger than "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB and was truncated."
            )
            content = content[:MAX_UPLOAD_BYTES]
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            continue

        if f.name.lower().endswith(".csv"):
            reader = csv.reader(io.StringIO(text))
            for row in reader:
                for cell in row:
                    raw_tokens.extend(extract_url_tokens(cell))
        else:  # .txt or generic
            for line in text.splitlines():
                if line.strip():
                    raw_tokens.extend(extract_url_tokens(line))

    cleaned, seen, skipped = [], set(), 0
    for tok in raw_tokens:
        norm = normalize_url(tok)
        if norm is None:
            skipped += 1
            continue
        key = canonical_key(norm)
        if key not in seen:
            seen.add(key)
            cleaned.append(norm)

    return cleaned, skipped


def classify_error(exc: Exception) -> str:
    """Rough classification of failure stage for a clearer error reason."""
    msg = str(exc).lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS/SSL handshake failed"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "Connection timed out (TCP)"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "Server response timed out"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        # Requests raises this both for true redirect loops and for chains
        # that are simply longer than max_redirects - don't overclaim.
        return "Too many redirects (exceeded limit)"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "name or service not known" in msg or "getaddrinfo failed" in msg or "nodename nor servname" in msg:
            return "DNS resolution failed"
        if "connection refused" in msg:
            return "Connection refused (TCP)"
        if "reset by peer" in msg:
            return "Connection reset by peer"
        return "Connection error"
    if isinstance(exc, requests.exceptions.MissingSchema):
        return "Invalid URL (missing scheme)"
    if isinstance(exc, requests.exceptions.InvalidURL):
        return "Malformed URL"
    return f"Request failed: {type(exc).__name__}"


def status_bucket(code):
    if code is None:
        return "error"
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


def get_thread_session(max_redirects: int, retries: int) -> requests.Session:
    """
    One Session per worker thread, reused across all URLs that thread
    handles - keeps connection pooling instead of a new TCP/TLS handshake
    per request. Also wires up automatic retry/backoff for transient
    failures (connection errors and 5xx/429 responses).
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    session.max_redirects = max_redirects
    return session


def check_url(url: str, method: str, timeout: float, verify_ssl: bool,
               max_redirects: int, follow_redirects: bool, retries: int) -> dict:
    """Perform the HTTP status / redirect check for a single URL."""
    result = {
        "URL": url,
        "Initial Status Code": None,
        "Status Code": None,
        "Status Meaning": "",
        "Final URL": "",
        "Redirected": False,
        "Redirect Count": 0,
        "Redirect Chain": "",
        "Location": "",
        "Response Time (ms)": None,
        "Content Type": "",
        "Content Length": "",
        "Server": "",
        "Retry-After": "",
        "Error Reason": "",
        "Timestamp (UTC)": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }

    session = get_thread_session(max_redirects, retries)

    start = time.perf_counter()
    try:
        resp = session.request(
            method,
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=follow_redirects,
            verify=verify_ssl,
            stream=True,
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        # Include the final hop in the chain, not just the intermediate
        # redirects, so the chain reads as a complete path A -> B -> C.
        hops = list(resp.history) + [resp]
        chain = [f"{h.status_code} {h.url}" for h in hops]
        was_redirected = bool(resp.history) or (
            not follow_redirects and 300 <= resp.status_code < 400
        )
        # The code returned by the very first request made — e.g. the 301
        # a URL responds with before it's followed to a final 200. Equal
        # to "Status Code" whenever there was no redirect.
        initial_status_code = resp.history[0].status_code if resp.history else resp.status_code

        result.update({
            "Initial Status Code": initial_status_code,
            "Status Code": resp.status_code,
            "Status Meaning": STATUS_MEANINGS.get(resp.status_code, ""),
            "Final URL": resp.url,
            "Redirected": was_redirected,
            "Redirect Count": len(resp.history),
            "Redirect Chain": " → ".join(chain) if resp.history else "",
            "Location": resp.headers.get("Location", "") if 300 <= resp.status_code < 400 else "",
            "Response Time (ms)": elapsed_ms,
            "Content Type": resp.headers.get("Content-Type", ""),
            # Never force-download the body just to compute a length: with
            # stream=True that would silently pull the entire response
            # (potentially huge) over the wire. Report what the server
            # declared, or "Unknown" if it didn't say.
            "Content Length": resp.headers.get("Content-Length", "Unknown"),
            "Server": resp.headers.get("Server", ""),
            "Retry-After": resp.headers.get("Retry-After", ""),
        })
        resp.close()

    except requests.exceptions.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        result["Response Time (ms)"] = elapsed_ms
        result["Error Reason"] = classify_error(exc)
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        result["Response Time (ms)"] = elapsed_ms
        result["Error Reason"] = f"Unexpected error: {exc}"

    return result


def run_bulk_check(urls, method, timeout, verify_ssl, max_redirects,
                    follow_redirects, retries, workers, progress_cb=None):
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                check_url, u, method, timeout, verify_ssl,
                max_redirects, follow_redirects, retries
            ): u for u in urls
        }
        done = 0
        total = len(urls)
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if progress_cb:
                progress_cb(done, total)
    # preserve original input order
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["URL"], 0))
    return results


def style_status(val):
    if pd.isna(val) or val == "":
        return "color:#6b7280;font-weight:600;"
    try:
        code = int(val)
    except (ValueError, TypeError):
        return ""
    bucket = status_bucket(code)
    colors = {"2xx": "#16a34a", "3xx": "#ca8a04", "4xx": "#dc2626", "5xx": "#b91c1c"}
    return f"color:{colors.get(bucket, '#111827')};font-weight:600;"


# ----------------------------------------------------------------------------
# Session state
# ----------------------------------------------------------------------------
if "results" not in st.session_state:
    st.session_state.results = []
if "last_run_meta" not in st.session_state:
    st.session_state.last_run_meta = {}

# ----------------------------------------------------------------------------
# Header / Hero (SEO-relevant on-page content, entity-focused, no stuffing)
# ----------------------------------------------------------------------------
st.title("🟢 HTTP Status Checker")
st.markdown(
    "#### Bulk URL & Redirect Checker — check status codes, redirect chains "
    "and response times for many links at once"
)
st.markdown(
    "Paste your links or upload a file to check HTTP status codes in bulk. "
    "This **HTTP status checker** validates every URL, follows redirects, "
    "and reports the exact response code — 200, 301, 302, 404, 410, 429, "
    "503, and more — so you always know whether a page is live, moved, or "
    "broken. It works equally well as an **HTTPS status checker**, a "
    "**bulk URL redirect checker** for SEO redirect audits, and a general "
    "**bulk redirect checker** for QA and DevOps link testing."
)

st.divider()

# ----------------------------------------------------------------------------
# Sidebar — settings
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Check Settings")

    method = st.selectbox(
        "Request method",
        ["GET", "HEAD"],
        index=0,
        help="GET is the most reliable way to test redirects and status "
             "codes. HEAD is faster, but some servers respond to it "
             "differently than GET (e.g. HEAD → 405 while GET → 200), so "
             "treat HEAD results as a quick pass, not a final verdict.",
    )
    follow_redirects = st.checkbox("Follow redirects", value=True)
    max_redirects = st.slider("Max redirects to follow", 1, 20, 10,
                               disabled=not follow_redirects)
    timeout = st.slider("Timeout per URL (seconds)", 1, 30, 10)
    verify_ssl = st.checkbox("Verify SSL/TLS certificates", value=True)
    retries = st.slider(
        "Retries on timeout / connection error / 5xx / 429", 0, 3, 1,
        help="Automatically retries transient failures with a short "
             "backoff before giving up, so a brief blip doesn't get "
             "reported as a false failure.",
    )
    workers = st.slider(
        "Concurrency (parallel workers)", 1, MAX_WORKERS, 15,
        help="Higher = faster checks, but be considerate of target "
             "servers and of other users sharing this app instance.",
    )

    st.divider()
    st.caption(
        f"This tool performs lightweight status/redirect checks only, up "
        f"to {MAX_URLS:,} URLs per run. It is not a crawler, monitoring "
        f"platform, or vulnerability scanner."
    )

# ----------------------------------------------------------------------------
# Input section
# ----------------------------------------------------------------------------
st.subheader("1. Add your URLs")

input_col1, input_col2 = st.columns([2, 1])

with input_col1:
    text_input = st.text_area(
        "Paste URLs (one per line)",
        height=180,
        placeholder="https://example.com\nhttps://example.com/blog\nexample.com/old-page",
    )

with input_col2:
    uploaded_files = st.file_uploader(
        "Or upload TXT / CSV",
        type=["txt", "csv"],
        accept_multiple_files=True,
        help="One URL per line (TXT) or URLs anywhere in the CSV cells. "
             f"Max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file.",
    )
    st.caption(f"Runs up to {MAX_URLS:,} URLs concurrently per check.")

urls, skipped_count = parse_input_urls(text_input, uploaded_files)

too_many = len(urls) > MAX_URLS
if too_many:
    st.error(
        f"🚫 {len(urls):,} URLs found, which is over the {MAX_URLS:,} "
        f"limit for a single run. Please split your list into smaller "
        f"batches."
    )
elif urls:
    msg = f"✅ {len(urls)} unique URL(s) ready to check."
    if skipped_count:
        msg += f" ({skipped_count} line(s)/cell(s) skipped — didn't look like a URL.)"
    st.success(msg)
else:
    st.info("Paste URLs above or upload a file to get started.")
    if skipped_count:
        st.warning(f"⚠️ {skipped_count} line(s)/cell(s) didn't look like a valid URL and were skipped.")

run_clicked = st.button(
    "🚀 Check Status Codes", type="primary",
    disabled=(len(urls) == 0 or too_many),
)

# ----------------------------------------------------------------------------
# Run the check
# ----------------------------------------------------------------------------
if run_clicked and urls and not too_many:
    progress_bar = st.progress(0.0, text="Starting checks…")

    def _progress(done, total):
        pct = done / total
        progress_bar.progress(pct, text=f"Checked {done} / {total} URLs…")

    t0 = time.perf_counter()
    results = run_bulk_check(
        urls, method, timeout, verify_ssl, max_redirects,
        follow_redirects, retries, workers, progress_cb=_progress,
    )
    total_time = round(time.perf_counter() - t0, 2)

    progress_bar.progress(1.0, text="Done!")
    st.session_state.results = results
    st.session_state.last_run_meta = {
        "count": len(results),
        "duration": total_time,
        "run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

# ----------------------------------------------------------------------------
# Results section
# ----------------------------------------------------------------------------
if st.session_state.results:
    st.divider()
    st.subheader("2. Results")

    df = pd.DataFrame(st.session_state.results)
    # Use pandas' nullable integer type so missing status codes (failed
    # requests) show as blank instead of forcing the whole column to float
    # (which would render "404.0" instead of "404").
    df["Status Code"] = df["Status Code"].astype("Int64")
    df["Initial Status Code"] = df["Initial Status Code"].astype("Int64")
    # Put Initial Status Code right after the URL so a redirected URL's
    # first (e.g. 301) code is visible without reading the chain text.
    cols = df.columns.tolist()
    cols.insert(cols.index("URL") + 1, cols.pop(cols.index("Initial Status Code")))
    df = df[cols]
    meta = st.session_state.last_run_meta

    total = len(df)
    ok = int((df["Status Code"].dropna().astype(int).between(200, 299)).sum())
    redirects = int((df["Status Code"].dropna().astype(int).between(300, 399)).sum())
    client_err = int((df["Status Code"].dropna().astype(int).between(400, 499)).sum())
    server_err = int((df["Status Code"].dropna().astype(int).between(500, 599)).sum())
    failed = int(df["Status Code"].isna().sum())

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total URLs", total)
    m2.metric("2xx OK", ok)
    m3.metric("3xx Redirect", redirects)
    m4.metric("4xx Error", client_err)
    m5.metric("5xx Error", server_err)
    m6.metric("Failed / No Response", failed)

    if meta:
        st.caption(
            f"Finished {meta['count']} checks in {meta['duration']}s "
            f"(run at {meta['run_at']})."
        )

    # Filters
    fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
    with fcol1:
        bucket_filter = st.multiselect(
            "Filter by status type",
            ["2xx", "3xx", "4xx", "5xx", "other", "error"],
            default=[],
        )
    with fcol2:
        only_redirects = st.checkbox("Only show redirected URLs")
    with fcol3:
        search_term = st.text_input("Search URL contains… (plain text, not regex)", "")

    filtered = df.copy()
    filtered["_bucket"] = filtered["Status Code"].apply(
        lambda c: status_bucket(int(c)) if pd.notna(c) else "error"
    )
    if bucket_filter:
        filtered = filtered[filtered["_bucket"].isin(bucket_filter)]
    if only_redirects:
        filtered = filtered[filtered["Redirected"] == True]  # noqa: E712
    if search_term:
        filtered = filtered[
            filtered["URL"].str.contains(search_term, case=False, na=False, regex=False)
        ]
    filtered = filtered.drop(columns=["_bucket"])

    st.caption(
        "**Initial Status Code** is the code the URL itself returned "
        "(e.g. `301`). **Status Code** is the code of the final page after "
        "any redirects were followed (e.g. `200`). They're equal when a "
        "URL didn't redirect."
    )

    styler = filtered.style
    if hasattr(styler, "map"):
        styler = styler.map(style_status, subset=["Status Code", "Initial Status Code"])
    else:  # older pandas versions
        styler = styler.applymap(style_status, subset=["Status Code", "Initial Status Code"])

    st.dataframe(
        styler,
        use_container_width=True,
        height=460,
    )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    st.subheader("3. Export results")
    e1, e2, e3 = st.columns(3)

    csv_bytes = filtered.to_csv(index=False).encode("utf-8")
    json_bytes = json.dumps(filtered.to_dict(orient="records"), indent=2, default=str).encode("utf-8")
    txt_lines = "\n".join(
        f"{row['URL']} -> initial: {row['Initial Status Code']}, "
        f"final: {row['Status Code']} ({row['Status Meaning']}) "
        f"final URL: {row['Final URL']}" for _, row in filtered.iterrows()
    )

    with e1:
        st.download_button(
            "⬇️ Download CSV", data=csv_bytes,
            file_name="http_status_results.csv", mime="text/csv",
            use_container_width=True,
        )
    with e2:
        st.download_button(
            "⬇️ Download JSON", data=json_bytes,
            file_name="http_status_results.json", mime="application/json",
            use_container_width=True,
        )
    with e3:
        st.download_button(
            "⬇️ Download TXT", data=txt_lines.encode("utf-8"),
            file_name="http_status_results.txt", mime="text/plain",
            use_container_width=True,
        )

# ----------------------------------------------------------------------------
# SEO / entity content section (below the fold, natural, non-stuffed)
# ----------------------------------------------------------------------------
st.divider()

with st.expander("ℹ️ What is an HTTP Status Checker?", expanded=False):
    st.markdown(
        """
An **HTTP status checker** sends a request to a URL and reports the HTTP
response code the server returns — for example `200 OK`, `301 Moved
Permanently`, `404 Not Found`, or `503 Service Unavailable`. It's the
fastest way to confirm whether a page is live, redirected, broken, or
blocking access.

This tool works as a **URL status checker**, a **site status checker**,
and a bulk URL checker — paste a single link or upload a batch at once.

**Common use cases**

- **SEO audits** — find broken links, redirect chains, and orphaned 404s
  across a site.
- **Redirect testing** — confirm a **301 redirect**, **302 redirect**, or
  a full redirect chain resolves to the correct final URL.
- **Migration QA** — verify old URLs correctly redirect after a domain
  or CMS migration.
- **Uptime spot-checks** — quickly confirm whether pages return `200`,
  or errors like `401`, `403`, `429`, or `503`.
- **Link-list cleanup** — run a **bulk link checker** pass on affiliate
  links, backlinks, or a sitemap export.
        """
    )

with st.expander("🔁 Bulk URL Redirect Checker — how redirect chains work", expanded=False):
    st.markdown(
        """
A **bulk redirect checker** doesn't just report the final destination —
it should show every hop in between. This tool exposes the full
**redirect chain**: every intermediate `301`, `302`, `307`, or `308`
response, in order, ending with the final response, until it reaches the
final URL or hits an error.

Long redirect chains slow pages down and can dilute SEO signals, so
finding and flattening them is a common part of any **redirect audit**.
Use the **Redirect Chain**, **Redirected**, and **Redirect Count**
columns above to spot pages that redirect more than once and simplify
them to a single direct redirect.
        """
    )

with st.expander("❓ Frequently asked questions", expanded=False):
    st.markdown(
        """
**What does HTTP status code 503 mean?**
`503 Service Unavailable` means the server is temporarily unable to
handle the request — often due to maintenance or overload. It's usually
transient, so a **503 response code** should be re-checked after a
short delay.

**What does HTTP status code 401 mean?**
`401 Unauthorized` means the request lacks valid authentication
credentials. It's common when checking pages behind a login.

**What is a 302 redirect vs a 301 redirect?**
`301 Moved Permanently` tells crawlers and browsers the move is
permanent and to update their links. `302 Found` signals a temporary
redirect. In practice, search engines weigh several signals — not the
status code alone — when deciding which URL to treat as canonical, so a
302 is a strong hint the original should stay canonical, not a
guarantee.

**What does status code 410 mean?**
`410 Gone` explicitly tells crawlers the resource was intentionally
removed and won't come back, which is stronger than a `404 Not Found`.

**Can this be used as a website redirect checker for an entire site?**
Yes — upload a sitemap export or crawl list as TXT/CSV and run it to
test redirects and status codes for many URLs at once, up to the
per-run limit shown in the sidebar.

**Does this tool work as an HTTPS status checker?**
Yes. It supports both `http://` and `https://` URLs and verifies TLS
certificates by default (this can be turned off in settings for testing
self-signed certs).
        """
    )

st.caption(
    "This tool is designed for HTTP status and redirect checking only. "
    "It does not crawl entire websites, scan for vulnerabilities, render "
    "pages like a browser, or provide continuous uptime monitoring."
)

# ----------------------------------------------------------------------------
# Support section
# ----------------------------------------------------------------------------
st.markdown(
    """
<div class="support-box">
<h3>🌟 Support My Work!</h3>
<p>Hey there! I'm <a href="https://x.com/DevenderKG" target="_blank">Devender Gupta</a> 👋<br>
If you've found this HTTP status checker helpful, interesting, or useful, I'd really appreciate your support. ❤️</p>
<p>👉 Read my <a href="https://devendergupta.netlify.app/" target="_blank">SEO blog</a>, explore my work, and follow along for more!</p>
</div>
    """,
    unsafe_allow_html=True,
)
