"""Streamlit UI for Fyers authentication. The auth logic itself stays in `src/auth.py`."""
import os
import time

import streamlit as st

from src.auth import (TOKEN_FILE, generate_access_token, get_auth_url, get_fyers_client,
                      load_credentials, save_credentials)

# Fyers access tokens last one day; an older token file is stale without asking the API.
TOKEN_MAX_AGE_S = 24 * 3600
OPEN_DIALOG_KEY = "_auth_open_dialog"
PROMPTED_KEY = "_auth_prompted"

# Status values: "valid" and "unverified" count as connected; the rest need (re-)authentication.
CONNECTED = ("valid", "unverified")


@st.cache_data(ttl=300, show_spinner=False)
def _check_session(token_mtime: float) -> tuple[str, str]:
    """Validate the saved token with the profile call. Cached for 5 minutes per token file version.

    Returns (status, label): "valid" (label = Fyers id), "expired", "missing", or "unverified"
    when Fyers could not be reached (offline use with the local cache stays possible).
    """
    if time.time() - token_mtime > TOKEN_MAX_AGE_S:
        return "expired", ""
    fyers = get_fyers_client()
    if fyers is None:
        return "missing", ""
    try:
        resp = fyers.get_profile()
    except Exception:
        return "unverified", ""
    if isinstance(resp, dict) and resp.get("s") == "ok":
        data = resp.get("data") or {}
        return "valid", str(data.get("fy_id") or data.get("name") or "")
    if isinstance(resp, dict) and resp.get("s") == "error":
        return "expired", ""
    return "unverified", ""


def session_status() -> tuple[str, str]:
    if not os.path.exists(TOKEN_FILE):
        return "missing", ""
    return _check_session(os.path.getmtime(TOKEN_FILE))


def request_dialog() -> None:
    """Button callback: ask the main script to open the auth dialog on the next run."""
    st.session_state[OPEN_DIALOG_KEY] = True


def render_status(status: str, label: str) -> None:
    """Compact connection row for the sidebar."""
    with st.container(horizontal=True, vertical_alignment="center"):
        if status == "valid":
            st.badge("Connected", icon=":material/check_circle:", color="green")
            if label:
                st.caption(label)
        elif status == "unverified":
            st.badge("Unverified", icon=":material/help:", color="orange",
                     help="Fyers could not be reached to validate the saved token.")
        else:
            st.badge("Not connected", icon=":material/error:", color="red",
                     help="No valid token." if status == "missing" else "The saved token has expired.")
        st.button("Re-authenticate" if status in CONNECTED else "Connect", key="auth_open",
                  icon=":material/key:", type="tertiary" if status in CONNECTED else "primary",
                  on_click=request_dialog)


def open_dialog_if_requested(status: str) -> None:
    """Call from the main script (outside `st.sidebar`): opens the dialog on request, and once
    automatically per session when there is no usable token."""
    requested = st.session_state.pop(OPEN_DIALOG_KEY, False)
    auto = status not in CONNECTED and not st.session_state.get(PROMPTED_KEY, False)
    if auto:
        st.session_state[PROMPTED_KEY] = True
    if requested or auto:
        auth_dialog()


@st.dialog("Connect to Fyers", width="medium")
def auth_dialog() -> None:
    saved = load_credentials()
    with st.expander("Credentials", expanded=not saved, icon=":material/badge:"):
        with st.form("auth_form", border=False):
            app_id = st.text_input("App ID (client ID)", value=saved["client_id"] if saved else "")
            secret_id = st.text_input("Secret ID", type="password", value=saved["secret_id"] if saved else "")
            redirect_uri = st.text_input(
                "Redirect URL",
                value=saved["redirect_uri"] if saved else "https://trade.fyers.in/api-login/redirect-uri/index.html")
            if st.form_submit_button("Save credentials", icon=":material/save:") and app_id and secret_id:
                save_credentials(app_id, secret_id, redirect_uri)
                st.success("Credentials encrypted and saved locally.")

    if not (app_id and redirect_uri):
        st.info("Enter your App ID and redirect URL to continue.")
        return

    st.markdown("**Step 1.** Log in and authorise the app.")
    st.link_button("Open Fyers login", get_auth_url(app_id, redirect_uri), icon=":material/open_in_new:")
    st.markdown("**Step 2.** Paste the auth code from the redirect URL.")
    code = st.text_input("Auth code", key="auth_code", label_visibility="collapsed",
                         placeholder="Auth code from the redirect URL")
    if st.button("Generate access token", type="primary", icon=":material/vpn_key:", disabled=not code.strip()):
        try:
            generate_access_token(app_id, secret_id, redirect_uri, code.strip())
        except Exception as e:
            st.error(f"Error: {e}")
        else:
            _check_session.clear()
            st.toast("Access token generated and saved.", icon=":material/check_circle:")
            st.rerun()
