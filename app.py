import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
from streamlit_autorefresh import st_autorefresh
import cv2
from pyzbar.pyzbar import decode
from PIL import Image
import numpy as np

# ====== DB Setup ===================
def create_tables():
    conn = sqlite3.connect('inventory.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS drums (
            DrumID TEXT PRIMARY KEY,
            OrderNo TEXT,
            RA TEXT,
            Quantity TEXT,
            CellType TEXT,
            Status TEXT,
            CurrentGrid TEXT,
            LastUpdated DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS grids (
            GridID TEXT PRIMARY KEY,
            Status TEXT
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
            RA TEXT,
            Quantity TEXT,
            CellType TEXT,
            Status TEXT,
            GridID TEXT,
            Timestamp DATETIME
        )
    ''')
    # Pre-populate 3x3 grid (A1..C3)
    for row in "ABC":
        for col in range(1, 4):
            grid_id = f"{row}{col}"
            c.execute("INSERT OR IGNORE INTO grids (GridID, Status) VALUES (?, 'Available')", (grid_id,))
    conn.commit()
    conn.close()

create_tables()

# ====== DB HELPERS ================
def get_db_connection():
    conn = sqlite3.connect('inventory.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def get_all_grids(conn):
    return pd.read_sql_query("SELECT * FROM grids", conn)

def get_available_grids(conn):
    return pd.read_sql_query("SELECT * FROM grids WHERE Status='Available'", conn)

def get_drum(conn, drum_id):
    return pd.read_sql_query("SELECT * FROM drums WHERE DrumID = ?", conn, params=(drum_id,))

def get_drums_by_grid(conn, grid_id):
    return pd.read_sql_query("SELECT * FROM drums WHERE CurrentGrid = ? AND Status = 'IN'", conn, params=(grid_id,))

def insert_drum(conn, drum_id, order_no, ra, cell_type, quantity):
    now = datetime.now()
    conn.execute("INSERT OR REPLACE INTO drums (DrumID, OrderNo, RA, Quantity, CellType, Status, CurrentGrid, LastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (drum_id, order_no, ra, quantity, cell_type, 'OUT', None, now))
    conn.commit()

def update_drum_info(conn, drum_id, order_no, ra, cell_type, quantity):
    now = datetime.now()
    conn.execute("UPDATE drums SET OrderNo=?, RA=?, Quantity=?, CellType=?, Status='OUT', CurrentGrid=NULL, LastUpdated=? WHERE DrumID=?",
                 (order_no, ra, quantity, cell_type, now, drum_id))
    conn.commit()

def update_drum_in(conn, drum_id, grid_id):
    now = datetime.now()
    # Mark drum as IN and update grid as Occupied if needed
    conn.execute("UPDATE drums SET Status = 'IN', CurrentGrid = ?, LastUpdated = ? WHERE DrumID = ?", (grid_id, now, drum_id))
    conn.execute("UPDATE grids SET Status = 'Occupied' WHERE GridID = ?", (grid_id,))
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
        INSERT INTO drum_history (DrumID, OrderNo, RA, Quantity, CellType, Status, GridID, Timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            drum_id, drum.iloc[0]['OrderNo'], drum.iloc[0]['RA'], drum.iloc[0]['Quantity'], drum.iloc[0]['CellType'],
            'OUT', grid_id, now
        ))
    # Mark drum as OUT and clear from grid
    conn.execute("UPDATE drums SET Status = 'OUT', OrderNo = NULL, Quantity = NULL, RA = NULL, CellType = NULL, CurrentGrid = NULL, LastUpdated = ? WHERE DrumID = ?", (now, drum_id))
    # If no more drums on this grid, mark grid as available
    other_in = get_drums_by_grid(conn, grid_id)
    if len(other_in) <= 1:  # Only this drum remains (about to be OUT)
        conn.execute("UPDATE grids SET Status = 'Available' WHERE GridID = ?", (grid_id,))
    conn.execute("INSERT INTO transactions (DrumID, GridID, Status, Timestamp) VALUES (?, ?, 'OUT', ?)", (drum_id, grid_id, now))
    conn.commit()
    return True

def batch_out_drums(conn, grid_id):
    drums = get_drums_by_grid(conn, grid_id)
    for _, drum in drums.iterrows():
        update_drum_out(conn, drum['DrumID'])

def shift_drum_grid(conn, drum_id, new_grid_id):
    now = datetime.now()
    drum = get_drum(conn, drum_id)
    if drum.empty or drum.iloc[0]['CurrentGrid'] is None:
        return False
    # Free old grid if this is the last drum
    old_grid = drum.iloc[0]['CurrentGrid']
    other_drums = get_drums_by_grid(conn, old_grid)
    if len(other_drums) <= 1:
        conn.execute("UPDATE grids SET Status = 'Available' WHERE GridID = ?", (old_grid,))
    # Move drum
    conn.execute("UPDATE drums SET CurrentGrid = ?, LastUpdated = ? WHERE DrumID = ?", (new_grid_id, now, drum_id))
    conn.execute("UPDATE grids SET Status = 'Occupied' WHERE GridID = ?", (new_grid_id,))
    conn.execute("INSERT INTO transactions (DrumID, GridID, Status, Timestamp) VALUES (?, ?, 'SHIFT', ?)", (drum_id, new_grid_id, now))
    conn.commit()
    return True

def batch_shift_drums(conn, old_grid_id, new_grid_id):
    drums = get_drums_by_grid(conn, old_grid_id)
    for _, drum in drums.iterrows():
        shift_drum_grid(conn, drum['DrumID'], new_grid_id)

def get_drum_history(conn):
    return pd.read_sql_query("SELECT * FROM drum_history", conn)

# ========= UI & MAIN LOGIC =============

st.set_page_config(page_title="Drum Inventory", layout="wide")
conn = get_db_connection()
page = st.sidebar.radio("Select Operation", [
    "Dashboard (Live)",
    "Scan QR for Drum Placement/Removal"
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
                conn.execute("INSERT OR IGNORE INTO grids (GridID, Status) VALUES (?, 'Available')", (grid_id,))
        conn.commit()
        st.success("All data cleared! Grids reset. Please refresh the page.")
        st.stop()

def dashboard(conn):
    st.title("📦 Drum Storage Grid Dashboard")

    # --- SEARCH BY ORDER ID ---
    st.subheader("🔎 Search Drums by Order Number")
    order_search = st.text_input("Enter Order Number to search", "")
    if order_search:
        results = pd.read_sql_query(
            "SELECT * FROM drums WHERE OrderNo LIKE ?",
            conn, params=(f"%{order_search}%",)
        )
        if not results.empty:
            st.success(f"Found {len(results)} drum(s) matching Order Number: {order_search}")
            st.dataframe(results)
        else:
            # Also search OUT history
            results_hist = pd.read_sql_query(
                "SELECT * FROM drum_history WHERE OrderNo LIKE ?",
                conn, params=(f"%{order_search}%",)
            )
            if not results_hist.empty:
                st.info(f"No drums currently IN or OUT with that Order Number, but found {len(results_hist)} in history.")
                st.dataframe(results_hist)
            else:
                st.error("No records found for this Order Number in active or history logs.")

    st.subheader("Grid Status (auto-refresh 10s)")
    grids = get_all_grids(conn)
    data = []
    for _, row in grids.iterrows():
        drums = get_drums_by_grid(conn, row["GridID"])
        data.append({
            "GridID": row["GridID"],
            "Status": row["Status"],
            "Drums Stacked": ", ".join(drums["DrumID"].tolist()) if not drums.empty else "(Empty)"
        })
    st.dataframe(pd.DataFrame(data))

    st.subheader("All Drums (Current Status)")
    st.dataframe(pd.read_sql_query("SELECT * FROM drums", conn))

    st.subheader("Drum OUT History Log")
    st.dataframe(get_drum_history(conn))
    st_autorefresh(interval=10*1000, key="refresh_dashboard")

def qr_page(conn):
    st.title("📷 Drum Placement & Removal/Shift")
    st.markdown("""
        <style>
        div[data-testid='column']{padding:0.3rem 0.2rem !important}
        .stButton>button{height:60px; font-size:1.2rem;}
        .room-btn {
            border-radius: 16px !important; 
            font-size: 1.5rem !important;
            min-width: 90px !important; 
            min-height: 60px !important;
            margin-bottom: 0.3em !important;
        }
        </style>
        """, unsafe_allow_html=True)

    state_defaults = {
        "batch_in_count": 1, "batch_in_placed": 0, "batch_in_grid": None,
        "last_drum_details": {}, "placement_mode": False,
        "shift_mode": False, "shift_all_mode": False,
        "shift_drum_id": "", "shift_all_grid": ""
    }
    for k, v in state_defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # --- Drum Batch Placement Selection ---
    if not st.session_state.placement_mode:
        st.markdown("#### How many drums will you place together (on a trolley/grid)?")
        cols = st.columns(3)
        for i, n in enumerate([1, 2, 3]):
            if cols[i].button(f"{n}", key=f"batch_{n}_pick"):
                st.session_state.batch_in_count = n
                st.session_state.batch_in_placed = 0
                st.session_state.placement_mode = True
                st.session_state.batch_in_grid = None
                st.session_state.last_drum_details = {}
                st.experimental_rerun()

    # --- Drum Batch Placement Workflow ---
    if st.session_state.placement_mode:
        if st.session_state.batch_in_grid is None:
            st.markdown("### <span style='font-size:1.1em'>Select grid (trolley/room spot) to place drums:</span>", unsafe_allow_html=True)
            available_grids = get_all_grids(conn)
            grid_ids = [row["GridID"] for _, row in available_grids.iterrows()]
            grid_status = {row["GridID"]: row["Status"] for _, row in available_grids.iterrows()}
            rows = sorted(set(g[0] for g in grid_ids))
            cols_ = sorted(set(g[1] for g in grid_ids))
            grid_matrix = {row: [f"{row}{col}" for col in cols_] for row in rows}

            # Room-like grid with color and click feedback
            for row in rows:
                grid_row = st.columns(len(cols_))
                for j, grid_id in enumerate(grid_matrix[row]):
                    status = grid_status.get(grid_id, "Occupied")
                    sel = st.session_state.batch_in_grid == grid_id
                    # Custom button coloring
                    btn_color = (
                        "background-color:#1976d2;color:white;border:4px solid #1565c0;" if sel else
                        ("background-color:#b9fbc0;color:#176a34;border:3px solid #2dcc8b;" if status == "Available"
                         else "background-color:#f2f2f2;color:#b1b1b1;border:2px solid #ddd;opacity:0.7;")
                    )
                    btn_html = (
                        f"<button class='room-btn' style='{btn_color}' disabled>{grid_id}</button>"
                        if status != "Available"
                        else f"""<form action="" method="post">
                                    <button name="pick_{grid_id}" type="submit" class="room-btn" style="{btn_color}">{grid_id}</button>
                                </form>"""
                    )
                    if status == "Available":
                        if grid_row[j].button(grid_id, key=f"grid_{grid_id}_btn", help="Tap to select", use_container_width=True):
                            st.session_state.batch_in_grid = grid_id
                            st.experimental_rerun()
                    else:
                        grid_row[j].markdown(btn_html, unsafe_allow_html=True)
            if not st.session_state.batch_in_grid:
                st.info("Tap any *green* grid to select. (Grey = occupied)")
                st.stop()

        st.success(f"Placing drum {st.session_state.batch_in_placed+1} of {st.session_state.batch_in_count} in grid {st.session_state.batch_in_grid}")

        # --- Add Drum Workflow ---
        drum_id = ""
        st.markdown("#### Add Drum")
        col_add, col_copy = st.columns([2, 1])
        with col_add:
            if st.button("Add Drum", key="add_drum_btn", type="primary", use_container_width=True):
                st.session_state['adding_drum'] = True
                st.experimental_rerun()

        if st.session_state.get('adding_drum', False):
            camera_enabled = st.checkbox("Enable camera for placement", key="placement_cam")
            if camera_enabled:
                image_data = st.camera_input("Scan Drum QR Code for Placement")
                if image_data:
                    img = Image.open(image_data)
                    img_np = np.array(img)
                    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                    decoded_objs = decode(img_bgr)
                    if decoded_objs:
                        drum_id = decoded_objs[0].data.decode("utf-8").strip().upper()
                        st.success(f"Scanned Drum ID: {drum_id}")
                    else:
                        st.warning("No QR code detected. Try again.")
            drum_id = st.text_input("Drum ID (scan or type)", value=drum_id, key="drum_in_id").strip().upper()
            # Copy previous details
            copy_prev = False
            if st.session_state.batch_in_placed > 0 and st.session_state.last_drum_details:
                copy_prev = col_copy.checkbox("Copy previous drum details?", key="copy_last")
            if copy_prev:
                last = st.session_state.last_drum_details
                order_no = st.text_input("Order Number", value=last.get("OrderNo", ""), key="order_no_copy")
                ra = st.text_input("RA Number", value=last.get("RA", ""), key="ra_copy")
                quantity = st.text_input("Quantity", value=last.get("Quantity", ""), key="qty_copy")
                cell_type = st.text_input("Cell Type", value=last.get("CellType", ""), key="type_copy")
            else:
                order_no = st.text_input("Order Number", "", key="order_no_new")
                ra = st.text_input("RA Number", "", key="ra_new")
                quantity = st.text_input("Quantity", "", key="qty_new")
                cell_type = st.text_input("Cell Type", "", key="type_new")

            col_place, col_skip = st.columns([2, 1])
            if col_place.button("IN / Place Drum in Grid", type="primary", key="place_drum_btn"):
                if not drum_id or not all([order_no, ra, quantity, cell_type]):
                    st.warning("Please fill all fields and scan/enter Drum ID!")
                else:
                    drum = get_drum(conn, drum_id)
                    if drum.empty:
                        insert_drum(conn, drum_id, order_no, ra, cell_type, quantity)
                    else:
                        update_drum_info(conn, drum_id, order_no, ra, cell_type, quantity)
                    update_drum_in(conn, drum_id, st.session_state.batch_in_grid)
                    st.session_state.last_drum_details = {
                        "OrderNo": order_no, "RA": ra, "Quantity": quantity, "CellType": cell_type
                    }
                    st.session_state.batch_in_placed += 1
                    st.session_state.adding_drum = False
                    if st.session_state.batch_in_placed >= st.session_state.batch_in_count:
                        st.success(f"All {st.session_state.batch_in_count} drums placed in {st.session_state.batch_in_grid}.")
                        st.session_state.placement_mode = False
                        st.session_state.batch_in_placed = 0
                        st.session_state.batch_in_grid = None
                        st.session_state.last_drum_details = {}
                    st.experimental_rerun()
            if col_skip.button("Cancel Drum", key="cancel_drum_btn"):
                st.session_state.adding_drum = False
                st.experimental_rerun()
        st.markdown("---")

    # --- OUT/SHIFT/REMOVE DRUMS ---
    st.header("Remove or Shift Drums")
    camera_enabled_out = st.checkbox("Enable camera for OUT/Shift operation")
    drum_id_out = ""
    if camera_enabled_out:
        image_data_out = st.camera_input("Scan Drum QR Code to Remove/Shift")
        if image_data_out:
            img = Image.open(image_data_out)
            img_np = np.array(img)
            img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            decoded_objs = decode(img_bgr)
            if decoded_objs:
                drum_id_out = decoded_objs[0].data.decode("utf-8").strip().upper()
                st.success(f"Scanned Drum ID: {drum_id_out}")
            else:
                st.warning("No QR code detected. Try again.")
    drum_id_out = st.text_input("Drum ID to Remove/Shift", value=drum_id_out, key="out_id").strip().upper()

    if drum_id_out:
        drum = get_drum(conn, drum_id_out)
        if not drum.empty and drum.iloc[0]['Status'] == 'IN':
            grid_id = drum.iloc[0]['CurrentGrid']
            st.info(f"Drum is on grid: {grid_id}")
            all_drums_on_grid = get_drums_by_grid(conn, grid_id)
            st.write("Drums on same grid:", ", ".join(all_drums_on_grid["DrumID"].tolist()))

            # Remove options
            col_out1, col_out2 = st.columns(2)
            if col_out1.button("Remove this Drum Only"):
                update_drum_out(conn, drum_id_out)
                st.success(f"Drum {drum_id_out} marked as OUT.")
                st.experimental_rerun()
            if len(all_drums_on_grid) > 1:
                if col_out2.button("Remove ALL Drums from this Grid"):
                    batch_out_drums(conn, grid_id)
                    st.success(f"All drums from grid {grid_id} marked as OUT.")
                    st.experimental_rerun()

            # --- Shift Single Drum ---
            col_shift1, col_shift2 = st.columns(2)
            if not st.session_state.shift_mode:
                if col_shift1.button("Shift this Drum to Another Grid"):
                    st.session_state.shift_mode = True
                    st.session_state.shift_drum_id = drum_id_out
                    st.experimental_rerun()
            else:
                if st.session_state.shift_drum_id == drum_id_out:
                    available_grids = get_available_grids(conn)
                    grid_choices = available_grids["GridID"].tolist()
                    new_grid = col_shift1.selectbox("Select new grid to shift this drum", grid_choices, key="shift_grid_select")
                    if col_shift1.button("Confirm Shift", key="shift_drum_confirm_btn"):
                        shift_drum_grid(conn, drum_id_out, new_grid)
                        st.success(f"Drum {drum_id_out} shifted to grid {new_grid}.")
                        st.session_state.shift_mode = False
                        st.session_state.shift_drum_id = ""
                        st.experimental_rerun()
                    if col_shift2.button("Cancel Shift", key="shift_drum_cancel_btn"):
                        st.session_state.shift_mode = False
                        st.session_state.shift_drum_id = ""
                        st.experimental_rerun()

            # --- Shift ALL Drums ---
            if len(all_drums_on_grid) > 1:
                if not st.session_state.shift_all_mode:
                    if st.button("Shift ALL Drums from this Grid to Another Grid"):
                        st.session_state.shift_all_mode = True
                        st.session_state.shift_all_grid = grid_id
                        st.experimental_rerun()
                else:
                    if st.session_state.shift_all_grid == grid_id and st.session_state.shift_all_mode:
                        available_grids = get_available_grids(conn)
                        grid_choices = available_grids["GridID"].tolist()
                        new_grid_all = st.selectbox("Select new grid to shift ALL drums", grid_choices, key="shift_all_grid_select")
                        col_all1, col_all2 = st.columns(2)
                        if col_all1.button("Confirm ALL Shift", key="shift_all_confirm_btn"):
                            batch_shift_drums(conn, grid_id, new_grid_all)
                            st.success(f"All drums from grid {grid_id} shifted to grid {new_grid_all}.")
                            st.session_state.shift_all_mode = False
                            st.session_state.shift_all_grid = ""
                            st.experimental_rerun()
                        if col_all2.button("Cancel ALL Shift", key="shift_all_cancel_btn"):
                            st.session_state.shift_all_mode = False
                            st.session_state.shift_all_grid = ""
                            st.experimental_rerun()
        else:
            st.warning("Drum not found or already OUT.")

if page == "Dashboard (Live)":
    dashboard(conn)
elif page == "Scan QR for Drum Placement/Removal":
    qr_page(conn)
conn.close()
