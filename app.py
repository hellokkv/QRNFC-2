import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

# Use streamlit-qrcode-scanner for back camera support
try:
    from streamlit_qrcode_scanner import qrcode_scanner
    QR_SCANNER_AVAILABLE = True
except ImportError:
    QR_SCANNER_AVAILABLE = False

import cv2
from pyzbar.pyzbar import decode
from PIL import Image
import numpy as np

#======DB Setup===================
def create_tables():
    conn = sqlite3.connect('inventory.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS drums (
            DrumID TEXT PRIMARY KEY,
            OrderNo TEXT,
            Quantity TEXT,
            RA TEXT,
            CellType TEXT,
            Status TEXT,
            CurrentGrid TEXT,
            LastUpdated DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS grids (
            GridID TEXT PRIMARY KEY,
            Status TEXT,
            CurrentDrumID TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            TxnID INTEGER PRIMARY KEY AUTOINCREMENT,
            DrumID TEXT,
            GridID TEXT,
            Status TEXT,
            Timestamp DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS drum_history (
            HistID INTEGER PRIMARY KEY AUTOINCREMENT,
            DrumID TEXT,
            OrderNo TEXT,
            Quantity TEXT,
            RA TEXT,
            CellType TEXT,
            Status TEXT,
            GridID TEXT,
            Timestamp DATETIME
        )
    ''')
    # Pre-populate 3x3 grid
    for row in "ABC":
        for col in range(1, 4):
            grid_id = f"{row}{col}"
            c.execute("INSERT OR IGNORE INTO grids (GridID, Status, CurrentDrumID) VALUES (?, 'Available', NULL)", (grid_id,))
    conn.commit()
    conn.close()

create_tables()

# ---- DB HELPERS -------
def get_db_connection():
    conn = sqlite3.connect('inventory.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def get_drums_by_orderno(conn, order_no):
    return pd.read_sql_query("SELECT * FROM drums WHERE OrderNo = ?", conn, params=(order_no,))

def get_available_grids(conn):
    return pd.read_sql_query("SELECT * FROM grids WHERE Status='Available'", conn)

def get_all_grids(conn):
    return pd.read_sql_query("SELECT * FROM grids", conn)

def get_all_drums(conn):
    return pd.read_sql_query("SELECT * FROM drums", conn)

def get_drum(conn, drum_id):
    return pd.read_sql_query("SELECT * FROM drums WHERE DrumID = ?", conn, params=(drum_id,))

def insert_drum(conn, drum_id, order_no, ra, cell_type, quantity):
    now = datetime.now()
    conn.execute("INSERT OR REPLACE INTO drums (DrumID, OrderNo, Quantity, RA, CellType, Status, CurrentGrid, LastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (drum_id, order_no, quantity, ra, cell_type, 'OUT', None, now))
    conn.commit()

def update_drum_info(conn, drum_id, order_no, ra, cell_type, quantity):
    now = datetime.now()
    conn.execute("UPDATE drums SET OrderNo=?, Quantity=?, RA=?, CellType=?, Status='OUT', CurrentGrid=NULL, LastUpdated=? WHERE DrumID=?",
                 (order_no, quantity, ra, cell_type, now, drum_id))
    conn.commit()

def update_drum_in(conn, drum_id, grid_id):
    now = datetime.now()
    conn.execute("UPDATE drums SET Status = 'IN', CurrentGrid = ?, LastUpdated = ? WHERE DrumID = ?", (grid_id, now, drum_id))
    conn.execute("UPDATE grids SET Status = 'Occupied', CurrentDrumID = ? WHERE GridID = ?", (drum_id, grid_id))
    conn.execute("INSERT INTO transactions (DrumID, GridID, Status, Timestamp) VALUES (?, ?, 'IN', ?)", (drum_id, grid_id, now))
    conn.commit()

def update_drum_out(conn, drum_id):
    now = datetime.now()
    drum = get_drum(conn, drum_id)
    if drum.empty or drum.iloc[0]['CurrentGrid'] is None:
        return False
    grid_id = drum.iloc[0]['CurrentGrid']
    # Log to history
    conn.execute("""
        INSERT INTO drum_history (DrumID, OrderNo, Quantity, RA, CellType, Status, GridID, Timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            drum_id,
            drum.iloc[0]['OrderNo'],
            drum.iloc[0]['Quantity'],
            drum.iloc[0]['RA'],
            drum.iloc[0]['CellType'],
            'OUT',
            grid_id,
            now
        ))
    # Mark OUT and clear info
    conn.execute("UPDATE drums SET Status = 'OUT', OrderNo = NULL, Quantity = NULL, RA = NULL, CellType = NULL, CurrentGrid = NULL, LastUpdated = ? WHERE DrumID = ?", (now, drum_id))
    conn.execute("UPDATE grids SET Status = 'Available', CurrentDrumID = NULL WHERE GridID = ?", (grid_id,))
    conn.execute("INSERT INTO transactions (DrumID, GridID, Status, Timestamp) VALUES (?, ?, 'OUT', ?)", (drum_id, grid_id, now))
    conn.commit()
    return True

def shift_drum_grid(conn, drum_id, new_grid_id):
    now = datetime.now()
    drum = get_drum(conn, drum_id)
    if drum.empty or drum.iloc[0]['CurrentGrid'] is None:
        return False
    old_grid = drum.iloc[0]['CurrentGrid']
    # Free up the old grid
    conn.execute("UPDATE grids SET Status = 'Available', CurrentDrumID = NULL WHERE GridID = ?", (old_grid,))
    # Occupy the new grid
    conn.execute("UPDATE grids SET Status = 'Occupied', CurrentDrumID = ? WHERE GridID = ?", (drum_id, new_grid_id))
    # Update drum's grid
    conn.execute("UPDATE drums SET CurrentGrid = ?, LastUpdated = ? WHERE DrumID = ?", (new_grid_id, now, drum_id))
    # Log shift transaction
    conn.execute("INSERT INTO transactions (DrumID, GridID, Status, Timestamp) VALUES (?, ?, 'SHIFT', ?)", (drum_id, new_grid_id, now))
    conn.commit()
    return True

def get_drum_history(conn):
    return pd.read_sql_query("SELECT * FROM drum_history", conn)

# ---- PAGES ------
def dashboard(conn):
    st.title("📦 Drum Storage Grid Dashboard")

    st.subheader("🔎 Search Drum by Order Number")
    search_order = st.text_input("Enter Order Number to search (case-sensitive)").strip()
    if search_order:
        result = get_drums_by_orderno(conn, search_order)
        if not result.empty:
            st.success(f"Found {len(result)} record(s) for Order Number: {search_order}")
            st.dataframe(result)
        else:
            st.error(f"No drums found for Order Number: {search_order}")

    st.subheader("Grid Status (Auto-refreshes every 10 seconds)")
    grids = get_all_grids(conn)
    st.dataframe(grids)
    st.subheader("All Drums (Current Status)")
    drums = get_all_drums(conn)
    st.dataframe(drums)
    st.subheader("Drum OUT History Log")
    drum_hist = get_drum_history(conn)
    st.dataframe(drum_hist)
    st.caption("Reloads automatically in 10 seconds.")
    st_autorefresh(interval=10*1000, key="refresh_dashboard")

def qr_page(conn):
    st.header("📷 Drum QR Scan (IN/OUT/Shift)")

    # Only reset before widget creation!
    if "reset_drum_id" not in st.session_state:
        st.session_state.reset_drum_id = False
    if "drum_id_input" not in st.session_state or st.session_state.reset_drum_id:
        st.session_state.drum_id_input = ""
        st.session_state.reset_drum_id = False

    # --- QR SCAN: Use streamlit-qrcode-scanner for camera selection ---
    st.subheader("Scan Drum QR Code")
    drum_scanned = None

    if QR_SCANNER_AVAILABLE:
        st.caption("Tap the flip icon to use the back camera if needed.")
        drum_scanned = qrcode_scanner(key="qrscanner")
        if drum_scanned:
            st.session_state.drum_id_input = drum_scanned
            st.success(f"✅ Scanned Drum ID: {drum_scanned}")
    else:
        st.warning("QR scanner component not installed. Only manual or camera photo scanning available.")

    # Fallback camera/photo scan for manual (front camera only)
    st.info(
        "Tip: If the QR scanner is not working or you prefer, use your phone's camera app to scan and paste the Drum ID below."
    )

    drum_id = st.text_input("Drum ID (scan or paste here)", value=st.session_state.drum_id_input, key="drum_id_input").strip().upper()

    if drum_id:
        drum = get_drum(conn, drum_id)
        if not drum.empty and drum.iloc[0]["Status"] == "IN":
            st.success("Drum is currently IN storage. Details below:")
            st.json(dict(drum.iloc[0]))

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Mark as OUT / Retrieve Drum", key="out_btn"):
                    if update_drum_out(conn, drum_id):
                        st.success(f"Drum {drum_id} marked as OUT and grid freed. History logged.")
                        st.session_state.reset_drum_id = True
                        st.experimental_rerun()
                    else:
                        st.error("Could not update drum status. Try again or check drum/grid state.")
            with col2:
                shift_state = st.session_state.get("shift_mode", False)
                if not shift_state:
                    if st.button("Shift Drum to Another Grid", key="shift_btn"):
                        st.session_state["shift_mode"] = True
                        st.experimental_rerun()
                else:
                    available_grids = get_available_grids(conn)
                    if available_grids.empty:
                        st.warning("No available grids to shift. Please make space.")
                    else:
                        grid_choices = available_grids["GridID"].tolist()
                        new_grid = st.selectbox("Select new grid to shift this drum", grid_choices, key="shift_grid_select")
                        if st.button("Confirm Shift", key="shift_confirm_btn"):
                            if shift_drum_grid(conn, drum_id, new_grid):
                                st.success(f"Drum {drum_id} shifted to grid {new_grid}.")
                                st.session_state["shift_mode"] = False
                                st.session_state.reset_drum_id = True
                                st.experimental_rerun()
                            else:
                                st.error("Could not shift drum. Try again.")
                        if st.button("Cancel Shift", key="cancel_shift_btn"):
                            st.session_state["shift_mode"] = False
                            st.experimental_rerun()

        else:
            st.info("Drum not found in system or currently OUT. Please enter details to place/IN:")
            order_no = st.text_input("Order Number", key="order_no_input")
            quantity = st.text_input("Enter Quantity (No. of cells)", key="quantity_input")
            ra = st.text_input("RA Number", key="ra_input")
            cell_type = st.text_input("Cell Type (A type, B type, etc)", key="cell_type_input")
            
            available_grids = get_available_grids(conn)
            st.write("Available Grids:")

            if "selected_grid" not in st.session_state:
                st.session_state.selected_grid = None

            def select_grid_callback(grid_id):
                st.session_state.selected_grid = grid_id

            # Header for neatness
            grid_cols = st.columns([1, 1, 2, 1])
            grid_cols[0].markdown("**GridID**")
            grid_cols[1].markdown("**Status**")
            grid_cols[2].markdown("**Current DrumID**")
            grid_cols[3].markdown("**Action**")
            for idx, row in available_grids.iterrows():
                cols = st.columns([1, 1, 2, 1])
                cols[0].write(row["GridID"])
                cols[1].write(row["Status"])
                cols[2].write(str(row["CurrentDrumID"]))
                if cols[3].button(f"Select", key=f"select_{row['GridID']}"):
                    select_grid_callback(row["GridID"])

            if st.session_state.selected_grid:
                st.success(f"Selected Grid: {st.session_state.selected_grid}")
                grid_id = st.session_state.selected_grid
            else:
                grid_id = None

            if order_no and quantity and ra and cell_type and grid_id:
                if st.button("IN / Place Drum in Grid"):
                    if not available_grids[available_grids["GridID"] == grid_id].empty:
                        if drum.empty:
                            insert_drum(conn, drum_id, order_no, ra, cell_type, quantity)
                        else:
                            update_drum_info(conn, drum_id, order_no, ra, cell_type, quantity)
                        update_drum_in(conn, drum_id, grid_id)
                        st.success(f"Drum {drum_id} placed in grid {grid_id}.")
                        st.session_state.reset_drum_id = True
                        st.session_state.selected_grid = None
                        st.experimental_rerun()
                    else:
                        st.error("Selected grid is not available or doesn't exist.")

# ---- MAIN APP -------
st.set_page_config(page_title="Drum Inventory", layout="wide")
conn = get_db_connection()

page = st.sidebar.radio("Select Operation", [
    "Dashboard (Live)",
    "Scan QR for Drum Placement"
])

with st.sidebar:
    st.markdown("---")
    reset_confirm = st.checkbox("Confirm Reset All Data", value=False)
    if st.button("⚠️ Reset All Data (Clear All Logs & Tables)", disabled=not reset_confirm):
        conn.execute("DELETE FROM drums")
        conn.execute("DELETE FROM grids")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM drum_history")
        for row in "ABC":
            for col in range(1, 4):
                grid_id = f"{row}{col}"
                conn.execute("INSERT OR IGNORE INTO grids (GridID, Status, CurrentDrumID) VALUES (?, 'Available', NULL)", (grid_id,))
        conn.commit()
        st.success("All data cleared! Grids reset. Please refresh the page.")
        st.stop()

if page == "Dashboard (Live)":
    dashboard(conn)
elif page == "Scan QR for Drum Placement":
    qr_page(conn)

conn.close()
