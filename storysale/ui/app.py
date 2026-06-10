"""Streamlit UI for browsing scraped sales.

Run with:
    streamlit run storysale/ui/app.py

The UI is read-mostly: search the FTS5 index, filter by clothing/brand/account/
date/price, click 👍/👎 to mark feedback (which keeps a row past retention).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

from storysale import config, logging_setup
from storysale.db import repo

DB_PATH = config.DB_PATH
THUMB_DIR = config.THUMB_DIR


# ---------- connection ----------

@st.cache_resource
def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Streamlit reruns the script on different threads but the cached connection
    # is shared, so SQLite's same-thread check has to be relaxed.
    return repo.connect(DB_PATH, check_same_thread=False)


# ---------- query layer ----------

def query_rows(
    conn: sqlite3.Connection,
    *,
    search_text: str = "",
    clothing: str = "",
    brand: str = "",
    account: str = "",
    min_price: int = 0,
    max_price: int = 9999,
    days_back: int = 30,
    only_needs_review: bool = False,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Build a filtered query. Uses FTS5 if search_text is non-empty, else falls back
    to the base table. Filters are AND-combined and applied post-FTS."""
    now_cutoff = int(time.time()) - days_back * 24 * 3600
    conditions = ["c.posted_at >= ?"]
    params: list = [now_cutoff]

    if clothing:
        conditions.append("c.clothing_json LIKE ?")
        params.append(f"%{clothing.lower()}%")
    if brand:
        conditions.append("c.brand = ?")
        params.append(brand)
    if account:
        conditions.append("c.account = ?")
        params.append(account)
    if only_needs_review:
        conditions.append("c.needs_review = 1")

    where = " AND ".join(conditions)

    if search_text.strip():
        sql = f"""
            SELECT c.* FROM content c
            JOIN content_fts f ON f.rowid = c.rowid
            WHERE content_fts MATCH ? AND {where}
            ORDER BY c.posted_at DESC LIMIT ?
        """
        rows = conn.execute(sql, [search_text, *params, limit]).fetchall()
    else:
        sql = f"SELECT c.* FROM content c WHERE {where} ORDER BY c.posted_at DESC LIMIT ?"
        rows = conn.execute(sql, [*params, limit]).fetchall()

    # Post-filter by price (cheap to do in Python on the truncated set).
    if min_price > 0 or max_price < 9999:
        kept = []
        for r in rows:
            prices = json.loads(r["prices_json"] or "[]")
            if any(min_price <= p <= max_price for p in prices):
                kept.append(r)
        return kept
    return list(rows)


def last_run(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM scrape_run ORDER BY id DESC LIMIT 1"
    ).fetchone()


def set_feedback(conn: sqlite3.Connection, ig_id: str, value: int) -> None:
    conn.execute("UPDATE content SET feedback = ? WHERE ig_id = ?", (value, ig_id))
    conn.commit()


# IG stories live for 24h after posting; we re-import the constant rather than
# hard-coding so a future change in repo.py automatically flows here.
from storysale.db.repo import STORY_LIFETIME  # noqa: E402


def _render_freshness(r: sqlite3.Row, *, now: int) -> None:
    """Render an 'expires in N hours' / 'expired Xh ago' caption for a signal
    based on its newest active-sale story's posted_at."""
    newest = r["newest_story_at"] or 0
    if newest <= 0:
        # No active-sale stories observed — nothing to time-bound.
        return
    expires_at = newest + STORY_LIFETIME
    delta = expires_at - now
    if delta > 0:
        hours = delta // 3600
        mins = (delta % 3600) // 60
        if hours >= 1:
            st.caption(f"⏳ expires in ~{hours}h {mins}m on IG")
        else:
            st.caption(f"⏳ expires in ~{mins}m on IG")
    else:
        ago = (-delta) // 3600
        st.caption(f"⚠️ expired ~{ago}h ago on IG (no longer visible)")


def _run_scraper_streaming() -> None:
    """Spawn the scraper subprocess and stream its output line-by-line into a
    code block in the sidebar, so the user sees progress instead of staring at
    a frozen spinner for the duration of a long scrape."""
    panel = st.empty()
    lines: list[str] = []
    with st.spinner("Scraping… (live output below)"):
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "storysale.cli", "--verbose", "scrape"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            # Keep the tail bounded so the UI doesn't try to render thousands
            # of lines if a long scrape spews logs.
            panel.code("\n".join(lines[-200:]))
        proc.wait()
    if proc.returncode != 0:
        st.error(f"scraper exited with code {proc.returncode}")


# ---------- UI ----------

def render():
    st.set_page_config(page_title="StorySale", layout="wide")
    st.title("StorySale")

    conn = get_conn()

    # --- sidebar: scraper status + run button (shared across tabs) ---
    with st.sidebar:
        st.header("Scraper")
        lr = last_run(conn)
        if lr:
            finished = lr["finished_at"]
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(finished)) if finished else "running…"
            st.write(f"Last run: **{when}**  ·  status: `{lr['status']}`")
            st.write(f"seen: {lr['items_seen']} · stored: {lr['items_stored']} · rejected: {lr['items_rejected']}")
            if lr["error"]:
                st.error(lr["error"])
        else:
            st.write("No runs yet.")

        if st.button("Run scraper now"):
            _run_scraper_streaming()
            st.cache_resource.clear()
            st.rerun()

        with st.expander("Recent scrape log (last 200 lines)"):
            st.code(logging_setup.tail(200), language="text")

    tab_sales, tab_search = st.tabs(["🔥 Story sales right now", "🔍 Search posts"])
    with tab_sales:
        _render_story_sales(conn)
    with tab_search:
        _render_post_search(conn)


def _render_story_sales(conn: sqlite3.Connection) -> None:
    st.caption(
        "Accounts whose most-recent story scrape contained a sale signal "
        "(price or sale language) and no disqualifier (h/o, c/o, sold). "
        "Cards disappear once their underlying IG stories expire (24h after posting)."
    )
    col_state, col_show_expired = st.columns([3, 1])
    with col_state:
        state_filter = st.radio(
            "Show", ["Active sales", "Offers only", "Sold-out", "Everything"],
            horizontal=True, index=0,
        )
    with col_show_expired:
        show_expired = st.checkbox(
            "Show expired", value=False,
            help="Include signals whose stories are already past IG's 24h window.",
        )
    state_map = {
        "Active sales": "active_sale", "Offers only": "offers_only",
        "Sold-out": "sold", "Everything": None,
    }
    rows = repo.list_account_signals(
        conn, state=state_map[state_filter], hide_expired=not show_expired,
    )
    if not rows:
        st.info(
            "No accounts to show. Either the scraper hasn't run yet, or every "
            "signal's underlying IG stories have already expired."
        )
        return

    now = int(time.time())
    for r in rows:
        with st.container(border=True):
            head = st.columns([3, 1, 1])
            with head[0]:
                st.subheader(f"@{r['account']}")
                checked = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["checked_at"]))
                st.caption(f"checked: {checked}")
                _render_freshness(r, now=now)
            with head[1]:
                st.metric("Stories (24h)", r["story_count_24h"])
            with head[2]:
                st.metric("Sale stories", r["active_sale_count"])

            badge_parts = []
            if r["state"] == "active_sale":
                badge_parts.append("🟢 active sale")
            elif r["state"] == "offers_only":
                badge_parts.append("🟡 offers only")
            elif r["state"] == "sold":
                badge_parts.append("🔴 sold")
            else:
                badge_parts.append("⚪ no signal")
            if r["needs_review"]:
                badge_parts.append("⚠️ OCR review")
            badge_parts.append(f"offers={r['offers_only_count']}  sold={r['sold_count']}")
            st.write(" · ".join(badge_parts))

            snippets = json.loads(r["snippets_json"] or "[]")
            if snippets:
                with st.expander(f"{len(snippets)} sale snippet(s)"):
                    for s in snippets:
                        st.write(f"> {s}")
            prices = json.loads(r["prices_json"] or "[]")
            if prices:
                st.caption(f"prices seen: {prices}")
            st.markdown(f"[Open @{r['account']} on Instagram →](https://www.instagram.com/{r['account']}/)")


def _render_post_search(conn: sqlite3.Connection) -> None:
    with st.sidebar:
        st.divider()
        st.header("Post filters")
        search_text = st.text_input("Search (FTS5)", placeholder="margiela, hoodie, $250…")
        clothing = st.text_input("Clothing term", placeholder="e.g. jacket")
        brand = st.text_input("Brand", placeholder="e.g. Rick Owens")
        accounts = repo.list_accounts(conn)
        account = st.selectbox("Account", [""] + accounts)
        min_p, max_p = st.slider("Price range", 0, 5000, (0, 5000))
        days_back = st.slider("Days back", 1, 30, 30)
        only_needs_review = st.checkbox("Only show OCR-needs-review")

    rows = query_rows(
        conn,
        search_text=search_text, clothing=clothing, brand=brand, account=account,
        min_price=min_p, max_price=max_p, days_back=days_back,
        only_needs_review=only_needs_review,
    )
    st.caption(f"{len(rows)} result(s)")

    for r in rows:
        with st.container(border=True):
            cols = st.columns([1, 4, 1])
            with cols[0]:
                if r["thumbnail_path"]:
                    thumb = THUMB_DIR.parent / r["thumbnail_path"]
                    if thumb.exists():
                        st.image(str(thumb), use_container_width=True)
                    else:
                        st.write("(image expired)")
                else:
                    st.write("(no image)")
            with cols[1]:
                badges = ["🔵 post" if r["kind"] == "post" else "🟣 story"]
                if r["needs_review"]:
                    badges.append("⚠️ OCR review")
                st.write(" · ".join(badges))
                st.write(f"**@{r['account']}** · {r['brand'] or '-'}")
                prices = json.loads(r["prices_json"] or "[]")
                clothing_terms = json.loads(r["clothing_json"] or "[]")
                st.write(f"💲{prices} · 👕{clothing_terms}")
                text = (r["caption"] or r["ocr_text"] or "")[:300]
                st.caption(text)
                st.markdown(f"[Open on Instagram →]({r['ig_url']})")
            with cols[2]:
                fb = r["feedback"] or 0
                up, down = st.columns(2)
                if up.button("👍", key=f"up_{r['ig_id']}", type="primary" if fb == 1 else "secondary"):
                    set_feedback(conn, r["ig_id"], 1 if fb != 1 else 0)
                    st.rerun()
                if down.button("👎", key=f"dn_{r['ig_id']}", type="primary" if fb == -1 else "secondary"):
                    set_feedback(conn, r["ig_id"], -1 if fb != -1 else 0)
                    st.rerun()


if __name__ == "__main__":
    render()
